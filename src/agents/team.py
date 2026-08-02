"""
팀 오케스트레이션 — 종목 하나에 대한 전체 심의 파이프라인.

    [전문가 풀]   도메인 전문가 8명 → 시장·섹터 컨텍스트 (기존 src/experts, 재사용)
          ↓
    [팬아웃/팬인] Analyst 3인 병렬 (LLM 미사용)
          ↓
    [생성-검증]   Bull/Bear 2라운드 토론 (LLM)
          ↓
    [감독자]      Trader 종합 → 제안 (LLM 미사용, 결정론적)
          ↓
    [생성-검증]   Risk 게이트(cross_validator 11규칙) → PM 최종 승인
          ↓
    TeamVerdict → 저장 → 대시보드 / Trade Wiki

용도는 두 가지다:
  - deliberate_candidate() : 매수 후보 심의
  - deliberate_holding()   : 보유 종목 재평가 (HOLD/SELL)

동시 심의 수를 제한한다 — 종목마다 LLM을 2~4회 부르므로,
제한 없이 풀면 스크리닝 한 번에 수십 건이 몰려 rate limit과 지연을 부른다.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from .analysts import AnalystTeam
from .portfolio_manager import PortfolioManager
from .researchers import ResearchTeam
from .trader import TraderAgent
from .types import PMDecision, Stance, TeamVerdict

RESULT_DIR = Path.home() / ".cache" / "ai_trader" / "team_verdicts"

# 동시 심의 상한 — LLM rate limit 보호
MAX_CONCURRENT = 3

# 종목당 전체 심의 타임아웃 (초)
DELIBERATION_TIMEOUT = 90.0

# GateChecker: symbol, stance → (통과여부, 차단게이트목록, 사유)
GateChecker = Callable[[str, str], Awaitable[tuple]]


class TradingTeam:
    """종목 단위 팀 심의 오케스트레이터"""

    def __init__(
        self,
        llm_manager=None,
        stock_validator=None,
        dart_checker=None,
        expert_orchestrator=None,
        price_provider=None,
        debate_rounds: int = 2,
        allow_pm_override: bool = True,
        max_concurrent: int = MAX_CONCURRENT,
        exit_manager=None,
    ):
        # 자동매도 금지 종목 확인용 — PM이 SELL 제안을 무효화하는 데 쓴다
        self._exit_manager = exit_manager
        self.analysts = AnalystTeam(
            stock_validator=stock_validator,
            dart_checker=dart_checker,
            orchestrator=expert_orchestrator,
            price_provider=price_provider,
        )
        self.research = ResearchTeam(llm_manager=llm_manager, rounds=debate_rounds)
        self.trader = TraderAgent()
        self.pm = PortfolioManager(allow_override=allow_pm_override)
        self._expert_orch = expert_orchestrator
        self._sem = asyncio.Semaphore(max_concurrent)
        # 결과 파일은 read-modify-write라 동시 심의 시 서로 덮어쓴다 → 직렬화 필요
        self._save_lock = asyncio.Lock()
        RESULT_DIR.mkdir(parents=True, exist_ok=True)

    def _exit_exempt_symbols(self) -> set:
        """
        자동매도 금지 종목 집합.

        ExitManager가 보유한 exit_exempt 목록을 그대로 쓴다 —
        여기서 config를 다시 읽으면 두 소스가 어긋날 수 있다.
        """
        em = self._exit_manager
        if em is None:
            return set()
        for attr in ("_exit_exempt", "exit_exempt", "_exit_exempt_symbols"):
            val = getattr(em, attr, None)
            if val:
                try:
                    return set(val)
                except TypeError:
                    continue
        return set()

    # ── 시장 컨텍스트 ──────────────────────────────────────
    def _market_context(self) -> str:
        """도메인 전문가 8명의 합의를 한 줄로 압축 (토론 프롬프트에 주입)"""
        if self._expert_orch is None:
            return ""
        try:
            snap = self._expert_orch.snapshot()
            if not snap:
                return ""
            score = self._expert_orch.aggregate_regime_score(snap)
            bias = self._expert_orch.aggregate_bias(snap)
            bias_str = getattr(bias, "value", str(bias))
            # 상위 findings 2개만 — 프롬프트 비대화 방지
            findings: List[str] = []
            for op in snap.values():
                if getattr(op, "is_valid", False) and op.key_findings:
                    findings.append(op.key_findings[0][:70])
                if len(findings) >= 2:
                    break
            ctx = f"전문가 합의 bias={bias_str}, 보정점수={score:+d}"
            if findings:
                ctx += " | " + " ; ".join(findings)
            return ctx
        except Exception as e:
            logger.debug(f"[팀] 시장 컨텍스트 수집 실패: {e}")
            return ""

    # ── 핵심 심의 ──────────────────────────────────────────
    async def _deliberate(
        self,
        symbol: str,
        name: str,
        holding: bool,
        indicators: Optional[Dict[str, Any]] = None,
        unrealized_pnl_pct: Optional[float] = None,
        gate_checker: Optional[GateChecker] = None,
    ) -> TeamVerdict:
        started = time.monotonic()
        verdict = TeamVerdict(symbol=symbol, name=name)

        try:
            # 1) Analyst 팀 (병렬, LLM 없음)
            reports = await self.analysts.run(symbol, name, indicators)
            verdict.reports = reports

            # 2) Research 팀 토론 (LLM)
            #    보유 종목이든 후보든 동일하게 "지금 사도 되는가"를 묻는다.
            #    보유 종목에 대해 "매수 불가" 결론이 나오면 곧 청산 근거가 된다.
            debate = await self.research.debate(
                symbol, name, reports, self._market_context()
            )
            verdict.debate = debate

            # 3) Trader 제안 (결정론적)
            proposal = self.trader.propose(
                symbol, name, reports, debate,
                holding=holding, unrealized_pnl_pct=unrealized_pnl_pct,
            )
            verdict.proposal = proposal

            # 4) 리스크 게이트 (매수 제안일 때만 조회)
            gate_passed, blocked, gate_reason = True, [], ""
            if proposal.stance == Stance.BUY and gate_checker is not None:
                try:
                    gate_passed, blocked, gate_reason = await gate_checker(
                        symbol, proposal.stance.value
                    )
                except Exception as e:
                    # 게이트 조회 실패 시 보수적으로 차단 (fail-closed)
                    logger.warning(f"[팀] {symbol} 게이트 조회 실패 → 차단: {e}")
                    gate_passed, blocked, gate_reason = False, ["gate_error"], str(e)

            # 5) PM 최종 결정
            decision = self.pm.decide(
                proposal, gate_passed, blocked, gate_reason,
                exit_exempt_symbols=self._exit_exempt_symbols(),
            )
            verdict.decision = decision

            if decision.overrode_gate:
                await self._alert_override(decision)

        except Exception as e:
            logger.exception(f"[팀] {symbol} 심의 실패: {e}")
            verdict.error = str(e)
            verdict.decision = PMDecision(
                symbol=symbol, approved=False, stance=Stance.HOLD,
                reason=f"심의 오류: {e}",
            )

        verdict.elapsed_sec = time.monotonic() - started
        await self._save(verdict)
        return verdict

    async def deliberate_candidate(
        self, symbol: str, name: str = "",
        indicators: Optional[Dict[str, Any]] = None,
        gate_checker: Optional[GateChecker] = None,
    ) -> TeamVerdict:
        """매수 후보 심의"""
        async with self._sem:
            try:
                return await asyncio.wait_for(
                    self._deliberate(symbol, name, holding=False,
                                     indicators=indicators,
                                     gate_checker=gate_checker),
                    timeout=DELIBERATION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[팀] {symbol} 심의 타임아웃")
                v = TeamVerdict(symbol=symbol, name=name, error="타임아웃")
                v.decision = PMDecision(symbol=symbol, approved=False,
                                        stance=Stance.HOLD, reason="심의 타임아웃")
                return v

    async def deliberate_holding(
        self, symbol: str, name: str = "",
        indicators: Optional[Dict[str, Any]] = None,
        unrealized_pnl_pct: Optional[float] = None,
    ) -> TeamVerdict:
        """보유 종목 재평가 — 게이트는 매수용이므로 적용하지 않는다"""
        async with self._sem:
            try:
                return await asyncio.wait_for(
                    self._deliberate(symbol, name, holding=True,
                                     indicators=indicators,
                                     unrealized_pnl_pct=unrealized_pnl_pct),
                    timeout=DELIBERATION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[팀] {symbol} 보유 재평가 타임아웃")
                v = TeamVerdict(symbol=symbol, name=name, error="타임아웃")
                v.decision = PMDecision(symbol=symbol, approved=True,
                                        stance=Stance.HOLD,
                                        reason="심의 타임아웃 — 현상 유지")
                return v

    async def deliberate_many(
        self, items: List[Dict[str, Any]], holding: bool = False,
        gate_checker: Optional[GateChecker] = None,
    ) -> List[TeamVerdict]:
        """
        여러 종목을 동시 심의 (세마포어가 실제 동시 실행 수를 제한).

        items: [{"symbol":..., "name":..., "indicators":..., "pnl_pct":...}, ...]
        """
        async def _one(it: Dict[str, Any]) -> TeamVerdict:
            if holding:
                return await self.deliberate_holding(
                    it["symbol"], it.get("name", ""),
                    it.get("indicators"), it.get("pnl_pct"),
                )
            return await self.deliberate_candidate(
                it["symbol"], it.get("name", ""),
                it.get("indicators"), gate_checker,
            )

        results = await asyncio.gather(
            *(_one(it) for it in items), return_exceptions=True
        )
        out: List[TeamVerdict] = []
        for it, r in zip(items, results):
            if isinstance(r, Exception):
                v = TeamVerdict(symbol=it.get("symbol", "?"),
                                name=it.get("name", ""), error=str(r))
                out.append(v)
            else:
                out.append(r)
        return out

    # ── 부가 ───────────────────────────────────────────────
    async def _alert_override(self, decision: PMDecision) -> None:
        """게이트 오버라이드는 반드시 알린다 (감사 + 텔레그램)"""
        try:
            from ..utils import audit_log
            audit_log.record(
                "pm_override", market="KR", symbol=decision.symbol,
                side="buy", reason=decision.reason,
                gates=",".join(decision.overridden_gates),
                size_mult=decision.size_multiplier,
            )
        except Exception as e:
            logger.warning(f"[팀] 오버라이드 감사기록 실패: {e}")

        try:
            from ..utils.telegram import send_alert
            await send_alert(
                f"⚠️ PM 게이트 오버라이드\n"
                f"종목: {decision.symbol}\n"
                f"차단됐던 게이트: {', '.join(decision.overridden_gates)}\n"
                f"사유: {decision.reason[:150]}\n"
                f"사이징: ×{decision.size_multiplier}"
            )
        except Exception as e:
            logger.warning(f"[팀] 오버라이드 알림 실패: {e}")

    async def _save(self, verdict: TeamVerdict) -> None:
        """
        대시보드가 읽을 수 있도록 일자별 파일에 누적.

        read-modify-write 이므로 동시 심의(MAX_CONCURRENT=3)가 겹치면
        서로의 결과를 덮어쓴다 → Lock으로 직렬화한다.
        임시파일에 쓰고 교체해 중간 상태가 읽히는 것도 막는다.
        """
        async with self._save_lock:
            try:
                path = RESULT_DIR / f"verdicts_{datetime.now():%Y%m%d}.json"
                existing: List[Dict[str, Any]] = []
                if path.exists():
                    try:
                        existing = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(existing, list):
                            existing = []
                    except (json.JSONDecodeError, OSError):
                        existing = []

                row = verdict.to_dict()
                row["saved_at"] = datetime.now().isoformat(timespec="seconds")
                # 같은 종목은 최신 것만 유지
                existing = [e for e in existing if e.get("symbol") != verdict.symbol]
                existing.append(row)
                # 파일 비대화 방지
                existing = existing[-100:]

                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                tmp.replace(path)   # 원자적 교체
            except Exception as e:
                logger.warning(f"[팀] 심의 결과 저장 실패: {e}")

    @staticmethod
    def load_today(limit: int = 50) -> List[Dict[str, Any]]:
        """오늘 심의 결과 조회 (대시보드용)"""
        path = RESULT_DIR / f"verdicts_{datetime.now():%Y%m%d}.json"
        if not path.exists():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return []
            return rows[-limit:][::-1]
        except (json.JSONDecodeError, OSError):
            return []

    def get_stats(self) -> Dict[str, Any]:
        return {
            "research": self.research.get_stats(),
            "pm": self.pm.get_stats(),
        }

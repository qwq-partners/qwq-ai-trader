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
import contextlib
import json
import os
import time
import uuid
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

# 결과 파일 저장 락은 **모듈 전역**이어야 한다.
# 인스턴스별 락으로 두면 TradingTeam이 둘 이상 만들어졌을 때(KR/US 분리, 테스트 등)
# 같은 일자 파일과 같은 .tmp를 동시에 read-modify-write해 결과가 유실된다.
_SAVE_LOCK = asyncio.Lock()

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
        # 저장 직렬화는 모듈 전역 락(_SAVE_LOCK)을 쓴다 — 위 주석 참조
        self._cancelled_count = 0   # 타임아웃 취소 건수 (통계 정합성 확인용)
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
        """
        도메인 전문가 8명의 합의를 한 줄로 압축 (토론 프롬프트에 주입).

        ⚠️ `orchestrator.snapshot()`은 **만료 여부와 무관하게** 캐시된 의견을 준다
        (`ExpertAgent.cached()` 주석에 "만료 무관"이라 명시돼 있다).
        전문가 의견 TTL은 6~24시간이라, 거르지 않으면 장중 판단에
        어제 만들어진 시장 진단을 "현재 상황"으로 주입하게 된다.

        여기서는 유효한 의견만 쓰고, 의견의 나이를 프롬프트에 명시해
        LLM이 오래된 정보임을 알고 판단하도록 한다.
        """
        if self._expert_orch is None:
            return ""
        try:
            snap = self._expert_orch.snapshot()
            if not snap:
                return ""

            fresh = {k: op for k, op in snap.items()
                     if getattr(op, "is_valid", False)}
            stale_n = len(snap) - len(fresh)

            if not fresh:
                # 전부 만료 — 조용히 빈 문자열을 반환하면 "중립 시장"과 구분되지 않는다.
                # 컨텍스트가 없다는 사실 자체를 프롬프트에 명시해 토론이 그걸 알고 판단하게 한다.
                logger.warning(
                    f"[팀] 전문가 의견 {len(snap)}건이 모두 만료 — 시장 컨텍스트 없음"
                )
                return ("⚠️ 시장 컨텍스트 없음 (전문가 의견 전부 만료). "
                        "거시 상황을 모르는 상태이므로 보수적으로 판단하라.")

            score = self._expert_orch.aggregate_regime_score(fresh)
            bias = self._expert_orch.aggregate_bias(fresh)
            bias_str = getattr(bias, "value", str(bias))

            # 가장 오래된 의견의 나이 — 컨텍스트 신선도의 하한
            now = datetime.now()
            ages = []
            findings: List[str] = []
            for op in fresh.values():
                issued = getattr(op, "issued_at", None)
                if issued is not None:
                    ages.append((now - issued).total_seconds() / 60.0)
                if op.key_findings and len(findings) < 2:
                    findings.append(op.key_findings[0][:70])

            ctx = f"전문가 합의({len(fresh)}명) bias={bias_str}, 보정점수={score:+d}"
            if ages:
                ctx += f", 최신도 {min(ages):.0f}~{max(ages):.0f}분 전"
            if stale_n:
                ctx += f" (만료 {stale_n}건 제외)"
            if findings:
                ctx += " | " + " ; ".join(findings)
            return ctx
        except Exception as e:
            logger.debug(f"[팀] 시장 컨텍스트 수집 실패: {e}")
            return ""

    def _symbol_context(self, symbol: str, name: str, sector: Optional[str]) -> str:
        """종목 위키 노트 + 섹터 카운슬 점수 (2026-08-07 — 심의 근거 보강)

        둘 다 캐시/파일 읽기만 — LLM·네트워크 없음. 실패 시 빈 문자열 (심의 비차단).
        """
        parts: List[str] = []
        try:
            tw = getattr(self._expert_orch, "trade_wiki", None)
            if tw is not None and hasattr(tw, "query_symbol"):
                note = tw.query_symbol(symbol)
                if note:
                    parts.append(f"종목 노트(과거 거래·최근 리서치): {note[:300]}")
        except Exception:
            pass
        try:
            if self._expert_orch is not None:
                sc = self._expert_orch.agents.get("sector_council")
                if sc is not None:
                    sec = sector or sc.sector_of_cached(symbol, name)
                    s_score = sc.sector_score(sec) if sec else None
                    if sec and s_score is not None:
                        parts.append(f"섹터 카운슬: {sec} {s_score:+d} (-100~+100)")
        except Exception:
            pass
        return " | ".join(parts)

    # ── 핵심 심의 ──────────────────────────────────────────
    async def _deliberate(
        self,
        symbol: str,
        name: str,
        holding: bool,
        indicators: Optional[Dict[str, Any]] = None,
        unrealized_pnl_pct: Optional[float] = None,
        gate_checker: Optional[GateChecker] = None,
        indicators_as_of: Optional[datetime] = None,
        sector: Optional[str] = None,
    ) -> TeamVerdict:
        started = time.monotonic()
        verdict = TeamVerdict(symbol=symbol, name=name)

        try:
            # 1) Analyst 팀 (병렬, LLM 없음)
            reports = await self.analysts.run(
                symbol, name, indicators, indicators_as_of=indicators_as_of
            )
            # 섹터는 배분기(allocator)가 집중도를 계산하는 데 쓰므로 보고서에 실어 둔다
            if sector:
                for r in reports:
                    if r.ok:
                        r.metrics.setdefault("sector", sector)
            verdict.reports = reports
            logger.debug(
                f"[팀] {symbol} 근거 신선도: "
                f"{AnalystTeam.freshness_summary(reports)}"
            )

            # 2) Research 팀 토론 (LLM)
            #    보유 종목이든 후보든 동일하게 "지금 사도 되는가"를 묻는다.
            #    보유 종목에 대해 "매수 불가" 결론이 나오면 곧 청산 근거가 된다.
            #    2026-08-07: 종목 위키·섹터 카운슬 컨텍스트를 시장 컨텍스트에 결합
            _ctx = self._market_context()
            _sym_ctx = self._symbol_context(symbol, name, sector)
            if _sym_ctx:
                _ctx = (_ctx + "\n" if _ctx else "") + _sym_ctx
            debate = await self.research.debate(
                symbol, name, reports, _ctx
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

        except asyncio.CancelledError:
            # 바깥 wait_for() 타임아웃으로 취소된 경우.
            # Exception만 잡으면 여기로 오지 않아 verdict 보정과 저장이 통째로 건너뛰어지고,
            # 그 전에 이미 증가한 공유 통계(ResearchTeam.stats / PortfolioManager.stats)만
            # 남아 "심의는 집계됐는데 결과는 없는" 상태가 된다.
            # 최소한의 실패 결과를 남기고 취소는 그대로 전파한다 (삼키면 안 된다).
            verdict.error = "취소됨(타임아웃)"
            verdict.decision = PMDecision(
                symbol=symbol, approved=False, stance=Stance.HOLD,
                reason="심의 취소 — 타임아웃",
            )
            verdict.elapsed_sec = time.monotonic() - started
            self._cancelled_count += 1
            with contextlib.suppress(Exception):
                await asyncio.shield(self._save(verdict))
            raise
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
        indicators_as_of: Optional[datetime] = None,
        sector: Optional[str] = None,
    ) -> TeamVerdict:
        """매수 후보 심의"""
        async with self._sem:
            try:
                return await asyncio.wait_for(
                    self._deliberate(symbol, name, holding=False,
                                     indicators=indicators,
                                     gate_checker=gate_checker,
                                     indicators_as_of=indicators_as_of,
                                     sector=sector),
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
        indicators_as_of: Optional[datetime] = None,
    ) -> TeamVerdict:
        """보유 종목 재평가 — 게이트는 매수용이므로 적용하지 않는다"""
        async with self._sem:
            try:
                return await asyncio.wait_for(
                    self._deliberate(symbol, name, holding=True,
                                     indicators=indicators,
                                     unrealized_pnl_pct=unrealized_pnl_pct,
                                     indicators_as_of=indicators_as_of),
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
                    indicators_as_of=it.get("indicators_as_of"),
                )
            return await self.deliberate_candidate(
                it["symbol"], it.get("name", ""),
                it.get("indicators"), gate_checker,
                indicators_as_of=it.get("indicators_as_of"),
                sector=it.get("sector"),
            )

        results = await asyncio.gather(
            *(_one(it) for it in items), return_exceptions=True
        )
        out: List[TeamVerdict] = []
        for it, r in zip(items, results):
            if isinstance(r, BaseException):
                # 다른 모든 경로는 decision을 채운다. 여기만 None이면
                # 호출측이 verdict.decision 존재를 전제할 때 이 경로만 깨진다.
                sym = it.get("symbol", "?")
                v = TeamVerdict(symbol=sym, name=it.get("name", ""), error=str(r))
                v.decision = PMDecision(
                    symbol=sym, approved=False, stance=Stance.HOLD,
                    reason=f"심의 예외: {r}",
                )
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
        async with _SAVE_LOCK:
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

                tmp = path.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
                tmp.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                tmp.replace(path)   # 원자적 교체
            except Exception as e:
                logger.warning(f"[팀] 심의 결과 저장 실패: {e}")

    @staticmethod
    def load_date(day: str, limit: int = 50) -> List[Dict[str, Any]]:
        """특정 일자 심의 결과 조회 (대시보드 타임라인용)

        Args:
            day: YYYYMMDD. 형식이 어긋나면 빈 목록 (경로 조작 방지도 겸한다)
            limit: 최신순 최대 건수
        """
        if not (isinstance(day, str) and len(day) == 8 and day.isdigit()):
            return []
        path = RESULT_DIR / f"verdicts_{day}.json"
        if not path.exists():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return []
            # limit<=0이면 rows[-0:]가 전체를 반환한다 — 의도와 정반대다
            if limit <= 0:
                return []
            return rows[-limit:][::-1]
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def available_dates(limit: int = 30) -> List[str]:
        """심의 결과가 남아 있는 날짜 목록 (최신순 YYYYMMDD)"""
        try:
            days = sorted(
                (p.stem.replace("verdicts_", "") for p in RESULT_DIR.glob("verdicts_*.json")),
                reverse=True,
            )
            return [d for d in days if len(d) == 8 and d.isdigit()][:max(1, limit)]
        except OSError:
            return []

    @staticmethod
    def load_today(limit: int = 50) -> List[Dict[str, Any]]:
        """오늘 심의 결과 조회 (대시보드용)"""
        return TradingTeam.load_date(f"{datetime.now():%Y%m%d}", limit=limit)

    def get_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "research": self.research.get_stats(),
            "pm": self.pm.get_stats(),
            "cancelled": self._cancelled_count,
        }
        # 재현성 — 승격 기준(동일 입력 판정 일치율 80%)을 상시 확인할 수 있게 노출한다
        try:
            from .reproducibility import LLMLedger
            stats["reproducibility"] = LLMLedger.agreement_rate()
            stats["model_usage"] = LLMLedger.model_usage()
        except Exception as e:
            logger.debug(f"[팀] 재현성 통계 조회 실패: {e}")
        return stats

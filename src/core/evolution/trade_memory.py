"""
QWQ AI Trader - 거래 메모리 시스템 (Trade Memory)

거래 경험을 3-Layer로 축적하여 같은 실수를 반복하지 않는 시스템.
PRISM-INSIGHT의 3-Layer 메모리 압축 패턴 차용.

Layer 1 (0~7일): 원시 기록 — 진입/청산 시점의 전체 지표 스냅샷
Layer 2 (8~30일): 요약 기록 — "섹터 + 조건 → 행동 → 결과" 형태
Layer 3 (31일+): 원칙 — "조건 = 규칙" (confidence 관리, 점수 보정 사용)
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from loguru import logger


@dataclass
class TradeOutcome:
    """Layer 1: 거래 결과 원시 기록"""
    symbol: str
    name: str
    strategy: str
    sector: str
    entry_date: str          # YYYY-MM-DD
    exit_date: str
    holding_days: int
    pnl_pct: float
    exit_type: str           # take_profit, stop_loss, trailing, etc.
    # 진입 시점 지표 스냅샷
    entry_indicators: Dict[str, Any] = field(default_factory=dict)
    # 시장 상황
    market_regime: str = "neutral"
    market_change_pct: float = 0.0  # KOSPI/KOSDAQ 평균 등락률
    market_level: str = ""           # KOSPI 레벨 구간 (예: "2700~2800")
    # 태그
    tags: List[str] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class TradeSummary:
    """Layer 2: 요약 기록"""
    pattern: str             # "반도체 + 기관매수 + SEPA 85점"
    action: str              # "매수 후 5일 보유"
    result: str              # "+8% 익절" 또는 "-4% 손절"
    strategy: str
    sector: str
    is_win: bool
    pnl_pct: float
    count: int = 1           # 동일 패턴 발생 횟수
    period: str = ""         # "2026-03-W13" (주차)


@dataclass
class TradePrinciple:
    """Layer 3: 추출된 원칙"""
    rule: str                # "반도체 섹터 기관 순매수 시 SEPA 적극 진입"
    confidence: float        # 0.0 ~ 1.0
    score_delta: int         # 매수 점수 보정 (-3 ~ +3)
    source_count: int        # 이 원칙을 뒷받침하는 거래 수
    last_verified: str       # 마지막 검증 날짜
    conditions: Dict[str, Any] = field(default_factory=dict)  # 매칭 조건
    created_at: str = ""
    active: bool = True


class TradeMemory:
    """
    거래 메모리 시스템

    거래 완료 → record_outcome() → Layer 1 저장
    주간 압축  → compress_layers() → Layer 1→2→3 변환
    매수 시    → get_score_adjustment() → Layer 3 원칙 기반 점수 보정
    """

    def __init__(self, cache_dir: str = None, llm_manager=None):
        self._cache_dir = Path(cache_dir or Path.home() / ".cache" / "ai_trader" / "trade_memory")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._llm_manager = llm_manager  # LLM 보조 회고 (선택적)

        self._layer1: List[TradeOutcome] = []
        self._layer2: List[TradeSummary] = []
        self._layer3: List[TradePrinciple] = []

        self._load()

    # ============================================================
    # Layer 1: 원시 기록
    # ============================================================

    def record_outcome(
        self,
        symbol: str,
        name: str,
        strategy: str,
        sector: str,
        entry_date: str,
        exit_date: str,
        holding_days: int,
        pnl_pct: float,
        exit_type: str,
        entry_indicators: Dict[str, Any] = None,
        market_regime: str = "neutral",
        market_change_pct: float = 0.0,
        market_level: str = "",
        tags: List[str] = None,
    ):
        """거래 완료 시 Layer 1에 기록"""
        outcome = TradeOutcome(
            symbol=symbol,
            name=name,
            strategy=strategy,
            sector=sector or "",
            entry_date=entry_date,
            exit_date=exit_date,
            holding_days=holding_days,
            pnl_pct=round(pnl_pct, 2),
            exit_type=exit_type,
            entry_indicators=entry_indicators or {},
            market_regime=market_regime,
            market_change_pct=round(market_change_pct, 2),
            market_level=market_level,
            tags=tags or [],
            timestamp=datetime.now().isoformat(),
        )
        self._layer1.append(outcome)
        self._save_layer1()
        logger.debug(
            f"[거래메모리] L1 기록: {symbol} {name} "
            f"{strategy} {pnl_pct:+.1f}% ({exit_type})"
        )

    # ============================================================
    # Layer 2: 요약 압축
    # ============================================================

    def _compress_to_layer2(self):
        """Layer 1 → Layer 2 요약 (7일 이상 된 기록)"""
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        to_compress = [o for o in self._layer1 if o.timestamp < cutoff]

        if not to_compress:
            return 0

        for outcome in to_compress:
            # 패턴 생성: "섹터 + 전략 + 핵심 지표"
            ind = outcome.entry_indicators
            parts = []
            if outcome.sector:
                parts.append(outcome.sector)
            parts.append(outcome.strategy)
            # 핵심 지표 태그
            rsi = ind.get("rsi_14") or ind.get("rsi")
            if rsi is not None:
                try:
                    parts.append(f"RSI{int(float(rsi))}")
                except (ValueError, TypeError):
                    pass
            atr = ind.get("atr_14") or ind.get("atr_pct")
            if atr is not None:
                try:
                    parts.append(f"ATR{float(atr):.0f}%")
                except (ValueError, TypeError):
                    pass
            foreign = ind.get("foreign_net_buy")
            if foreign is not None and foreign > 0:
                parts.append("기관매수")

            pattern = " + ".join(parts) if parts else outcome.strategy

            result_str = f"{outcome.pnl_pct:+.1f}% {outcome.exit_type}"
            action = f"매수→{outcome.holding_days}일 보유"

            summary = TradeSummary(
                pattern=pattern,
                action=action,
                result=result_str,
                strategy=outcome.strategy,
                sector=outcome.sector,
                is_win=outcome.pnl_pct > 0,
                pnl_pct=outcome.pnl_pct,
                period=datetime.now().strftime("%Y-W%W"),
            )
            self._layer2.append(summary)

        # 압축된 원시 기록 제거
        self._layer1 = [o for o in self._layer1 if o.timestamp >= cutoff]
        count = len(to_compress)
        self._save_layer1()
        self._save_layer2()
        logger.info(f"[거래메모리] L1→L2 압축: {count}건")
        return count

    # ============================================================
    # Layer 3: 원칙 추출
    # ============================================================

    def _extract_principles(self):
        """Layer 2 → Layer 3 원칙 추출 (30일 이상 축적 후)"""
        if len(self._layer2) < 10:
            return 0

        # 전략+섹터별 승패 집계
        pattern_stats: Dict[str, Dict] = {}
        for s in self._layer2:
            key = f"{s.strategy}|{s.sector}" if s.sector else s.strategy
            if key not in pattern_stats:
                pattern_stats[key] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "patterns": []}
            if s.is_win:
                pattern_stats[key]["wins"] += 1
            else:
                pattern_stats[key]["losses"] += 1
            pattern_stats[key]["total_pnl"] += s.pnl_pct
            pattern_stats[key]["patterns"].append(s.pattern)

        new_principles = 0
        for key, stats in pattern_stats.items():
            total = stats["wins"] + stats["losses"]
            if total < 5:
                continue  # 최소 5건 이상

            win_rate = stats["wins"] / total
            avg_pnl = stats["total_pnl"] / total

            # 기존 원칙 업데이트 or 신규 생성 (conditions 기반 정확 매칭)
            _key_parts = key.split("|")
            _match_strategy = _key_parts[0]
            _match_sector = _key_parts[1] if len(_key_parts) > 1 else ""
            existing = next(
                (p for p in self._layer3
                 if p.conditions.get("strategy") == _match_strategy
                 and p.conditions.get("sector", "") == _match_sector),
                None
            )

            if win_rate >= 0.6 and avg_pnl > 1.0:
                # 성공 패턴 → 긍정 원칙
                rule = f"{key} 패턴: 승률 {win_rate:.0%}, 평균 {avg_pnl:+.1f}% → 적극 진입"
                delta = min(3, int((win_rate - 0.5) * 10))
                if existing:
                    existing.confidence = min(1.0, existing.confidence + 0.1)
                    existing.source_count = total
                    existing.last_verified = date.today().isoformat()
                    existing.score_delta = delta
                else:
                    self._layer3.append(TradePrinciple(
                        rule=rule,
                        confidence=round(win_rate, 2),
                        score_delta=delta,
                        source_count=total,
                        last_verified=date.today().isoformat(),
                        conditions={"strategy": key.split("|")[0], "sector": key.split("|")[1] if "|" in key else ""},
                        created_at=datetime.now().isoformat(),
                    ))
                    new_principles += 1

            elif win_rate <= 0.35 and avg_pnl < -1.0:
                # 실패 패턴 → 경고 원칙
                rule = f"{key} 패턴: 승률 {win_rate:.0%}, 평균 {avg_pnl:+.1f}% → 진입 주의"
                delta = max(-3, -int((0.5 - win_rate) * 10))
                if existing:
                    existing.confidence = min(1.0, existing.confidence + 0.1)
                    existing.source_count = total
                    existing.last_verified = date.today().isoformat()
                    existing.score_delta = delta
                else:
                    self._layer3.append(TradePrinciple(
                        rule=rule,
                        confidence=round(1 - win_rate, 2),
                        score_delta=delta,
                        source_count=total,
                        last_verified=date.today().isoformat(),
                        conditions={"strategy": key.split("|")[0], "sector": key.split("|")[1] if "|" in key else ""},
                        created_at=datetime.now().isoformat(),
                    ))
                    new_principles += 1

        # 시장 레벨별 승률 분석 (PRISM의 지수 변곡점 학습)
        level_stats: Dict[str, Dict] = {}
        for s in self._layer2:
            # Layer 2에는 market_level이 없으므로 Layer 1에서 집계
            pass
        for o in self._layer1:
            if o.market_level:
                lv = o.market_level
                if lv not in level_stats:
                    level_stats[lv] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                if o.pnl_pct > 0:
                    level_stats[lv]["wins"] += 1
                else:
                    level_stats[lv]["losses"] += 1
                level_stats[lv]["total_pnl"] += o.pnl_pct

        for lv, stats in level_stats.items():
            total = stats["wins"] + stats["losses"]
            if total < 5:
                continue
            win_rate = stats["wins"] / total
            avg_pnl = stats["total_pnl"] / total
            if win_rate <= 0.35:
                _lv_existing = next(
                    (p for p in self._layer3 if p.conditions.get("market_level") == lv),
                    None
                )
                if not _lv_existing:
                    self._layer3.append(TradePrinciple(
                        rule=f"KOSPI {lv} 구간: 승률 {win_rate:.0%}, 평균 {avg_pnl:+.1f}% → 보수적 진입",
                        confidence=round(1 - win_rate, 2),
                        score_delta=-2,
                        source_count=total,
                        last_verified=date.today().isoformat(),
                        conditions={"market_level": lv},
                        created_at=datetime.now().isoformat(),
                    ))
                    new_principles += 1

        # 오래된 원칙 비활성화 (90일 미검증)
        cutoff = (date.today() - timedelta(days=90)).isoformat()
        for p in self._layer3:
            if p.last_verified < cutoff and p.active:
                p.active = False
                logger.info(f"[거래메모리] 원칙 비활성화 (90일 미검증): {p.rule}")

        # confidence < 0.3 비활성화
        for p in self._layer3:
            if p.confidence < 0.3 and p.active:
                p.active = False

        if new_principles:
            self._save_layer3()
            logger.info(f"[거래메모리] L2→L3 원칙 추출: {new_principles}건 신규")
        return new_principles

    # ============================================================
    # 점수 보정 (매수 시 호출)
    # ============================================================

    def get_score_adjustment(self, strategy: str, sector: str = "",
                             market_level: str = "") -> int:
        """
        Layer 3 원칙 기반 매수 점수 보정

        Returns:
            score_delta: -3 ~ +3 범위의 점수 보정값
        """
        total_delta = 0
        matched_rules = []

        for p in self._layer3:
            if not p.active or p.confidence < 0.5:
                continue

            cond = p.conditions

            # 시장 레벨 원칙 (전략/섹터 무관, 레벨만 매칭)
            if cond.get("market_level"):
                if market_level and cond["market_level"] == market_level:
                    total_delta += p.score_delta
                    matched_rules.append(f"{p.rule} ({p.score_delta:+d})")
                continue

            # 전략 매칭
            if cond.get("strategy") and cond["strategy"] != strategy:
                continue
            # 섹터 매칭 (비어있으면 모든 섹터)
            if cond.get("sector") and sector and cond["sector"] != sector:
                continue

            total_delta += p.score_delta
            matched_rules.append(f"{p.rule} ({p.score_delta:+d})")

        # 클램핑
        total_delta = max(-3, min(3, total_delta))

        if matched_rules:
            logger.debug(
                f"[거래메모리] 점수보정 {strategy}/{sector}: {total_delta:+d} "
                f"({len(matched_rules)}개 원칙)"
            )

        return total_delta

    # ============================================================
    # 주간 압축 (금요일 evolve 후 호출)
    # ============================================================

    def compress_layers(self):
        """Layer 1→2→3 전체 압축 실행"""
        l1_count = self._compress_to_layer2()
        l3_count = self._extract_principles()

        # LLM 보조 회고: 최근 손실 거래에서 교훈 추출 (선택적)
        llm_insights = 0
        if self._llm_manager and len(self._layer2) >= 5:
            try:
                llm_insights = self._llm_retrospective()
            except Exception as e:
                logger.debug(f"[거래메모리] LLM 회고 실패 (무시): {e}")

        logger.info(
            f"[거래메모리] 압축 완료: L1→L2 {l1_count}건, L2→L3 원칙 {l3_count}건, "
            f"LLM회고 {llm_insights}건 "
            f"(L1={len(self._layer1)}, L2={len(self._layer2)}, "
            f"L3={len([p for p in self._layer3 if p.active])}개 활성)"
        )

    def _llm_retrospective(self) -> int:
        """LLM 보조 회고: 최근 손실 거래 패턴에서 교훈 추출 (PRISM 4단계 차용)"""
        if not self._llm_manager:
            return 0

        # 최근 손실 거래 5건 요약
        recent_losses = [s for s in self._layer2 if not s.is_win][-5:]
        if len(recent_losses) < 3:
            return 0

        loss_summary = "\n".join(
            f"- {s.pattern} → {s.result} ({s.strategy})"
            for s in recent_losses
        )

        prompt = (
            f"최근 손실 거래 {len(recent_losses)}건을 분석하세요:\n"
            f"{loss_summary}\n\n"
            f"반복되는 실패 패턴 1~2개를 추출하고, "
            f"각각 '조건 → 회피 규칙' 형태로 작성하세요."
        )

        try:
            import asyncio
            # 동기 컨텍스트에서 호출될 수 있으므로 try
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 이미 이벤트 루프 내 — 직접 호출 불가, 스킵
                return 0
        except RuntimeError:
            return 0

        return 0  # 비동기 환경에서만 실행 가능 — 향후 async 버전 필요

    # ============================================================
    # 요약
    # ============================================================

    def get_summary(self) -> Dict:
        """메모리 상태 요약"""
        active_principles = [p for p in self._layer3 if p.active]
        return {
            "layer1_count": len(self._layer1),
            "layer2_count": len(self._layer2),
            "layer3_active": len(active_principles),
            "layer3_total": len(self._layer3),
            "principles": [
                {"rule": p.rule, "confidence": p.confidence, "delta": p.score_delta}
                for p in active_principles
            ],
        }

    # ============================================================
    # 영속화
    # ============================================================

    def _save_layer1(self):
        try:
            path = self._cache_dir / "layer1.json"
            data = [asdict(o) for o in self._layer1[-200:]]  # 최근 200건만
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"[거래메모리] L1 저장 실패: {e}")

    def _save_layer2(self):
        try:
            path = self._cache_dir / "layer2.json"
            data = [asdict(s) for s in self._layer2[-500:]]  # 최근 500건만
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"[거래메모리] L2 저장 실패: {e}")

    def _save_layer3(self):
        try:
            path = self._cache_dir / "layer3_principles.json"
            data = [asdict(p) for p in self._layer3]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"[거래메모리] L3 저장 실패: {e}")

    def _load(self):
        # Layer 1
        try:
            path = self._cache_dir / "layer1.json"
            if path.exists():
                data = json.loads(path.read_text())
                self._layer1 = [TradeOutcome(**d) for d in data]
        except Exception as e:
            logger.warning(f"[거래메모리] L1 로드 실패: {e}")

        # Layer 2
        try:
            path = self._cache_dir / "layer2.json"
            if path.exists():
                data = json.loads(path.read_text())
                self._layer2 = [TradeSummary(**d) for d in data]
        except Exception as e:
            logger.warning(f"[거래메모리] L2 로드 실패: {e}")

        # Layer 3
        try:
            path = self._cache_dir / "layer3_principles.json"
            if path.exists():
                data = json.loads(path.read_text())
                self._layer3 = [TradePrinciple(**d) for d in data]
                active = sum(1 for p in self._layer3 if p.active)
                if active:
                    logger.info(f"[거래메모리] L3 원칙 로드: {active}개 활성")
        except Exception as e:
            logger.warning(f"[거래메모리] L3 로드 실패: {e}")

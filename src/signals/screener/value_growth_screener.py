"""
QWQ AI Trader - 밸류코어(가치·성장 장기보유) 스크리너 (2026-08-04)

설계: docs/strategies/value-growth-core-design.md
코어홀딩(모멘텀 코어)과 정반대 철학 — "싸게 사서 기다린다".
CoreScreener를 상속해 유니버스/지표/수급 파이프라인을 재사용하되,
진입 필터와 스코어링을 가치·성장 2버킷으로 교체한다.

파이프라인:
    유니버스 → 일봉 지표 → 사전 필터(가격·유동성·추세 게이트)
    → DART 재무 보강 (fail-closed) → 자격 필터 → 수급/자산증가율 보강
    → 버킷별 스코어링 → 섹터 캡 → 정렬

코어스크리너와의 핵심 차이:
    - 신고가/60일 상승 요구 없음 (소외주 허용) — 대신 "떨어지는 칼"만 회피
    - 재무 미확보 = 후보 제외 (fail-closed; 코어는 fail-open 감점 생략)
    - 금융/지주 v1 제외, 동일 업종 최대 1종목 (저PBR 쏠림 방지)
"""

import time as _time
from typing import Dict, List, Optional

from loguru import logger

from .core_screener import CoreScreener, CoreCandidate

# 금융/지주 제외 (v1 — 업종 상대 밸류에이션 도입 전까지)
_FINANCIAL_SECTOR_KEYWORDS = ("은행", "증권", "보험", "금융", "카드", "캐피탈")
_FINANCIAL_NAME_KEYWORDS = ("금융지주", "홀딩스", "은행", "증권", "보험", "카드", "캐피탈")


class ValueGrowthScreener(CoreScreener):
    """가치·성장 2버킷 장기보유 스크리너 (CoreScreener 파이프라인 재사용)"""

    # DART 재무 보강 가드 (asset_growth 패턴 — 장애 시 스캔 전체 지연 방지)
    _FIN_TIME_BUDGET_SEC = 150.0
    _FIN_MAX_CONSEC_FAIL = 5

    def __init__(self, broker, kis_market_data, stock_master=None,
                 config: Optional[Dict] = None):
        super().__init__(broker, kis_market_data, stock_master, config)
        cfg = self._config
        self._min_profit_years = int(cfg.get("min_profit_years", 2))
        self._max_debt_ratio = float(cfg.get("max_debt_ratio_pct", 200.0))
        self._trend_gate_ma200_mult = float(cfg.get("trend_gate_ma200_mult", 0.9))
        self._trend_gate_min_60d = float(cfg.get("trend_gate_min_60d_pct", -15.0))
        self._sector_cap = int(cfg.get("max_per_sector", 1))
        self._exclude_financials = bool(cfg.get("exclude_financials", True))

    # ── 전체 스캔 (파이프라인 순서가 코어와 다르다) ──────────────────
    async def run_full_scan(self) -> List[CoreCandidate]:
        logger.info("[밸류코어] 전체 스캔 시작...")

        universe = await self._build_universe()
        logger.info(f"[밸류코어] 유니버스: {len(universe)}개")
        if not universe:
            return []

        candidates = await self._calculate_indicators(universe)
        logger.info(f"[밸류코어] 지표 계산 완료: {len(candidates)}개")

        pre = [c for c in candidates if self._pre_filter(c)]
        logger.info(f"[밸류코어] 사전 필터(유동성·추세 게이트) 통과: {len(pre)}개")

        await self._enrich_financials(pre)

        qualified = [c for c in pre if self._qualification_filter(c)]
        logger.info(f"[밸류코어] 자격 필터(재무 fail-closed) 통과: {len(qualified)}개")
        if not qualified:
            return []

        await self._enrich_supply_demand(qualified)
        await self._enrich_asset_growth(qualified)

        self._build_percentile_cache(qualified)
        scored: List[CoreCandidate] = []
        for c in qualified:
            # 두 버킷을 각각 채점하되 reasons는 배정된 버킷 것만 남긴다 (P2-4)
            base_reasons = list(c.reasons)
            v = self._score_value(c)
            v_reasons = c.reasons[len(base_reasons):]
            c.reasons = list(base_reasons)
            g = self._score_growth(c)
            g_reasons = c.reasons[len(base_reasons):]
            # 높은 쪽 버킷으로 배정 (둘 다 컷 미달이어도 기록 — 컷은 호출측에서)
            if v >= g:
                c.score, c.indicators["bucket"] = v, "value"
                c.reasons = base_reasons + v_reasons
            else:
                c.score, c.indicators["bucket"] = g, "growth"
                c.reasons = base_reasons + g_reasons
            scored.append(c)

        scored = self._apply_sector_cap(scored)
        scored.sort(key=lambda c: c.score, reverse=True)

        for i, c in enumerate(scored[:10]):
            logger.info(
                f"[밸류코어] #{i+1} {c.symbol} {c.name} "
                f"[{c.indicators.get('bucket')}]: {c.score:.1f}점 "
                f"({', '.join(c.reasons[:4])})"
            )
        self._last_candidates = scored
        return scored

    # ── 필터 ─────────────────────────────────────────────────
    def _pre_filter(self, c: CoreCandidate) -> bool:
        """유동성 + value trap 방어 추세 게이트 (재무 조회 전 경량 필터)"""
        ind = c.indicators
        close = ind.get("close")
        if close is None or close < self._min_price:
            return False
        avg_tv = ind.get("avg_trading_value")
        if avg_tv is None or avg_tv < self._min_avg_trading_value:
            return False
        # 떨어지는 칼 회피: MA200×0.9 이상 (신고가/상승 요구는 없음)
        ma200 = ind.get("ma200")
        if ma200 is None or ma200 <= 0 or close < ma200 * self._trend_gate_ma200_mult:
            return False
        chg60 = ind.get("change_60d")
        if chg60 is None or chg60 < self._trend_gate_min_60d:
            return False
        # 금융/지주 제외 (v1)
        if self._exclude_financials and self._is_financial(c):
            return False
        return True

    def _is_financial(self, c: CoreCandidate) -> bool:
        sector = str(c.indicators.get("sector", ""))
        if any(k in sector for k in _FINANCIAL_SECTOR_KEYWORDS):
            return True
        return any(k in (c.name or "") for k in _FINANCIAL_NAME_KEYWORDS)

    def _qualification_filter(self, c: CoreCandidate) -> bool:
        """재무 자격 필터 — 스냅샷 미확보는 탈락 (fail-closed)"""
        snap = c.indicators.get("fin_snapshot")
        if snap is None:
            return False
        if snap.profit_years < self._min_profit_years:
            return False
        debt = snap.debt_ratio_pct
        if debt is None or debt >= self._max_debt_ratio:
            return False
        return True

    # ── DART 재무 보강 ────────────────────────────────────────
    async def _enrich_financials(self, candidates: List[CoreCandidate]) -> None:
        try:
            from ..fundamentals.financials import get_financials_provider
            provider = get_financials_provider()
        except Exception as e:
            logger.warning(f"[밸류코어] 재무 프로바이더 로드 실패 — 전 종목 탈락(fail-closed): {e}")
            return

        started = _time.monotonic()
        enriched = 0
        consec_fail = 0
        for i, c in enumerate(candidates):
            if _time.monotonic() - started > self._FIN_TIME_BUDGET_SEC:
                logger.warning(
                    f"[밸류코어] 재무 보강 시간 예산 초과 — {i}/{len(candidates)}개에서 중단"
                )
                break
            try:
                snap = await provider.get_snapshot(c.symbol)
                consec_fail = 0
            except Exception as e:
                logger.debug(f"[밸류코어] {c.symbol} 재무 조회 실패: {e}")
                consec_fail += 1
                if consec_fail >= self._FIN_MAX_CONSEC_FAIL:
                    logger.warning(f"[밸류코어] 재무 조회 연속 {consec_fail}회 실패 — 중단")
                    break
                continue
            if snap is not None:
                c.indicators["fin_snapshot"] = snap
                enriched += 1
        logger.info(
            f"[밸류코어] 재무 보강: {enriched}/{len(candidates)}개 "
            f"({_time.monotonic() - started:.1f}s)"
        )

    # ── 스코어링: 가치 버킷 (100점) ───────────────────────────
    def _score_value(self, c: CoreCandidate) -> float:
        ind = c.indicators
        snap = ind["fin_snapshot"]
        score = 0.0

        # 밸류에이션 45: PBR 분위 25 + PER 분위 20 (후보군 내 상대 순위)
        # (설계 초안의 배당 10점은 DIV 데이터 소스 미확보로 재분배 — 2026-08-04 리뷰 P1-2.
        #  pykrx KRX 인증 장애로 배선 불가. 소스 확보 시 재도입: PBR 25→20, 안정성 15→10)
        score += self._percentile_points(c, "pbr", 25, low_is_good=True)
        score += self._percentile_points(c, "per", 20, low_is_good=True)
        pbr = ind.get("pbr")
        if pbr is not None and 0 < pbr < 1.0:
            c.reasons.append(f"PBR{pbr:.2f}")

        # 퀄리티 45: ROE 15 + 영업이익률 10 + 이익 안정성 15 + 시총 5
        roe = snap.roe_pct
        if roe is not None:
            if roe >= 10:
                score += 15
                c.reasons.append(f"ROE{roe:.0f}%")
            elif roe >= 8:
                score += 10
        margin = snap.op_margin_pct
        if margin is not None:
            if margin >= 10:
                score += 10
            elif margin >= 5:
                score += 5
        if snap.profit_years >= 3:
            score += 15
            c.reasons.append("3년흑자")
        elif snap.profit_years >= 2:
            score += 9
        if ind.get("rank", 999) <= 50:
            score += 5

        # 수급·추세 10
        close, ma200 = ind.get("close"), ind.get("ma200")
        if close is not None and ma200 is not None and close > ma200:
            score += 5
        fnb = ind.get("foreign_net_buy_5d")
        inb = ind.get("inst_net_buy_5d")
        if (fnb is not None and fnb > 0) or (inb is not None and inb > 0):
            score += 5

        score += self._asset_growth_penalty(ind)
        return max(score, 0.0)

    # ── 스코어링: 성장 버킷 (100점) ───────────────────────────
    def _score_growth(self, c: CoreCandidate) -> float:
        ind = c.indicators
        snap = ind["fin_snapshot"]
        score = 0.0

        # 성장성 50
        rev_yoy = snap.revenue_yoy_pct
        if rev_yoy is not None:
            if rev_yoy >= 15:
                score += 20
                c.reasons.append(f"매출+{rev_yoy:.0f}%")
            elif rev_yoy >= 10:
                score += 12
        op_yoy = snap.op_yoy_pct
        if op_yoy is not None:
            if op_yoy >= 25:
                score += 20
                c.reasons.append(f"영익+{op_yoy:.0f}%")
            elif op_yoy >= 15:
                score += 12
        mi = snap.op_margin_improve_pp
        if mi is not None and mi >= 1.0:
            score += 10

        # 가격 체크 20: PEG (PER / 순이익 성장률)
        per = ind.get("per")
        net, net_prev = snap.net, snap.net_prev
        if per is not None and per > 0 and net is not None and net_prev is not None \
                and net_prev > 0 and net > net_prev:
            eps_growth = (net / net_prev - 1.0) * 100.0
            peg = per / eps_growth if eps_growth > 0 else None
            if peg is not None:
                if peg < 1.0:
                    score += 20
                    c.reasons.append(f"PEG{peg:.1f}")
                elif peg < 1.5:
                    score += 12

        # 퀄리티 20
        roe = snap.roe_pct
        if roe is not None and roe >= 12:
            score += 10
        debt = snap.debt_ratio_pct
        if debt is not None and debt < 100:
            score += 5
        if snap.profit_years >= 2:
            score += 5

        # 추세 10
        close, ma200 = ind.get("close"), ind.get("ma200")
        if close is not None and ma200 is not None and close > ma200:
            score += 5
        chg60 = ind.get("change_60d")
        if chg60 is not None and chg60 > 0:
            score += 5

        score += self._asset_growth_penalty(ind)
        return max(score, 0.0)

    # ── 헬퍼 ─────────────────────────────────────────────────
    def _percentile_points(self, c: CoreCandidate, key: str, max_pts: float,
                           low_is_good: bool) -> float:
        """후보군 내 상대 분위 점수 (하위 20% 만점 / 40% 60% / 60% 30%)"""
        vals = getattr(self, "_percentile_cache", {}).get(key)
        if vals is None:
            return 0.0
        v = c.indicators.get(key)
        if v is None or v <= 0:
            return 0.0
        rank = sum(1 for x in vals if x < v) / len(vals)   # 낮을수록 rank 작음
        if not low_is_good:
            rank = 1.0 - rank
        if rank <= 0.2:
            return max_pts
        if rank <= 0.4:
            return max_pts * 0.6
        if rank <= 0.6:
            return max_pts * 0.3
        return 0.0

    def _build_percentile_cache(self, candidates: List[CoreCandidate]) -> None:
        self._percentile_cache = {}
        for key in ("pbr", "per"):
            vals: List[float] = []
            for c in candidates:
                v = c.indicators.get(key)
                if v is not None and v > 0:
                    vals.append(float(v))
            if vals:
                self._percentile_cache[key] = sorted(vals)

    def _asset_growth_penalty(self, ind: Dict) -> float:
        """총자산 증가율 과다 감점 (기존 코어홀딩과 동일 기준)"""
        growth = ind.get("asset_growth_pct")
        if growth is None:
            return 0.0
        if growth >= 50:
            return -5.0
        if growth >= 30:
            return -3.0
        return 0.0

    def _apply_sector_cap(self, scored: List[CoreCandidate]) -> List[CoreCandidate]:
        """동일 업종 최대 N종목 (버킷 무관 — 저PBR 업종 쏠림 방지)"""
        scored.sort(key=lambda c: c.score, reverse=True)
        seen: Dict[str, int] = {}
        kept = []
        for c in scored:
            sector = str(c.indicators.get("sector", "")) or "미분류"
            if sector != "미분류" and seen.get(sector, 0) >= self._sector_cap:
                logger.debug(f"[밸류코어] 섹터 캡 제외: {c.symbol} ({sector})")
                continue
            seen[sector] = seen.get(sector, 0) + 1
            kept.append(c)
        return kept

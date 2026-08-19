"""조건부 변동성 타게팅 — 모멘텀 계열 사이징 축소 오버레이 (2026-08-19)

Bongaerts, Kang & van Dijk (FAJ 2020) 'Conditional Volatility Targeting' 응용:
모멘텀 팩터는 고변동 국면에서 수익·위험 특성이 급격히 악화 — 실현변동성이
임계를 넘을 때**만** 노출을 줄이는 조건부 스케일링이 무조건 타게팅보다 우수
(모멘텀 Sharpe ~2배, MDD 54→20% 보고. 103개 팩터 중 유의 개선은 모멘텀 계열뿐).
상세: docs/research/ai-trading-research-2026-08.md

우리 적용 (downscale-only):
  - KOSPI 20일 실현변동성(연율화 %)을 하루 1회 계산해 캐시 파일에 저장
    (사이징 핫패스는 캐시만 읽는다 — 네트워크 호출 없음)
  - vol <= VOL_THRESHOLD: 배율 1.0 (평상시 무개입 — '조건부'의 핵심)
  - vol >  VOL_THRESHOLD: mult = VOL_TARGET / vol, 하한 MIN_MULT
  - 모멘텀 계열 전략의 신규 매수 사이징에만 적용. 레버리지(>1.0) 없음, 청산 무관.
  - 캐시 부재/노후(3일+) 시 1.0 (fail-open — 개별 ATR 사이징이 1차 방어선)

캘린더 시즈널리티와 동일한 '사이징 배율' 패턴. VOL_TARGETING=0 으로 비활성화.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Tuple

from loguru import logger

# 임계/목표 연율 변동성 (%) — 극단 국면(전체 거래일의 ~11%)에서만 개입.
# 로컬 검증 (KODEX 200, 2015~2026.08, 룩어헤드 방지 shift(1)):
#   바이앤홀드          CAGR +15.6% Sharpe 0.721 MDD -40.8%
#   조건부(>18%)        CAGR +12.0% Sharpe 0.753 MDD -33.3%  ← CAGR 희생 과다
#   조건부(>25%) 채택   CAGR +13.5% Sharpe 0.787 MDD -34.8%  ← 균형점
# 고변동 국면은 평균 수익이 오히려 높지만(반등 포함) 꼬리가 2배 나쁨
# (fwd20 하위5%: -11.2% vs -6.4%) — 임계를 높여 재앙 국면만 자른다.
VOL_THRESHOLD = 25.0
VOL_TARGET = 25.0
MIN_MULT = 0.4           # 최악 국면에서도 사이즈 40%는 유지 (전면 차단은 하지 않는다)
LOOKBACK_DAYS = 20       # 실현변동성 창
FETCH_DAYS = 70          # FDR 조회 여유 (주말/공휴일 포함)
STALE_LIMIT_DAYS = 3     # 캐시 이 이상 노후 시 무개입(1.0)
RET_OUTLIER_ABS = 0.12   # 일수익률 |12%| 초과 = 데이터 오류로 간주하고 제외
                         # (지수 서킷브레이커 영역 — 실측: FDR 069500에 +24.2%/일 오염 확인)

# 조건부 타게팅의 실증 근거가 모멘텀 계열에 한정되므로 적용 대상도 한정한다
MOMENTUM_STRATEGIES = frozenset({
    "sepa_trend", "gap_and_go", "momentum_breakout", "vcp_breakout",
})

_CACHE_FILE = Path.home() / ".cache" / "ai_trader" / "vol_targeting.json"
_mem_cache: dict = {}    # 파일 I/O 절약용 (사이징은 신호마다 호출)
_logged: set = set()     # 하루 한 번만 INFO 로그


def _enabled() -> bool:
    return os.getenv("VOL_TARGETING", "1") != "0"


async def refresh_vol_state() -> None:
    """KOSPI 실현변동성 갱신 (하루 1회, 장전 스케줄러에서 호출).

    실패는 삼킨다 — 캐시가 노후하면 사이징 쪽이 알아서 1.0으로 폴백한다.
    """
    if not _enabled():
        return
    try:
        import FinanceDataReader as fdr
        import pandas as pd  # noqa: F401 (fdr 의존)

        def _fetch():
            # KS11(KRX 지수 API)이 간헐 차단됨(LOGOUT 실측) → KODEX 200 폴백
            try:
                df = fdr.DataReader("KS11")
            except Exception:
                df = fdr.DataReader("069500")
            return df["Close"].tail(FETCH_DAYS)

        closes = await asyncio.to_thread(_fetch)
        rets = closes.pct_change().dropna()
        # 데이터 오류 가드 — 오염된 하루가 변동성을 폭증시켜 최대 축소를 오발동시킨다
        _n_outlier = int((rets.abs() > RET_OUTLIER_ABS).sum())
        if _n_outlier:
            logger.warning(
                f"[변동성타게팅] 일수익률 이상치 {_n_outlier}건 제외 (|r|>{RET_OUTLIER_ABS:.0%})"
            )
            rets = rets[rets.abs() <= RET_OUTLIER_ABS]
        rets = rets.tail(LOOKBACK_DAYS)
        if len(rets) < LOOKBACK_DAYS // 2:
            logger.warning(f"[변동성타게팅] 수익률 표본 부족 ({len(rets)}) — 갱신 생략")
            return
        realized = float(rets.std() * math.sqrt(252) * 100)

        if realized > VOL_THRESHOLD:
            mult = max(MIN_MULT, VOL_TARGET / realized)
        else:
            mult = 1.0

        state = {
            "date": date.today().isoformat(),
            "realized_vol": round(realized, 2),
            "mult": round(mult, 3),
        }
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(state, ensure_ascii=False))
        _mem_cache.clear()
        _mem_cache.update(state)
        logger.info(
            f"[변동성타게팅] KOSPI 20일 실현변동성 {realized:.1f}% "
            f"(임계 {VOL_THRESHOLD:.0f}%) → 모멘텀 사이징 ×{mult:.2f}"
        )
    except Exception as e:
        logger.warning(f"[변동성타게팅] 갱신 실패 (무시): {e}")


def _load_state() -> dict:
    if _mem_cache:
        return _mem_cache
    try:
        if _CACHE_FILE.exists():
            state = json.loads(_CACHE_FILE.read_text())
            _mem_cache.update(state)
            return state
    except Exception:
        pass
    return {}


def vol_targeting_multiplier(strategy: str) -> Tuple[float, str]:
    """전략별 변동성 타게팅 사이징 배율.

    Returns:
        (배율, 사유 태그) — 무개입이면 (1.0, "")
    """
    if not _enabled():
        return 1.0, ""
    if strategy not in MOMENTUM_STRATEGIES:
        return 1.0, ""

    state = _load_state()
    _raw_mult = state.get("mult")
    try:
        mult = float(_raw_mult) if _raw_mult is not None else 1.0
    except (ValueError, TypeError):
        return 1.0, ""
    # 방어 (Codex P2): NaN/inf는 fail-open, 정상값은 [MIN_MULT, 1.0]로 clamp —
    # 오염된 캐시가 Decimal 금액 계산으로 전파되는 것을 차단
    if not math.isfinite(mult):
        return 1.0, ""
    if mult >= 1.0:
        return 1.0, ""
    mult = max(mult, MIN_MULT)

    # 캐시 노후 검사 — 오래된 고변동 판정으로 계속 축소하지 않는다
    try:
        cache_date = date.fromisoformat(state.get("date", ""))
        if (date.today() - cache_date).days > STALE_LIMIT_DAYS:
            return 1.0, ""
    except (ValueError, TypeError):
        return 1.0, ""

    tag = f"고변동({state.get('realized_vol', '?')}%)"
    key = (state.get("date"), strategy)
    if key not in _logged:
        _logged.add(key)
        if len(_logged) > 64:
            _logged.clear()
            _logged.add(key)
        logger.info(
            f"[변동성타게팅] {strategy} 사이징 ×{mult:.2f} — {tag}"
        )
    return mult, tag

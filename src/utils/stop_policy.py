"""손절 해석 공통 함수 (2026-09-14 리뷰 후속 T2 — F1)

ExitManager.update_price 의 손절 판정과 엔진 위험 사이징의 분모가 **같은 해석**을 쓰도록 우선순위를
한 곳에 둔다. 기존 정상 입력에서의 우선순위는 그대로다:

    dynamic(ATR 산출값, min_dynamic_stop_pct 하한 클램프) > strategy(전략 고정 SL, 미클램프) > global(ExitConfig)
    비코어는 장중 급락 cap (INTRADAY_CRASH_PARAMS) 을 상한으로 적용, 코어는 제외

비율 입력은 전부 Decimal 또는 None. 형 변환은 호출 경계(stop_pct_or_none)에서 하고, 0·음수·NaN·Inf 는
ValueError 로 거부한다 (명시적 안전 보강 — 무효 설정으로 손절 0% 나 사이징 0 나눗셈이 생기지 않게).
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional

__all__ = ["StopDecision", "resolve_effective_stop", "stop_pct_or_none", "make_entry_stop_resolver"]


@dataclass(frozen=True)
class StopDecision:
    stop_pct: Decimal          # 순손익률(net, KR 은 수수료 포함) 기준 손절폭 %
    source: str                # "dynamic" | "strategy" | "global"
    crash_capped: bool         # 장중 급락 cap 이 실제로 값을 낮췄는지


def stop_pct_or_none(value: Any) -> Optional[Decimal]:
    """호출 경계 형 변환: None 은 None, 그 외는 Decimal(str(value)). 변환 불가는 ValueError."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError(f"손절 비율로 해석할 수 없는 값: {value!r}") from e


def resolve_effective_stop(*, dynamic_stop_pct: Optional[Decimal], fixed_stop_pct: Optional[Decimal],
                           global_stop_pct: Decimal, min_dynamic_stop_pct: Decimal,
                           crash_stop_pct: Optional[Decimal], is_core: bool) -> StopDecision:
    supplied = (dynamic_stop_pct, fixed_stop_pct, global_stop_pct,
                min_dynamic_stop_pct, crash_stop_pct)
    if any(v is not None and (not v.is_finite() or v <= 0) for v in supplied):
        raise ValueError("유효하지 않은 손절 설정")
    if dynamic_stop_pct is not None:
        value = max(dynamic_stop_pct, min_dynamic_stop_pct)
        source = "dynamic"
    elif fixed_stop_pct is not None:
        value, source = fixed_stop_pct, "strategy"
    else:
        value, source = global_stop_pct, "global"
    capped = not is_core and crash_stop_pct is not None and value > crash_stop_pct
    if capped:
        value = crash_stop_pct
    if not value.is_finite() or value <= 0:
        raise ValueError("유효하지 않은 초기 손절폭")
    return StopDecision(value, source, capped)


def make_entry_stop_resolver(exit_manager: Any,
                             strategy_exit_params: Mapping[str, Mapping[str, Any]],
                             ) -> Callable[[Optional[str]], StopDecision]:
    """엔진 `resolve_entry_stop(strategy) -> StopDecision` 콜백.

    kr_scheduler 의 신규 fill 등록(register_position)과 같은 전략별 설정(run_trader._strategy_exit_params)을
    조회한다. 신규 등록은 price_history 없이 호출되므로 ATR 동적 손절이 생기지 않는다 → dynamic 은 항상 None
    (atr_pct_hint 는 트레일링 용도). 글로벌·min 은 ExitManager.resolve_stop 이 현재 상태로 해석한다.
    급락 cap 은 **분모에 적용하지 않는다**(apply_crash_cap=False) — cap 은 해제되면 SL 이 원래 값으로 돌아가므로
    타이트한 임시 SL 로 나누면 급락 중 포지션이 커진다. `crash_capped` 는 '주문 시점에 cap 활성' 표시로만 쓴다.
    """
    def resolve(strategy: Optional[str]) -> StopDecision:
        params = strategy_exit_params.get(strategy, {}) if strategy else {}
        is_core = bool(params.get("is_core", False)) or strategy == "core_holding"
        return exit_manager.resolve_stop(
            dynamic_stop_pct=None, fixed_stop_pct=params.get("stop_loss_pct"), is_core=is_core,
            apply_crash_cap=False,
        )
    return resolve

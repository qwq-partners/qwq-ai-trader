"""조건부 진입계획(EntryPlan) 주문 직전 검증기 — T11 계약 2.3 (2026-09-15).

정본은 `src.core.batch_analyzer.PendingSignal` 이다(별도 주문 파이프라인·복사 캐시를 만들지
않는다). 이 모듈은 계획과 최신 호가·시장 상태를 받아 허용/대기/거절과 사유 코드를 내는
**순수 함수**만 제공한다. 이번 단계에서는 **shadow 전용** — 결과로 기존 실주문을 허용하거나
차단하지 않는다(엔진은 기록만 한다).

원칙:
- 종목 적합성(팀 판단)과 즉시 진입 가능성(이 검증기)은 분리한다.
- setup 별 필수 조건(sepa_pullback 밴드, vcp_breakout 트리거, gap_vwap 의 VWAP)을 공통 틀
  안에서 적용하되 무조건 같은 규칙으로 합치지 않는다.
- 계획이 없으면 검증하지 않는다 — 현재가로 그럴듯한 계획을 자동 생성하지 않는다.
- 손절·계획 위험은 기존 ExitManager/stop_policy/entry_risk 해석을 따른다(여기서 목표가·손절가를
  바꾸지 않는다).
- 시장가 주문 직전 재조회만으로 가격 상한을 보장한다고 주장하지 않는다(사유 코드 PRICE_ABOVE_CAP
  는 "검증 시점" 판정이다).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

PLAN_CHECK_STATUS = ("allow", "wait", "reject")

# 사유 코드 (계약 2.3) — 접두사 뒤에 ':<이름>' 이 붙을 수 있다
REASON_CODES = (
    "PLAN_EXPIRED", "PRICE_ABOVE_CAP", "PRICE_BELOW_BAND", "TRIGGER_NOT_MET",
    "QUOTE_MISSING", "QUOTE_STALE", "INPUT_MISSING", "INTRADAY_BLOCK", "INVALIDATED",
    "RISK_BUDGET", "SLOT_FULL", "DAILY_LIMIT", "COST_RR_LOW", "CHECKER_ERROR",
    # 계약 상수. 본문 구현(2026-09-15) 이후 어떤 경로에서도 생성되지 않는다 —
    # 원장 소비자는 "미구현 상태가 있다"고 읽지 말 것. 제거는 계약 소유자와 함께.
    "CHECKER_NOT_IMPLEMENTED",
)

# 호가 신선도 상한(초) — 이보다 오래된 호가로는 판정하지 않는다 (정책값: 장중 5분 루프 주기)
QUOTE_MAX_AGE_SEC = 300


@dataclass
class PlanCheck:
    """주문 직전 shadow 검증 결과"""

    status: str                                   # allow | wait | reject
    reasons: List[str] = field(default_factory=list)
    expected_fill_price: Optional[float] = None
    # (목표-예상체결-비용)/(예상체결-손절+비용); 계산 불가면 None.
    # ⚠️ 비용은 계획 assumptions 가 준 것만 반영한다 — 현재 생산자는 **매수 수수료만**
    #    싣는다(assumptions.fee_side="buy_only"). 왕복 비용(매도 수수료+거래세 포함 0.227%)
    #    을 쓸지는 정책 결정이므로 여기서 임의로 더하지 않는다. 컷오프를 정할 때 이 낙관
    #    편향을 반드시 감안할 것 (1차 리뷰 advisory).
    cost_adjusted_rr: Optional[float] = None
    checked_at: Optional[datetime] = None
    quote_as_of: Optional[datetime] = None
    missing_inputs: List[str] = field(default_factory=list)
    plan_id: str = ""
    setup: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("checked_at", "quote_as_of"):
            v = getattr(self, k)
            d[k] = v.isoformat(timespec="seconds") if isinstance(v, datetime) else None
        return d


# setup 별 필수 조건. `setup_rules` 로 덮어쓸 수 있다(테스트·정책 실험용).
#   required_quote_keys: 호가 dict 에 반드시 있어야 하는 키
#   require_trigger:     trigger.level(또는 breakout_trigger) 이 있어야 하고 그 위여야 한다
DEFAULT_SETUP_RULES: Dict[str, Dict[str, Any]] = {
    "gap_vwap": {"required_quote_keys": ["vwap"]},
    "vcp_breakout": {"require_trigger": True},
}

# 급락 단계 판정은 기존 batch_analyzer 규칙을 그대로 미러한다 — **새 차단을 추가하지 않는다**.
#   severe = 신규 진입 전면 차단(reject) / crash = SEPA 계열만 차단(wait) /
#   caution·normal = 최소 점수 상향(점수·사이징은 이 검증기 소관이 아니라 무판정).
_SEPA_KEYS = ("sepa_trend", "sepa_pullback")


def _as_dt(v: Any) -> Optional[datetime]:
    """datetime | ISO 문자열 → datetime (변환 실패는 None)"""
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def _as_float(v: Any) -> Optional[float]:
    """숫자 변환 — 0.0 도 유효값이므로 falsy 판정을 쓰지 않는다"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _plan_dict(plan: Any) -> Dict[str, Any]:
    """PendingSignal | dict → dict (정본은 PendingSignal, 복사 캐시를 만들지 않는다)"""
    if isinstance(plan, dict):
        return plan
    to_dict = getattr(plan, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(getattr(plan, "__dict__", None) or {})


def check_entry_plan(
    plan: Any,
    quote: Optional[Dict[str, Any]],
    now: datetime,
    *,
    intraday_level: Optional[str] = None,
    risk_ctx: Optional[Dict[str, Any]] = None,
    setup_rules: Optional[Dict[str, Any]] = None,
) -> PlanCheck:
    """계획(PendingSignal 또는 그 dict)과 최신 호가로 허용/대기/거절을 판정한다 (순수 함수).

    Args:
        plan: `PendingSignal` 인스턴스 또는 `to_dict()` 결과.
        quote: {"price", "as_of"(datetime|ISO), "vwap"(선택), "bid"/"ask"(선택)} — None 이면 QUOTE_MISSING.
        now: 판정 시각(주입).
        intraday_level: 급락 감지기 수준 normal|caution|crash|severe|None.
        risk_ctx: {"risk_budget_ok": bool|None, "slots_available": int|None, "daily_buys_left": int|None,
                   "fee_bps": float|None, "slippage_bps": float|None} — 없으면 해당 항목은 판정하지 않는다.
        setup_rules: setup 별 필수 조건 덮어쓰기(테스트용).

    판정 원칙:
        - reject = 이 계획으로는 더 갈 수 없다(만료·무효화·severe).
          wait = 상태가 바뀌면 통과할 수 있다(호가·트리거·밴드·한도).
        - 사유는 모아서 전부 돌려준다. 조기 반환은 호가가 없거나 노후해 **가격 판정 자체가
          불가능할 때**만 한다(노후 호가로 무효화를 판정하면 근거 없는 reject 가 된다).
        - `COST_RR_LOW` 는 사유만 부착하고 status 를 바꾸지 않는다 — 컷오프 정책값 미정.
        - `required_inputs` 는 **수치 입력만** 받는다(숫자로 변환되지 않으면 결측 처리).
          등급 문자열·불리언 근거는 여기 넣지 말 것.
        - 어떤 경우에도 예외를 밖으로 내지 않는다(CHECKER_ERROR).
    """
    try:
        p = _plan_dict(plan)
        plan_id = str(p.get("plan_id") or "")
        setup = str(p.get("setup") or "")
        strategy = str(p.get("strategy") or "")
        rules = dict(DEFAULT_SETUP_RULES)
        if setup_rules:
            rules.update(setup_rules)
        rule = rules.get(setup) or {}

        _inv = p.get("invalidation")
        invalidation = _inv if isinstance(_inv, dict) else {}
        _trg = p.get("trigger")
        trigger = _trg if isinstance(_trg, dict) else {}

        reasons: List[str] = []
        missing: List[str] = []
        flags = {"reject": False, "wait": False}

        def _add(kind: str, code: str) -> None:
            flags[kind] = True
            if code not in reasons:
                reasons.append(code)

        def _result(expected_fill=None, rr=None, quote_as_of=None) -> PlanCheck:
            status = "reject" if flags["reject"] else ("wait" if flags["wait"] else "allow")
            return PlanCheck(
                status=status, reasons=reasons, expected_fill_price=expected_fill,
                cost_adjusted_rr=rr, checked_at=now, quote_as_of=quote_as_of,
                missing_inputs=missing, plan_id=plan_id, setup=setup,
            )

        # ── 1) 만료 (호가와 무관) ────────────────────────────────────────────
        for raw in (p.get("expires_at"), invalidation.get("expires_at")):
            exp = _as_dt(raw)
            if exp is not None and now > exp:
                _add("reject", "PLAN_EXPIRED")
                break

        # ── 2) 장중 급락 (호가와 무관) — 기존 규칙 미러 ──────────────────────
        _levels = invalidation.get("intraday_levels")
        levels = list(_levels) if isinstance(_levels, (list, tuple)) else ["severe"]
        if intraday_level == "severe" and "severe" in levels:
            _add("reject", "INTRADAY_BLOCK:severe")
        elif intraday_level == "crash" and (strategy in _SEPA_KEYS or setup in _SEPA_KEYS):
            _add("wait", "INTRADAY_BLOCK:crash")

        # ── 3) 호가 — 없거나 노후하면 가격 판정을 하지 않는다 ────────────────
        price = _as_float((quote or {}).get("price")) if quote else None
        if price is None or price <= 0:
            _add("wait", "QUOTE_MISSING")
            return _result()

        as_of = _as_dt(quote.get("as_of"))
        if as_of is None:
            _add("wait", "QUOTE_STALE")
            return _result()
        age_sec = (now - as_of).total_seconds()
        if age_sec > QUOTE_MAX_AGE_SEC or age_sec < 0:
            # 미래 시각도 신뢰하지 않는다 (T10 B 의 as_of 기준과 동일)
            _add("wait", "QUOTE_STALE")
            return _result(quote_as_of=as_of)

        # ── 4) 필수 입력 ─────────────────────────────────────────────────────
        for name in (p.get("missing_inputs") or []):
            if str(name) not in missing:
                missing.append(str(name))
            _add("wait", f"INPUT_MISSING:{name}")
        for key in (rule.get("required_quote_keys") or []):
            if _as_float(quote.get(key)) is None:
                if key not in missing:
                    missing.append(key)
                _add("wait", f"INPUT_MISSING:{key}")
        for name in (p.get("required_inputs") or []):
            if str(name) in missing:
                continue
            if _as_float(p.get(name)) is None and _as_float(quote.get(name)) is None:
                missing.append(str(name))
                _add("wait", f"INPUT_MISSING:{name}")

        # ── 5) 무효화 (reject) ───────────────────────────────────────────────
        for key in ("below_price", "stop_price"):
            lvl = _as_float(invalidation.get(key))
            if lvl is not None and lvl > 0 and price <= lvl:
                _add("reject", f"INVALIDATED:{key}")

        # ── 6) 가격 밴드 (wait) ──────────────────────────────────────────────
        cap = _as_float(p.get("max_entry_price"))
        if cap is not None and cap > 0 and price > cap:
            # 이월 불가 계획이어도 reject 하지 않는다 — 장중 눌리면 밴드로 복귀할 수 있고,
            # 폐기 판정은 기존 batch_analyzer CARRY_REASONS 소관이다
            _add("wait", "PRICE_ABOVE_CAP")
        # ⚠️ 현재 어떤 생산자도 entry_band_low 를 채우지 않는다(sepa_pullback 의 눌림목
        #    하한이 아직 정의되지 않음) → 운영에서 PRICE_BELOW_BAND 는 발화하지 않는다.
        #    상한(max_entry_price)만 유효하다 (1차 리뷰 advisory).
        band_low = _as_float(p.get("entry_band_low"))
        if band_low is not None and band_low > 0 and price < band_low:
            _add("wait", "PRICE_BELOW_BAND")

        # ── 7) 돌파 트리거 (wait) — '초과' 조건 ──────────────────────────────
        require_trigger = bool(rule.get("require_trigger"))
        is_breakout = (str(p.get("entry_mode") or "") == "breakout"
                       or str(trigger.get("type") or "") == "breakout")
        if is_breakout or require_trigger:
            level = _as_float(trigger.get("level"))
            if level is None or level <= 0:
                level = _as_float(p.get("breakout_trigger"))
            if level is None or level <= 0:
                if require_trigger:
                    if "trigger.level" not in missing:
                        missing.append("trigger.level")
                    _add("wait", "INPUT_MISSING:trigger.level")
            elif price <= level:
                _add("wait", "TRIGGER_NOT_MET")

        # ── 8) 위험 예산·슬롯·일일 한도 (risk_ctx 가 있을 때만) ───────────────
        rc = risk_ctx or {}
        if rc.get("risk_budget_ok") is False:
            _add("wait", "RISK_BUDGET")
        slots = rc.get("slots_available")
        if slots is not None and int(slots) <= 0:
            _add("wait", "SLOT_FULL")
        left = rc.get("daily_buys_left")
        if left is not None and int(left) <= 0:
            _add("wait", "DAILY_LIMIT")

        # ── 9) 예상 체결가·비용 반영 손익비 ──────────────────────────────────
        # 운영 주문은 시장가라 상한이 보장되지 않는다. 매도호가가 있으면 그쪽이 더 정직하다.
        ask = _as_float(quote.get("ask"))
        expected_fill = ask if (ask is not None and ask > 0) else price

        _asm = p.get("assumptions")
        assumptions = _asm if isinstance(_asm, dict) else {}
        fee_bps = _as_float(assumptions.get("fee_bps"))
        if fee_bps is None:
            fee_bps = _as_float(rc.get("fee_bps"))
        slip_bps = _as_float(assumptions.get("slippage_bps"))
        if slip_bps is None:
            slip_bps = _as_float(rc.get("slippage_bps"))

        rr = None
        target = _as_float(p.get("target_price"))
        stop = _as_float(p.get("stop_price"))
        if target is not None and stop is not None and target > 0 and stop > 0:
            # 목표가·손절가는 계획값 그대로 — 여기서 바꾸지 않는다
            cost = expected_fill * ((fee_bps if fee_bps is not None else 0.0)
                                    + (slip_bps if slip_bps is not None else 0.0)) / 10000.0
            downside = expected_fill - stop + cost
            if downside > 0:
                rr = (target - expected_fill - cost) / downside
                if rr < 1.0 and "COST_RR_LOW" not in reasons:
                    reasons.append("COST_RR_LOW")   # status 는 바꾸지 않는다

        return _result(expected_fill=expected_fill, rr=rr, quote_as_of=as_of)

    except Exception as e:
        return PlanCheck(status="wait", reasons=[f"CHECKER_ERROR:{type(e).__name__}"], checked_at=now)

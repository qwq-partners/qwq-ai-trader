"""청산 reason 문자열 → exit_type 태그 (단일 출처).

`KRScheduler._classify_exit_type` 의 본문을 그대로 옮긴 순수 함수다. attach 의
`gateway._fill_metadata` 가 같은 분류를 써야 하는데 `src/execution/safety/` 가 스케줄러를
import 하면 순환이 생긴다 — 그래서 분류만 여기로 내리고 양쪽이 이것을 부른다(P0-3 Q-6).
"""
from __future__ import annotations


def classify_exit_type(reason) -> str:
    """exit_reason 문자열 → exit_type 태그 변환 (단일 출처, 중복 방지)

    ExitManager / batch_analyzer / kr_scheduler 세 곳에서 발생하는
    모든 reason 패턴을 커버한다.

    패턴 우선순위 (위에서 아래):
      0. 긴급 청산 (킬스위치/긴급전량청산 — 손절보다 먼저 판별)
      1. 손절
      2. 트레일링
      3. 본전 이탈 (breakeven)
      4. stale 계열 (횡보·무효화·저효율·보유기간 초과·코어홀딩 조기경보)
      5. RSI2 청산 → take_profit
      6. 분할 익절 (3차→2차→1차 순서 — "2차"가 "1차" 포함 오탐 방지)
      7. 일반 익절
      8. 테마 EOD / fill_detected / 기타 → manual

    빈 문자열·None 은 `'manual'` 이다(호출자가 빈 태그를 싣지 않게 한다).
    """
    r = reason if reason is not None else ""
    # 2026-08-05 P2: "긴급전량청산" 등이 manual로 오분류되던 데드 조건 복원.
    # risk/manager.record_exit가 ("stop_loss","emergency_stop")를 당일 손절
    # 등록 대상으로 취급하므로 emergency_stop 반환 시 재진입 강화 정책이 걸린다.
    # (소비처 확인: DB VARCHAR(30)/저널/메모리/위키 모두 자유 문자열 — 안전)
    if "긴급" in r or "emergency" in r.lower():
        return "emergency_stop"
    if "손절" in r or "stop" in r.lower():
        return "stop_loss"
    if "트레일링" in r or "trailing" in r.lower():
        return "trailing"
    if "본전 이탈" in r or "breakeven" in r.lower():
        return "breakeven"
    if ("횡보 청산" in r or "추세 무효화" in r
            or "익절후 저효율" in r
            or "보유기간 초과" in r
            or "코어홀딩 조기경보" in r):
        return "stale"
    if "RSI2 청산" in r:
        return "take_profit"
    if "3차" in r:
        return "third_take_profit"
    if "2차" in r:
        return "second_take_profit"
    if "1차" in r:
        return "first_take_profit"
    if "익절" in r or "take_profit" in r.lower():
        return "take_profit"
    return "manual"

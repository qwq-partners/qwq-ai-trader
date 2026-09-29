"""sell_specific 15초 폴백 — 취소 먼저·잔고 재조회·요청 중 안 팔린 몫만 (2026-09-29).

스크립트 최상위 load_env() 는 운영 .env 를 읽으므로, 소스에서 그 호출 줄만 지운 뒤
메모리에서 exec 해 모듈로 쓴다(파일 사본 없음). 브로커·토큰·락·sleep 은 가짜로 바꿔 main() 을 실제로 돌린다.
"""
import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parent.parent / "scripts" / "sell_specific.py"


def _load():
    src = SRC.read_text(encoding="utf-8")
    assert src.count("\nload_env()\n") == 1
    mod = types.ModuleType("sell_specific_under_test")
    mod.__file__ = str(SRC)
    saved_path = list(sys.path)  # 스크립트가 src/ 를 sys.path 맨 앞에 넣는다 — 다른 시험 import 오염 방지
    try:
        exec(compile(src.replace("\nload_env()\n", "\n", 1), str(SRC), "exec"), mod.__dict__)
    finally:
        sys.path[:] = saved_path
    return mod


ss = _load()


@pytest.mark.parametrize("requested,before,now,expected", [
    (10, 100, 90, 0),     # 100 보유·10 요청 전량 체결 → 남은 90주를 팔지 않는다
    (10, 100, 96, 6),     # 10 중 4 체결 → 6
    (10, 100, 100, 10),   # 미체결 → 요청 수량 그대로
    (10, None, 100, 0),   # 스냅샷 실패(또는 스냅샷에 없던 종목) → 폴백 없음
    (150, 100, 100, 100), # 요청 > 보유 → 현재 보유
    (150, 100, 60, 60),   # 요청 > 보유, 일부 체결 → 현재 보유
    (10, 100, 0, 0),      # 현재 보유 0(조회 실패 포함) → 폴백 없음
    (10, 100, 105, 10),   # 그사이 보유가 늘어도 요청 수량까지만
])
def test_fallback_qty(requested, before, now, expected):
    assert ss.fallback_qty(requested, before, now) == expected


class FakeBroker:
    def __init__(self, events, snapshots, cancel_count):
        self.events, self._snaps, self._cancel = events, list(snapshots), cancel_count

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_positions(self):
        self.events.append(("positions",))
        return {s: SimpleNamespace(quantity=q) for s, q in self._snaps.pop(0).items()}

    async def get_best_bid(self, sym):
        return 50000

    async def submit_order(self, order):
        self.events.append(("submit", order.symbol, order.quantity, order.order_type.name))
        return True, "OID"

    async def cancel_all_for_symbol(self, sym):
        self.events.append(("cancel", sym))
        return self._cancel


def _run(monkeypatch, argv, snapshots, cancel_count):
    events = []

    async def fake_sleep(sec):
        if sec >= 1:  # 주문 간 0.5초 간격은 순서 검사에서 뺀다
            events.append(("sleep", sec))

    monkeypatch.setattr(sys, "argv", ["sell_specific.py", *argv])
    monkeypatch.setattr(ss, "hold_or_exit", lambda name: None)
    monkeypatch.setattr(ss, "KISTokenManager", lambda: None)
    monkeypatch.setattr(ss, "KISBroker", lambda token_manager: FakeBroker(events, snapshots, cancel_count))
    monkeypatch.setattr(ss, "asyncio", SimpleNamespace(sleep=fake_sleep))
    asyncio.run(ss.main())
    return events


def _markets(events):
    return [e for e in events if e[0] == "submit" and e[3] == "MARKET"]


def test_partial_fill_cancels_then_refetches_then_sells_rest(monkeypatch):
    """(a)+(e) 4주 체결 → 취소 1건 → 재조회 96 → 시장가 6, 순서 = 제출→15초→취소→재조회→시장가."""
    ev = _run(monkeypatch, ["005930:10"], [{"005930": 100}, {"005930": 96}], cancel_count=1)
    assert ev == [
        ("positions",),
        ("submit", "005930", 10, "LIMIT"),
        ("sleep", 15),
        ("cancel", "005930"),
        ("sleep", 1),
        ("positions",),
        ("submit", "005930", 6, "MARKET"),
    ]


def test_full_fill_still_cancels_and_places_nothing(monkeypatch):
    """(b) 전량 체결 → 취소는 항상 부르고(0건) 추가 주문 없음."""
    ev = _run(monkeypatch, ["005930:10"], [{"005930": 100}, {"005930": 90}], cancel_count=0)
    assert ("cancel", "005930") in ev
    assert _markets(ev) == []


def test_cancel_zero_and_short_is_ambiguous(monkeypatch, capsys):
    """(c) 취소 0건인데 목표 미달 → 지정가 생존 가능 — 추가 주문 없이 경고."""
    ev = _run(monkeypatch, ["005930:10"], [{"005930": 100}, {"005930": 96}], cancel_count=0)
    assert _markets(ev) == []
    assert "005930 취소 0건·목표 미달 — 상태 불명, 추가 주문 안 함, MTS/HTS 확인" in capsys.readouterr().out


def test_duplicate_symbol_args_are_merged(monkeypatch):
    """(d) 10+10 → 지정가 1건 20주, 취소·폴백도 1건."""
    ev = _run(monkeypatch, ["005930:10", "005930:10"], [{"005930": 100}, {"005930": 95}], cancel_count=1)
    limits = [e for e in ev if e[0] == "submit" and e[3] == "LIMIT"]
    assert limits == [("submit", "005930", 20, "LIMIT")]
    assert ev.count(("cancel", "005930")) == 1
    assert _markets(ev) == [("submit", "005930", 15, "MARKET")]


def test_refetch_failure_means_no_fallback(monkeypatch):
    """재조회 실패(빈 응답) → 현재 보유 0 → 폴백 0."""
    ev = _run(monkeypatch, ["005930:10"], [{"005930": 100}, {}], cancel_count=1)
    assert _markets(ev) == []

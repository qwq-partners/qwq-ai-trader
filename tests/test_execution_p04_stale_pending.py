"""P0-4 항목 9 — `KRScheduler._cleanup_stale_pending` 의 attach 인지(가드 1줄).

`tests/test_engine_legacy_stale_eviction_characterization.py` 의 자매 파일이다. 그쪽은
runtime 없는 legacy `engine.py` 세 경로를 고정하고, 이 파일은 같은 종류의 결함이 있는
스케줄러 경로를 attach/legacy 두 갈래로 고정한다.

고치는 결함(계획 §2-4): `cancel_all_for_symbol` 은 브로커 인메모리 `_pending_orders` 만
도는데 attach 는 `GuardedKISTransport` 가 직접 POST 하므로 그 캐시가 비어 항상 0 을
돌려준다. 스케줄러는 0 을 "주문이 이미 소멸"로 읽어 pending 해제 → `clear_pending` →
`exit_manager.rollback_stage` 까지 간다 — 거래소에 살아 있는 SELL 위에서 단계를 되감는다.

**미설치(legacy) 경로는 바이트 동일**이다. D3 가 그 대조를 든다 — 이 파일이 legacy 동작을
옳다고 말하는 것이 아니라, 가드가 legacy 를 건드리지 않았음을 증명할 뿐이다.

실행: venv/bin/python -m pytest tests/test_execution_p04_stale_pending.py -q
"""
import asyncio
import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.types import MarketSession  # noqa: E402
from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402

# 취소 0 건·취소 예외 두 갈래를 심는 합성 브로커는 legacy 특성화 파일의 것을 그대로 쓴다.
from test_engine_legacy_stale_eviction_characterization import Broker  # noqa: E402
# `partial_missing`(D5)은 실제 `_sync_portfolio` 를 구동해야 보이므로 그 특성화의 fixture 를 쓴다.
from test_sync_portfolio_characterization import _make, _pos  # noqa: E402

SYM = '005930'
OTHER = '000660'   # D5 에서 KIS 응답에 남아 있는 종목(전체 빈 응답 방어와 분리)


class _RiskManager:
    def __init__(self, pending=()):
        self._pending_orders = set(pending)
        self.cleared = []

    async def clear_pending(self, symbol):
        self.cleared.append(symbol)


class _ExitManager:
    def __init__(self):
        self.rolled_back = []

    def is_exit_exempt(self, symbol):
        """이 legacy 대조 표본은 면제 종목이 아니다(main 조회 인터페이스)."""
        return False

    def rollback_stage(self, symbol):
        self.rolled_back.append(symbol)


def scheduler(*, attached, broker, stale_minutes=60, rm_pending=(SYM,)):
    """`_cleanup_stale_pending` 이 만지는 속성만 가진 최소 bot + 스케줄러.

    `attached=True` 면 `engine._execution_runtime` 에 표식 객체를 둔다 — 가드의 술어는
    P0-3 이 `:1026`·`:1919` 에 쓴 것과 같아서 None 인지 아닌지만 본다.
    `rm_pending` 은 sidecar(`engine.risk_manager`) 장부다 — attach 의 실제 상태는 **빈 집합**
    (pending 은 owner 가 들고 sidecar 는 비어 있다)이고, 그때 bot 장부의 행은 함수 뒷부분의
    고아 정리 블록으로 간다(재현 P2-1).
    """
    planted = datetime.now() - timedelta(minutes=stale_minutes)
    rm = _RiskManager(pending=set(rm_pending))
    bot = SimpleNamespace(
        broker=broker,
        engine=SimpleNamespace(
            risk_manager=rm,
            _execution_runtime=SimpleNamespace(marker='attach') if attached else None,
        ),
        exit_manager=_ExitManager(),
        running=False,
        _exit_pending_symbols={SYM},
        _exit_pending_timestamps={SYM: planted},
        _get_current_session=lambda: MarketSession.REGULAR,
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    return sched, bot


def ledgers_intact(bot, planted):
    return (bot._exit_pending_symbols == {SYM}
            and bot._exit_pending_timestamps == {SYM: planted})


# ── D1. attach 에서는 직접 호출도 아무것도 하지 않는다 (RED) ──────────────────

def test_d1_attached_cleanup_sends_no_cancel_and_keeps_the_pending_books():
    """attach 설치 상태에서 stale 행을 심고 직접 호출 → 취소·해제·단계 되감기 전부 0."""
    broker = Broker(cancelled=0)
    sched, bot = scheduler(attached=True, broker=broker)
    planted = dict(bot._exit_pending_timestamps)

    asyncio.run(sched._cleanup_stale_pending())

    assert broker.calls == []
    assert bot.engine.risk_manager.cleared == []
    assert bot.exit_manager.rolled_back == []
    assert ledgers_intact(bot, planted[SYM])


def test_d1b_attached_cleanup_skips_the_orphan_block_too():
    """attach 의 실제 상태(sidecar 장부 비어 있음)에서는 bot 장부의 행이 **고아** 로 분류돼
    함수 뒷부분의 고아 정리 블록(취소 POST + `rollback_stage` + 장부 삭제)으로 간다 —
    가드가 함수 맨 앞이 아니라 첫 stale 루프에만 걸리면 이 표본이 죽는다(재현 P2-1·x1).
    main 병합 때 가드를 main 판 본문 위에 다시 얹을 자리를 이 시험이 고정한다."""
    broker = Broker(cancelled=0)
    sched, bot = scheduler(attached=True, broker=broker, rm_pending=())
    planted = dict(bot._exit_pending_timestamps)

    asyncio.run(sched._cleanup_stale_pending())

    assert broker.calls == []
    assert bot.engine.risk_manager.cleared == []
    assert bot.exit_manager.rolled_back == []
    assert ledgers_intact(bot, planted[SYM])


# ── D2. 주기 호출자도 같은 가드 뒤에 있다 (커버 단언) ─────────────────────────

def test_d2_the_independent_60s_loop_is_covered_by_the_same_guard(monkeypatch):
    """`run_pending_cleanup` 의 한 주기가 실제로 `_cleanup_stale_pending` 에 닿고도 무해하다.

    가드가 함수 맨 앞이 아니라 `_check_exit_signal` 의 호출부(`:1043`)에만 있으면
    이 주기 경로가 그대로 취소를 낸다 — 그것을 잡는 단언이다.
    """
    broker = Broker(cancelled=0)
    sched, bot = scheduler(attached=True, broker=broker)
    planted = dict(bot._exit_pending_timestamps)

    entered = []
    real = sched._cleanup_stale_pending

    async def counted():
        entered.append(1)
        await real()

    sched._cleanup_stale_pending = counted

    slept = []

    async def _sleep(seconds):
        slept.append(seconds)
        if seconds >= 60:
            bot.running = False

    monkeypatch.setattr(kr_scheduler.asyncio, 'sleep', _sleep)
    bot.running = True
    asyncio.run(sched.run_pending_cleanup())

    assert slept == [30, 60]        # 기동 대기 1회 + 주기 1회 = 정확히 한 주기
    assert entered == [1]           # 그 주기가 대상 함수에 닿았다
    assert broker.calls == []
    assert bot.engine.risk_manager.cleared == []
    assert bot.exit_manager.rolled_back == []
    assert ledgers_intact(bot, planted[SYM])


# ── D3. 미설치(legacy) 경로 바이트 동일 대조 ─────────────────────────────────

def test_d3_legacy_releases_the_pending_even_when_the_cancel_matched_nothing():
    """`_execution_runtime` 이 None 이면 종전대로 — 취소 0 건을 최종성으로 읽는다.

    2026-08-04 P1 의 의도된 완화를 그대로 고정한다(legacy 특성화 파일의 같은 계약).
    """
    broker = Broker(cancelled=0)
    sched, bot = scheduler(attached=False, broker=broker)

    asyncio.run(sched._cleanup_stale_pending())

    assert broker.calls == [('cancel', SYM)]
    assert bot.engine.risk_manager.cleared == [SYM]
    assert bot.exit_manager.rolled_back == [SYM]
    assert bot._exit_pending_symbols == set()
    assert bot._exit_pending_timestamps == {}


def test_d3_legacy_keeps_the_pending_for_15_minutes_when_the_cancel_raises():
    """취소 API 예외는 원 주문 생존 가능 → 15 분까지 pending 유지, 단계 되감기 없음."""
    broker = Broker(cancelled=RuntimeError('KIS 취소 실패'))
    sched, bot = scheduler(attached=False, broker=broker, stale_minutes=10)
    planted = dict(bot._exit_pending_timestamps)

    asyncio.run(sched._cleanup_stale_pending())

    assert broker.calls == [('cancel', SYM)]
    assert bot.engine.risk_manager.cleared == []
    assert bot.exit_manager.rolled_back == []
    assert ledgers_intact(bot, planted[SYM])


def test_d3_legacy_force_releases_after_15_minutes_of_failed_cancels():
    """15 분 초과는 영구 교착 방지로 강제 해제한다 — 이것도 legacy 그대로다."""
    broker = Broker(cancelled=RuntimeError('KIS 취소 실패'))
    sched, bot = scheduler(attached=False, broker=broker, stale_minutes=20)

    asyncio.run(sched._cleanup_stale_pending())

    assert broker.calls == [('cancel', SYM)]
    assert bot.engine.risk_manager.cleared == [SYM]
    assert bot.exit_manager.rolled_back == [SYM]
    assert bot._exit_pending_symbols == set()


# ── D4. 가드로 사라지는 보호 신호가 없다는 구조 단언 (P0-3 교훈 점검) ─────────

def test_d4_the_cleanup_body_emits_no_signal():
    """본문에 `emit`/`Signal` 발행이 0 건이라 조기 return 이 보호 신호를 삼키지 않는다."""
    source = inspect.getsource(KRScheduler._cleanup_stale_pending)

    assert 'emit' not in source
    assert 'Signal' not in source


# ── D5. 설치 전제 특성화 — bot 수준 두 장부는 attach 에서도 읽힌다 ────────────

def _partial_missing_retry_calls(monkeypatch, *, exit_pending):
    """attach 에서 `_sync_portfolio` 를 한 번 돌리고 KIS 포지션 조회 횟수를 돌려준다.

    `partial_missing`(`kr_scheduler.py:1284`)은 attach 조기 return **앞**에 있어 attach 에서도
    읽힌다. 봇 보유 두 종목 중 하나만 KIS 응답에서 빠지고 주식평가액이 양수면 재조회 방어가
    도는데(전체 빈 응답 방어와 섞이지 않도록 다른 한 종목은 응답에 둔다), 빠진 종목이
    `_exit_pending_symbols` 에 있으면 차집합에서 빠져 방어가 억제된다.
    """
    present = {OTHER: _pos(OTHER)}
    sched, bot, _sleeps = _make(
        monkeypatch,
        bot_positions=[_pos(SYM), _pos(OTHER)],
        balance={'stock_value': '210000', 'cash': '100000'},
        kis_seq=[dict(present), dict(present)],
    )
    bot.engine._execution_runtime = SimpleNamespace(marker='attach')
    bot._exit_pending_symbols.update(exit_pending)
    asyncio.run(sched._sync_portfolio())
    return bot.broker.get_positions_calls


def test_d5_empty_pending_books_leave_the_partial_missing_retry_defense_intact(monkeypatch):
    """두 장부가 비어 있으면(=P0-3 뒤 attach 의 정상 상태) 재조회 방어가 그대로 돈다."""
    assert _partial_missing_retry_calls(monkeypatch, exit_pending=()) == 2


def test_d5_a_stale_pending_row_suppresses_the_retry_defense_for_that_symbol(monkeypatch):
    """장부에 행이 남아 있으면 그 종목이 `partial_missing` 에서 빠져 재조회가 사라진다.

    설치기는 bot 을 보지 못한다 — 그래서 이것은 **설치 호출자 계약**이다
    (`install_attached_runtime` docstring, 차단 사유 표의 설치 전제).
    """
    assert _partial_missing_retry_calls(monkeypatch, exit_pending=(SYM,)) == 1

"""KRScheduler._sync_portfolio 특성화(characterization) 테스트

대상: src/schedulers/kr_scheduler.py::KRScheduler._sync_portfolio
      (30초마다 KIS 잔고/포지션과 봇 포트폴리오를 대조하는 루프 —
       유령 포지션·재시작 연쇄 매도·저널 유실 사고의 진원지)

이 파일은 "현재 코드가 실제로 하는 일"을 고정한다. 동작을 바꾸면 여기가 깨져야 한다.
2026-09-13: 특성화로 드러난 4건(exit_exempt 즉시 삭제·부분 누락 미재시도·등록 실패 전파·잔고 실패 후 TR 낭비) 수정 후 기대치 갱신.
2026-09-14: 리뷰 F3(전부 매도 pending일 때 빈 응답 방어 우회)·F6(등록 재시도가 _sync 폴백 상실) 회귀 테스트 추가 —
            run_fill_check() 재시도 경로까지 실제 구동(sleep 패치로 1회 반복 후 탈출).

실행: venv/bin/python -m pytest tests/test_sync_portfolio_characterization.py -q
프로덕션 캐시·네트워크 무접촉 — 브로커/ExitManager/RiskManager는 전부 기록용 가짜.
(가짜 ExitManager는 파일 영속화를 하지 않으므로 Path.home() 패치가 필요 없다.)
"""

import asyncio
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.types import Portfolio, Position  # noqa: E402
from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402


# ── 가짜 협력자 ────────────────────────────────────────────────────────────────

class _Broker:
    """get_positions()는 호출마다 positions_seq를 하나씩 소비 (재시도 경로 검증용)."""

    def __init__(self, balance, positions_seq):
        self.balance = balance
        self.positions_seq = list(positions_seq)
        self.get_positions_calls = 0
        self.fills_seq = []   # run_fill_check용: check_fills() 호출마다 하나씩 소비

    async def get_open_orders(self):
        return ["open"] if self.fills_seq else []

    async def check_fills(self):
        return self.fills_seq.pop(0) if self.fills_seq else []

    async def get_account_balance(self):
        return self.balance

    async def get_positions(self):
        self.get_positions_calls += 1
        return self.positions_seq.pop(0) if self.positions_seq else {}


class _ExitManager:
    def __init__(self, exempt=()):
        self._exit_exempt = set(exempt)
        self.registered = []   # (position, kwargs)
        self.removed = []

    def register_position(self, position, **kwargs):
        self.registered.append((position, kwargs))

    def remove_position(self, symbol):
        self.removed.append(symbol)
        return True

    def is_exit_exempt(self, symbol):
        return symbol in self._exit_exempt


class _RiskManager:
    def __init__(self):
        self.sync_status = []
        self.buy_filled = []

    def set_sync_status(self, healthy):
        self.sync_status.append(healthy)

    def on_buy_filled(self, symbol):
        self.buy_filled.append(symbol)


def _pos(symbol, qty=10, avg="10000", cur="10500", strategy=None, name=""):
    return Position(
        symbol=symbol, name=name or symbol, quantity=qty,
        avg_price=Decimal(avg), current_price=Decimal(cur), strategy=strategy,
    )


def _make(monkeypatch, *, bot_positions, balance, kis_seq, cash="100000",
          exempt=(), symbol_strategy=None, exit_params=None):
    """_sync_portfolio가 만지는 속성만 가진 최소 bot + KRScheduler(초기화 생략)."""
    sleeps = []

    async def _sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)
    monkeypatch.setattr(kr_scheduler, "trading_logger",
                        SimpleNamespace(log_portfolio_sync=lambda **kw: None))

    async def _emit(event):
        pass

    portfolio = Portfolio(cash=Decimal(cash),
                          positions={p.symbol: p for p in bot_positions})
    bot = SimpleNamespace(
        broker=_Broker(balance, kis_seq),
        engine=SimpleNamespace(
            portfolio=portfolio,
            risk_manager=SimpleNamespace(_zombie_candidate_symbols=set(),
                                         _kis_qty_mismatch_count={}),
            emit=_emit,
        ),
        running=False,
        trade_journal=None,
        ws_feed=None,
        _exit_reasons={},
        exit_manager=_ExitManager(exempt),
        risk_manager=_RiskManager(),
        _portfolio_lock=asyncio.Lock(),
        _exit_pending_symbols=set(),
        _exit_pending_timestamps={},
        _sell_blocked_symbols={},
        _watch_symbols=[],
        _symbol_strategy=dict(symbol_strategy or {}),
        _strategy_exit_params=exit_params if exit_params is not None else {"_sync": {}},
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    sched._pending_exit_registrations = set()
    sched._pending_exit_registration_misses = {}
    sched._exempt_missing_count = {}
    return sched, bot, sleeps


def _run(sched):
    asyncio.run(sched._sync_portfolio())


_SYNC_PARAMS = {"stop_loss_pct": 3.0, "trailing_stop_pct": 2.0, "first_exit_pct": 3.0,
                "stale_high_days": 2}   # run_trader "_sync" 보수 설정과 동일 값


def _fill_check_once(monkeypatch, sched, sleeps):
    """run_fill_check()를 실제로 1회 반복시키고 탈출 — 주기 sleep(≥1초)에서 running=False."""
    bot = sched.bot

    async def _sleep(sec):
        sleeps.append(sec)
        if sec >= 1:
            bot.running = False

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)
    bot.running = True
    asyncio.run(sched.run_fill_check())


def _fail_n_times(exit_manager, n):
    """register_position 을 처음 n회만 실패시키고 이후는 정상 기록."""
    calls = {"n": 0}
    real = exit_manager.register_position

    def _reg(position, **kw):
        calls["n"] += 1
        if calls["n"] <= n:
            raise RuntimeError(f"register 실패 #{calls['n']}")
        real(position, **kw)

    exit_manager.register_position = _reg
    return calls


def _buy_fill(symbol, qty=10, price="10000"):
    from src.core.types import Fill, OrderSide
    return Fill(order_id=f"o-{symbol}", symbol=symbol, side=OrderSide.BUY,
                quantity=qty, price=Decimal(price))


# ── 1. KIS 에만 있는 포지션 → sync 등록 (전략/is_core 는 캐시에서 복원) ─────────

def test_kis_only_position_is_registered_with_cached_strategy(monkeypatch):
    kis = {
        "005930": _pos("005930", qty=5, avg="70000", cur="71000"),   # 전략 캐시 있음
        "000660": _pos("000660", qty=3, avg="150000", cur="149000"),  # 캐시 없음 → _sync 폴백
    }
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[],
        balance={"stock_value": 802_000, "available_cash": 100_000},
        kis_seq=[kis],
        symbol_strategy={"005930": "core_holding"},
        exit_params={"core_holding": {"is_core": True, "stop_loss_pct": 10.0},
                     "_sync": {"stop_loss_pct": 5.0}},
    )
    _run(sched)

    pf = bot.engine.portfolio
    assert set(pf.positions) == {"005930", "000660"}
    assert pf.positions["005930"].strategy == "core_holding"
    assert pf.positions["000660"].strategy is None

    reg = {p.symbol: kw for p, kw in bot.exit_manager.registered}
    assert reg["005930"]["is_core"] is True and reg["005930"]["stop_loss_pct"] == 10.0
    assert reg["000660"]["is_core"] is False and reg["000660"]["stop_loss_pct"] == 5.0
    assert sorted(bot._watch_symbols) == ["000660", "005930"]
    assert sorted(bot.risk_manager.buy_filled) == ["000660", "005930"]
    assert bot.risk_manager.sync_status == [True]
    assert bot.exit_manager.removed == [] and sleeps == []


# ── 2. 봇에만 있고 KIS 0건 + 평가액 > 0 → API 오류 간주: 5초 재시도 후 건너뜀 ────

def test_empty_kis_with_stock_value_is_retried_then_skipped(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930")],
        balance={"stock_value": 105_000, "available_cash": 50_000},
        kis_seq=[{}, {}],   # 1차·재시도 모두 0건
    )
    _run(sched)

    assert sleeps == [5]
    assert bot.broker.get_positions_calls == 2
    assert "005930" in bot.engine.portfolio.positions      # 제거 없음
    assert bot.exit_manager.removed == []
    assert bot.risk_manager.sync_status == [False]
    assert bot.engine.portfolio.cash == Decimal("100000")  # 현금 동기화도 건너뜀


# ── 3. 봇에만 있고 KIS 0건 + 평가액 == 0 → 진짜 빈 계좌: 유령 제거 (2026-09-03) ──

def test_empty_kis_with_zero_stock_value_removes_ghost(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930"), _pos("000660")],
        balance={"stock_value": 0, "available_cash": 300_000},
        kis_seq=[{}],
    )
    # 000660 은 매도 pending 1분 → 유령 판정 보류돼야 함
    bot._exit_pending_symbols.add("000660")
    bot._exit_pending_timestamps["000660"] = datetime.now() - timedelta(seconds=60)
    bot._sell_blocked_symbols["005930"] = "x"
    bot.engine.risk_manager._zombie_candidate_symbols.add("005930")
    bot.engine.risk_manager._kis_qty_mismatch_count["005930"] = 3
    _run(sched)

    assert sleeps == [] and bot.broker.get_positions_calls == 1
    assert set(bot.engine.portfolio.positions) == {"000660"}
    assert bot.exit_manager.removed == ["005930"]
    assert "005930" not in bot._sell_blocked_symbols
    assert bot.engine.risk_manager._zombie_candidate_symbols == set()
    assert bot.engine.risk_manager._kis_qty_mismatch_count == {}
    assert bot.risk_manager.sync_status == [True]
    assert bot.engine.portfolio.cash == Decimal("300000")


# ── 4. 수량/평단가 불일치 → KIS 값으로 덮어씀 ──────────────────────────────────

def test_quantity_and_avg_price_are_overwritten_from_kis(monkeypatch):
    sched, bot, _ = _make(
        monkeypatch,
        bot_positions=[_pos("005930", qty=10, avg="10000", cur="10500"),
                       _pos("000660", qty=2, avg="9000", cur="9100")],
        balance={"stock_value": 1_000_000, "available_cash": 250_000},
        kis_seq=[{
            "005930": _pos("005930", qty=7, avg="10200", cur="10900"),
            "000660": _pos("000660", qty=2, avg="0", cur="0"),   # 0 값은 무시돼야 함
        }],
    )
    _run(sched)

    p = bot.engine.portfolio.positions["005930"]
    assert (p.quantity, p.avg_price, p.current_price) == (7, Decimal("10200"), Decimal("10900"))
    q = bot.engine.portfolio.positions["000660"]
    assert (q.quantity, q.avg_price, q.current_price) == (2, Decimal("9000"), Decimal("9100"))
    assert bot.engine.portfolio.cash == Decimal("250000")
    assert bot.exit_manager.registered == [] and bot.exit_manager.removed == []
    assert bot.risk_manager.sync_status == [True]


# ── 5. 잔고 조회 실패({}) → 동기화 건너뜀 ───────────────────────────────────────

def test_empty_balance_skips_sync(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930")],
        balance={}, kis_seq=[{}],
    )
    _run(sched)

    assert bot.risk_manager.sync_status == [False]
    assert "005930" in bot.engine.portfolio.positions
    assert bot.exit_manager.removed == [] and sleeps == []
    # 잔고 실패면 get_positions 호출 전에 반환 (원장 TR 낭비 방지, 2026-09-13)
    assert bot.broker.get_positions_calls == 0


# ── 6. ExitManager 등록 실패 → _pending_exit_registrations 대기열, 나머지 동기화 계속 ──

def test_register_failure_is_queued_and_sync_continues(monkeypatch):
    sched, bot, _ = _make(
        monkeypatch,
        bot_positions=[_pos("000660", qty=2, avg="9000", cur="9100")],
        balance={"stock_value": 500_000, "available_cash": 250_000},
        kis_seq=[{
            "005930": _pos("005930", qty=5, avg="70000"),          # 신규 → 등록 중 예외
            "000660": _pos("000660", qty=9, avg="9500", cur="9700"),  # 수량 불일치 (갱신 안 돼야)
        }],
    )

    def _boom(position, **kw):
        raise RuntimeError("register 실패")

    bot.exit_manager.register_position = _boom
    _run(sched)

    # 등록 실패는 격리: 포지션은 유지 + 재시도 대기열, 나머지 동기화(수량·현금)는 정상 진행 (2026-09-13)
    assert bot.risk_manager.sync_status == [True]
    assert sched._pending_exit_registrations == {"005930"}
    assert "005930" in bot.engine.portfolio.positions
    q = bot.engine.portfolio.positions["000660"]
    assert (q.quantity, q.avg_price, q.current_price) == (9, Decimal("9500"), Decimal("9700"))
    assert bot.engine.portfolio.cash == Decimal("250000")
    assert not bot._portfolio_lock.locked()


# ── 7. 부분 누락 응답 → 1회 재시도, exit_exempt 종목은 3주기 연속 누락 전엔 제거 금지 ────

def test_partial_missing_is_retried_and_recovered(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch,
        bot_positions=[_pos("005930"), _pos("000660")],
        balance={"stock_value": 210_000, "available_cash": 78_000},
        kis_seq=[{"005930": _pos("005930")},                          # 000660 빠진 부분 응답
                 {"005930": _pos("005930"), "000660": _pos("000660")}],  # 재시도에서 복구
    )
    _run(sched)

    assert sleeps == [5] and bot.broker.get_positions_calls == 2
    assert set(bot.engine.portfolio.positions) == {"005930", "000660"}
    assert bot.exit_manager.removed == []
    assert bot.risk_manager.sync_status == [True]


def test_partial_missing_pending_sell_symbol_does_not_trigger_retry(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch,
        bot_positions=[_pos("005930"), _pos("000660")],
        balance={"stock_value": 105_000, "available_cash": 78_000},
        kis_seq=[{"005930": _pos("005930")}],
    )
    bot._exit_pending_symbols.add("000660")          # 매도 주문 중 → 정상 누락
    bot._exit_pending_timestamps["000660"] = datetime.now()
    _run(sched)

    assert sleeps == [] and bot.broker.get_positions_calls == 1
    assert "000660" in bot.engine.portfolio.positions  # pending 보류 (기존 동작)


def test_partial_missing_persisting_removes_non_exempt_ghost(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch,
        bot_positions=[_pos("005930"), _pos("000660")],
        balance={"stock_value": 105_000, "available_cash": 78_000},
        kis_seq=[{"005930": _pos("005930")}, {"005930": _pos("005930")}],
    )
    _run(sched)

    assert sleeps == [5] and bot.broker.get_positions_calls == 2
    assert set(bot.engine.portfolio.positions) == {"005930"}   # 재시도 후에도 없으면 진짜 유령
    assert bot.exit_manager.removed == ["000660"]


def test_exit_exempt_symbol_survives_two_missing_cycles_then_removed_on_third(monkeypatch):
    def _cycle(sched, bot):
        bot.broker.positions_seq = [{"005930": _pos("005930")}, {"005930": _pos("005930")}]
        _run(sched)

    sched, bot, sleeps = _make(
        monkeypatch,
        bot_positions=[_pos("087010", qty=120, avg="90000", strategy="manual"),
                       _pos("005930")],
        balance={"stock_value": 10_905_000, "available_cash": 78_000},
        kis_seq=[],
        exempt={"087010"},
    )
    _cycle(sched, bot)
    assert "087010" in bot.engine.portfolio.positions and bot.exit_manager.removed == []
    assert sched._exempt_missing_count == {"087010": 1}
    _cycle(sched, bot)
    assert "087010" in bot.engine.portfolio.positions and bot.exit_manager.removed == []
    assert sched._exempt_missing_count == {"087010": 2}
    _cycle(sched, bot)                                   # 3주기 연속 누락 → 실제 부재로 판정
    assert "087010" not in bot.engine.portfolio.positions
    assert bot.exit_manager.removed == ["087010"]
    assert sched._exempt_missing_count == {}
    assert sleeps == [5, 5, 5]


def test_exit_exempt_missing_counter_resets_when_symbol_reappears(monkeypatch):
    sched, bot, _ = _make(
        monkeypatch,
        bot_positions=[_pos("087010", qty=120, avg="90000", strategy="manual"),
                       _pos("005930")],
        balance={"stock_value": 10_905_000, "available_cash": 78_000},
        kis_seq=[],
        exempt={"087010"},
    )
    bot.broker.positions_seq = [{"005930": _pos("005930")}, {"005930": _pos("005930")}]
    _run(sched)
    assert sched._exempt_missing_count == {"087010": 1}
    bot.broker.positions_seq = [{"005930": _pos("005930"), "087010": _pos("087010", qty=120)}]
    _run(sched)
    assert sched._exempt_missing_count == {}
    assert "087010" in bot.engine.portfolio.positions and bot.exit_manager.removed == []


# ── 8. F3: 봇 보유 전부가 매도 pending + KIS 0건 + 평가액 > 0 → 빈 응답 방어(재시도)는 우회되면 안 됨 ──

def test_empty_reply_with_stale_sell_pending_is_retried(monkeypatch):
    p = _pos("005930")
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[p],
        balance={"stock_value": 105000, "available_cash": 100000},
        kis_seq=[{}, {"005930": p}],
    )
    bot._exit_pending_symbols.add("005930")
    bot._exit_pending_timestamps["005930"] = datetime.now() - timedelta(minutes=31)
    _run(sched)
    assert bot.broker.get_positions_calls == 2
    assert sleeps == [5]
    assert "005930" in bot.engine.portfolio.positions
    assert bot.exit_manager.removed == []


def test_empty_reply_persisting_with_stale_pending_and_zombie_keeps_state(monkeypatch):
    """재시도에도 평가액>0·0건이면 동기화 실패 기록 + 상태 보존 — pending 31분·좀비 후보도 방어를 못 넘는다."""
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930"), _pos("000660")],
        balance={"stock_value": 210_000, "available_cash": 50_000},
        kis_seq=[{}, {}],
    )
    for s in ("005930", "000660"):
        bot._exit_pending_symbols.add(s)
        bot._exit_pending_timestamps[s] = datetime.now() - timedelta(minutes=31)
    bot.engine.risk_manager._zombie_candidate_symbols.add("000660")
    _run(sched)

    assert sleeps == [5] and bot.broker.get_positions_calls == 2
    assert set(bot.engine.portfolio.positions) == {"005930", "000660"}
    assert bot.exit_manager.removed == []
    assert bot.risk_manager.sync_status == [False]
    assert bot.engine.portfolio.cash == Decimal("100000")   # 현금 동기화도 보류
    assert bot._exit_pending_symbols == {"005930", "000660"}  # pending 상태 보존


# ── 9. F6: 등록 파라미터 조회 단일화 — strategy → _sync → {} (복사본) ─────────────

def test_resolve_registration_params_priority_and_copy(monkeypatch):
    sched, bot, _ = _make(
        monkeypatch, bot_positions=[], balance={"stock_value": 0}, kis_seq=[],
        exit_params={"sepa_trend": {"stop_loss_pct": 5.0}, "_sync": dict(_SYNC_PARAMS)},
    )
    assert sched._resolve_registration_params("sepa_trend") == {"stop_loss_pct": 5.0}
    assert sched._resolve_registration_params("unknown_strategy") == _SYNC_PARAMS
    assert sched._resolve_registration_params(None) == _SYNC_PARAMS
    got = sched._resolve_registration_params(None)
    got["stop_loss_pct"] = 99.0
    assert bot._strategy_exit_params["_sync"]["stop_loss_pct"] == 3.0   # 복사본이라 원본 불변

    bot._strategy_exit_params = {}
    assert sched._resolve_registration_params("sepa_trend") == {}
    assert sched._resolve_registration_params(None) == {}


def test_sync_registration_failure_is_retried_by_fill_check_with_sync_params(monkeypatch):
    """전략 불명 sync 포지션: 최초 등록 1회 실패 → run_fill_check 재시도 — 양쪽 다 _sync 설정."""
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[],
        balance={"stock_value": 105_000, "available_cash": 100_000},
        kis_seq=[{"005930": _pos("005930")}],
        exit_params={"sepa_trend": {"stop_loss_pct": 5.0}, "_sync": dict(_SYNC_PARAMS)},
    )
    calls = _fail_n_times(bot.exit_manager, 1)
    _run(sched)
    assert sched._pending_exit_registrations == {"005930"}
    assert bot.exit_manager.registered == []

    _fill_check_once(monkeypatch, sched, sleeps)

    assert calls["n"] == 2
    assert sched._pending_exit_registrations == set()
    (pos, kw), = bot.exit_manager.registered
    assert pos.symbol == "005930"
    assert (kw["stop_loss_pct"], kw["trailing_stop_pct"], kw["first_exit_pct"],
            kw["stale_high_days"]) == (3.0, 2.0, 3.0, 2)
    assert kw["is_core"] is False and kw["atr_pct_hint"] is None
    assert sleeps[-1] == 15   # 유휴 폴링 주기


def test_sync_registration_uses_strategy_params_on_both_attempts(monkeypatch):
    """전략이 알려진 sync 포지션은 최초·재시도 모두 전략 설정(_sync 아님)."""
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[],
        balance={"stock_value": 105_000, "available_cash": 100_000},
        kis_seq=[{"005930": _pos("005930")}],
        symbol_strategy={"005930": "sepa_trend"},
        exit_params={"sepa_trend": {"stop_loss_pct": 5.0, "atr_pct": 1.5},
                     "_sync": dict(_SYNC_PARAMS)},
    )
    _fail_n_times(bot.exit_manager, 1)
    _run(sched)
    _fill_check_once(monkeypatch, sched, sleeps)

    (pos, kw), = bot.exit_manager.registered
    assert pos.strategy == "sepa_trend"
    assert kw["stop_loss_pct"] == 5.0 and kw["trailing_stop_pct"] is None
    assert kw["atr_pct_hint"] is None   # 재시도 ATR hint 는 시그널 캐시에서만 (기존 동작 유지)
    assert sched._pending_exit_registrations == set()


# ── 10. fill_check 대기열: BUY 등록 예외·포지션 지연·반복 실패·삭제된 포지션 ─────────

def test_buy_fill_registration_exception_is_queued_then_retried(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930", strategy="sepa_trend")],
        balance={}, kis_seq=[],
        exit_params={"sepa_trend": {"stop_loss_pct": 5.0}, "_sync": dict(_SYNC_PARAMS)},
    )
    bot.broker.fills_seq = [[_buy_fill("005930")]]
    calls = _fail_n_times(bot.exit_manager, 2)   # BUY 등록 + 다음 주기 1차 재시도 모두 실패
    _fill_check_once(monkeypatch, sched, sleeps)

    assert calls["n"] == 1                                   # 같은 주기엔 재시도하지 않음
    assert sched._pending_exit_registrations == {"005930"}   # 실패 중에는 대기열 유지
    assert bot.exit_manager.registered == []
    assert bot.risk_manager.buy_filled == ["005930"]
    assert sleeps[-1] == 2   # 미체결 있음 → 2초 폴링

    _fill_check_once(monkeypatch, sched, sleeps)             # 1차 재시도 실패 → 대기열 유지
    assert calls["n"] == 2 and sched._pending_exit_registrations == {"005930"}

    _fill_check_once(monkeypatch, sched, sleeps)             # 2차 재시도 성공
    assert calls["n"] == 3
    assert sched._pending_exit_registrations == set()
    (pos, kw), = bot.exit_manager.registered
    assert pos.symbol == "005930" and kw["stop_loss_pct"] == 5.0


def test_buy_fill_with_delayed_position_is_registered_when_position_appears(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[], balance={}, kis_seq=[],
        exit_params={"_sync": dict(_SYNC_PARAMS)},
    )
    bot.broker.fills_seq = [[_buy_fill("005930")]]
    _fill_check_once(monkeypatch, sched, sleeps)

    assert sleeps.count(0.1) == 10                     # 포지션 생성 최대 1초 대기
    assert sched._pending_exit_registrations == {"005930"}
    assert bot.exit_manager.registered == []

    bot.engine.portfolio.positions["005930"] = _pos("005930")   # 엔진이 뒤늦게 포지션 생성 (전략 불명)
    _fill_check_once(monkeypatch, sched, sleeps)
    assert sched._pending_exit_registrations == set()
    (pos, kw), = bot.exit_manager.registered
    assert pos.symbol == "005930" and kw["stop_loss_pct"] == 3.0   # 전략 불명 → _sync 폴백


def test_retry_keeps_failing_symbol_and_drops_deleted_position(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930")], balance={}, kis_seq=[],
    )
    sched._pending_exit_registrations = {"005930", "000660"}   # 000660 은 이미 삭제된 포지션

    def _boom(position, **kw):
        raise RuntimeError("register 실패")

    bot.exit_manager.register_position = _boom
    _fill_check_once(monkeypatch, sched, sleeps)
    assert sched._pending_exit_registrations == {"005930", "000660"}   # 부재 1주기: 아직 보류
    _fill_check_once(monkeypatch, sched, sleeps)              # 반복 실패해도 대기열 유지, 부재 2주기
    assert sched._pending_exit_registrations == {"005930", "000660"}
    _fill_check_once(monkeypatch, sched, sleeps)              # 부재 3주기 연속 → 삭제된 것으로 정리
    assert sched._pending_exit_registrations == {"005930"}
    assert sched._pending_exit_registration_misses == {}
    assert sleeps.count(15) == 3


def test_delayed_position_within_three_cycles_is_still_registered(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[], balance={}, kis_seq=[],
        exit_params={"_sync": {"stop_loss_pct": 3.0}},
    )
    sched._pending_exit_registrations = {"005930"}
    _fill_check_once(monkeypatch, sched, sleeps)
    _fill_check_once(monkeypatch, sched, sleeps)              # 2주기 부재 — 아직 대기열 유지
    assert sched._pending_exit_registrations == {"005930"}
    bot.engine.portfolio.positions["005930"] = _pos("005930")   # 엔진이 뒤늦게 포지션 생성
    _fill_check_once(monkeypatch, sched, sleeps)
    assert sched._pending_exit_registrations == set()
    assert sched._pending_exit_registration_misses == {}
    (pos, kw), = bot.exit_manager.registered
    assert pos.symbol == "005930" and kw["stop_loss_pct"] == 3.0

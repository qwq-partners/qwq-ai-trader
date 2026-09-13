"""KRScheduler._sync_portfolio 특성화(characterization) 테스트

대상: src/schedulers/kr_scheduler.py::KRScheduler._sync_portfolio
      (30초마다 KIS 잔고/포지션과 봇 포트폴리오를 대조하는 루프 —
       유령 포지션·재시작 연쇄 매도·저널 유실 사고의 진원지)

이 파일은 "현재 코드가 실제로 하는 일"을 고정한다. 동작을 바꾸면 여기가 깨져야 한다.

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

    portfolio = Portfolio(cash=Decimal(cash),
                          positions={p.symbol: p for p in bot_positions})
    bot = SimpleNamespace(
        broker=_Broker(balance, kis_seq),
        engine=SimpleNamespace(
            portfolio=portfolio,
            risk_manager=SimpleNamespace(_zombie_candidate_symbols=set(),
                                         _kis_qty_mismatch_count={}),
        ),
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
    return sched, bot, sleeps


def _run(sched):
    asyncio.run(sched._sync_portfolio())


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
    # 특성: 잔고 실패 판정 전에 get_positions 도 이미 호출됨 (원장 TR 1건 낭비)
    assert bot.broker.get_positions_calls == 1


# ── 6. 루프 도중 예외 → 바깥 except 흡수, 나머지 포지션은 손대지 않음 ─────────────

def test_exception_mid_loop_is_swallowed_without_touching_other_positions(monkeypatch):
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

    assert bot.risk_manager.sync_status == [False]
    q = bot.engine.portfolio.positions["000660"]
    assert (q.quantity, q.avg_price, q.current_price) == (2, Decimal("9000"), Decimal("9100"))
    assert bot.engine.portfolio.cash == Decimal("100000")
    assert not bot._portfolio_lock.locked()
    # 특성: 예외 직전에 이미 portfolio 에 들어간 신규 포지션은 그대로 남는다
    # (ExitManager 미등록 상태 — 다음 sync 에선 common 으로 분류돼 재등록되지 않음)
    assert "005930" in bot.engine.portfolio.positions


# ── 7. exit_exempt 종목이 KIS 응답에 빠져도 제거되면 안 된다 ──────────────────────

@pytest.mark.xfail(
    strict=True,
    reason="_sync_portfolio 는 is_exit_exempt 를 확인하지 않는다 — KIS 가 부분 응답"
           "(평가액>0, 다른 종목은 있음)을 돌려주면 exit_exempt 종목도 즉시 유령 제거되고 "
           "ExitManager.remove_position 으로 단계/영속 상태까지 지워진다 (재시도 없음).",
)
def test_exit_exempt_symbol_is_never_removed_when_missing_on_kis(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch,
        bot_positions=[_pos("087010", qty=120, avg="90000", strategy="manual"),
                       _pos("005930")],
        balance={"stock_value": 10_905_000, "available_cash": 78_000},
        kis_seq=[{"005930": _pos("005930")}],   # 087010 만 빠진 부분 응답
        exempt={"087010"},
    )
    _run(sched)

    assert sleeps == []                       # 0건이 아니므로 재시도 경로도 아님
    assert "087010" in bot.engine.portfolio.positions
    assert "087010" not in bot.exit_manager.removed

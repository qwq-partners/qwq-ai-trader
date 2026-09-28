"""실거래 초과수익 원장 — 스케줄러 배선 (설계 A §4·§7, 계획 T4·T5).

20:30 진화 블록 끝의 [초과수익] 단계와 토요일 한 줄을 실제 KRScheduler 코루틴으로 확인한다.
하네스는 tests/test_loop_heartbeat_integration.py 의 패턴(object.__new__ + 가짜 시계 + sleep 패치)을 따른다.
Path.home 은 tmp_path, 브로커·DB 는 가짜 — 운영 캐시·네트워크 무접촉.
"""

import asyncio
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics import excess_return as er  # noqa: E402
from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402
from src.utils import loop_heartbeat as hb  # noqa: E402

EVO_NOW = datetime(2026, 9, 15, 20, 30)   # 화요일, evolution_time 정각


class _FakeClockDatetime(datetime):
    _now = EVO_NOW

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(hb, "_beats", {})
    monkeypatch.setattr(hb, "_states", {})
    monkeypatch.setattr(hb, "DAILY_SCHEDULE", dict(hb.DAILY_SCHEDULE))
    sent = []

    async def _fake_send_alert(text, *a, **kw):
        sent.append(text)
        return True

    async def _fake_send_error_alert(self, *a, **kw):
        return None

    monkeypatch.setattr(kr_scheduler, "send_alert", _fake_send_alert)
    monkeypatch.setattr(KRScheduler, "_send_error_alert", _fake_send_error_alert)
    import src.utils.telegram as telegram_mod
    monkeypatch.setattr(telegram_mod, "get_telegram_notifier", lambda: None)
    return sent


# ── 20:30 블록: evolve 성공·예외·부재 세 경우 모두 단계가 한 번 돈다 ──────────────

def _run_evolution_block(monkeypatch, evolver):
    monkeypatch.setattr(kr_scheduler, "is_kr_market_holiday", lambda d: d.weekday() >= 5)
    import src.core.evolution.quality_validator as qv_mod
    import src.analytics.counterfactual_tracker as cf_mod

    class _FakeQV:
        def __init__(self, *a, **kw):
            pass

        async def run_daily_validation(self, **kw):
            return {}

    class _FakeCF:
        async def update(self, broker):
            return None

    monkeypatch.setattr(qv_mod, "QualityValidator", _FakeQV)
    monkeypatch.setattr(cf_mod, "get_counterfactual_tracker", lambda: _FakeCF())
    _FakeClockDatetime._now = EVO_NOW
    monkeypatch.setattr(kr_scheduler, "datetime", _FakeClockDatetime)

    calls = []

    async def _record_step(self, bot, now):
        calls.append(now)

    monkeypatch.setattr(KRScheduler, "_run_excess_return_step", _record_step)

    bot = SimpleNamespace(
        running=True,
        config=SimpleNamespace(get=lambda *a, **k: {"evolution_time": "20:30"}),
        engine=None, risk_manager=None, daily_reviewer=None, strategy_evolver=evolver,
    )

    async def _sleep(sec):
        bot.running = False

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched.run_evolution_scheduler())
    return calls


def test_step_runs_after_evolve_success(monkeypatch):
    async def _ok(days=7):
        return {"status": "keep"}
    calls = _run_evolution_block(monkeypatch, SimpleNamespace(evolve=_ok))
    assert calls == [EVO_NOW]
    assert hb.loop_status()["kr_evolution_scheduler"]["consecutive_failures"] == 0


def test_step_runs_after_evolve_exception(monkeypatch):
    async def _boom(days=7):
        raise RuntimeError("evolve 실패(가짜)")
    calls = _run_evolution_block(monkeypatch, SimpleNamespace(evolve=_boom))
    assert calls == [EVO_NOW]
    assert hb.loop_status()["kr_evolution_scheduler"]["consecutive_failures"] == 1


def test_step_runs_without_evolver(monkeypatch):
    calls = _run_evolution_block(monkeypatch, None)
    assert calls == [EVO_NOW]


# ── 단계 실행 ─────────────────────────────────────────────────────────────────

def _out_dir(tmp_path):
    return tmp_path / ".cache" / "ai_trader" / "excess_return"


def _fake_fetch():
    trade = {
        "id": "OK1", "symbol": "005930", "name": "", "entry_time": datetime(2026, 9, 1, 9, 5),
        "entry_price": 10000, "entry_quantity": 100, "entry_reason": "", "entry_strategy": "sepa_trend",
        "entry_signal_score": 0, "market_context": "{}", "exit_time": datetime(2026, 9, 3, 14, 0),
        "exit_price": 10500, "exit_quantity": 100, "exit_reason": "", "exit_type": "trailing",
        "pnl": 47000, "pnl_pct": 0, "holding_minutes": 0,
    }
    leg = {"trade_id": "OK1", "event_time": datetime(2026, 9, 3, 14, 0), "price": 10500,
           "quantity": 100, "exit_type": "trailing", "exit_reason": ""}

    async def fetch(sql, *args):
        return [trade] if "FROM trades" in sql else [leg]
    return fetch


def _bars():
    return [{"date": f"202609{d:02d}", "close": 100.0 + d} for d in (1, 2, 3, 4, 7, 8)]


def _bot(broker, db_available=True):
    tj = SimpleNamespace(pool=SimpleNamespace(fetch=_fake_fetch()), _db_available=db_available)
    return SimpleNamespace(trade_journal=tj, broker=broker)


def test_step_without_db_or_broker_touches_nothing(tmp_path):
    sched = object.__new__(KRScheduler)
    for bot in (SimpleNamespace(),
                SimpleNamespace(trade_journal=None, broker=object()),
                SimpleNamespace(trade_journal=SimpleNamespace(pool=None, _db_available=True), broker=object()),
                _bot(broker=None),
                _bot(broker=object(), db_available=False)):
        asyncio.run(sched._run_excess_return_step(bot, EVO_NOW))
    assert not (tmp_path / ".cache").exists()


def test_step_writes_ledger_files(tmp_path):
    broker = SimpleNamespace(get_daily_prices=AsyncMock(return_value=_bars()))
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._run_excess_return_step(_bot(broker), EVO_NOW))
    out = _out_dir(tmp_path)
    for name in ("kodex200_daily.csv", "positions.jsonl", "summary.json", "summary_history.jsonl"):
        assert (out / name).is_file(), name
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["windows"]["all"]["all"]["n"] == 1
    broker.get_daily_prices.assert_awaited_once()


def test_step_swallows_broker_exception(tmp_path):
    broker = SimpleNamespace(get_daily_prices=AsyncMock(side_effect=RuntimeError("KIS 장애(가짜)")))
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._run_excess_return_step(_bot(broker), EVO_NOW))   # 예외 전파 없음


def test_step_swallows_timeout(monkeypatch, tmp_path):
    async def _hang(**kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(er, "run_daily_update", _hang)
    monkeypatch.setattr(KRScheduler, "_EXCESS_RETURN_TIMEOUT_SEC", 0.05)
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._run_excess_return_step(_bot(object()), EVO_NOW))  # TimeoutError 삼킴
    assert not _out_dir(tmp_path).exists()


def test_step_swallows_exporter_load_failure(monkeypatch, tmp_path):
    def _broken(root):
        raise ImportError("exporter 로드 실패(가짜)")

    monkeypatch.setattr(er, "load_exporter", _broken)
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._run_excess_return_step(_bot(object()), EVO_NOW))


# ── 토요일 한 줄 ──────────────────────────────────────────────────────────────

def test_weekly_line_sent_html_escaped(monkeypatch, tmp_path, _isolate):
    out = _out_dir(tmp_path)
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps({"computed_date": "2026-09-10"}), encoding="utf-8")
    monkeypatch.setattr(er, "format_weekly_line", lambda s: f"n<30 & {s['computed_date']}")
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._send_excess_return_weekly_line())
    assert _isolate == ["n&lt;30 &amp; 2026-09-10"]


def test_weekly_line_not_sent_without_summary(_isolate):
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._send_excess_return_weekly_line())
    assert _isolate == []


def test_weekly_line_swallows_broken_summary(tmp_path, _isolate):
    out = _out_dir(tmp_path)
    out.mkdir(parents=True)
    (out / "summary.json").write_text("{broken", encoding="utf-8")
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._send_excess_return_weekly_line())
    assert _isolate == []


def test_weekly_line_with_real_summary(tmp_path, _isolate):
    broker = SimpleNamespace(get_daily_prices=AsyncMock(return_value=_bars()))
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._run_excess_return_step(_bot(broker), EVO_NOW))
    asyncio.run(sched._send_excess_return_weekly_line())
    assert len(_isolate) == 1 and "09-15 계산" in _isolate[0] and "n=1" in _isolate[0]


# ── 배선 위치 (소스 구조) ─────────────────────────────────────────────────────

def test_wiring_positions_in_source():
    evo = inspect.getsource(KRScheduler.run_evolution_scheduler)
    step_line = next(ln for ln in evo.splitlines() if "_run_excess_return_step" in ln)
    # if/else(진화) 블록 밖·`if last_review_date != today:` 안 — 들여쓰기 24칸(메서드 원본 기준 20칸 + 4)
    indent = len(step_line) - len(step_line.lstrip())
    evo_if = next(ln for ln in evo.splitlines() if "if bot.strategy_evolver:" in ln)
    assert indent == len(evo_if) - len(evo_if.lstrip())
    assert evo.index("_run_excess_return_step") > evo.index("strategy_evolver.evolve(")
    assert evo.index("_run_excess_return_step") < evo.rindex("await asyncio.sleep(60)")

    weekly = inspect.getsource(KRScheduler.run_post_exit_review_scheduler)
    assert weekly.index("_send_excess_return_weekly_line") > weekly.index("[게이트분석] 실행 실패")
    assert weekly.index("_send_excess_return_weekly_line") < weekly.index("[후속복기] 실행 오류")

    step_src = inspect.getsource(KRScheduler._run_excess_return_step)
    assert "wait_for" in step_src and "except Exception" in step_src


# ── 2단계: 대사 배선 ─────────────────────────────────────────────────────────

def test_step_passes_execute_and_write_queue(monkeypatch, tmp_path):
    seen = {}

    async def _capture(**kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(er, "run_daily_update", _capture)

    async def _execute(sql, *args):
        return None

    queue = object()
    tj = SimpleNamespace(pool=SimpleNamespace(fetch=_fake_fetch(), execute=_execute),
                         _db_available=True, _write_queue=queue)
    sched = object.__new__(KRScheduler)
    asyncio.run(sched._run_excess_return_step(SimpleNamespace(trade_journal=tj, broker=object()), EVO_NOW))
    assert seen["execute"] is _execute and seen["write_queue"] is queue
    assert seen["fetch"] is tj.pool.fetch and seen["now"] == EVO_NOW
    assert KRScheduler._EXCESS_RETURN_TIMEOUT_SEC == 90

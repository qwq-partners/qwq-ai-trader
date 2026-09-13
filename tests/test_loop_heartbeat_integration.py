"""KRScheduler 실제 루프 × loop_heartbeat 통합 테스트 (2026-09-14 리뷰 F5 재현·수정 검증)

09-13 최초 하트비트는 "조회 전에 beat"(DART)·"전량 실패에도 beat"(REST)·"예외를 삼킨 뒤
beat"(진화 스케줄러) 패턴에 무방비였다 — 40분간 5회 전부 실패해도 정체로 안 잡혔다.
여기서는 합성 mock으로 **실제 스케줄러 코루틴**(run_dart_alert_scheduler/run_rest_price_feed/
run_evolution_scheduler)을 직접 구동해 loop_heartbeat 레지스트리가 실패를 실제로 드러내는지 검증한다
— `tests/test_sync_portfolio_characterization.py`의 object.__new__(KRScheduler) + sleep 패치 패턴 재사용.
async 테스트 구동은 `asyncio.run()`으로 직접 감싼다(이 저장소는 pytest-asyncio 마커를 쓰지 않는 관례).

실행: venv/bin/python -m pytest tests/test_loop_heartbeat_integration.py -q
프로덕션 캐시·네트워크 무접촉 — Path.home()을 tmp_path로 패치, DartChecker/QualityValidator/
counterfactual tracker는 전부 가짜로 교체한다.
"""

import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.types import MarketSession  # noqa: E402
from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402
from src.utils import loop_heartbeat as hb  # noqa: E402

TUE_10 = datetime(2026, 9, 15, 10, 0)   # 화요일 정규장


class _FakeClockDatetime(datetime):
    """kr_scheduler.datetime 을 이 클래스로 패치해 now()를 제어한다.
    나머지(.date()/.strftime()/연산)는 실제 datetime 그대로 상속받아 동작."""

    _now = TUE_10

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(hb, "_beats", {})
    monkeypatch.setattr(hb, "_started", TUE_10.timestamp())
    monkeypatch.setattr(hb, "_states", {})
    monkeypatch.setattr(hb, "DAILY_SCHEDULE", dict(hb.DAILY_SCHEDULE))
    _FakeClockDatetime._now = TUE_10
    # loop_heartbeat 내부의 time.time()을 가짜 시계에 연동 — 실제 벽시계와 혼용되면
    # 정체 판정 단언이 우연히 참이 되는 경우가 생긴다(리뷰 advisory (b)).
    monkeypatch.setattr(hb.time, "time", lambda: _FakeClockDatetime._now.timestamp())

    # 텔레그램 발송 경로를 명시적으로 무해화 — 현재 시나리오는 도달하지 않지만,
    # 실행 경로가 바뀌어도 실네트워크 호출 없이 결정적으로 동작하도록 고정한다
    # (리뷰 advisory (c)).
    async def _fake_send_alert(*a, **kw):
        return True

    async def _fake_send_error_alert(self, *a, **kw):
        return None

    monkeypatch.setattr(kr_scheduler, "send_alert", _fake_send_alert)
    monkeypatch.setattr(KRScheduler, "_send_error_alert", _fake_send_error_alert)

    import src.utils.telegram as telegram_mod
    monkeypatch.setattr(telegram_mod, "get_telegram_notifier", lambda: None)


def _weekday_holiday(d) -> bool:
    return d.weekday() >= 5


# ── T4 F5 재현: DART 조회 전 beat → 전량 실패해도 정체 아님, 수정 후 실패로 드러남 ──

def test_dart_scheduler_all_failures_over_40min_marks_failure_and_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "is_kr_market_holiday", _weekday_holiday)

    import src.signals.fundamentals.dart_checker as dart_checker_mod

    class _FakeDartChecker:
        def __init__(self):
            self._enabled = True
            self._corp_code_map = {"005930": "corp"}
            self.calls = 0

        async def ensure_corp_code_map(self):
            pass

        async def check_disclosures(self, symbol, days=1, use_cache=False):
            self.calls += 1
            raise RuntimeError("DART API 오류(가짜)")

    monkeypatch.setattr(dart_checker_mod, "DartChecker", _FakeDartChecker)

    _FakeClockDatetime._now = datetime(2026, 9, 15, 10, 0)
    monkeypatch.setattr(kr_scheduler, "datetime", _FakeClockDatetime)

    sleep_calls = []
    N = 5  # 10분 간격 5회 실패

    async def _sleep(sec):
        sleep_calls.append(sec)
        if len(sleep_calls) > N:
            raise asyncio.CancelledError()
        _FakeClockDatetime._now = _FakeClockDatetime._now + timedelta(minutes=10)

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)

    pos = SimpleNamespace(name="삼성전자", unrealized_pnl_pct=0)
    bot = SimpleNamespace(engine=SimpleNamespace(portfolio=SimpleNamespace(positions={"005930": pos})))
    sched = object.__new__(KRScheduler)
    sched.bot = bot

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sched.run_dart_alert_scheduler())

    status = hb.loop_status()["kr_dart_alert"]
    assert status["last_success"] is None
    assert status["consecutive_failures"] == N
    assert "전부 실패" in (status.get("failure_reason") or "")

    # 40분(5×10분) 경과 후에도 last_success 미갱신 → check()가 정체로 잡는다 (F5 재현·수정 확인)
    stale = hb.check(_FakeClockDatetime._now, is_holiday=_weekday_holiday)
    assert "kr_dart_alert" in stale


def test_dart_scheduler_no_holdings_is_idle_not_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "is_kr_market_holiday", _weekday_holiday)

    import src.signals.fundamentals.dart_checker as dart_checker_mod

    class _FakeDartChecker:
        def __init__(self):
            self._enabled = True
            self._corp_code_map = {"005930": "corp"}

        async def ensure_corp_code_map(self):
            pass

        async def check_disclosures(self, symbol, days=1, use_cache=False):
            raise AssertionError("보유 종목이 없으면 조회 자체가 없어야 한다")

    monkeypatch.setattr(dart_checker_mod, "DartChecker", _FakeDartChecker)

    _FakeClockDatetime._now = datetime(2026, 9, 15, 10, 0)
    monkeypatch.setattr(kr_scheduler, "datetime", _FakeClockDatetime)

    _idle_sleep_calls = []

    async def _sleep(sec):
        _idle_sleep_calls.append(sec)
        if len(_idle_sleep_calls) > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)

    bot = SimpleNamespace(engine=SimpleNamespace(portfolio=SimpleNamespace(positions={})))
    sched = object.__new__(KRScheduler)
    sched.bot = bot

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sched.run_dart_alert_scheduler())

    status = hb.loop_status()["kr_dart_alert"]
    assert status["idle_reason"] == "보유 종목 없음"
    assert status["consecutive_failures"] == 0
    # 유휴는 정체 기준(_beats)을 갱신하지만 last_success(실제 완수 시각)는 갱신하지 않는다
    assert status["last_success"] is None
    assert "kr_dart_alert" in hb._beats


# ── REST 피드: 대상 있음·성공 0 → 실패 / 대상 0(포지션 없음) → 유휴 ──────────────

async def _noop(*a, **kw):
    return None


def _make_rest_bot(monkeypatch, *, positions, get_quote):
    bot = SimpleNamespace(
        broker=SimpleNamespace(get_quote=get_quote),
        ws_feed=None,
        engine=SimpleNamespace(
            portfolio=SimpleNamespace(positions=positions),
            emit=_noop,
        ),
        exit_manager=None,
        running=True,
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    sched._composite_cache_date = date.today()   # pykrx 네트워크 호출 회피 (이미 갱신됨 취급)
    sched._ma5_cache = {}
    sched._prev_day_low = {}
    sched._get_current_session = lambda: MarketSession.REGULAR

    calls = {"n": 0}

    async def _sleep(sec):
        calls["n"] += 1
        if calls["n"] >= 2:   # 초기 대기(1) + 본 루프 1회 완주 후 트레일링 sleep(2) → 종료
            bot.running = False

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)
    return sched, bot


def test_rest_feed_all_quote_failures_is_failure_not_idle(monkeypatch):
    pos = SimpleNamespace()

    async def _fail_quote(symbol):
        raise RuntimeError("시세 조회 실패(가짜)")

    sched, bot = _make_rest_bot(monkeypatch, positions={"005930": pos}, get_quote=_fail_quote)

    asyncio.run(sched.run_rest_price_feed())

    status = hb.loop_status()["kr_rest_price_feed"]
    assert status["last_success"] is None
    assert status["consecutive_failures"] == 1
    assert "전부 실패" in (status.get("failure_reason") or "")


def test_rest_feed_post_classification_exception_does_not_overwrite_success(monkeypatch):
    """보유종목 분류가 성공으로 끝난 뒤, 같은 반복의 후속 블록(로그 등)에서 예외가 나도
    이미 확정한 성공 판정을 실패로 덮어쓰면 안 된다 — note만 남는다 (리뷰 advisory (e))."""
    pos = SimpleNamespace()

    async def _ok_quote(symbol):
        return {"price": 70000, "open": 69000, "high": 70500, "low": 68500,
                "volume": 1000, "change_pct": 1.0, "prev_close": 69300}

    sched, bot = _make_rest_bot(monkeypatch, positions={"005930": pos}, get_quote=_ok_quote)

    def _raising_info(msg, *a, **kw):
        if "WS백업" in msg:
            raise RuntimeError("로그 훅 오류(가짜, 분류 이후 블록 시뮬레이션)")

    monkeypatch.setattr(kr_scheduler.logger, "info", _raising_info)

    asyncio.run(sched.run_rest_price_feed())

    status = hb.loop_status()["kr_rest_price_feed"]
    assert status["consecutive_failures"] == 0
    assert status["last_success"] is not None
    assert "후속 블록 오류" in (status.get("note") or "")


def test_rest_feed_no_holding_targets_is_idle(monkeypatch):
    async def _unused_quote(symbol):
        raise AssertionError("대상 종목이 없으면 호출되지 않아야 한다")

    sched, bot = _make_rest_bot(monkeypatch, positions={}, get_quote=_unused_quote)

    asyncio.run(sched.run_rest_price_feed())

    status = hb.loop_status()["kr_rest_price_feed"]
    assert status["idle_reason"] == "보유종목 0건 또는 WS 전량 커버"
    assert status["consecutive_failures"] == 0
    # 유휴는 정체 기준(_beats)을 갱신하지만 last_success(실제 완수 시각)는 갱신하지 않는다
    assert status["last_success"] is None
    assert "kr_rest_price_feed" in hb._beats


# ── 진화 스케줄러: evolve() 예외를 삼킨 뒤 beat 하던 경로 제거 확인 ──────────────

def test_evolution_scheduler_evolve_exception_is_failure_not_swallowed_success(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "is_kr_market_holiday", _weekday_holiday)

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

    _FakeClockDatetime._now = datetime(2026, 9, 15, 20, 30)   # 화요일, evolution_time 정각
    monkeypatch.setattr(kr_scheduler, "datetime", _FakeClockDatetime)

    async def _evolve_raises(days=7):
        raise RuntimeError("evolve() 실행 실패(가짜)")

    bot = SimpleNamespace(
        running=True,
        config=SimpleNamespace(get=lambda *a, **k: {"evolution_time": "20:30"}),
        engine=None,
        risk_manager=None,
        daily_reviewer=None,
        strategy_evolver=SimpleNamespace(evolve=_evolve_raises),
    )

    async def _sleep(sec):
        bot.running = False   # 한 번의 20:30 블록 실행 직후 종료

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _sleep)

    sched = object.__new__(KRScheduler)
    sched.bot = bot

    asyncio.run(sched.run_evolution_scheduler())

    status = hb.loop_status()["kr_evolution_scheduler"]
    assert status["last_success"] is None
    assert status["consecutive_failures"] == 1
    assert "evolve() 실행 실패" in (status.get("failure_reason") or "")


# ── 재시작 전 완료된 날짜 상태 복원 (리뷰 advisory (a)) ─────────────────────
#
# 재시작 시점이 예정시각+grace 이내라도, 재시작 전에 오늘 일일 잡이 이미 끝났다면
# 정체로 오탐하면 안 된다 — 3개 스케줄러(진화·수확shadow·변동성타게팅) 모두
# 시작 시 오늘 날짜 상태 파일을 읽어 record_success(note="재시작 전 완료 복원")로
# 복원한다. 진화는 DAILY_SCHEDULE도 config의 evolution_time으로 동기화한다.

def test_restore_already_completed_today_state_all_three_daily_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    today_iso = date.today().isoformat()

    cache_dir = tmp_path / ".cache" / "ai_trader"
    cache_dir.mkdir(parents=True)
    (cache_dir / "evolution_state.json").write_text(
        json.dumps({"last_review_date": today_iso})
    )
    (cache_dir / "harvest_shadow").mkdir()
    (cache_dir / "harvest_shadow" / "last_run.json").write_text(
        json.dumps({"date": today_iso})
    )
    (cache_dir / "vol_targeting.json").write_text(
        json.dumps({"date": today_iso, "vol": 0.18, "multiplier": 1.0})
    )

    # 진화: bot.running=False로 while 진입 없이 상단 복원 코드만 실행
    evo_bot = SimpleNamespace(
        running=False,
        config=SimpleNamespace(get=lambda *a, **k: {"evolution_time": "21:05"}),
    )
    evo_sched = object.__new__(KRScheduler)
    evo_sched.bot = evo_bot
    asyncio.run(evo_sched.run_evolution_scheduler())

    evo_status = hb.loop_status()["kr_evolution_scheduler"]
    assert evo_status["note"] == "재시작 전 완료 복원"
    assert hb.DAILY_SCHEDULE["kr_evolution_scheduler"] == (21, 5)

    # 수확shadow / 변동성타게팅: while True 무조건 루프 — 상단 복원 코드 실행 직후
    # 첫 sleep(60)에서 CancelledError를 주입해 종료
    async def _cancel_sleep(sec):
        raise asyncio.CancelledError()

    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _cancel_sleep)

    harvest_sched = object.__new__(KRScheduler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(harvest_sched.run_harvest_shadow_scheduler())
    harvest_status = hb.loop_status()["kr_harvest_shadow"]
    assert harvest_status["note"] == "재시작 전 완료 복원"

    vol_sched = object.__new__(KRScheduler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(vol_sched.run_vol_targeting_scheduler())
    vol_status = hb.loop_status()["kr_vol_targeting"]
    assert vol_status["note"] == "재시작 전 완료 복원"

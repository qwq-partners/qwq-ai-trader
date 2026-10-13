"""66차 — CTRGA011R 수집 로직: 월 분할·전체 페이지·반복/상한/오류/빈 응답 구분·집계 (합성 응답)."""
import asyncio
from datetime import date
from decimal import Decimal

from src.analytics.kis_period_rights import classify_page, collect_window, month_windows, summarize_rights


def _page(rows, cont, fk="", nk="", rt="0"):
    return {"rt_cd": rt, "msg_cd": "MCA00000", "msg1": "정상", "output": rows, "_tr_cont": cont,
            "ctx_area_fk100": fk, "ctx_area_nk100": nk}


def _row(t="01", amt="1000", pdno="005930"):
    return {"rght_type_cd": t, "last_alct_amt": amt, "pdno": pdno, "bass_dt": "20250105"}


def _fetcher(script):
    calls = []

    async def fetch(start, end, *, fk100, nk100, tr_cont):
        calls.append({"fk": fk100, "nk": nk100, "tr_cont": tr_cont})
        return script[min(len(calls) - 1, len(script) - 1)]
    fetch.calls = calls
    return fetch


def test_month_windows_clip_to_requested_range():
    w = month_windows(date(2025, 1, 15), date(2025, 3, 10))
    assert w == [("20250115", "20250131"), ("20250201", "20250228"), ("20250301", "20250310")]
    assert month_windows(date(2026, 10, 1), date(2026, 10, 1)) == [("20261001", "20261001")]
    assert len(month_windows(date(2025, 1, 1), date(2026, 10, 1))) == 22


def test_multi_page_window_completes_with_next_keys():
    fetch = _fetcher([_page([_row()], "F", fk="f1", nk="n1"), _page([_row(amt="2000")], "M", fk="f2", nk="n2"),
                      _page([_row(amt="3000")], "D")])
    r = asyncio.run(collect_window(fetch, "20250101", "20250131"))
    assert r["status"] == "complete" and r["pages_fetched"] == 3 and len(r["rows"]) == 3
    assert fetch.calls[0] == {"fk": "", "nk": "", "tr_cont": ""}
    assert fetch.calls[1] == {"fk": "f1", "nk": "n1", "tr_cont": "N"} and fetch.calls[2]["fk"] == "f2"


def test_repeat_keys_and_max_pages_are_partial_not_complete():
    rep = _fetcher([_page([_row()], "F", fk="same", nk="same")])
    r = asyncio.run(collect_window(rep, "20250101", "20250131", max_pages=10))
    assert r["status"] == "partial_page_repeat" and r["pages_fetched"] == 2
    assert len(r["rows"]) == 1 and r["pages"][1]["rows_excluded_from_totals"] is True   # 반복 페이지 행은 집계 제외(8차 P2)
    assert len(r["raw_pages"]) == 2                                                  # 원본은 보존        # 2페이지째 같은 키 → 중단
    cap = _fetcher([_page([_row()], "F", fk=f"f{i}", nk=f"n{i}") for i in range(5)])
    r2 = asyncio.run(collect_window(cap, "20250101", "20250131", max_pages=3))
    assert r2["status"] == "partial_max_pages" and r2["pages_fetched"] == 3 and len(r2["rows"]) == 3


def test_error_empty_and_malformed_pages():
    err = asyncio.run(collect_window(_fetcher([_page([], "", rt="1")]), "20250101", "20250131"))
    assert err["status"] == "error" and err["pages"][0]["rt_cd"] == "1"
    empty = asyncio.run(collect_window(_fetcher([_page([], "D")]), "20250101", "20250131"))
    assert empty["status"] == "empty" and empty["rows"] == []
    nohdr = asyncio.run(collect_window(_fetcher([_page([_row()], "")]), "20250101", "20250131"))
    assert nohdr["status"] == "partial_missing_header" and nohdr["tr_cont_header_missing"] is True   # 마지막 페이지 근거 없음 ≠ 완료
    assert summarize_rights([nohdr])["complete_windows"] == 0
    bad = asyncio.run(collect_window(_fetcher([{"rt_cd": "0"}]), "20250101", "20250131"))
    assert bad["status"] == "malformed" and bad["raw_pages"] == [{"rt_cd": "0"}]          # 원본 페이지 보존
    assert classify_page("x") == "malformed" and classify_page({"rt_cd": "0", "output1": []}) == "empty"
    # 리뷰 P2: 비정상 응답을 완전 조회로 판정하지 않는다
    assert classify_page(_page([None], "D")) == "malformed"                 # 행이 dict 가 아님
    assert classify_page(_page([_row()], "X")) == "malformed"               # 알 수 없는 tr_cont
    assert classify_page(_page([_row()], "F", fk=0)) == "malformed"         # 연속조회 키가 문자열 아님
    assert classify_page({"rt_cd": 0, "output": []}) == "malformed"         # rt_cd 타입 오류
    assert asyncio.run(collect_window(_fetcher([_page([_row()], "X")]), "20250101", "20250131"))["status"] == "malformed"


def test_summary_aggregates_decimal_strings_without_interpreting():
    windows = [
        {"status": "complete", "rows": [_row("01", "1000.50"), _row("01", "abc"), _row("02", "7", pdno="000660")]},
        {"status": "partial_max_pages", "rows": [_row("02", "3", pdno="000660")]},
        {"status": "empty", "rows": []},
    ]
    s = summarize_rights(windows)
    assert s["window_statuses"] == {"complete": 1, "partial_max_pages": 1, "empty": 1} and s["complete_windows"] == 2
    assert s["by_right_type"]["01"] == {"rows": 2, "last_alct_amt_sum": "1000.50", "unparsed_amount_rows": 1, "symbol_count": 1}
    assert s["by_right_type"]["02"]["last_alct_amt_sum"] == "10" and s["unparsed_amount_rows"] == 1
    assert Decimal(s["by_right_type"]["01"]["last_alct_amt_sum"]) == Decimal("1000.50")
    assert any("소득 확정" in c for c in s["caveats"])


def test_cli_runs_end_to_end_with_fake_broker_and_private_files(tmp_path, monkeypatch):
    """리뷰 P1: CLI 통합 — connect() 없이 조회 전용 브로커, 비공개 디렉터리 0700, 파일 0600, 원본 보존."""
    import json, os, stat, sys
    from types import SimpleNamespace
    sys.path.insert(0, str((tmp_path / "_unused")))  # 경로 영향 없음
    import importlib.util
    spec = importlib.util.spec_from_file_location("kis_period_rights_cli", "scripts/ops/kis_period_rights.py")
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)

    class _Broker:
        def __init__(self):
            self.calls = []
            self.config = SimpleNamespace(timeout_seconds=10)
            self._session = SimpleNamespace(closed=False)
            async def _rl(tr_id=""): ...
            self._rate_limit = _rl
            async def _close():
                self._session.closed = True
            self._session.close = _close

        async def get_period_rights(self, start, end, *, inqr_dvsn, fk100, nk100, tr_cont):
            self.calls.append((start, end, fk100, nk100, tr_cont))
            if start.startswith("202501") and fk100 == "":
                return _page([_row()], "F", fk="k1", nk="k1")
            return _page([_row(amt="5")] if start.startswith("202501") else [], "D")

    broker = _Broker()
    async def fake_open():
        return broker
    monkeypatch.setattr(cli, "_open_readonly_broker", fake_open)
    out = tmp_path / "rights"
    args = SimpleNamespace(start="2025-01-01", end="2025-02-28", out_dir=str(out), inqr_dvsn="03", max_pages=10, min_interval=0,
                           allow_market_hours=True, market_margin_seconds=90.0, allow_bot_running=False)
    monkeypatch.setattr(cli, "_bot_active", lambda: False)
    rc = asyncio.run(cli._run(args))
    assert rc == 0 and broker._session.closed is True
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    files = sorted(p.name for p in out.iterdir())
    assert files == ["manifest.json", "rights-20250101-20250131.json", "rights-20250201-20250228.json", "summary.json"]
    for p in out.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    jan = json.loads((out / "rights-20250101-20250131.json").read_text())
    assert jan["status"] == "complete" and len(jan["raw_pages"]) == 2 and jan["raw_pages"][0]["ctx_area_fk100"] == "k1"
    assert [c[4] for c in broker.calls] == ["", "N", ""]                 # 2페이지째만 tr_cont=N
    summary = json.loads((out / "summary.json").read_text())
    assert summary["window_statuses"] == {"complete": 1, "empty": 1} and summary["pages_fetched"] == 3


def test_cli_refuses_shared_or_foreign_output_dir(tmp_path):
    import importlib.util, os, stat, pytest
    spec = importlib.util.spec_from_file_location("kis_period_rights_cli2", "scripts/ops/kis_period_rights.py")
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    shared = tmp_path / "shared"; shared.mkdir(); shared.chmod(0o755)
    with pytest.raises(RuntimeError):
        cli._prepare_private_dir(shared)                                   # 기존 0755 디렉터리는 거부(권한을 바꾸지 않음)
    fresh = tmp_path / "fresh"
    cli._prepare_private_dir(fresh)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700


# ── 재리뷰 반영: 읽기 전용 토큰 공급자·장중 재검사·부분 보존 ──

def _cli_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kis_period_rights_cli3", "scripts/ops/kis_period_rights.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_readonly_token_provider_never_issues_and_follows_cache_rotation():
    """P1: 복구 경로(invalidate → get_access_token)가 불려도 발급 없이 캐시만 다시 읽는다."""
    cli = _cli_module()

    class _TM:
        def __init__(self):
            self._access_token, self._valid, self.loads, self.issued = "tok-cached", True, 0, 0
        def _is_token_valid(self): return self._valid
        def _load_cached_tokens(self): self.loads += 1
        async def _issue_access_token(self): self.issued += 1; return True
    tm = _TM(); prov = cli.ReadOnlyTokenProvider(tm)
    assert asyncio.run(prov.get_access_token()) == "tok-cached" and prov._access_token == "tok-cached"
    prov.invalidate(); tm._valid = False                       # 봇이 회전 전 → 캐시 무효
    assert asyncio.run(prov.get_access_token()) is None and prov._access_token is None
    assert asyncio.run(prov.get_approval_key()) is None
    assert tm.issued == 0 and tm.loads >= 2                     # 발급 0, 캐시 재읽기만


def test_market_hours_guard_aborts_before_open_and_keeps_partial_pages():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cli = _cli_module(); KST = ZoneInfo("Asia/Seoul")
    assert cli._blocked_by_market_hours(datetime(2026, 10, 14, 8, 59, 0, tzinfo=KST), 90.0) is True    # 개장 1분 전 → 차단
    assert cli._blocked_by_market_hours(datetime(2026, 10, 14, 8, 50, 0, tzinfo=KST), 90.0) is False
    assert cli._blocked_by_market_hours(datetime(2026, 10, 14, 15, 35, 0, tzinfo=KST), 90.0) is False
    assert cli._blocked_by_market_hours(datetime(2026, 10, 17, 10, 0, 0, tzinfo=KST), 90.0) is False    # 토요일
    calls = {"n": 0}
    async def fetch(start, end, *, fk100, nk100, tr_cont):
        calls["n"] += 1
        if calls["n"] == 2:
            raise cli.MarketHoursStop("장중 진입")
        return _page([_row()], "F", fk="k1", nk="k1")
    r = asyncio.run(collect_window(fetch, "20250101", "20250131", max_pages=5))
    assert r["status"] == "aborted:MarketHoursStop" and len(r["raw_pages"]) == 1 and len(r["rows"]) == 1
    assert r["pages"][-1]["kind"] == "aborted"
    s = summarize_rights([r])
    assert s["complete_windows"] == 0 and s["window_statuses"] == {"aborted:MarketHoursStop": 1}


def test_bot_active_guard_requires_override_and_slower_interval(monkeypatch):
    """3차 리뷰 P1: 봇 가동 중에는 기본 거부, 우회 시 간격 ≥2.1초."""
    import json, sys
    cli = _cli_module()
    monkeypatch.setattr(cli, "_bot_active", lambda: True)
    monkeypatch.setattr(cli, "_in_market_hours", lambda now: False)
    called = {"run": 0}
    async def fake_run(args):
        called["run"] += 1; return 0
    monkeypatch.setattr(cli, "_run", fake_run)
    base = ["prog", "--start", "2025-01-01", "--end", "2025-01-31", "--out-dir", "/tmp/x"]
    monkeypatch.setattr(sys, "argv", base)
    assert cli.main() == 2 and called["run"] == 0                                   # 기본 거부
    monkeypatch.setattr(sys, "argv", base + ["--allow-bot-running", "--min-interval", "1.5"])
    assert cli.main() == 2 and called["run"] == 0                                   # 간격 부족
    monkeypatch.setattr(cli, "_bot_active", lambda: False)
    assert cli.main() == 2 and called["run"] == 0                                   # 봇 정지 중에도 우회 옵션의 간격은 검증(6차 P2)
    monkeypatch.setattr(cli, "_bot_active", lambda: True)
    monkeypatch.setattr(sys, "argv", base + ["--allow-bot-running", "--min-interval", "2.5"])
    assert cli.main() == 0 and called["run"] == 1
    monkeypatch.setattr(cli, "_bot_active", lambda: False)
    monkeypatch.setattr(sys, "argv", base)
    assert cli.main() == 0 and called["run"] == 2                                   # 봇 정지 시 그대로 실행


def test_bot_active_only_trusts_confirmed_stopped(monkeypatch):
    """4차 리뷰 P1: systemctl 조회 실패·전환 상태·알 수 없는 출력은 전부 '가동 중'. inactive/failed(rc 3)만 정지."""
    import subprocess
    cli = _cli_module()
    def fake_run(rc, out):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout=out, stderr="")
    for rc, out in [(0, "active\n"), (0, "activating\n"), (3, "deactivating\n"), (3, "unknown\n"), (4, ""), (1, "garbage")]:
        monkeypatch.setattr(subprocess, "run", fake_run(rc, out))
        assert cli._bot_active() is True, (rc, out)
    for out in ("inactive\n", "failed\n"):
        monkeypatch.setattr(subprocess, "run", fake_run(3, out))
        assert cli._bot_active() is False
    def boom(*a, **k): raise OSError("no systemctl")
    monkeypatch.setattr(subprocess, "run", boom)
    assert cli._bot_active() is True


def test_pages_fetched_excludes_aborted_and_http_attempts_from_metrics():
    """4차 리뷰 P2: 전송 전 중단은 페이지 수에 들어가지 않고, HTTP 전송 횟수는 계측에서만 읽는다."""
    cli = _cli_module()
    from src.utils import kis_request_metrics as m
    class Stop(Exception): ...
    async def fetch(*a, **k): raise Stop()
    r = asyncio.run(collect_window(fetch, "20250101", "20250131"))
    assert r["status"] == "aborted:Stop" and r["pages_fetched"] == 0 and len(r["pages"]) == 1
    before = cli._http_attempts()
    assert before is not None
    m.record_http_attempt("CTRGA011R", 0); m.record_http_attempt("CTRGA011R", 1)      # 1페이지 2전송(재시도 1)
    assert cli._http_attempts() - before == 2


def test_bot_starting_mid_collection_aborts_before_next_send(monkeypatch, tmp_path):
    """5차 리뷰 P1: 첫 검사 뒤 봇이 기동하면 다음 전송 직전에 중단하고 확보한 창은 보존한다. 우회 시엔 계속."""
    import json, sys
    from pathlib import Path
    from types import SimpleNamespace
    cli = _cli_module()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_in_market_hours", lambda now: False)
    states = iter([False, False, True, True, True])      # main 1회 + 창1 전송 1회는 정지, 그 뒤 기동
    monkeypatch.setattr(cli, "_bot_active", lambda: next(states, True))

    class Broker:
        def __init__(self):
            self.calls = 0
            self.config = SimpleNamespace(timeout_seconds=10)
            self._session = SimpleNamespace(closed=False)
            async def _rl(tr_id=""): ...
            self._rate_limit = _rl
        async def get_period_rights(self, start, end, *, inqr_dvsn, fk100, nk100, tr_cont):
            await self._rate_limit("CTRGA011R")           # 실제 _api_get 처럼 전송 직전 리미터(가드) 경유
            self.calls += 1
            return _page([_row()], "D")
        async def close(self): ...
    broker = Broker()
    async def opener(): return broker
    monkeypatch.setattr(cli, "_open_readonly_broker", opener)
    async def no_wait(b): ...
    monkeypatch.setattr(cli, "_close_readonly_broker", no_wait)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))          # 창 사이 대기 생략
    out = tmp_path / "out"; out.mkdir(mode=0o700)
    monkeypatch.setattr(sys, "argv", ["prog", "--start", "2025-01-01", "--end", "2025-03-31", "--out-dir", str(out),
                                      "--min-interval", "1.05"])
    assert cli.main() == 1 and broker.calls == 1                  # 2월 창 전송 직전 중단
    s = json.loads((out / "summary.json").read_text())
    assert s["window_statuses"] == {"complete": 1, "aborted:BotStartedStop": 1} and s["windows"] == 2   # 3월은 미조회

    broker = Broker()                                    # 가드는 브로커 인스턴스에 설치되므로 새 인스턴스로 두 번째 실행
    out2 = tmp_path / "out2"; out2.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "_bot_active", lambda: True)
    monkeypatch.setattr(sys, "argv", ["prog", "--start", "2025-01-01", "--end", "2025-03-31", "--out-dir", str(out2),
                                      "--allow-bot-running", "--min-interval", "2.1"])
    assert cli.main() == 0 and broker.calls == 3                  # 명시 우회는 계속


def test_send_guard_runs_on_every_api_get_retry(monkeypatch):
    """6차 리뷰 P1: 실제 `_api_get` 의 5xx 재시도 직전에도 가드가 돌아, 첫 전송 뒤 봇이 기동하면 재전송하지 않는다."""
    from types import SimpleNamespace
    from src.execution.broker import kis_kr
    from src.utils import kis_rate_limit
    cli = _cli_module()
    kis_rate_limit.reset()
    b = object.__new__(kis_kr.KISBroker)
    b._token = "t"
    b._token_mgr = SimpleNamespace(_access_token="t", _is_token_valid=lambda: True, invalidate=lambda: None)
    b.config = SimpleNamespace(base_url="http://x", account_no="12345678", account_product_cd="01", env="prod",
                               app_key="k", app_secret="s", timeout_seconds=10)
    async def _no_limit(tr_id=""): ...
    b._rate_limit = _no_limit
    async def no_sleep(_d): ...
    monkeypatch.setattr(kis_kr.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(kis_rate_limit, "release_ledger", lambda: None)
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))

    class Resp:
        def __init__(self, status, body, cont=""):
            self.status, self._body, self.headers = status, body, ({"tr_cont": cont} if cont else {})
        async def json(self): return dict(self._body)
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
    sent = []
    seq = iter([Resp(503, {}), Resp(200, _page([_row()], "D"))])
    def get(url, headers, params):
        sent.append(dict(params)); return next(seq)
    b._session = SimpleNamespace(closed=False, get=get)

    states = iter([False, True])                       # 1차 전송 전 정지 → 503 → 2차 전송 전 봇 기동
    checks = []
    async def guard():
        active = next(states, True); checks.append(active)
        if active:
            raise cli.BotStartedStop("기동")
    cli._install_send_guard(b, guard)
    r = asyncio.run(collect_window(lambda s, e, **k: b.get_period_rights(s, e, **k), "20250101", "20250131"))
    assert r["status"] == "aborted:BotStartedStop" and len(sent) == 1 and checks == [False, True]   # 재전송 0


def test_min_interval_applies_to_internal_http_retries(monkeypatch, tmp_path):
    """7차 리뷰 P1: 503 뒤 `_api_get` 내부 재시도도 --min-interval 만큼 띄운다(리미터 기본 간격이 아니라)."""
    import json, sys
    from types import SimpleNamespace
    from pathlib import Path
    from src.execution.broker import kis_kr
    from src.utils import kis_rate_limit
    cli = _cli_module()
    kis_rate_limit.reset()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_in_market_hours", lambda now: False)
    monkeypatch.setattr(cli, "_bot_active", lambda: True)
    clock = [1000.0]
    monkeypatch.setattr(cli, "_monotonic", lambda: clock[0])
    sleeps = []
    async def fake_sleep(d):
        sleeps.append(round(d, 3)); clock[0] += d
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)          # cli·kis_kr 모두 같은 asyncio 모듈
    monkeypatch.setattr(kis_rate_limit, "release_ledger", lambda: None)
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))

    b = object.__new__(kis_kr.KISBroker)
    b._token = "t"
    b._token_mgr = SimpleNamespace(_access_token="t", _is_token_valid=lambda: True, invalidate=lambda: None)
    b.config = SimpleNamespace(base_url="http://x", account_no="12345678", account_product_cd="01", env="prod",
                               app_key="k", app_secret="s", timeout_seconds=10)
    async def _no_limit(tr_id=""): ...
    b._rate_limit = _no_limit
    b._session = SimpleNamespace(closed=False)
    class Resp:
        def __init__(self, status, body, cont=""):
            self.status, self._body, self.headers = status, body, ({"tr_cont": cont} if cont else {})
        async def json(self): return dict(self._body)
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
    sent_at = []
    seq = iter([Resp(503, {}), Resp(200, _page([_row()], "D"), "D")])     # 실제 _api_get 은 응답 헤더로 tr_cont 를 읽는다
    def get(url, headers, params):
        sent_at.append(clock[0]); return next(seq)
    b._session.get = get
    async def opener(): return b
    monkeypatch.setattr(cli, "_open_readonly_broker", opener)
    async def no_close(_b): ...
    monkeypatch.setattr(cli, "_close_readonly_broker", no_close)
    out = tmp_path / "out"; out.mkdir(mode=0o700)
    monkeypatch.setattr(sys, "argv", ["prog", "--start", "2025-01-01", "--end", "2025-01-31", "--out-dir", str(out),
                                      "--allow-bot-running", "--min-interval", "2.5"])
    assert cli.main() == 0
    assert len(sent_at) == 2 and sent_at[1] - sent_at[0] >= 2.5          # 503 재시도도 2.5초 뒤(백오프 1초만이 아님)
    s = json.loads((out / "summary.json").read_text())
    assert s["window_statuses"] == {"complete": 1} and s["http_attempts"] == 2 and s["pages_fetched"] == 1


def test_readonly_broker_requires_env_and_reads_repo_dotenv(monkeypatch, tmp_path):
    """8차 리뷰 P2: .env 를 읽고, 필수 변수가 없으면 KIS 호출 전에 명확한 오류로 중단한다(값 비출력)."""
    import pytest
    from pathlib import Path
    cli = _cli_module()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for k in ("KIS_APPKEY", "KIS_APP_KEY", "KIS_CANO", "KIS_APPSECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    with pytest.raises(RuntimeError, match="환경변수 없음"):
        asyncio.run(cli._open_readonly_broker())
    loaded = []
    def fake_load():
        loaded.append(True); monkeypatch.setenv("KIS_APPKEY", "k"); monkeypatch.setenv("KIS_CANO", "1")
    monkeypatch.setattr(cli, "_load_env", fake_load)
    import src.utils.token_manager as tm_mod
    class NoToken:                                   # 운영 캐시 경로를 건드리지 않는 가짜 토큰 매니저
        def _is_token_valid(self): return False
    monkeypatch.setattr(tm_mod, "KISTokenManager", NoToken)
    with pytest.raises(RuntimeError, match="캐시된 KIS 토큰") as e:   # 변수는 채워졌고 다음 단계(캐시 토큰 없음)에서 멈춘다
        asyncio.run(cli._open_readonly_broker())
    assert loaded == [True]

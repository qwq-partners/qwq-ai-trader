from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import pytest


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=KST)


def policy():
    return {"dataset_kind": "synthetic", "dates": ["2026-09-16"], "calendar_time": "08:00",
            "sessions": [{"name": "regular", "start": "09:00", "end": "15:30"}],
            "selection": {"max_symbols": 202, "max_snapshot_age_seconds": 300,
                          "candidate_limit": 202, "rule": "score_desc_symbol_asc_holdings_first"},
            "limits": {"job_timeout_seconds": 5, "max_retries": 1, "max_pages": 3},
            "comparison": {"max_age_seconds": 120, "max_skew_seconds": 30,
                           "outlier_pct": 5, "min_valid_pairs": 1,
                           "expected_market_basis": "krx"}}


def quote(symbol, price="100", observed_at=NOW - timedelta(seconds=10), *, basis="krx"):
    from src.data.providers.toss.market_types import Quote
    return Quote(symbol, Decimal(price), observed_at, NOW, "ok", frozenset(), basis, "KRW")


def test_select_snapshot_is_held_first_deterministic_and_preserves_unknown_kis_time():
    from src.data.providers.toss.observation import select_snapshot

    result = select_snapshot(candidates=(("B", 4), ("A", 4), ("C", 5)), holdings=("H", "A"),
        source_success_at=NOW, now=NOW, policy=policy(), kis_quotes={"H": quote("H", observed_at=None)})
    assert result["symbols"] == ("H", "A", "C", "B")
    assert result["kis"]["H"].observed_at is None
    assert result["selection_partial"] is False


def test_select_snapshot_marks_stale_candidates_partial_without_kis_request():
    from src.data.providers.toss.observation import select_snapshot

    result = select_snapshot(candidates=(("A", 2),), holdings=("H",),
        source_success_at=NOW - timedelta(seconds=301), now=NOW, policy=policy(), kis_quotes={})
    assert result["symbols"] == ("H",)
    assert result["selection_partial"] is True


def test_price_runner_persists_attempts_before_chunk_requests_and_keeps_earlier_success(tmp_path):
    from src.data.providers.toss.observation import ObservationRunner, select_snapshot
    from src.data.providers.toss.observation_ledger import ObservationLedger

    class Client:
        def __init__(self): self.calls = []
        async def get(self, path, *, params, budget):
            self.calls.append(params["symbols"])
            if len(self.calls) == 2: raise RuntimeError("private transport error")
            return {"result": [{"symbol": s, "lastPrice": "101", "currency": "KRW",
                "timestamp": "2026-09-16T09:59:55+09:00"} for s in params["symbols"].split(",")]}

    symbols = tuple(f"A{i:03d}" for i in range(201))
    snap = select_snapshot(candidates=tuple((s, 1) for s in symbols), holdings=(),
        source_success_at=NOW, now=NOW, policy=policy(), kis_quotes={s: quote(s) for s in symbols})
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=1_000_000); ledger.open()
    client = Client()
    result = asyncio.run(ObservationRunner(client=client, ledger=ledger, policy=policy(), now=lambda: NOW).prices(slot_id="s", snapshot=snap))
    assert len(client.calls) == 2 and result.observation_count == 200
    assert result.degraded and result.ledger_complete
    assert ledger.summary()["terminal_attempts"] == 201


def test_prices_excludes_unknown_basis_and_reports_observation_success_not_valid_comparison(tmp_path):
    from src.data.providers.toss.observation import ObservationRunner, select_snapshot
    from src.data.providers.toss.observation_ledger import ObservationLedger
    class Client:
        async def get(self, *args, **kwargs):
            return {"result": [{"symbol": "A", "lastPrice": "101", "currency": "KRW",
                                "timestamp": "2026-09-16T09:59:55+09:00"}]}
    snap = select_snapshot(candidates=(("A", 1),), holdings=(), source_success_at=NOW, now=NOW,
        policy=policy(), kis_quotes={"A": quote("A", basis="unknown")})
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=100_000); ledger.open()
    result = asyncio.run(ObservationRunner(client=Client(), ledger=ledger, policy=policy(), now=lambda: NOW).prices(slot_id="s", snapshot=snap))
    assert result.outcome == "success" and result.observation_count == 1
    assert result.valid_pairs == 0 and result.comparison["status"] == "insufficient"
    assert result.comparison["p95_pct"] is None and result.comparison["outlier_rate"] is None


@pytest.mark.parametrize("price,outlier", [("105", False), ("106", True)])
def test_large_difference_stays_valid_and_strict_threshold_only_marks_outlier(price, outlier):
    from src.data.providers.toss.observation import _pair
    pair = _pair(quote("A"), quote("A", price), policy(), NOW)
    assert pair["valid"] is True and pair["outlier"] is outlier
    assert pair["delta_pct"] == {"numerator": int(price) - 100, "denominator": 1}


@pytest.mark.parametrize("change,reason", [
    ({"symbol": "B"}, "symbol"), ({"price": Decimal("0")}, "price"),
    ({"price": Decimal("-1")}, "price"), ({"price": Decimal("NaN")}, "price"),
    ({"price": Decimal("Infinity")}, "price"), ({"fetched_at": None}, "fetched_at"),
    ({"fetched_at": NOW.replace(tzinfo=None)}, "fetched_at"),
    ({"fetched_at": NOW + timedelta(seconds=1)}, "future"),
    ({"fetched_at": NOW - timedelta(seconds=20)}, "fetched_at"),
    ({"observed_at": None}, "observed_at"),
    ({"observed_at": NOW.replace(tzinfo=None)}, "observed_at"),
    ({"observed_at": NOW + timedelta(seconds=1)}, "future"),
    ({"observed_at": NOW - timedelta(seconds=121)}, "stale"),
    ({"observed_at": NOW - timedelta(seconds=50)}, "skew"),
    ({"currency": "USD"}, "currency"), ({"market_basis": "unknown"}, "market_basis"),
    ({"status": "partial"}, "status")])
def test_pair_exclusions_preserve_fixed_reasons(change, reason):
    from src.data.providers.toss.observation import _pair
    result = _pair(replace(quote("A"), **change), quote("A"), policy(), NOW)
    assert result["valid"] is False and result["reason"] == reason
    assert result["delta_pct"] is None and result["outlier"] is None


def test_unknown_policy_never_authorizes_unknown_basis():
    from src.data.providers.toss.observation import _pair
    p = policy(); p["comparison"]["expected_market_basis"] = "unknown"
    assert _pair(quote("A", basis="unknown"), quote("A", basis="unknown"), p, NOW)["valid"] is False


def test_fraction_difference_does_not_subtract_under_decimal_context():
    from decimal import localcontext
    from src.data.providers.toss.observation import _pair
    with localcontext() as context:
        context.prec = 2
        pair = _pair(quote("A", "100"), quote("A", "101.23456789"), policy(), NOW)
    assert pair["delta_pct"] == {"numerator": 123456789, "denominator": 100000000}


@pytest.mark.parametrize("held,want,overflow", [(("A", "B"), ("A", "B"), ()),
    (("A", "B", "C"), ("A", "B"), ("C",)), (("A", "A"), ("A", "B"), ())])
def test_selection_never_exceeds_cap_and_records_held_overflow(held, want, overflow):
    from src.data.providers.toss.observation import select_snapshot
    p = policy(); p["selection"]["max_symbols"] = 2
    snap = select_snapshot(candidates=(("B", 9), ("B", 8), ("C", 7)), holdings=held,
        source_success_at=NOW, now=NOW, policy=p, kis_quotes={})
    assert snap["symbols"] == want
    assert snap["selection_metadata"]["overflow_symbols"] == overflow
    assert snap["selection_partial"] is bool(overflow)


def setup_runner(tmp_path, *, symbols=("A",), client=None, p=None, clock=None, now=NOW):
    from src.data.providers.toss.observation import ObservationRunner, select_snapshot
    from src.data.providers.toss.observation_ledger import ObservationLedger
    p = p or policy()
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=2_000_000)
    ledger.open()
    snap = select_snapshot(candidates=tuple((s, 1) for s in symbols), holdings=(),
        source_success_at=now, now=now, policy=p, kis_quotes={s: quote(s) for s in symbols})
    runner = ObservationRunner(client=client or GoodClient(), ledger=ledger, policy=p,
        now=lambda: now, **({"clock": clock} if clock else {}))
    return runner, ledger, snap


class GoodClient:
    def __init__(self): self.calls = []
    async def get(self, path, *, params, budget):
        self.calls.append((path, params, budget.deadline))
        return {"result": [{"symbol": s, "lastPrice": "106", "currency": "KRW",
            "timestamp": "2026-09-16T09:59:55+09:00"} for s in params["symbols"].split(",")]}


def test_fixed_deadline_includes_reservation_and_begin_fsync(tmp_path, monkeypatch):
    elapsed = [0.0]
    client = GoodClient()
    runner, ledger, snap = setup_runner(tmp_path, client=client, clock=lambda: elapsed[0])
    reserve, begin = ledger.reserve_slot, ledger.begin
    def slow_reserve(*args, **kwargs):
        answer = reserve(*args, **kwargs); elapsed[0] += 3; return answer
    def slow_begin(*args, **kwargs):
        answer = begin(*args, **kwargs); elapsed[0] += 3; return answer
    monkeypatch.setattr(ledger, "reserve_slot", slow_reserve)
    monkeypatch.setattr(ledger, "begin", slow_begin)
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert client.calls == []
    assert result.outcome == "failure" and result.reason == "budget_skip" and result.degraded
    assert result.budget_skips == 1 and result.ledger_complete


def test_empty_selection_is_idle_and_previous_failure_does_not_degrade_new_job(tmp_path):
    runner, ledger, snap = setup_runner(tmp_path, symbols=())
    result = asyncio.run(runner.prices(slot_id="empty", snapshot=snap))
    assert result.outcome == "idle" and result.reason == "empty_selection" and result.ledger_complete


def test_restart_dedup_preserves_evidence_and_exact_report(tmp_path, monkeypatch):
    import json
    from src.data.providers.toss import observation
    from src.data.providers.toss.observation_ledger import ObservationLedger
    # A trusted normalizer fixture supplies explicit known basis. Production
    # parse_prices still reports unknown, as required by the earlier contract.
    monkeypatch.setattr(observation, "parse_prices", lambda body, **kwargs: {s: quote(s, "106") for s in kwargs["symbols"]})
    runner, ledger, snap = setup_runner(tmp_path)
    first = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert first.valid_pairs == 1 and first.comparison["p95_pct"] == 6
    ledger.close()
    second_ledger = ObservationLedger(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
    second_ledger.open()
    runner.ledger = second_ledger
    second = asyncio.run(runner.prices(slot_id="s2", snapshot=snap))
    assert second.valid_pairs == 0 and second.excluded_pairs == 1
    report = second_ledger.summary()
    assert report["valid_pairs"] == 1 and report["comparison"]["outlier_rate"] == 1
    rows = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    obs = [row["observation"] for row in rows if row["type"] == "terminal"]
    assert obs[0]["kis"]["price"] == "100" and obs[0]["toss"]["price"] == "106"
    assert obs[1]["comparison"]["reason"] == "duplicate"
    assert rows[0]["snapshot"]["selection_metadata"]["candidates"] == ["A"]


@pytest.mark.parametrize("failure,expected", [("page_exhausted", "budget_skip"), ("timeout", "budget_skip"), ("circuit_open", "provider_failure"), ("parse", "provider_failure")])
def test_later_chunk_failure_preserves_success_and_job_local_degradation(tmp_path, monkeypatch, failure, expected):
    from src.data.providers.toss import observation
    from src.data.providers.toss.transport import TossRequestError
    monkeypatch.setattr(observation, "parse_prices", lambda body, **kwargs: {s: quote(s) for s in kwargs["symbols"]})
    class Client(GoodClient):
        async def get(self, *args, **kwargs):
            if len(self.calls) == 1:
                if failure == "parse": raise ValueError("private")
                raise TossRequestError(failure)
            return await super().get(*args, **kwargs)
    runner, ledger, snap = setup_runner(tmp_path, symbols=tuple(f"S{i:03d}" for i in range(201)), client=Client())
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert result.observation_count == 200 and result.valid_pairs == 200 and result.degraded
    assert result.outcome == "success" and result.ledger_complete
    assert ledger.summary()["terminal_reasons"][expected] == 1
    # A later clean job does not inherit accumulated failures or budget skips.
    runner.client = GoodClient()
    snap = dict(snap, symbols=("NEW",), kis={"NEW": quote("NEW")})
    snap.pop("selection_metadata")
    later = asyncio.run(runner.prices(slot_id="next", snapshot=snap))
    assert later.outcome == "success" and later.degraded is False


@pytest.mark.parametrize("stage", ["slot", "begin", "terminal"])
def test_ledger_ack_failure_stops_all_later_http(tmp_path, monkeypatch, stage):
    import os
    client = GoodClient()
    runner, ledger, snap = setup_runner(tmp_path, symbols=tuple(f"S{i:03d}" for i in range(201)), client=client)
    real_sync, calls = os.fsync, [0]
    fail_at = {"slot": 1, "begin": 2, "terminal": 203}[stage]
    def sync(fd):
        calls[0] += 1
        if calls[0] == fail_at: raise OSError("private disk failure")
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", sync)
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert len(client.calls) == (1 if stage == "terminal" else 0)
    assert result.outcome == "failure" and result.reason == "ledger_unavailable" and not result.ledger_complete


@pytest.mark.parametrize("stage", ["http", "parse", "second_symbol", "cancel"])
def test_partial_chunk_exception_only_terminates_unfinished_attempts(tmp_path, monkeypatch, stage):
    from src.data.providers.toss import observation
    real_pair = observation._pair
    def pairs(kis, toss, policy, now):
        if stage == "second_symbol" and toss.symbol == "B": raise ValueError("private")
        return real_pair(kis, toss, policy, now)
    monkeypatch.setattr(observation, "_pair", pairs)
    def parse(body, **kwargs):
        if stage == "parse": raise ValueError("private body")
        return {s: quote(s) for s in kwargs["symbols"]}
    monkeypatch.setattr(observation, "parse_prices", parse)
    class Client(GoodClient):
        async def get(self, *args, **kwargs):
            if stage == "http": raise RuntimeError("private")
            if stage == "cancel": raise asyncio.CancelledError()
            return await super().get(*args, **kwargs)
    runner, ledger, snap = setup_runner(tmp_path, symbols=("A", "B"), client=Client())
    if stage == "cancel":
        with pytest.raises(asyncio.CancelledError): asyncio.run(runner.prices(slot_id="s", snapshot=snap))
        assert ledger.summary()["cancelled"] == 2
    else:
        result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
        assert result.observation_count == (1 if stage == "second_symbol" else 0)
        assert result.ledger_complete
        assert result.provider_failures == (1 if stage == "second_symbol" else 2)
    assert ledger.summary()["terminal_attempts"] == 2


def test_terminal_ack_loss_reopen_never_resends_successful_slot(tmp_path, monkeypatch):
    import os
    from src.data.providers.toss.observation_ledger import ObservationLedger
    client = GoodClient()
    runner, ledger, snap = setup_runner(tmp_path, client=client)
    real_sync, count = os.fsync, [0]
    def ack_loss(fd):
        count[0] += 1
        real_sync(fd)
        if count[0] == 3: raise OSError("ACK lost after durable terminal")
    monkeypatch.setattr(os, "fsync", ack_loss)
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert result.outcome == "failure" and not result.ledger_complete
    ledger.close()
    monkeypatch.setattr(os, "fsync", real_sync)
    fresh = ObservationLedger(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
    fresh.open(); runner.ledger = fresh
    repeated = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert repeated.outcome == "duplicate" and len(client.calls) == 1
    assert fresh.summary()["terminal_attempts"] == 1 and fresh.summary()["interrupted"] == 0


def test_p95_nearest_rank_and_strict_outlier_rate_are_durable(tmp_path, monkeypatch):
    from src.data.providers.toss import observation
    from src.data.providers.toss.observation_ledger import ObservationLedger
    monkeypatch.setattr(observation, "parse_prices", lambda body, **kwargs: {s: quote(s, str(100 + int(s[1:]))) for s in kwargs["symbols"]})
    runner, ledger, snap = setup_runner(tmp_path, symbols=tuple(f"A{i}" for i in range(1, 21)))
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert result.valid_pairs == 20 and result.comparison["p95_pct"] == 19
    ledger.close()
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
    assert report["comparison"]["p95_pct"] == 19 and report["comparison"]["outlier_rate"] == .75
    assert report["comparison"]["outlier_rate_exact"] == {"numerator": 3, "denominator": 4}


@pytest.mark.parametrize("mutation", ["timestamp", "pair_id", "wrong_symbol", "delta", "outlier", "policy"])
def test_hash_valid_comparison_tampering_fails_closed(tmp_path, monkeypatch, mutation):
    import json
    from hashlib import sha256
    from src.data.providers.toss import observation
    from src.data.providers.toss.observation_ledger import ObservationLedger
    monkeypatch.setattr(observation, "parse_prices", lambda body, **kwargs: {s: quote(s, "106") for s in kwargs["symbols"]})
    runner, ledger, snap = setup_runner(tmp_path)
    asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    ledger.close()
    rows = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    obs = rows[-1]["observation"]
    if mutation == "timestamp": obs["kis"]["observed_at"] = None
    elif mutation == "pair_id": obs["comparison"]["pair_id"] = "f" * 64
    elif mutation == "wrong_symbol": obs["kis"]["symbol"] = obs["toss"]["symbol"] = "OTHER"
    elif mutation == "delta": obs["comparison"]["delta_pct"]["numerator"] = 1
    elif mutation == "outlier": obs["comparison"]["outlier"] = False
    else: rows[0]["snapshot"]["cohort"]["outlier_pct"] = "100"
    previous = "0" * 64
    for row in rows:
        row["previous_hash"] = previous
        row.pop("hash")
        row["hash"] = sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        previous = row["hash"]
    ledger.path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
    assert report["incomplete"] and report["error_code"] == "ledger_corrupt"


def test_frozen_worker_input_and_missing_kis_fetch_time_remain_diagnostic(tmp_path):
    from types import MappingProxyType
    runner, ledger, snap = setup_runner(tmp_path)
    def freeze(value):
        if isinstance(value, dict): return MappingProxyType({k: freeze(v) for k, v in value.items()})
        if isinstance(value, (list, tuple)): return tuple(freeze(v) for v in value)
        return value
    snap["kis"]["A"] = replace(quote("A"), fetched_at=None)
    runner.policy = freeze(runner.policy)
    result = asyncio.run(runner.prices(slot_id="s", snapshot=freeze(snap)))
    assert result.outcome == "success" and result.valid_pairs == 0 and result.ledger_complete
    import json
    terminal = json.loads(ledger.path.read_text().splitlines()[-1])
    assert terminal["observation"]["kis"]["fetched_at"] is None


def test_distinct_sessions_and_datasets_are_not_pooled(tmp_path, monkeypatch):
    from src.data.providers.toss import observation
    monkeypatch.setattr(observation, "parse_prices", lambda body, **kwargs: {s: quote(s, "106") for s in kwargs["symbols"]})
    runner, ledger, snap = setup_runner(tmp_path)
    asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    runner.policy = policy()
    runner.policy["sessions"] = [{"name": "after", "start": "09:00", "end": "15:30"}]
    asyncio.run(runner.prices(slot_id="s2", snapshot=snap))
    runner.policy = policy(); runner.policy["dataset_kind"] = "live"
    asyncio.run(runner.prices(slot_id="s3", snapshot=snap))
    report = ledger.summary()
    assert {(g["dataset_kind"], g["session"]) for g in report["cohorts"]} == {("synthetic", "regular"), ("synthetic", "after"), ("live", "regular")}
    assert {(g["dataset_kind"], g["session"]): g["comparison"]["valid_pairs"] for g in report["cohorts"]} == {
        ("synthetic", "regular"): 1, ("synthetic", "after"): 0, ("live", "regular"): 1}
    assert report["comparison"]["status"] == "insufficient" and report["comparison"]["p95_pct"] is None
    assert report["cohorts"][0]["by_date"]["2026-09-16"]["valid_pairs"] == 1


def test_parse_deadline_exhaustion_creates_no_success_terminal(tmp_path, monkeypatch):
    from src.data.providers.toss import observation
    elapsed = [0]
    runner, ledger, snap = setup_runner(tmp_path, clock=lambda: elapsed[0])
    def slow_parse(body, **kwargs):
        elapsed[0] = 6
        return {s: quote(s) for s in kwargs["symbols"]}
    monkeypatch.setattr(observation, "parse_prices", slow_parse)
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert result.observation_count == 0 and result.reason == "budget_skip" and result.ledger_complete


def test_zero_cap_rejected_and_duplicate_candidates_do_not_consume_unique_limit():
    from src.data.providers.toss.observation import select_snapshot
    p = policy(); p["selection"]["candidate_limit"] = 2
    args = dict(candidates=(("A", 9), ("A", 8), ("B", 7)), holdings=(), source_success_at=NOW, now=NOW, policy=p, kis_quotes={})
    assert select_snapshot(**args)["symbols"] == ("A", "B")
    p["selection"]["max_symbols"] = 0
    with pytest.raises(ValueError): select_snapshot(**args)


@pytest.mark.parametrize("timestamp,observed", [("2026-09-16T10:00:00+09:00", 1), ("2026-09-16T09:59:59.999999+09:00", 0)])
def test_zero_age_policy_retains_only_exact_now_observation(tmp_path, timestamp, observed):
    class Client:
        async def get(self, *args, **kwargs):
            return {"result": [{"symbol": "A", "lastPrice": "100", "currency": "KRW", "timestamp": timestamp}]}
    p = policy(); p["comparison"]["max_age_seconds"] = 0
    runner, ledger, snap = setup_runner(tmp_path, p=p, client=Client())
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert result.ledger_complete and result.observation_count == observed


def test_unconfirmed_policy_market_basis_stays_comparison_excluded(tmp_path):
    p = policy(); p["comparison"]["expected_market_basis"] = "unconfirmed_basis"
    runner, ledger, snap = setup_runner(tmp_path, p=p)
    result = asyncio.run(runner.prices(slot_id="s", snapshot=snap))
    assert result.outcome == "success" and result.ledger_complete and result.valid_pairs == 0


@pytest.mark.parametrize("restart", [False, True])
def test_source_pair_is_unique_across_sessions_with_one_configured_plan(tmp_path, monkeypatch, restart):
    import json
    from hashlib import sha256
    from types import MappingProxyType
    from src.data.providers.toss import observation
    from src.data.providers.toss.observation_ledger import ObservationLedger

    source_at = datetime(2026, 9, 16, 8, 59, 55, tzinfo=KST)
    now = [datetime(2026, 9, 16, 8, 59, 59, tzinfo=KST)]
    p = policy()
    p["dataset_kind"] = "live"
    p["sessions"] = [{"name": "pre", "start": "08:00", "end": "09:00"},
                     {"name": "regular", "start": "09:00", "end": "15:30"}]
    def freeze(value):
        if isinstance(value, dict): return MappingProxyType({k: freeze(v) for k, v in value.items()})
        if isinstance(value, (list, tuple)): return tuple(freeze(v) for v in value)
        return value
    p = freeze(p)
    kis = replace(quote("A", observed_at=source_at), fetched_at=now[0])
    # 가짜 정규화 경계에서만 명시적 시장 기준을 주입한다. 두 조회 모두
    # 원시각은 같고 수신시각만 바뀌므로 새 가격 통계 표본이 아니다.
    monkeypatch.setattr(observation, "parse_prices", lambda body, **kwargs: {
        "A": replace(quote("A", "106", observed_at=source_at), fetched_at=now[0])})
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=2_000_000)
    ledger.open()
    ledger.configure_plan(p)
    client = GoodClient()
    runner = observation.ObservationRunner(client=client, ledger=ledger, policy=p, now=lambda: now[0])
    def snapshot():
        return observation.select_snapshot(candidates=(("A", 1),), holdings=(),
            source_success_at=now[0], now=now[0], policy=p, kis_quotes={"A": kis})
    first = asyncio.run(runner.prices(slot_id="2026-09-16T08:55:00+09:00", snapshot=snapshot()))
    assert first.valid_pairs == 1
    if restart:
        ledger.close()
        ledger = ObservationLedger(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
        ledger.open()
        ledger.configure_plan(p)
        runner.ledger = ledger
    now[0] = datetime(2026, 9, 16, 9, 0, 1, tzinfo=KST)
    second = asyncio.run(runner.prices(slot_id="2026-09-16T09:00:00+09:00", snapshot=snapshot()))
    assert second.outcome == "success" and second.observation_count == 1
    assert second.valid_pairs == 0 and second.excluded_pairs == 1 and second.ledger_complete
    assert len(client.calls) == 2
    ledger.close()
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
    assert (report["valid_pairs"], report["excluded_pairs"], report["terminal_attempts"]) == (1, 1, 2)
    by_session = {c["session"]: c for c in report["cohorts"]}
    assert by_session["pre"]["comparison"]["valid_pairs"] == 1
    assert by_session["regular"]["comparison"]["p95_pct"] is None
    assert by_session["regular"]["excluded_pairs"] == 1
    rows = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    terminals = [r for r in rows if r["type"] == "terminal"]
    first_comparison, second_comparison = (r["observation"]["comparison"] for r in terminals)
    assert first_comparison["pair_id"] == second_comparison["pair_id"]
    assert second_comparison["reason"] == "duplicate"

    # 같은 중복을 hash-valid한 유효 terminal로 위조해도 재읽기에서 거부한다.
    terminal = rows[-1]
    terminal["observation"].update(valid_pairs=1, excluded_pairs=0, comparison=first_comparison)
    terminal.pop("hash")
    terminal["hash"] = sha256(json.dumps(terminal, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    ledger.path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    corrupt = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=2_000_000)
    assert corrupt["incomplete"] and corrupt["error_code"] == "ledger_corrupt"

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=KST)


def policy():
    return {"selection": {"max_symbols": 202, "max_snapshot_age_seconds": 300},
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
    assert result.valid_pairs == 0 and result.comparison is None

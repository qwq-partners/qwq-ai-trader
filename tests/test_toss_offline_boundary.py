"""Toss Phase 1 replay CLI의 오프라인 경계 계약."""

import asyncio
from datetime import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from src.data.providers.toss.shadow import ShadowManifest, summarize_pairs


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "replay_toss_shadow.py"
FIXTURES = ROOT / "tests" / "fixtures" / "toss"


def _offline_env():
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "TZ": "Asia/Seoul",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOSS_API": "1",
    }


class _IntegrationTransport:
    """외부 HTTP만 대체하는 응답 큐; client/token/market은 실제 구현을 사용한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.paths = []
        self.closed = False

    async def request(self, method, path, *, params, headers, timeout):
        self.paths.append((method, path, dict(params)))
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


def test_synthetic_issuer_client_prices_candles_and_shadow_use_the_real_offline_chain(tmp_path):
    """fake issuer/HTTP 외에는 실제 Phase 1 컴포넌트를 연결한다."""
    from src.data.providers.toss.client import TossClient
    from src.data.providers.toss.market_data import fetch_daily_candles, parse_prices
    from src.data.providers.toss.rate_limit import GroupRateLimiter, RequestBudget
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import SecureTokenStore
    from src.data.providers.toss.transport import HttpResponse

    issued = []

    async def issuer():
        issued.append(True)
        return {"access_token": "synthetic-integration-bearer", "expires_in": 3600, "token_type": "Bearer"}

    transport = _IntegrationTransport([
        HttpResponse(200, {}, {"result": [{
            "symbol": "005930", "lastPrice": "100.2", "currency": "KRW",
            "timestamp": "2026-09-16T09:59:30+09:00",
        }]}),
        HttpResponse(200, {}, {"result": {"candles": [{
            "timestamp": "2026-09-15T00:00:00+09:00", "openPrice": "99", "highPrice": "101",
            "lowPrice": "98", "closePrice": "100", "volume": 100, "currency": "KRW",
        }], "nextBefore": None}}),
    ])
    store = SecureTokenStore((tmp_path / "tokens").resolve(), "offline-integration-client")
    tokens = TokenManager(store, role="issuer", issuer=issuer, enabled=True)
    limiter = GroupRateLimiter(limits={
        "MARKET_DATA": 100, "MARKET_DATA_CHART": 100, "MARKET_INFO": 100,
    })
    client = TossClient(
        transport=transport, tokens=tokens, limiter=limiter, enabled=True, role="sender",
        sender_lock_path=tmp_path / "sender.lock", circuit_failure_threshold=2,
        circuit_open_seconds=5,
    )

    async def run():
        # 실제 구현 연결이 의도라 시계는 주입하지 않는다. 30초는 만료 원인이 아니라 고착 감시용이다
        # (실파일 잠금·fsync 가 호스트 부하로 느려져도 성공 경로가 timeout 으로 바뀌지 않게 한다).
        deadline = time.monotonic() + 30
        assert await tokens.bootstrap(approved=True, deadline=deadline) == "synthetic-integration-bearer"
        fetched_at = datetime.fromisoformat("2026-09-16T10:00:00+09:00")
        async with client:
            price_body = await client.get(
                "/api/v1/prices", params={"symbols": "005930"}, budget=RequestBudget(30),
            )
            quote = parse_prices(
                price_body, symbols=["005930"], fetched_at=fetched_at, now=fetched_at,
                max_age_seconds=300,
            )["005930"]
            candles = await fetch_daily_candles(
                client, symbol="005930", expected_dates=["20260915"], fetched_at=fetched_at,
                budget=RequestBudget(30), market_basis="krx",
            )
        return fetched_at, quote, candles

    fetched_at, quote, candles = asyncio.run(run())
    manifest_data = json.loads((FIXTURES / "phase1_manifest.json").read_text(encoding="utf-8"))
    manifest_data.update(min_valid_pairs=1, min_coverage=1.0, p95_limit_pct=0.2,
                         outlier_threshold_pct=0.2, max_outlier_fraction=0.0)
    result = summarize_pairs([{
        "pair_id": "integration-1", "symbol": "005930", "now": fetched_at.isoformat(),
        "kis": {"price": 100.0, "observed_at": "2026-09-16T09:59:30+09:00",
                "fetched_at": fetched_at.isoformat(), "status": "ok", "latency_ms": 1.0},
        "toss": {"price": float(quote.price), "observed_at": quote.observed_at.isoformat(),
                 "fetched_at": quote.fetched_at.isoformat(), "status": quote.status, "latency_ms": 1.0},
    }], ShadowManifest.from_dict(manifest_data))

    assert issued == [True]
    assert transport.paths == [
        ("GET", "/api/v1/prices", {"symbols": "005930"}),
        ("GET", "/api/v1/candles", {"symbol": "005930", "interval": "1d", "count": 200, "adjusted": True}),
    ]
    assert quote.status == "ok"
    assert candles.complete is True
    assert result["valid_pairs"] == 1
    assert result["p95_difference_pct"] == 0.2
    assert result["status"] == "within_limits"
    assert result["synthetic_only"] is True
    assert result["production_eligible"] is False
    assert transport.closed is True


def test_cli_help_exposes_only_manifest_and_input_without_live_or_credential_options():
    # live/credential 옵션을 추가하거나 help가 실행 중 인증으로 향하면 실패한다.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        env=_offline_env(),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--manifest" in result.stdout
    assert "--input" in result.stdout
    assert "--live" not in result.stdout
    assert "credential" not in result.stdout.lower()
    assert "token" not in result.stdout.lower()
    assert result.stderr == ""


def test_cli_reads_synthetic_files_and_writes_a_json_report_only_to_stdout(tmp_path):
    # cache/result 파일을 만들거나 TOSS_API=1로 live를 암묵 활성화하면 실패한다.
    manifest = tmp_path / "manifest.json"
    pairs = tmp_path / "pairs.json"
    manifest.write_text((FIXTURES / "phase1_manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    pairs.write_text((FIXTURES / "phase1_pairs.json").read_text(encoding="utf-8"), encoding="utf-8")
    before = sorted(path.name for path in tmp_path.iterdir())

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--input", str(pairs)],
        cwd=ROOT,
        env=_offline_env(),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert report["attempted_pairs"] == 6
    assert report["valid_pairs"] == 2
    assert report["p95_difference_pct"] == 1.0
    assert report["synthetic_only"] is True
    assert report["production_eligible"] is False

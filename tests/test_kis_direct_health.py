"""상태 조회는 직접 KIS 요청 집계를 브로커와 분리해 읽기만 한다."""

from types import SimpleNamespace

from src.dashboard.data_collector import DashboardDataCollector
from src.utils import kis_request_metrics, kis_direct_metrics
from test_kis_pagination_protocol import broker


def test_health_exposes_independent_direct_snapshot_without_reset(broker, monkeypatch):
    monkeypatch.setattr(kis_request_metrics, "_recorder", kis_request_metrics.RequestMetrics())
    monkeypatch.setattr(kis_direct_metrics, "_recorder", kis_direct_metrics.DirectKISMetrics())
    broker._session = None
    collector = DashboardDataCollector(SimpleNamespace(
        broker=broker,
        engine=SimpleNamespace(risk_manager=None),
        strategy_manager=None,
    ))
    with kis_direct_metrics.observe_get("market_data", "index_price", "FHPUP02100000") as observed:
        observed.observe_response(SimpleNamespace(status=503))
    before = kis_request_metrics.snapshot()
    direct_before = kis_direct_metrics.snapshot()
    health = collector.get_system_health()["broker"]
    direct = health["kis_direct_requests"]
    assert direct["scope"] == "kr_market_data_and_screener_get"
    assert direct["window"] == "process_lifetime"
    assert direct == direct_before
    assert direct["http"][0]["attempts"] == 1
    assert health["kis_requests"] == before
    direct["http"][0]["attempts"] = -1
    direct["http"].append({"private_mutation": True})
    refreshed = collector.get_system_health()["broker"]
    assert {"private_mutation": True} not in refreshed["kis_direct_requests"]["http"]
    assert refreshed["kis_direct_requests"] == direct_before == kis_direct_metrics.snapshot()
    assert refreshed["kis_requests"] == before == kis_request_metrics.snapshot()

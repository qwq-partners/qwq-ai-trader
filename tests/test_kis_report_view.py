from datetime import datetime, timezone
from unittest.mock import Mock
from src.data.providers.kis_market_data import KISMarketData, _report_numbers


def test_report_numbers_preserve_missing_and_real_zero():
    raw = {'bstp_nmix_prpr': '3000', 'bstp_nmix_prdy_ctrt': '', 'bstp_nmix_oprc': '0', 'bstp_nmix_hgpr': 'nan'}
    result = _report_numbers(raw)
    assert result['price'] == 3000
    assert result['change_pct'] is None
    assert result['open'] == 0
    assert result['high'] is None
    assert _report_numbers({'bstp_nmix_prdy_ctrt': '0'})['change_pct'] == 0


def test_peek_does_not_promote_receipt_or_legacy_zero():
    provider = KISMarketData(token_manager=Mock())
    provider._cache['index_price_0001'] = {'price': 3000, 'change_pct': 0, 'source': 'kis'}
    provider._cache_ts['index_price_0001'] = datetime.now()
    assert provider.peek_report_indices() == ()
    provider._cache['index_price_0001'].update(report_values={'price': 3000, 'change_pct': None}, received_at=datetime.now(timezone.utc).isoformat())
    row = provider.peek_report_indices()[0]
    assert row['change_pct'] is None
    assert row['source_observed_at'] is None
    row['price'] = 1
    assert provider.peek_report_indices()[0]['price'] == 3000

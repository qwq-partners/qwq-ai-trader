"""공유 보수적 요율과 월 예산 예약·정산의 오프라인 회귀 검사."""
from datetime import date, datetime, timezone
import sqlite3

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMResponse
from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger
from src.utils.llm_pricing import rate_quote


@pytest.mark.parametrize('model,input_rate,output_rate', [
    ('gpt-5.6-sol', 5.0, 20.0),
    ('gpt-6.1-sol', 2.5, 10.0),
    ('gpt-6-astra', 12.5, 50.0),
])
@pytest.mark.parametrize('inputs', [0, 100, 272_000, 272_001])
def test_aggregate_input_uses_one_cache_write_ceiling(model, input_rate, output_rate, inputs):
    # 일반 입력과 캐시 쓰기는 중복 가산하지 않고, 긴 문맥 할증만 별도로 적용한다.
    quote = rate_quote(model, inputs, 1000)
    long_context = inputs > 272_000
    expected_input = input_rate * (2 if long_context else 1)
    expected_output = output_rate * (1.5 if long_context else 1)
    assert quote.input_usd_per_million == expected_input
    assert quote.output_usd_per_million == expected_output
    assert quote.cost_usd == pytest.approx((inputs * expected_input + 1000 * expected_output) / 1_000_000)
    assert 'cache_write_ceiling' in quote.basis
    assert 'not_invoice' in quote.basis
    assert 'no_cache_read_discount' in quote.basis
    assert ('long_context' in quote.basis) == long_context


@pytest.mark.parametrize('model,input_rate,output_rate,cost', [
    ('gemini-3.8-flash', 1.5, 7.5, 0.009),
    ('gemini-4-argon', 4.0, 20.0, 0.024),
])
@pytest.mark.parametrize('on_date', [
    date(2026, 10, 15), date(2026, 11, 30), date(2026, 12, 1),
    date(2026, 12, 31), date(2027, 1, 1), date(2027, 10, 15),
])
def test_gemini_post_promo_ceiling_survives_intro_expiry(model, input_rate, output_rate, cost, on_date):
    quote = rate_quote(model, 1000, 1000, on_date=on_date)
    assert quote is not None
    assert quote.input_usd_per_million == input_rate
    assert quote.output_usd_per_million == output_rate
    assert quote.cost_usd == pytest.approx(cost)
    assert 'post_promo_ceiling' in quote.basis
    assert 'not_invoice' in quote.basis


@pytest.mark.parametrize('model', [
    'gpt-6.1-sol-unknown', 'gpt-6-astra-latest', 'gemini-4-argon-preview',
    'gemini-3.8-flash-unknown', 'claude-sonnet-5-5', 'unpriced-model',
])
def test_unknown_exact_id_does_not_inherit_family_price(model):
    assert rate_quote(model, 100, 100) is None


def test_legacy_mini_does_not_get_new_cache_or_context_premium():
    quote = rate_quote('gpt-5-mini', 300_000, 1000)
    assert quote.cost_usd == pytest.approx(0.077)
    assert quote.input_usd_per_million == 0.25
    assert quote.output_usd_per_million == 2.0
    assert 'cache_write_ceiling' not in quote.basis
    assert 'long_context' not in quote.basis


@pytest.mark.asyncio
@pytest.mark.parametrize('model,reservation,settlement', [
    ('gpt-5.6-sol', 0.00457, 0.0015),
    ('gpt-6.1-sol', 0.002285, 0.00075),
    ('gpt-6-astra', 0.011425, 0.00375),
])
async def test_manager_reserves_and_settles_same_cache_ceiling(tmp_path, model, reservation, settlement):
    manager = LLMManager(LLMConfig(
        openai_api_key='test-only', openai_model_heavy=model,
        monthly_budget_usd=100, budget_state_path=str(tmp_path / 'budget.sqlite3'),
    ))

    class ReportedUsageClient:
        async def complete(self, *args, **kwargs):
            # 요청 전 UTF-8 바이트 상한: 2 * (1 + 256) = 514 입력 토큰.
            assert manager.get_monthly_budget_status()['reserved_usd'] == pytest.approx(reservation)
            return LLMResponse('ok', model, LLMProvider.OPENAI, input_tokens=100, output_tokens=50)

    manager.openai = ReportedUsageClient()
    try:
        response = await manager.complete_with('p', LLMProvider.OPENAI, weight='heavy', max_tokens=100)
        assert response.success
        status = manager.get_monthly_budget_status()
        assert status['monthly_limit_usd'] == 100
        assert status['reserved_usd'] == 0
        assert status['spent_usd'] == pytest.approx(settlement)
        assert status['settled_count'] == 1
        assert not status['bound_breach']
        assert manager.daily_usage.estimated_cost_usd == pytest.approx(settlement)
    finally:
        manager.monthly_budget.close()


def test_cache_write_ceiling_prevents_underpriced_monthly_admission(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 0.0021)
    try:
        # 일반 요율 $0.002028은 통과하지만 캐시 쓰기 상한 $0.002285는 초과한다.
        with pytest.raises(BudgetBlocked, match='monthly_limit_exceeded'):
            ledger.reserve('gpt-6.1-sol', 'p', '', 100)
        status = ledger.status()
        assert status['monthly_limit_usd'] == 0.0021
        assert status['reserved_usd'] == 0
        assert status['spent_usd'] == 0
    finally:
        ledger.close()


def test_new_quote_does_not_reprice_settled_history_on_reopen(tmp_path):
    path = tmp_path / 'budget.sqlite3'
    clock = lambda: datetime(2026, 10, 15, tzinfo=timezone.utc)
    ledger = MonthlyBudgetLedger(path, 100, clock=clock)
    token = ledger.reserve('gpt-6.1-sol', 'p', '', 100)
    # 이전 일반 입력 요율로 이미 정산된 기록은 새 정책에서 수정하지 않는다.
    ledger.settle(token, outcome='known', actual_cost_usd=0.0007)
    activated_at = ledger.status()['activated_at']
    ledger.close()
    with sqlite3.connect(path) as db:
        before = db.execute('SELECT * FROM reservations WHERE token=?', (token,)).fetchone()

    reopened = MonthlyBudgetLedger(path, 100, clock=clock)
    try:
        status = reopened.status()
        assert status['spent_usd'] == 0.0007
        assert status['activated_at'] == activated_at
        next_token = reopened.reserve('gpt-6.1-sol', 'p', '', 100)
        assert reopened.status()['reserved_usd'] == pytest.approx(0.002285)
        reopened.settle(next_token, outcome='not_sent')
        assert reopened.status()['spent_usd'] == 0.0007
        with sqlite3.connect(path) as db:
            after = db.execute('SELECT * FROM reservations WHERE token=?', (token,)).fetchone()
        assert after == before
    finally:
        reopened.close()

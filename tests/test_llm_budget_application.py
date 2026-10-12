"""Application budget activation and unmetered-provider boundary; no paid requests."""
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('raw', [{}, {'llm':{}}, {'llm':{'monthly_budget':None}},
    {'llm':{'monthly_budget':100}}, {'llm':{'monthly_budget':100,'budget_state_path':''}}])
def test_runner_rejects_missing_monthly_budget_instead_of_unbudgeted_defaults(raw):
    from scripts import run_trader
    helper = getattr(run_trader, 'configure_llm_from_app', None)
    assert helper is not None, 'startup must require the explicit monthly guard'
    with pytest.raises(ValueError):
        helper(SimpleNamespace(raw=raw))


@pytest.mark.asyncio
@pytest.mark.parametrize('caller',['expert','regime'])
async def test_monthly_guard_blocks_unmetered_search_before_creating_http_session(monkeypatch,caller):
    import aiohttp
    from src.utils import llm
    from src.experts.base import ExpertAgent
    from src.core.market_regime import MarketRegimeAdapter
    monkeypatch.setattr(llm,'unmetered_paid_calls_allowed',lambda:False,raising=False)
    sessions=[]
    def no_http(*args,**kwargs):
        sessions.append(True)
        raise RuntimeError('synthetic: paid transport must not be entered')
    monkeypatch.setattr(aiohttp,'ClientSession',no_http)
    if caller=='expert':
        subject=SimpleNamespace(perplexity_key='synthetic-key',name='synthetic')
        result=await ExpertAgent._perplexity_search(subject,'synthetic query')
    else:
        result=await MarketRegimeAdapter._fetch_perplexity_context(None,'synthetic-key')
    assert result == ''
    assert sessions == []


def test_runner_activates_config_without_creating_ledger_or_paid_client(tmp_path, monkeypatch):
    from scripts.run_trader import configure_llm_from_app
    from src.utils import llm
    monkeypatch.setattr(llm, '_llm_manager', None)
    monkeypatch.setattr(llm, '_pending_config', None)
    path = tmp_path / 'budget.sqlite3'
    configured = configure_llm_from_app(SimpleNamespace(raw={'llm': {
        'monthly_budget': 100, 'daily_budget': 5, 'budget_state_path': str(path)}}))
    assert configured.monthly_budget_usd == 100
    assert llm._pending_config is configured
    assert llm._llm_manager is None and not path.exists()
    assert not llm.unmetered_paid_calls_allowed()


def test_shipped_configuration_has_user_limit_and_persistent_path():
    from pathlib import Path
    import yaml
    from src.utils.llm import LLMConfig
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/default.yml').read_text())
    cfg = LLMConfig.from_config(raw)
    assert cfg.monthly_budget_usd == 100 and cfg.daily_budget_usd == 5
    assert cfg.budget_state_path == '~/.local/state/qwq-ai-trader/llm-budget.sqlite3'

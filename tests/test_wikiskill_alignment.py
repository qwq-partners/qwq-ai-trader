"""WikiSkill 정렬 회귀 테스트 (2026-09-13)

대상: 재제안 억제(소스 무관) / LLM 제안자용 기각 컨텍스트(게이트 수치 비공개) /
      일일 복기 → 게이트 경유 제안 / 배분 이력 컨텍스트 / GateResult.wf / TeamVerdict 위키 플래그
프로덕션 캐시·네트워크 무접촉.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.evolution import candidate_ledger, daily_reviewer  # noqa: E402
from src.core.evolution.strategy_evolver import StrategyEvolver  # noqa: E402
from src.core.evolution.backtest_gate import GateResult  # noqa: E402
from src.agents.types import TeamVerdict  # noqa: E402

LEDGER = [
    {"parameter": "sepa_trend.min_score", "old_value": 70, "new_value": 65, "event": "rejected_by_backtest",
     "reason": "walk-forward 미달", "gate": {"baseline": {"total_return_pct": 1.0}}},
    {"parameter": "gap_and_go.min_score", "old_value": 60, "new_value": 65, "event": "rollback", "reason": "승률 악화"},
    {"parameter": "core_holding.min_score", "old_value": 70, "new_value": 72, "event": "applied", "reason": "ok"},
]


def _evolver() -> StrategyEvolver:
    return object.__new__(StrategyEvolver)


def test_recently_decided_against_covers_reject_and_rollback(monkeypatch):
    monkeypatch.setattr(candidate_ledger, "load_candidates", lambda days=90: LEDGER)
    ev = _evolver()
    assert ev._recently_decided_against("sepa_trend.min_score")      # 게이트 기각
    assert ev._recently_decided_against("gap_and_go.min_score")      # 롤백도 억제 대상
    assert not ev._recently_decided_against("core_holding.min_score")  # 적용·유지는 억제 안 함


def test_rejection_context_lists_decisions_without_gate_metrics(monkeypatch):
    monkeypatch.setattr(candidate_ledger, "load_candidates", lambda days=90: LEDGER)
    ctx = StrategyEvolver._build_rejection_context()
    assert ctx.startswith("## 최근 기각·롤백 후보")
    assert "sepa_trend.min_score 70→65 [rejected_by_backtest]" in ctx
    assert "gap_and_go.min_score 60→65 [rollback]" in ctx
    assert "core_holding" not in ctx
    assert "total_return_pct" not in ctx and "1.0" not in ctx  # held-out 수치 누출 금지


def test_daily_review_trigger_goes_through_gate_path(monkeypatch):
    review = {"parameter_suggestions": [
        {"strategy": "sepa_trend", "parameter": "min_score", "suggested_value": 60, "confidence": 0.4},   # 저신뢰 skip
        {"strategy": "sepa_trend", "parameter": "stop_loss_pct", "suggested_value": 4, "confidence": 0.9},  # 잠금 skip
        {"strategy": "sepa_trend", "parameter": "min_score", "suggested_value": 65, "confidence": 0.8, "reason": "과다 진입"},
    ]}
    monkeypatch.setattr(daily_reviewer, "get_daily_reviewer",
                        lambda: SimpleNamespace(load_llm_review=lambda d: review))
    ev = _evolver()
    ev._locked_params = {"stop_loss_pct"}
    ev._resolve_param_targets = lambda key: [tuple(key.split(".", 1))]
    ev._get_param_value = lambda s, p: 70
    ev._clamp_value = lambda p, v, c: v
    t = ev._find_daily_review_trigger()
    assert t == {"strategy": "sepa_trend", "parameter": "min_score", "old_value": 70, "new_value": 65,
                 "reason": "일일 복기 제안: 과다 진입", "source": "daily_review"}
    monkeypatch.setattr(daily_reviewer, "get_daily_reviewer",
                        lambda: SimpleNamespace(load_llm_review=lambda d: None))
    assert ev._find_daily_review_trigger() is None


def test_recent_rebalance_context_uses_last_entries(monkeypatch, tmp_path):
    hist = tmp_path / "rebalance_history.json"
    hist.write_text(json.dumps([
        {"timestamp": f"2026-08-0{i}T00:00:00", "before": {"a": i}, "after": {"a": i + 1}, "reasoning": f"r{i}"}
        for i in range(1, 7)
    ]), encoding="utf-8")
    monkeypatch.setattr(StrategyEvolver, "_REBALANCE_HISTORY_PATH", hist)
    ctx = _evolver()._recent_rebalance_context()
    lines = ctx.splitlines()
    assert len(lines) == 4 and lines[0].startswith("- 2026-08-03") and "r6" in lines[-1]
    monkeypatch.setattr(StrategyEvolver, "_REBALANCE_HISTORY_PATH", tmp_path / "missing.json")
    assert _evolver()._recent_rebalance_context() == ""


def test_gate_result_serializes_walk_forward():
    r = GateResult(False, "wf 미달", baseline={"total_return_pct": 1.0}, candidate={"total_return_pct": 2.0},
                   wf={"base": [1.0, 2.0, 3.0], "cand": [0.5, 2.5, 3.5], "wins": 2, "min_wins": 2})
    d = r.to_dict()
    assert d["wf"]["wins"] == 2 and d["wf"]["cand"] == [0.5, 2.5, 3.5]
    assert GateResult(True, "ok").to_dict()["wf"] == {}


def test_team_verdict_carries_wiki_flag():
    v = TeamVerdict(symbol="005930")
    assert v.to_dict()["wiki_context_used"] is False
    v.wiki_context_used = True
    assert v.to_dict()["wiki_context_used"] is True

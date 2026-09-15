"""T11 계약 커밋 스모크 — 근거·판단·EntryPlan·원장 계약의 직렬화·호환·격리 (2026-09-15).

구현(A/B/C/D)은 별도 테스트 파일에서 검증한다. 여기서는 계약 타입이 기존 경로를 깨지 않고
(기본값 호환) 직렬화·역직렬화·멱등 원장이 동작하는지만 고정한다. 운영 캐시 무접촉(tmp_path).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from src.agents import team_ledger
from src.agents.types import (
    AnalystKind, AnalystReport, EvidenceItem, TeamAssessment, TeamVerdict, DebateTurn,
)
from src.core.batch_analyzer import PendingSignal
from src.execution.entry_plan import PlanCheck, check_entry_plan
from src.utils.data_freshness import DataPoint, missing


def test_analyst_report_defaults_keep_legacy_semantics():
    r = AnalystReport(kind=AnalystKind.NEWS, symbol="005930", score=10, confidence=0.5)
    assert r.data_status == "unknown" and r.evidence == [] and r.observed_at is None
    d = r.to_dict()
    assert d["evidence"] == [] and d["observed_at"] is None and "age_minutes" in d


def test_evidence_item_from_datapoint_keeps_missing_and_unknown_time():
    now = datetime(2026, 9, 15, 10, 0)
    ok = EvidenceItem.from_datapoint(
        DataPoint(value=2.0, as_of=now - timedelta(minutes=5), source="kis", ttl_seconds=600),
        metric="night_futures_pct", collected_at=now)
    assert ok.status == "full" and ok.usable and ok.valid_until == now + timedelta(minutes=5)
    unknown_time = EvidenceItem.from_datapoint(
        DataPoint(value=1.0, as_of=None, source="kis"), metric="x", collected_at=now)
    assert unknown_time.status == "partial" and unknown_time.observed_at is None   # now 로 채우지 않는다
    gone = EvidenceItem.from_datapoint(missing("kis", "조회 실패"), metric="x", collected_at=now)
    assert gone.status == "insufficient" and not gone.usable and gone.value is None


def test_pending_signal_roundtrip_with_and_without_plan_fields():
    base = dict(symbol="005930", name="삼성전자", strategy="sepa_trend", side="buy",
                entry_price=70000.0, max_entry_price=72100.0, stop_price=66500.0,
                target_price=80000.0, score=80.0, reason="r",
                created_at="2026-09-15T08:20:00", expires_at="2026-09-15T15:30:00")
    legacy = PendingSignal.from_dict(dict(base))          # 구 JSON (계획 필드 없음)
    assert legacy.plan_id == "" and legacy.trigger == {} and legacy.setup == ""
    full = PendingSignal(**base, plan_id="p1", setup="vcp_breakout",
                         trigger={"type": "breakout", "level": 71000.0, "satisfied": None, "satisfied_at": None},
                         invalidation={"stop_price": 66500.0}, required_inputs=["quote"], exit_policy_ref="exit_manager:sepa_trend")
    again = PendingSignal.from_dict(json.loads(json.dumps(full.to_dict())))
    assert again == full                                   # 직렬화 왕복에서 조건이 유실되지 않는다
    unknown = PendingSignal.from_dict({**base, "future_field": 1})
    assert unknown.symbol == "005930"                      # 미래 키는 무시(예외 없음)


def test_entry_plan_checker_placeholder_is_conservative_and_never_raises():
    now = datetime(2026, 9, 15, 9, 1)
    res = check_entry_plan({"plan_id": "p1", "setup": "sepa_pullback"}, {"price": 1.0}, now)
    assert isinstance(res, PlanCheck) and res.status == "wait" and res.reasons
    assert check_entry_plan(None, None, now).status in ("wait", "reject")
    assert res.to_dict()["checked_at"] == now.isoformat(timespec="seconds")


def test_team_assessment_defaults_are_abstain_and_uncalibrated():
    a = TeamAssessment(symbol="005930")
    d = a.to_dict()
    assert d["success_probability"] is None and d["calibration_status"] == "uncalibrated"
    assert d["stance_v2"] == "abstain" and d["merit_status"] == "abstain"
    v = TeamVerdict(symbol="005930", assessment=a, deliberation_id="abc", slot="10:30")
    vd = v.to_dict()
    assert vd["assessment"]["policy_version"] and vd["deliberation_id"] == "abc"
    t = DebateTurn(1, "bull", True, "APPROVE ...", change_reason={"kind": "new_evidence", "text": "x"})
    assert t.change_reason["kind"] == "new_evidence"


def test_team_ledger_append_is_idempotent_and_keeps_history(tmp_path):
    row = {"symbol": "005930", "decided_at": "2026-09-15T10:30:00", "slot": "10:30",
           "input_snapshot_hash": "h1", "decision": {"stance": "buy", "approved": True},
           "raw": {"api_key": "SECRET", "note": "github_pat_" + "A" * 30}}
    d1 = team_ledger.append_deliberation(row, ledger_dir=tmp_path)
    d2 = team_ledger.append_deliberation(dict(row), ledger_dir=tmp_path)   # 같은 입력 재시도
    later = {**row, "decided_at": "2026-09-15T13:00:00", "slot": "13:00",
             "decision": {"stance": "hold", "approved": False}}
    d3 = team_ledger.append_deliberation(later, ledger_dir=tmp_path)
    assert d1 == d2 and d1 != d3
    rows = team_ledger.load_day("2026-09-15", ledger_dir=tmp_path)
    assert len(rows) == 2                                   # 재시도는 표본을 부풀리지 않는다
    hist = team_ledger.history_for_symbol("005930", "2026-09-15", ledger_dir=tmp_path)
    assert [h["slot"] for h in hist] == ["10:30", "13:00"]   # 같은 날 여러 시점 판단 보존
    assert rows[0]["raw"]["api_key"] == "***" and "github_pat_" not in json.dumps(rows)
    assert team_ledger.count_samples(rows) == {"buy_approved": 1, "hold": 1}
    assert not team_ledger.append_deliberation({"symbol": "x"}, ledger_dir=Path("/proc/forbidden")) \
        , "쓰기 실패는 None 을 돌려주고 예외를 내지 않는다"

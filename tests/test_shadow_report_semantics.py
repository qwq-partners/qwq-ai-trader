"""Report counts describe observations, not buy orders or eligible samples."""

import json
from datetime import datetime

import pytest

import scripts.shadow_report as report_mod
from src.agents import team_ledger


@pytest.fixture
def report_inputs(tmp_path, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 25, 11, 0)

    monkeypatch.setattr(report_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(report_mod, "VERDICT_DIR", tmp_path / "verdicts")
    monkeypatch.setattr(report_mod, "LEDGER_DIR", tmp_path / "llm")
    monkeypatch.setattr(team_ledger, "LEDGER_DIR", tmp_path / "team")

    def write(verdicts, decisions=(), calls=()):
        for directory, name, rows, jsonl in (
            (report_mod.VERDICT_DIR, "verdicts_20261023.json", verdicts, False),
            (team_ledger.LEDGER_DIR, "deliberations_20261023.jsonl", decisions, True),
            (report_mod.LEDGER_DIR, "llm_20261023.jsonl", calls, True),
        ):
            directory.mkdir(exist_ok=True)
            payload = "\n".join(json.dumps(r) for r in rows) if jsonl else json.dumps(rows)
            (directory / name).write_text(payload, encoding="utf-8")

    return write


def decision(stance, approved):
    return {"decision": {"stance": stance, "approved": approved}}


def test_accepting_hold_and_sell_is_not_reported_as_buy_approval(report_inputs):
    report_inputs([
        decision("hold", True), decision("hold", True), decision("buy", True),
        decision("sell", True), decision("hold", False),
    ])

    report = report_mod.build_report(days=7)

    assert "일별·종목별 최신 판정 5건" in report
    assert "의견 수용 4 / 미수용 1" in report
    assert "HOLD 2" in report and "BUY 1" in report and "SELL 1" in report
    assert "의견 수용은 주문·체결을 의미하지 않습니다" in report
    assert "승인 4" not in report and "보류·거부 1" not in report


@pytest.mark.parametrize("latest_count", [1, 201])
def test_snapshot_or_ledger_count_cannot_certify_independent_samples(report_inputs, latest_count):
    # Even >200 distinct IDs do not establish independent, eligible observations.
    rows = [{"deliberation_id": f"d-{i}"} for i in range(205)]
    report_inputs([decision("hold", True) for _ in range(latest_count)], rows + [rows[0]])

    report = report_mod.build_report(days=7)

    assert f"일별·종목별 최신 판정 {latest_count}건" in report
    assert "판단(중복 제거) 205건 / 원장 레코드(재시도 포함) 206건" in report
    assert "독립 심의 표본: 미검증 (기준 200건)" in report
    assert f"표본 {latest_count}/200" not in report
    assert "표본 205/200" not in report


def test_repeat_consistency_uses_groups_not_all_calls_or_response_text(report_inputs):
    calls = [
        {"prompt_hash": h, "verdict": verdict, "response": str(i), "success": True}
        for i, (h, verdict) in enumerate([
            ("same", True), ("same", True), ("mixed", True), ("mixed", False),
            ("single", True),
        ])
    ]
    report_inputs([decision("hold", True)], calls=calls)

    report = report_mod.build_report(days=7)

    assert "저장 판정 일치율 50.0% (1/2그룹" in report
    assert "반복 prompt_hash" in report
    assert "응답 내용·모델별 재현성·투자 성과를 검증한 값이 아닙니다" in report
    assert "LLM 호출 5건" in report
    assert "✅ 일치율" not in report

"""Suppressed ledger records must not become model calls or agreement samples."""
import json

import pytest

from src.agents import reproducibility


@pytest.fixture
def ledger_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(reproducibility, "LEDGER_DIR", tmp_path)

    def write(rows):
        (tmp_path / "llm_20261028.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    return write


def test_eight_records_mean_five_calls_and_three_suppressed(ledger_rows):
    sent = [{"provider": "openai", "model": "synthetic", "success": True,
             "request_sent": True, "prompt_hash": "actual", "verdict": True}
            for _ in range(4)]
    failed = {"provider": "gemini", "model": "synthetic", "success": False,
              "request_sent": True, "prompt_hash": "failed", "verdict": None}
    # An inconsistent producer's success=True must not override not-sent.
    suppressed = [{"provider": "gemini", "model": "", "success": True,
                   "request_sent": False, "prompt_hash": "actual", "verdict": False}
                  for _ in range(3)]
    ledger_rows(sent + [failed] + suppressed)
    stats = reproducibility.LLMLedger.agreement_rate("20261028")
    assert stats["total_calls"] == 5
    assert stats["total_records"] == 8
    assert (stats["sent"], stats["not_sent"], stats["unknown"]) == (5, 3, 0)
    assert stats["agreement_rate"] == 100.0
    assert stats["repeated_prompts"] == 1
    assert reproducibility.LLMLedger.model_usage("20261028") == {
        "openai/synthetic": 4, "gemini/synthetic": 1}


@pytest.mark.parametrize("repeated", [True, False])
def test_missing_and_null_legacy_transport_remain_unknown(ledger_rows, repeated):
    first = {"provider": "openai", "model": "legacy", "success": True,
             "prompt_hash": "first", "verdict": True}
    second = {**first, "request_sent": None,
              "prompt_hash": "first" if repeated else "second"}
    ledger_rows([first, second])
    stats = reproducibility.LLMLedger.agreement_rate("20261028")
    assert stats["total_records"] == stats["total_calls"] == 2
    assert (stats["sent"], stats["not_sent"], stats["unknown"]) == (0, 0, 2)
    assert stats["measurable"] is repeated
    assert reproducibility.LLMLedger.model_usage("20261028") == {"openai/legacy": 2}


def test_only_unsent_success_records_are_not_an_agreement_sample(ledger_rows):
    ledger_rows([{"request_sent": False, "success": True, "prompt_hash": "same",
                  "verdict": True, "provider": "gemini", "model": ""}] * 2)
    stats = reproducibility.LLMLedger.agreement_rate("20261028")
    assert stats["total_calls"] == 0
    assert stats["unique_prompts"] == 0
    assert stats["measurable"] is False
    assert reproducibility.LLMLedger.model_usage("20261028") == {}

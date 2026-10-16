"""비교 작업자 검증 진입점 — 가짜 공급자·임시 원장·합성 봉투로 offer→실행→stop 을 한 번 돌린다.

네트워크·자격증명·운영 캐시를 쓰지 않는다(임시 디렉터리 원장, API 키 빈 값). 출력은 작업자 health JSON.
사용: venv/bin/python scripts/dev/comparison_worker_dryrun.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analytics import model_comparison_input as mci  # noqa: E402
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy  # noqa: E402
from src.analytics.model_comparison_runner import PairedComparisonRunner, SingleAttemptAdapter  # noqa: E402
from src.analytics.model_comparison_worker import ComparisonWorker  # noqa: E402
from src.utils.llm import GeminiClient, LLMConfig, LLMProvider, LLMResponse, OpenAIClient  # noqa: E402
from src.utils.llm_budget import MonthlyBudgetLedger  # noqa: E402

MODELS = {(LLMProvider.OPENAI, "light"): "gpt-5-mini", (LLMProvider.GEMINI, "light"): "gemini-3.1-flash-lite",
          (LLMProvider.OPENAI, "heavy"): "gpt-5.6-sol", (LLMProvider.GEMINI, "heavy"): "gemini-3.1-pro-preview"}
_EMPTY = LLMConfig(openai_api_key="", gemini_api_key="")


class _FakeOpenAI(OpenAIClient):
    async def complete(self, prompt, system="", model=None, **kw):
        text = "APPROVE" if kw.get("reasoning_effort") == "low" else "REJECT"
        return LLMResponse(text, model, LLMProvider.OPENAI, input_tokens=100, output_tokens=10,
                           requested_model=model, resolved_model=model, usage_known=True)


class _FakeGemini(GeminiClient):
    async def complete(self, prompt, system="", model=None, **kw):
        return LLMResponse("ACCEPT", model, LLMProvider.GEMINI, input_tokens=100, output_tokens=10,
                           requested_model=model, resolved_model=model, usage_known=True)


class _Models:
    def _get_model(self, provider, weight):
        return MODELS[(provider, weight)]


def main() -> int:
    from src.core.cross_validator import SINGLE_FALLBACK_TIMEOUT
    suffix = "\n이 매수 시그널을 승인하시겠습니까? YES 또는 NO로 답하고, 한 줄 사유를 적어주세요."
    controls = mci.operating_role_controls(_Models(), fallback_prompt_suffix=suffix,
                                           fallback_timeout=SINGLE_FALLBACK_TIMEOUT)
    manifest = {"experiment_id": "DRYRUN", "enabled": True, "fixed_at": "2026-01-01T00:00:00+00:00",
                "horizon_seconds": 900, "strict_on_disagreement": controls["strict_on_disagreement"],
                "empty_retries": controls["empty_retries"], "current": controls["bull"],
                "candidate": dict(controls["bull"], reasoning_effort="low"), "bear": controls["bear"],
                "fallback_chain": controls["fallback_chain"],
                "fallback_chain_timeout_seconds": controls["fallback_chain_timeout_seconds"]}
    with tempfile.TemporaryDirectory() as tmp:
        ledger = MonthlyBudgetLedger(Path(tmp) / "budget.db", 100, policy_enabled=True)
        guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(
            experiment_id="DRYRUN", daily_attempt_cap=20, allowed_models=tuple(sorted(set(MODELS.values()))),
            max_output_tokens=400, experiment_usd_ceiling='100', valid_from='2026-01-01', valid_until='2027-12-31',
            pairs_per_day=1, max_common_context_bytes=16384))
        runner = PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(
            openai=_FakeOpenAI(_EMPTY), gemini=_FakeGemini(_EMPTY)))
        worker = ComparisonWorker(runner, manifest=manifest, journal_dir=Path(tmp) / "journal").start()
        from src.analytics.llm_decision_record import new_id, now_kst
        at = now_kst().isoformat()
        envelope = mci.build_envelope(
            decision_id=new_id("dec"), signal_id="dryrun-signal", symbol="000000", strategy="dryrun", score=90.0,
            market_regime="sideways", common_context="<entry_facts>{\"dryrun\":true}</entry_facts>",
            role_controls=controls, neutral_sizing=None, producer=mci.producer_identity(**worker.producer_config()),
            input_available_at=at, handed_off_at=at)
        status = worker.offer(envelope)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker.health()["finished"] + worker.health()["incomplete"] < 1:
            time.sleep(0.01)
        health = worker.stop(20)
        from src.analytics.model_comparison_worker import read_offer_journal
        journal = sorted(worker.journal_dir.glob("offers-*.jsonl"))
        records = [r for path in journal for r in read_offer_journal(path, max_bytes=1 << 20)["records"]]
        print(json.dumps({"offer": status, "health": health, "ledger": ledger.status(),
                          "journal": {"paths": [str(p) for p in journal],
                                      "records": [(r["kind"], r["status"]) for r in records]}},
                         default=str, ensure_ascii=False, indent=1))
        ok = (status == "accepted" and health["finished"] == 1 and health["drained"] and not health["alive"]
              and health["journal_drained"] and [r["status"] for r in records] == ["accepted", "started", "finished"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""전문가 결측 입력 처리 테스트 (계획서 T9 요청 4 — C 담당, 2026-09-14)

F12 재현: KR시장 전문가는 수급·공매도 원자료가 전부 비어 있어도 score=0·confidence
0.4 언저리(=중립·꽤 확신)로 보고했다. 거시 전문가의 수동 오버라이드는 만료가 없어
5월에 적어 둔 값이 9월에도 그대로 반영됐다.

수정 후: ExpertOpinion에 data_status("ok"/"partial"/"insufficient")·missing_inputs를
추가하고, confidence 상한을 적용하며, orchestrator 집계에서 insufficient는 가중 0으로
제외한다. 수동 오버라이드는 valid_until(없으면 파일 mtime+14일) 만료 시 무시한다.

네트워크·DB·운영 캐시(~/.cache/ai_trader) 접근 없음 — 전부 monkeypatch/tmp_path.
실행: venv/bin/python -m pytest tests/test_expert_missing_inputs.py -q
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.experts.base import ExpertAgent  # noqa: E402
from src.experts.kr_market_expert import KRMarketExpert  # noqa: E402
from src.experts.macro_economist import MacroEconomist  # noqa: E402
from src.experts.orchestrator import ExpertOrchestrator  # noqa: E402
from src.experts.types import ExpertConfig, ExpertOpinion, RegimeBias  # noqa: E402
from src.utils.data_freshness import (  # noqa: E402
    CONFIDENCE_CAP_INSUFFICIENT,
    CONFIDENCE_CAP_PARTIAL,
)


def _mk_expert(perplexity_key: str = "") -> KRMarketExpert:
    return KRMarketExpert(ExpertConfig(), llm_manager=None, perplexity_key=perplexity_key)


# ─────────────────────────────────────────
# kr_market_expert — 수급·공매도 전부 결측
# ─────────────────────────────────────────
def test_all_inputs_missing_marks_insufficient(monkeypatch):
    expert = _mk_expert()

    async def _empty(*a, **kw):
        return {}

    monkeypatch.setattr(expert, "_fetch_investor_flows", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _empty)
    monkeypatch.setattr(expert, "_fetch_short_balance", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi200_futures", _empty)

    op = asyncio.run(expert._analyze())

    assert op.score == 0
    assert op.data_status == "insufficient"
    assert op.confidence <= CONFIDENCE_CAP_INSUFFICIENT
    assert "외국인/기관 수급" in op.missing_inputs
    assert "공매도 잔고" in op.missing_inputs


def test_partial_inputs_marks_partial_not_insufficient(monkeypatch):
    expert = _mk_expert()

    async def _flows():
        return {"foreign_5d_billion": 100.0}

    async def _short():
        return {"avg_short_ratio": 1.2}

    async def _empty(*a, **kw):
        return {}

    monkeypatch.setattr(expert, "_fetch_investor_flows", _flows)
    monkeypatch.setattr(expert, "_fetch_short_balance", _short)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi200_futures", _empty)

    op = asyncio.run(expert._analyze())

    assert op.data_status == "partial"
    assert op.confidence <= CONFIDENCE_CAP_PARTIAL


def test_full_inputs_marks_ok(monkeypatch):
    expert = _mk_expert()

    async def _flows():
        return {"foreign_5d_billion": 2500.0, "institutional_5d_billion": 200.0}

    async def _short():
        return {"avg_short_ratio": 1.5}

    async def _kospi():
        return {"vs_ma20_pct": 1.0, "chg_5d_pct": 0.5}

    async def _futures():
        return {"overnight_chg_pct": 0.3}

    monkeypatch.setattr(expert, "_fetch_investor_flows", _flows)
    monkeypatch.setattr(expert, "_fetch_short_balance", _short)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _kospi)
    monkeypatch.setattr(expert, "_fetch_kospi200_futures", _futures)

    op = asyncio.run(expert._analyze())

    assert op.data_status == "ok"
    assert op.missing_inputs == []


# ─────────────────────────────────────────
# orchestrator — insufficient는 가중 0으로 집계 제외
# ─────────────────────────────────────────
def _opinion(expert: str, score: int, confidence: float, data_status: str = "ok") -> ExpertOpinion:
    return ExpertOpinion(
        expert=expert,
        score=score,
        regime_bias=RegimeBias.BULL if score > 0 else RegimeBias.NEUTRAL,
        confidence=confidence,
        data_status=data_status,
    )


def test_aggregate_excludes_insufficient_expert():
    orch = ExpertOrchestrator(ExpertConfig())
    opinions = {
        # 결측인데도 옛 버그였다면 score=90이 집계에 크게 기여했을 것 — insufficient는 완전 제외되어야 함
        "kr_market_expert": _opinion("kr_market_expert", score=90, confidence=0.2, data_status="insufficient"),
        "macro_economist": _opinion("macro_economist", score=10, confidence=0.6, data_status="ok"),
    }
    agg = orch.aggregate_regime_score(opinions)
    # insufficient가 제외되면 macro_economist(10점) 단독 기여만 남는다 (0.3 스케일)
    assert agg == int(max(-30, min(30, 10 * 0.3)))


def test_data_status_summary_reports_insufficient_count():
    orch = ExpertOrchestrator(ExpertConfig())
    opinions = {
        "kr_market_expert": _opinion("kr_market_expert", score=0, confidence=0.2, data_status="insufficient"),
        "macro_economist": _opinion("macro_economist", score=10, confidence=0.6, data_status="ok"),
    }
    summary = orch.data_status_summary(opinions)
    assert summary["counts"]["insufficient"] == 1
    assert "kr_market_expert" in summary["insufficient_experts"]
    assert summary["note"] == "자료 부족 1명"


# ─────────────────────────────────────────
# macro_economist — 수동 오버라이드 valid_until 만료
# ─────────────────────────────────────────
def _mk_macro() -> MacroEconomist:
    return MacroEconomist(ExpertConfig(), llm_manager=None, perplexity_key="")


def test_expired_override_is_ignored(tmp_path, monkeypatch):
    override_path = tmp_path / "manual_macro_overrides.json"
    past = (datetime.now() - timedelta(days=1)).date().isoformat()
    override_path.write_text(
        json.dumps({"cpi_yoy": {"value": 9.9, "valid_until": past}}),
        encoding="utf-8",
    )
    macro = _mk_macro()
    monkeypatch.setattr(
        "src.experts.macro_economist._MANUAL_OVERRIDE_PATH", override_path
    )

    result = macro._load_manual_overrides()

    assert "cpi_yoy" not in result


def test_fresh_override_is_applied(tmp_path, monkeypatch):
    override_path = tmp_path / "manual_macro_overrides.json"
    future = (datetime.now() + timedelta(days=1)).date().isoformat()
    override_path.write_text(
        json.dumps({"cpi_yoy": {"value": 3.3, "valid_until": future}}),
        encoding="utf-8",
    )
    macro = _mk_macro()
    monkeypatch.setattr(
        "src.experts.macro_economist._MANUAL_OVERRIDE_PATH", override_path
    )

    result = macro._load_manual_overrides()

    assert result["cpi_yoy"] == 3.3


def test_legacy_flat_override_expires_after_default_ttl(tmp_path, monkeypatch):
    override_path = tmp_path / "manual_macro_overrides.json"
    # 구 스키마: {"fed_decision": "hold"} — valid_until 없음, 파일 mtime을 작성일로 취급
    override_path.write_text(json.dumps({"fed_decision": "hold"}), encoding="utf-8")
    old_mtime = (datetime.now() - timedelta(days=20)).timestamp()
    os.utime(override_path, (old_mtime, old_mtime))

    macro = _mk_macro()
    monkeypatch.setattr(
        "src.experts.macro_economist._MANUAL_OVERRIDE_PATH", override_path
    )

    result = macro._load_manual_overrides()

    # 작성(mtime) + 14일 기본 TTL을 넘겼으므로 무시되어야 함
    assert "fed_decision" not in result


def test_legacy_flat_override_within_default_ttl_is_applied(tmp_path, monkeypatch):
    override_path = tmp_path / "manual_macro_overrides.json"
    override_path.write_text(json.dumps({"fed_decision": "hold"}), encoding="utf-8")
    recent_mtime = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(override_path, (recent_mtime, recent_mtime))

    macro = _mk_macro()
    monkeypatch.setattr(
        "src.experts.macro_economist._MANUAL_OVERRIDE_PATH", override_path
    )

    result = macro._load_manual_overrides()

    assert result["fed_decision"] == "hold"

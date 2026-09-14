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
from typing import Optional

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
def _opinion(
    expert: str,
    score: int,
    confidence: float,
    data_status: str = "ok",
    bias: Optional[RegimeBias] = None,
) -> ExpertOpinion:
    if bias is None:
        bias = RegimeBias.BULL if score > 0 else RegimeBias.NEUTRAL
    return ExpertOpinion(
        expert=expert,
        score=score,
        regime_bias=bias,
        confidence=confidence,
        data_status=data_status,
    )


def _four_valid_market_experts(overrides: dict) -> dict:
    """MIN_VALID_EXPERTS(4) 커버리지를 충족하는 기본 시장체제 전문가 4명 +
    overrides로 덮어쓴 항목. 커버리지 게이트와 무관하게 다른 동작(제외 등)만
    단독으로 검증하고 싶을 때 사용."""
    base = {
        "macro_economist": _opinion("macro_economist", score=5, confidence=0.6, data_status="ok"),
        "kr_market_expert": _opinion("kr_market_expert", score=5, confidence=0.6, data_status="ok"),
        "us_market_expert": _opinion("us_market_expert", score=5, confidence=0.6, data_status="ok"),
        "kr_economy_expert": _opinion("kr_economy_expert", score=5, confidence=0.6, data_status="ok"),
    }
    base.update(overrides)
    return base


def test_aggregate_excludes_insufficient_expert():
    orch = ExpertOrchestrator(ExpertConfig())
    opinions = _four_valid_market_experts({
        # 결측인데도 옛 버그였다면 score=90이 집계에 크게 기여했을 것 — insufficient는 완전 제외되어야 함
        "kr_market_expert": _opinion("kr_market_expert", score=90, confidence=0.2, data_status="insufficient"),
    })
    agg_with_insufficient = orch.aggregate_regime_score(opinions)

    # insufficient 대신 같은 자리에 ok 의견이 있었다면 나왔을 값과 비교 — 결측이
    # 집계에 전혀 기여하지 않았음을 직접 확인한다(값만 우연히 같아지는 취약한 계산식
    # 비교 대신, "빠져도 결과가 바뀌지 않는다" 사실 자체를 검증).
    opinions_without = dict(opinions)
    del opinions_without["kr_market_expert"]
    agg_dropped_entirely = ExpertOrchestrator(ExpertConfig()).aggregate_regime_score(opinions_without)
    assert agg_with_insufficient == agg_dropped_entirely


def test_aggregate_regime_score_below_min_valid_experts_returns_zero():
    """MIN_VALID_EXPERTS(4) 미만이면 소수 표만으로 ±20에 도달하지 못하게 0(무보정)
    (2026-09-14 리뷰 advisory — 규칙#11 valid_n>=4 가드와 동일 값)."""
    orch = ExpertOrchestrator(ExpertConfig())
    opinions = {
        # 강한 BULL 2명뿐 — 옛 로직이면 큰 양수 보정이 나왔을 것
        "macro_economist": _opinion("macro_economist", score=90, confidence=0.85, data_status="ok"),
        "kr_market_expert": _opinion("kr_market_expert", score=90, confidence=0.85, data_status="ok"),
    }
    assert orch.aggregate_regime_score(opinions) == 0


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
    # 유효 시장체제 전문가 1명(macro_economist) < MIN_VALID_EXPERTS(4) → 커버리지 부족 표시
    assert summary["valid_n"] == 1
    assert summary["insufficient_coverage"] is True


def test_data_status_summary_coverage_ok_when_enough_valid_experts():
    orch = ExpertOrchestrator(ExpertConfig())
    opinions = _four_valid_market_experts({})
    summary = orch.data_status_summary(opinions)
    assert summary["valid_n"] == 4
    assert summary["insufficient_coverage"] is False


def test_aggregate_bias_excludes_insufficient_expert():
    """blocking: aggregate_bias도 aggregate_regime_score와 같은 기준으로
    insufficient를 제외해야 한다 — 그렇지 않으면 '모른다'가 NEUTRAL 표로
    집계돼 score(제외)와 bias(포함)가 같은 스냅샷에서 다른 상태를 말하게 된다."""
    orch = ExpertOrchestrator(ExpertConfig())
    opinions = {
        # insufficient NEUTRAL — 옛 로직이면 이 표가 다수를 차지해 bias가 neutral이 됨
        "kr_market_expert": _opinion(
            "kr_market_expert", score=0, confidence=0.2,
            data_status="insufficient", bias=RegimeBias.NEUTRAL,
        ),
        "macro_economist": _opinion(
            "macro_economist", score=50, confidence=0.15,
            data_status="ok", bias=RegimeBias.BULL,
        ),
    }
    assert orch.aggregate_bias(opinions) == RegimeBias.BULL
    # aggregate_regime_score도 같은 스냅샷에서 insufficient를 제외하므로(valid_n<4라
    # 커버리지 게이트로 0이 되지만) 두 집계가 같은 전제("모른다는 셈에 안 넣는다")를
    # 공유한다는 사실 자체를 회귀로 고정 — score만 제외/bias는 포함되는 상태 불일치 방지.
    assert orch.aggregate_regime_score(opinions) == 0  # valid_n=1 < MIN_VALID_EXPERTS


def test_partial_bear_expert_still_reaches_bear_consensus(monkeypatch):
    """advisory: CONFIDENCE_CAP_PARTIAL(0.7)이 bear_consensus 임계(0.7)와 같아
    공매도만 결측인 BEAR 전문가도 안전 방향(약세) 합의에는 여전히 도달해야 한다
    (부분 결측이 방어 합의를 막으면 안 됨)."""
    expert = _mk_expert()

    async def _flows():
        return {"foreign_5d_billion": -3000.0, "institutional_5d_billion": -1500.0}

    async def _kospi():
        return {"vs_ma20_pct": -4.0}

    async def _futures():
        return {"overnight_chg_pct": -2.5}

    async def _short():
        return {}  # 공매도만 결측 → partial

    async def _mkt(*a, **kw):
        return "외국인 매도 심화, 옵션 풋콜 비율 상승, 신용융자 잔고 감소"

    monkeypatch.setattr(expert, "_fetch_investor_flows", _flows)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _kospi)
    monkeypatch.setattr(expert, "_fetch_kospi200_futures", _futures)
    monkeypatch.setattr(expert, "_fetch_short_balance", _short)
    monkeypatch.setattr(expert, "_perplexity_search", _mkt)

    op = asyncio.run(expert._analyze())

    assert op.data_status == "partial"
    assert op.missing_inputs == ["공매도 잔고"]
    assert op.regime_bias == RegimeBias.BEAR
    assert len(op.key_findings) == 4  # "4-finding BEAR"
    assert op.confidence == CONFIDENCE_CAP_PARTIAL

    orch = ExpertOrchestrator(ExpertConfig())
    second_bear = ExpertOpinion(
        expert="macro_economist", score=-40,
        regime_bias=RegimeBias.BEAR, confidence=0.75, data_status="ok",
    )
    assert orch.bear_consensus(opinions={expert.name: op, "macro_economist": second_bear}) is True


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


def test_valid_until_date_only_is_valid_through_end_of_day(tmp_path, monkeypatch):
    """advisory: 날짜만 적은 valid_until("YYYY-MM-DD")은 자정(00:00)이 아니라
    그날 23:59:59까지 유효(포함)로 해석한다 — 당일 오전에 적어 둔 '오늘까지'
    값이 당일 낮 시각에 이미 만료된 것처럼 보이면 안 된다."""
    override_path = tmp_path / "manual_macro_overrides.json"
    today = datetime.now().date().isoformat()
    override_path.write_text(
        json.dumps({"cpi_yoy": {"value": 3.3, "valid_until": today}}),
        encoding="utf-8",
    )
    macro = _mk_macro()
    monkeypatch.setattr(
        "src.experts.macro_economist._MANUAL_OVERRIDE_PATH", override_path
    )

    result = macro._load_manual_overrides()

    assert result["cpi_yoy"] == 3.3


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

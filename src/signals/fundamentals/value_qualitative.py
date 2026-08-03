"""
밸류코어 LLM 정성 검증 — "왜 싼가?"의 판별 (2026-08-04)

설계: docs/strategies/value-growth-core-design.md §4.5
퀀트 스크리닝이 놓치는 3대 함정을 LLM으로 거른다:
  ① 사이클 피크 (이익 정점 = 저PER 착시)
  ② 이익의 질 (일회성 손익으로 부풀려진 ROE)
  ③ 구조적 디스카운트 vs 일시적 소외

- 모델: gpt-5.4 (STRATEGY_ANALYSIS 라인, fallback Gemini Pro) — 거래 복기와 동일 경로
- fail-closed: LLM 실패 시 해당 종목 신규 매수 보류 (기존 보유는 무관)
- 판정 4항목 × 0~25점, 종합 >= pass_score(기본 60) 통과
- 분기 리밸런스 시 후보 top ~10에만 호출 — 비용 무시 가능
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

RESULT_DIR = Path.home() / ".cache" / "ai_trader" / "value_qualitative"

_SYSTEM = (
    "당신은 한국 주식 가치투자 심사역이다. 퀀트 스크리닝을 통과한 저평가/성장 후보가 "
    "'싼 이유가 있는 함정'인지 '진짜 기회'인지 판별한다. 보수적으로 채점하라 — "
    "확신이 없으면 낮은 점수를 준다."
)

_PROMPT_TEMPLATE = """다음 종목이 가치투자 함정인지 판별하라.

## 종목: {name} ({symbol}) — {bucket} 버킷, 퀀트 점수 {score:.0f}점
## 재무 (DART {fiscal_year}사업연도, 연결 기준)
{financials}

## 밸류에이션
{valuation}

## 최근 뉴스/컨텍스트 (없으면 "정보 없음"으로 두고 알려진 지식으로 판단)
{context}

## 채점 (각 0~25점)
1. cycle: 사이클 위치 — 이익이 업황 정점 부근이면 낮게 (경기민감 업종 저PER은 의심)
2. earnings_quality: 이익의 질 — 일회성 손익/자산 처분 의심이면 낮게
3. discount_nature: 디스카운트 성격 — 구조적(지배구조·사양산업)이면 낮게, 일시적 소외면 높게
4. catalyst: 재평가 촉매 — 주주환원·업황 반전·실적 개선 등 가시적 촉매가 있으면 높게

JSON만 출력:
{{"cycle": 0-25, "earnings_quality": 0-25, "discount_nature": 0-25, "catalyst": 0-25,
"verdict": "pass|fail", "one_line": "판정 근거 한 문장"}}"""


@dataclass
class QualitativeVerdict:
    symbol: str
    total: float
    passed: bool
    detail: Dict[str, Any] = field(default_factory=dict)


class ValueQualitativeValidator:
    """후보 종목 LLM 정성 검증 (fail-closed)"""

    def __init__(self, llm_manager, config: Optional[Dict] = None):
        self._llm = llm_manager
        cfg = config or {}
        self._pass_score = float(cfg.get("llm_pass_score", 60.0))

    async def validate(self, candidates: List, news_context: Optional[Dict[str, str]] = None,
                       ) -> Dict[str, QualitativeVerdict]:
        """
        candidates: CoreCandidate 리스트 (indicators에 fin_snapshot/bucket 필요)
        news_context: {symbol: 뉴스 요약 텍스트} (없으면 LLM 자체 지식으로 판단)

        Returns: {symbol: QualitativeVerdict} — LLM 실패 종목은 passed=False (fail-closed)
        """
        if self._llm is None:
            logger.warning("[밸류코어 LLM] LLM 미설정 — 전 종목 보류 (fail-closed)")
            return {}

        from src.utils.llm import LLMTask

        results: Dict[str, QualitativeVerdict] = {}
        for c in candidates:
            snap = c.indicators.get("fin_snapshot")
            if snap is None:
                continue
            prompt = self._build_prompt(c, snap, (news_context or {}).get(c.symbol, ""))
            try:
                # max_tokens 1000: reasoning 모델은 사고 토큰이 한도에 포함될 수 있어
                # JSON 절단 → fail-closed 오탈락 방지를 위해 여유 확보 (리뷰 P2-7)
                parsed = await self._llm.complete_json(
                    prompt, task=LLMTask.STRATEGY_ANALYSIS,
                    system=_SYSTEM, max_tokens=1000,
                )
            except Exception as e:
                logger.warning(f"[밸류코어 LLM] {c.symbol} 호출 실패 — 보류: {e}")
                results[c.symbol] = QualitativeVerdict(c.symbol, 0.0, False,
                                                       {"error": str(e)})
                continue

            if not isinstance(parsed, dict) or "error" in parsed:
                logger.warning(f"[밸류코어 LLM] {c.symbol} 응답 오류 — 보류: {parsed}")
                results[c.symbol] = QualitativeVerdict(c.symbol, 0.0, False,
                                                       {"error": str(parsed)})
                continue

            total = 0.0
            for k in ("cycle", "earnings_quality", "discount_nature", "catalyst"):
                v = parsed.get(k)
                if isinstance(v, (int, float)):
                    total += max(0.0, min(25.0, float(v)))
            passed = total >= self._pass_score and parsed.get("verdict") != "fail"
            results[c.symbol] = QualitativeVerdict(c.symbol, total, passed, parsed)
            logger.info(
                f"[밸류코어 LLM] {c.symbol} {c.name}: {total:.0f}점 "
                f"{'통과' if passed else '탈락'} — {parsed.get('one_line', '')}"
            )

        self._persist(results)
        return results

    def _build_prompt(self, c, snap, context: str) -> str:
        def _f(v, suffix=""):
            return "N/A" if v is None else f"{v:,.1f}{suffix}"

        def _eok(v):
            return v / 1e8 if v is not None else None

        fin_lines = (
            f"- 매출 {_f(_eok(snap.revenue), '억')} "
            f"(YoY {_f(snap.revenue_yoy_pct, '%')}) · "
            f"영업이익 {_f(_eok(snap.op), '억')} "
            f"(YoY {_f(snap.op_yoy_pct, '%')})\n"
            f"- 순이익 {_f(_eok(snap.net), '억')} · "
            f"ROE {_f(snap.roe_pct, '%')} · 부채비율 {_f(snap.debt_ratio_pct, '%')} · "
            f"연속 흑자 {snap.profit_years}년 · "
            f"영업이익률 개선 {_f(snap.op_margin_improve_pp, 'pp')}"
        )
        ind = c.indicators
        val_lines = (
            f"- PER {_f(ind.get('per'))} · PBR {_f(ind.get('pbr'))} · "
            f"업종 {ind.get('sector') or 'N/A'} · "
            f"60일 수익률 {_f(ind.get('change_60d'), '%')}"
        )
        return _PROMPT_TEMPLATE.format(
            name=c.name, symbol=c.symbol,
            bucket=ind.get("bucket", "?"), score=c.score,
            fiscal_year=snap.fiscal_year,
            financials=fin_lines, valuation=val_lines,
            context=context or "정보 없음",
        )

    def _persist(self, results: Dict[str, QualitativeVerdict]) -> None:
        """판정 기록 저장 — 다음 분기 리밸런스 컨텍스트/감사 추적용"""
        if not results:
            return
        try:
            RESULT_DIR.mkdir(parents=True, exist_ok=True)
            path = RESULT_DIR / f"verdicts_{datetime.now():%Y%m%d}.json"
            path.write_text(json.dumps(
                {s: {"total": v.total, "passed": v.passed, "detail": v.detail}
                 for s, v in results.items()},
                ensure_ascii=False, indent=1,
            ))
        except Exception as e:
            logger.debug(f"[밸류코어 LLM] 판정 저장 실패: {e}")

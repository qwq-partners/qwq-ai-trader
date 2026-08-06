"""sector-council — 섹터별 현황 조사 전문가 (2026-08-07 신규, 사용자 요청)

반도체·IT·자동차·2차전지·바이오·건설·조선·철강·은행·증권·보험·화학·방산
13개 섹터를 **단일 전문가가 내부 병렬 분석**한다. 섹터별 전문가를 여러 명
상시 등록하면 시장 체제 가중평균이 희석되므로, 이 전문가는:

  - 시장 체제 집계(aggregate_regime_score·aggregate_bias·bear_consensus)에서 **제외**
  - 종목 단위 판단(크로스검증 규칙#12 shadow)과 브리핑 섹터 섹션에만 사용

신호 구성 (섹터당):
  - 정량 60%: 섹터 ETF 20일 수익률 (SectorMomentumProvider 재사용 — KIS/30분 파일캐시)
  - 정성 40%: Perplexity 1회 호출로 전 섹터 뉴스 동향 JSON (실패 시 정량만, 가중 0.8)

소비처:
  - cross_validator 규칙#12: `sector_of_cached()` + `sector_score()` (동기 — validate가 sync)
  - kr_scheduler 브리핑: raw_evidence["sector_scores"] 상위/하위 표시
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from loguru import logger

from src.data.providers.sector_momentum import (
    SECTOR_ETF_MAP,
    SectorMomentumProvider,
)

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias

# 강한 약세 임계 — 규칙#12 shadow 감지 기준 (관측 후 조정)
SECTOR_BEAR_THRESHOLD = -40
# 정량(ETF 모멘텀%) → 점수 배율: 20일 ±10% ≈ ±100점
_QUANT_SCALE = 10.0


class SectorCouncilExpert(ExpertAgent):
    name = "sector_council"
    refresh_minutes = 180              # 참고용 메타 — 실제 TTL은 _build_opinion(valid_hours=3)
    cost_per_call_usd = 0.01           # Perplexity 1회

    def __init__(self, config, llm_manager=None, perplexity_key: str = ""):
        super().__init__(config, llm_manager, perplexity_key)
        # broker 없이 생성 — ETF 모멘텀은 스크리너가 갱신하는 30분 파일캐시를 공유.
        # 장전(캐시 만료) 대비 run_trader에서 set_broker()로 KIS 브로커 주입.
        self._provider = SectorMomentumProvider(broker=None)

    def set_broker(self, broker) -> None:
        """KIS 브로커 주입 (ETF 모멘텀 직접 조회용 — 장전 캐시 만료 대비)"""
        self._provider._broker = broker

    # ─────────────────────────────────────────
    # 분석
    # ─────────────────────────────────────────
    async def _analyze(self) -> ExpertOpinion:
        momentum = await self._provider.get_all_sector_momentum()  # {섹터: 20d%}
        qual = await self._fetch_qual_signals()                    # {섹터: {score, reason}}

        scores: Dict[str, Dict[str, Any]] = {}
        for sector in SECTOR_ETF_MAP:
            mom = momentum.get(sector)
            quant = (
                max(-100.0, min(100.0, mom * _QUANT_SCALE))
                if isinstance(mom, (int, float)) else None
            )
            q_entry = qual.get(sector) or {}
            q_score = q_entry.get("score")
            if not isinstance(q_score, (int, float)):
                q_score = None

            if quant is None and q_score is None:
                continue
            if quant is None:
                final = q_score * 0.6          # 정성 단독 — 보수적 축소
            elif q_score is None:
                final = quant * 0.8            # 정량 단독
            else:
                final = quant * 0.6 + q_score * 0.4

            scores[sector] = {
                "score": int(max(-100, min(100, final))),
                "momentum": round(mom, 2) if isinstance(mom, (int, float)) else None,
                "reason": str(q_entry.get("reason", ""))[:80],
                # 정량 축 부재 = 뉴스 단독 점수 — 소비처에서 낮게 신뢰해야 함
                # (2026-08-07 반도체 +49 사례: ETF 캐시 부재로 뉴스 반등론만 반영)
                "qual_only": quant is None,
            }

        if not scores:
            return self._build_opinion(
                score=0, bias=RegimeBias.NEUTRAL, confidence=0.2,
                findings=["섹터 데이터 수집 실패 (ETF 캐시·Perplexity 모두 부재)"],
                sectors=[], raw={"sector_scores": {}, "qual_ok": bool(qual)},
                valid_hours=1,
            )

        # 전문가 레벨 점수 = 섹터 평균 (breadth) — 체제 집계에는 안 쓰이고 표시용
        avg = sum(v["score"] for v in scores.values()) / len(scores)
        bias = (
            RegimeBias.BULL if avg > 15
            else RegimeBias.BEAR if avg < -15
            else RegimeBias.NEUTRAL
        )
        confidence = min(0.85, 0.30 + 0.04 * len(scores) + (0.1 if qual else 0.0))
        # 정량(ETF 모멘텀) 전멸 = 뉴스 단독 분석 — 신뢰도 상한 0.5 (2026-08-07)
        quant_n = sum(1 for v in scores.values() if v["momentum"] is not None)
        if quant_n == 0:
            confidence = min(confidence, 0.5)

        ranked = sorted(scores.items(), key=lambda x: -x[1]["score"])
        findings: List[str] = []
        for sector, v in ranked[:3]:
            if v["score"] >= 20:
                findings.append(self._fmt_finding("🟢", sector, v))
        for sector, v in ranked[::-1][:3]:
            if v["score"] <= -20:
                findings.append(self._fmt_finding("🔴", sector, v))
        if not findings:
            findings = [f"섹터 전반 중립 ({len(scores)}개 분석, 평균 {avg:+.0f})"]

        return self._build_opinion(
            score=int(avg),
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=[s for s, v in scores.items() if abs(v["score"]) >= 30],
            raw={"sector_scores": scores, "qual_ok": bool(qual)},
            valid_hours=3,
        )

    @staticmethod
    def _fmt_finding(icon: str, sector: str, v: Dict[str, Any]) -> str:
        mom = v.get("momentum")
        mom_txt = (
            f", ETF {mom:+.1f}%" if isinstance(mom, (int, float)) else ", 뉴스 단독"
        )
        reason = v.get("reason") or ""
        reason_txt = f" — {reason[:40]}" if reason else ""
        return f"{icon} {sector} {v['score']:+d}{mom_txt}{reason_txt}"

    # ─────────────────────────────────────────
    # 정성 신호 — Perplexity 1회로 전 섹터
    # ─────────────────────────────────────────
    async def _fetch_qual_signals(self) -> Dict[str, Dict[str, Any]]:
        sectors = list(SECTOR_ETF_MAP.keys())
        prompt = (
            "한국 주식시장의 다음 섹터별 최근 1주일 동향(뉴스·수급·업황)을 평가해 "
            "JSON만 출력해줘. 형식: {\"섹터명\": {\"score\": -100~100 정수, "
            "\"reason\": \"한 줄 근거\"}}. 다른 텍스트 없이 JSON만. "
            f"섹터: {', '.join(sectors)}"
        )
        text = await self._perplexity_search(prompt, max_tokens=900)
        if not text:
            return {}
        # LLM JSON은 통짜 json.loads가 자주 깨진다 (내부 따옴표, 각주 마커 등)
        # → 섹터별 정규식 추출로 부분 복구 (한 섹터가 깨져도 나머지는 살림)
        out: Dict[str, Dict[str, Any]] = {}
        for sector in SECTOR_ETF_MAP:
            m = re.search(
                rf'"{re.escape(sector)}"\s*:\s*\{{([^{{}}]*)\}}', text
            )
            if not m:
                continue
            body = m.group(1)
            sm = re.search(r'"score"\s*:\s*(-?\d+)', body)
            if not sm:
                continue
            rm = re.search(r'"reason"\s*:\s*"([^"]*)', body)
            out[sector] = {
                "score": max(-100, min(100, int(sm.group(1)))),
                "reason": (rm.group(1) if rm else "")[:80],
            }
        if not out:
            logger.debug(f"[{self.name}] 정성 신호 파싱 실패 (응답 {len(text)}자)")
        return out

    # ─────────────────────────────────────────
    # 소비처용 동기 헬퍼 (cross_validator.validate는 sync)
    # ─────────────────────────────────────────
    def sector_of_cached(self, symbol: str, name: str = "") -> Optional[str]:
        """종목 → 섹터 (provider의 동기 메모이즈 조회에 위임 — 네트워크 없음)"""
        return self._provider.sector_of_cached(symbol, name)

    def sector_score(self, sector: str) -> Optional[int]:
        """캐시된 의견에서 섹터 점수 반환 (만료·부재 시 None)"""
        op = self._cached_opinion
        if op is None or not op.is_valid:
            return None
        entry = (op.raw_evidence or {}).get("sector_scores", {}).get(sector)
        return entry.get("score") if isinstance(entry, dict) else None

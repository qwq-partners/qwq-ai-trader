"""정규화 교훈 저장소 — Wiki ACE 격상 (2026-08-10, 하네스 설계 Phase 2)

Trade Wiki의 자유 문장 교훈은 안정 ID·dedup이 없어 중복 축적되고 200줄
제한에 잘려나갔다 (brevity bias). 이 스토어가 **진실 원천**이 되고,
Markdown 위키는 사람이 읽는 projection으로 유지된다.

ACE 역할 분담:
  Generator — position_ledger._finalize가 확정 포지션을 결정론 분류해 후보 등록
  Reflector — 토요일 weakness_miner 집계로 지지/반례 갱신 (단일 사례 과잉 일반화 억제)
  Curator   — 상태 전이 candidate(n<5) → active(n≥5) → deprecated(60일 무보강) 및
              결정론 dedup (identifier 병합 — LLM은 의미 병합의 진실 원천이 될 수 없음)

identifier = pattern|mechanism|strategy|regime|v1 (정규화 키 — 종목명·자유문장 금지)
파일: ~/.cache/ai_trader/wiki/lessons.json (identifier 키 dict, 원자적 재작성)

실패는 절대 매매·위키를 막지 않는다 (전부 삼킴+경고).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

_STORE_PATH = Path.home() / ".cache" / "ai_trader" / "wiki" / "lessons.json"

SCHEMA_VERSION = "v1"
ACTIVE_MIN_SAMPLES = 5
DEPRECATE_AFTER_DAYS = 60

# 패턴별 사람이 읽는 설명 템플릿 (LLM 아닌 결정론 — 재현 가능)
_PATTERN_DESC = {
    "exit_profit_giveback": "MFE +3% 이상 도달 후 최종 손실 — 청산 정책이 이익을 반납하는 패턴",
    "fast_stop_cluster": "보유 2일 이내 손절 반복 — 진입 신호 품질 의심",
    "slow_bleed": "10일 이상 보유 · MFE 1% 미만 · 손실 — 추세 없는 진입 방치",
    "data_quality": "원장 수량 불일치 (unreliable) — 데이터 파이프라인 점검 필요",
}


def make_identifier(pattern: str, mechanism: str, strategy: str, regime: str) -> str:
    return f"{pattern}|{mechanism}|{strategy or 'unknown'}|{regime or 'unknown'}|{SCHEMA_VERSION}"


class LessonStore:
    def __init__(self, path: Path = _STORE_PATH):
        self._path = path
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[교훈스토어] 로드 실패 (빈 상태): {e}")
        return {}

    def _save(self) -> None:
        # 통합 리뷰 P0-3 (Codex): temp+os.replace 원자 교체 — 저장 도중 프로세스
        # 종료 시 잘린 JSON이 진실 원천을 파손하는 것을 방지
        try:
            import os
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"[교훈스토어] 저장 실패: {e}")

    # ── Generator/Reflector 공용 upsert (결정론 dedup) ──────────
    def upsert(self, pattern: str, mechanism: str, strategy: str, regime: str,
               evidence_ref: str = "", supporting: bool = True,
               sample_size: Optional[int] = None,
               effect_size: Optional[float] = None) -> None:
        """동일 identifier면 근거·통계만 병합 (설명 재작성 없음 — context collapse 방지)"""
        try:
            key = make_identifier(pattern, mechanism, strategy, regime)
            now = datetime.now().isoformat()
            rec = self._data.get(key)
            if rec is None:
                rec = {
                    "identifier": key,
                    "description": _PATTERN_DESC.get(pattern, pattern),
                    "scope": {"strategy": strategy, "regime": regime},
                    "evidence": {"supporting": [], "contradicting": []},
                    "verifier": {"pattern_id": pattern, "sample_size": 0,
                                 "effect_size": None},
                    "confidence": 0.3,
                    "status": "candidate",
                    "first_seen": now,
                }
                self._data[key] = rec
            side = "supporting" if supporting else "contradicting"
            if evidence_ref and evidence_ref not in rec["evidence"][side]:
                rec["evidence"][side].append(evidence_ref)
                rec["evidence"][side] = rec["evidence"][side][-50:]  # 롤링
            if sample_size is not None:
                rec["verifier"]["sample_size"] = int(sample_size)
            if effect_size is not None:
                rec["verifier"]["effect_size"] = float(effect_size)
            n_sup = len(rec["evidence"]["supporting"])
            n_con = len(rec["evidence"]["contradicting"])
            rec["confidence"] = round(
                min(0.9, 0.3 + 0.06 * n_sup - 0.1 * n_con), 2
            )
            rec["last_seen"] = now
            self._save()
        except Exception as e:
            logger.warning(f"[교훈스토어] upsert 실패 (무시): {e}")

    # ── Curator: 주간 상태 전이 ─────────────────────────────────
    def curate(self) -> Dict[str, int]:
        """candidate→active(표본 5+), 60일 무보강→deprecated. 삭제는 없음 (반례 보존)"""
        moved = {"activated": 0, "deprecated": 0}
        try:
            cutoff = (datetime.now() - timedelta(days=DEPRECATE_AFTER_DAYS)).isoformat()
            for rec in self._data.values():
                n = int(rec.get("verifier", {}).get("sample_size", 0) or 0)
                status = rec.get("status", "candidate")
                if status == "candidate" and n >= ACTIVE_MIN_SAMPLES:
                    rec["status"] = "active"
                    moved["activated"] += 1
                elif status == "active" and rec.get("last_seen", "") < cutoff:
                    rec["status"] = "deprecated"
                    moved["deprecated"] += 1
            self._save()
        except Exception as e:
            logger.warning(f"[교훈스토어] curate 실패: {e}")
        return moved

    # ── 소비: 크로스검증/위키 컨텍스트 ──────────────────────────
    def format_context(self, strategy: str = "", regime: str = "",
                       max_items: int = 3) -> str:
        """active 교훈 요약 문자열 ('' = 해당 없음). LLM 컨텍스트용"""
        try:
            hits = []
            for rec in self._data.values():
                if rec.get("status") != "active":
                    continue
                scope = rec.get("scope", {})
                if strategy and scope.get("strategy") not in (strategy, "unknown"):
                    continue
                if regime and scope.get("regime") not in (regime, "unknown"):
                    continue
                hits.append(rec)
            hits.sort(key=lambda r: -float(r.get("confidence", 0)))
            return "\n".join(
                f"⚡ {r['description']} "
                f"(표본 {r['verifier'].get('sample_size', 0)}, "
                f"신뢰 {r.get('confidence', 0):.1f})"
                for r in hits[:max_items]
            )
        except Exception:
            return ""


_store: Optional[LessonStore] = None


def get_lesson_store() -> LessonStore:
    global _store
    if _store is None:
        _store = LessonStore()
    return _store

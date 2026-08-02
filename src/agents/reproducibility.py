"""
LLM 재현성 원장 — 매매 판단에 쓰인 모든 LLM 호출을 재현 가능하게 남긴다.

■ 왜 필요한가

토론 결과는 Trader 점수를 `+20/-40` 바꾸고 매수 여부를 가른다.
그런데 LLM은 같은 입력에도 다른 답을 낼 수 있다. 기록이 없으면:

    - "그날 왜 이 종목을 샀나"를 사후에 설명할 수 없다
    - 모델을 교체했을 때 전후 성과를 같은 전략으로 비교할 수 없다
    - shadow 성과가 좋았을 때 그게 재현 가능한 실력인지 운인지 구분할 수 없다

승격 기준의 "재현성 80%"를 측정하려면 **무엇을 물었고 무엇이 돌아왔는지**가
전부 남아 있어야 한다. 요약본으로는 재실행 비교가 불가능하다.

■ 무엇을 남기는가

    프롬프트 전문 + 해시 / 응답 전문 / 실제 응답 모델 ID / provider /
    호출 파라미터 / 입력 데이터 스냅샷 해시 / 판정 / 지연

프롬프트 해시를 따로 두는 이유는, 재실행 시 **입력이 동일한지**를
문자열 전체 비교 없이 확인하기 위해서다. 입력이 달라졌다면 판정이 달라도
그건 비재현이 아니다.

■ append-only

원장은 절대 덮어쓰지 않는다. 판단의 근거를 사후에 고칠 수 있으면 감사 기록이 아니다.
위치: `~/.cache/ai_trader/llm_ledger/llm_YYYYMMDD.jsonl`
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

LEDGER_DIR = Path.home() / ".cache" / "ai_trader" / "llm_ledger"

# 원장에 남기는 본문 길이 상한 (재현 비교가 목적이라 충분히 길게 둔다)
MAX_TEXT = 8000


def sha256_short(text: str, length: int = 16) -> str:
    """해시 앞부분 — 로그·비교용으로 짧게"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:length]


@dataclass
class LLMCallRecord:
    """LLM 호출 1건의 재현 정보"""

    call_id: str
    ts: str
    symbol: str
    role: str                       # "bull" | "bear" | 기타
    round_no: int

    provider: str = ""
    model: str = ""                 # **실제 응답 모델 ID** (요청 모델과 다를 수 있다)
    params: Dict[str, Any] = field(default_factory=dict)

    prompt_hash: str = ""
    prompt: str = ""
    system: str = ""
    response: str = ""
    verdict: Optional[bool] = None

    # 이 호출이 본 입력 데이터(분석가 보고서)의 스냅샷 해시.
    # 재실행 시 입력이 같은지 확인하는 기준 — 입력이 달라졌으면 판정 차이는 비재현이 아니다.
    input_snapshot_hash: str = ""

    latency_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("prompt", "response", "system"):
            if d.get(k):
                d[k] = d[k][:MAX_TEXT]
        return d


class LLMLedger:
    """append-only LLM 호출 원장"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.stats = {"recorded": 0, "failed": 0}
        if enabled:
            LEDGER_DIR.mkdir(parents=True, exist_ok=True)

    # ── 기록 ───────────────────────────────────────────────
    def record(
        self,
        *,
        symbol: str,
        role: str,
        round_no: int,
        prompt: str,
        system: str,
        response: str,
        provider: str,
        model: str,
        params: Optional[Dict[str, Any]] = None,
        verdict: Optional[bool] = None,
        input_snapshot: str = "",
        latency_ms: float = 0.0,
        success: bool = True,
        error: Optional[str] = None,
    ) -> Optional[LLMCallRecord]:
        """
        호출 1건을 원장에 남긴다.

        실패해도 매매를 막지 않는다 — 기록은 부가 기능이므로 예외를 삼킨다.
        """
        if not self.enabled:
            return None

        rec = LLMCallRecord(
            call_id=uuid.uuid4().hex[:12],
            ts=datetime.now().isoformat(timespec="seconds"),
            symbol=symbol, role=role, round_no=round_no,
            provider=provider, model=model,
            params=dict(params or {}),
            prompt_hash=sha256_short(f"{system}\n{prompt}"),
            prompt=prompt, system=system, response=response,
            verdict=verdict,
            input_snapshot_hash=sha256_short(input_snapshot) if input_snapshot else "",
            latency_ms=latency_ms, success=success, error=error,
        )

        try:
            path = LEDGER_DIR / f"llm_{datetime.now():%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False, default=str) + "\n")
            self.stats["recorded"] += 1
        except Exception as e:
            self.stats["failed"] += 1
            logger.warning(f"[재현성원장] 기록 실패 ({symbol}/{role}): {e}")

        return rec

    # ── 조회·분석 ──────────────────────────────────────────
    @staticmethod
    def load(day: Optional[str] = None) -> List[Dict[str, Any]]:
        """하루치 원장 로드 (day: YYYYMMDD, 없으면 오늘)"""
        d = day or f"{datetime.now():%Y%m%d}"
        path = LEDGER_DIR / f"llm_{d}.jsonl"
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as e:
            logger.warning(f"[재현성원장] 읽기 실패: {e}")
        return rows

    @staticmethod
    def agreement_rate(day: Optional[str] = None) -> Dict[str, Any]:
        """
        재현성 지표 — **같은 입력(prompt_hash)** 에 대해 판정이 얼마나 일치했는지.

        승격 기준의 "동일 입력 재실행 판정 일치율 80% 이상"을 측정한다.
        입력이 다르면 판정이 달라도 비재현이 아니므로, prompt_hash로 묶어서 본다.
        """
        rows = LLMLedger.load(day)
        groups: Dict[str, List[Optional[bool]]] = {}
        for r in rows:
            h = r.get("prompt_hash")
            if not h or not r.get("success"):
                continue
            groups.setdefault(h, []).append(r.get("verdict"))

        repeated = {h: v for h, v in groups.items() if len(v) >= 2}
        if not repeated:
            return {
                "measurable": False,
                "reason": "동일 입력 반복 호출 없음 — 재현성 측정 불가",
                "total_calls": len(rows),
                "unique_prompts": len(groups),
            }

        consistent = sum(1 for v in repeated.values() if len(set(v)) == 1)
        return {
            "measurable": True,
            "agreement_rate": round(consistent / len(repeated) * 100, 1),
            "repeated_prompts": len(repeated),
            "consistent_prompts": consistent,
            "total_calls": len(rows),
            "unique_prompts": len(groups),
        }

    @staticmethod
    def model_usage(day: Optional[str] = None) -> Dict[str, int]:
        """실제 응답 모델별 호출 수 — 모델 교체·폴백 발생을 추적한다"""
        rows = LLMLedger.load(day)
        usage: Dict[str, int] = {}
        for r in rows:
            key = f"{r.get('provider','?')}/{r.get('model','?')}"
            usage[key] = usage.get(key, 0) + 1
        return usage

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


_ledger: Optional[LLMLedger] = None


def get_ledger(enabled: bool = True) -> LLMLedger:
    global _ledger
    if _ledger is None:
        _ledger = LLMLedger(enabled=enabled)
    return _ledger


def snapshot_reports(reports) -> str:
    """
    분석가 보고서를 재현 비교용 문자열로 직렬화한다.

    나이(age_minutes)는 호출 시각에 따라 계속 변하므로 **제외한다** —
    포함하면 같은 데이터도 매번 다른 해시가 나와 재현성 비교가 불가능해진다.
    """
    parts = []
    for r in reports or []:
        if not getattr(r, "ok", False):
            continue
        parts.append(f"{r.kind.value}:{r.score}:{round(r.confidence, 3)}:{r.summary}")
    return "|".join(sorted(parts))

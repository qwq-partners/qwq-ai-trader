"""Counterfactual 추적기 (2026-08-08 — Codex 전략 리뷰 과제①/③)

AI 게이트가 차단·감지한 매수 후보의 "만약 거래했다면" 후속 수익률을 추적한다.
분기 말에 각 규칙/전문가가 실제로 얼마를 지켰는지(피한 손실) 또는 놓쳤는지
(놓친 수익) 증명하는 기반 데이터.

소스 (기록만 되고 후속 추적이 없던 shadow 로그):
  ~/.cache/ai_trader/rule11_shadow_log.jsonl  — 전문가 BEAR 합의 감지
  ~/.cache/ai_trader/rule12_shadow_log.jsonl  — 섹터 카운슬 약세 감지

산출:
  counterfactual_state.json — {key: {symbol, source, date, entry_px, r1, r5, r20}}
  · 가상 진입가 = 감지일(이후 첫 거래일) 종가, rN = N세션 후 종가 대비 수익률(%)
  · 갱신은 저녁 품질검증 잡에서 1일 1회 (KIS get_daily_prices — 오래된 순 반환)

해석: 차단 후보의 rN이 음수(-) = 게이트가 손실을 막았다 (정확한 차단).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_CACHE_DIR = Path.home() / ".cache" / "ai_trader"
_STATE_PATH = _CACHE_DIR / "counterfactual_state.json"
_SOURCES = {
    "rule11": _CACHE_DIR / "rule11_shadow_log.jsonl",
    "rule12": _CACHE_DIR / "rule12_shadow_log.jsonl",
}
_HORIZONS = (("r1", 1), ("r5", 5), ("r20", 20))


class CounterfactualTracker:
    """shadow 차단 후보의 가상 성과 추적"""

    def __init__(self):
        self._state: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            if _STATE_PATH.exists():
                return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[CF추적] 상태 로드 실패: {e}")
        return {}

    def _save(self) -> None:
        try:
            from ..utils.atomic_io import atomic_write_json
            atomic_write_json(_STATE_PATH, self._state)
        except Exception as e:
            logger.warning(f"[CF추적] 상태 저장 실패: {e}")

    # ── 수집 ───────────────────────────────────────────────
    def _ingest_sources(self) -> int:
        """shadow 로그에서 신규 감지 건을 상태에 등록 (일일 dedup 키)"""
        added = 0
        for source, path in _SOURCES.items():
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                        day = str(r.get("timestamp", ""))[:10]
                        sym = r.get("symbol", "")
                        if not day or not sym:
                            continue
                        key = f"{source}|{sym}|{day}"
                        if key in self._state:
                            continue
                        self._state[key] = {
                            "symbol": sym,
                            "source": source,
                            "date": day,
                            "sector": r.get("sector"),
                            "entry_px": None,
                            "r1": None, "r5": None, "r20": None,
                        }
                        added += 1
                    except (json.JSONDecodeError, TypeError):
                        continue
            except Exception as e:
                logger.debug(f"[CF추적] {source} 읽기 실패: {e}")
        return added

    # ── 갱신 ───────────────────────────────────────────────
    async def update(self, broker) -> Dict[str, int]:
        """미완성 항목의 가상 진입가·후속 수익률 채움 (일일 1회 호출)"""
        added = self._ingest_sources()
        filled = 0
        pending = [
            (k, v) for k, v in self._state.items()
            if v.get("r20") is None  # r20까지 완성되면 종료
        ]
        for key, entry in pending[:50]:  # 호출당 상한 (API 보호)
            try:
                prices = await broker.get_daily_prices(entry["symbol"], days=45)
                if not prices or len(prices) < 2:
                    continue
                # get_daily_prices는 오래된 순 — (날짜, 종가) 리스트 구성
                rows = []
                for bar in prices:
                    d = str(bar.get("date", "") or bar.get("stck_bsop_date", ""))
                    c = float(bar.get("close", 0) or bar.get("stck_clpr", 0) or 0)
                    if len(d) == 8 and c > 0:
                        rows.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", c))
                # 감지일 이후 첫 거래일 = 기준점
                idx0 = next(
                    (i for i, (d, _) in enumerate(rows) if d >= entry["date"]), None
                )
                if idx0 is None:
                    continue
                if entry.get("entry_px") is None:
                    entry["entry_px"] = rows[idx0][1]
                base = entry["entry_px"]
                for field, n in _HORIZONS:
                    if entry.get(field) is None and idx0 + n < len(rows):
                        entry[field] = round(
                            (rows[idx0 + n][1] - base) / base * 100, 2
                        )
                        filled += 1
            except Exception as e:
                logger.debug(f"[CF추적] {key} 갱신 실패: {e}")
        if added or filled:
            self._save()
            logger.info(f"[CF추적] 신규 {added}건 등록, {filled}개 수익률 채움")
        return {"added": added, "filled": filled}

    # ── 요약 (주간 성적표) ──────────────────────────────────
    def summary(self) -> str:
        """소스별 차단 정확도 요약 — rN < 0 이면 '손실 회피 적중'"""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for v in self._state.values():
            if v.get("r5") is not None:
                groups.setdefault(v["source"], []).append(v)
        if not groups:
            return "counterfactual 표본 없음 (r5 완성 건 0)"
        lines = []
        for source, items in sorted(groups.items()):
            n = len(items)
            avoided = sum(1 for i in items if (i.get("r5") or 0) < 0)
            avg_r5 = sum((i.get("r5") or 0) for i in items) / n
            r20_items = [i for i in items if i.get("r20") is not None]
            avg_r20 = (
                sum(i["r20"] for i in r20_items) / len(r20_items)
                if r20_items else None
            )
            line = (
                f"{source}: {n}건 | 5일 뒤 하락 {avoided}건 ({avoided/n*100:.0f}% 적중) "
                f"| 평균 r5 {avg_r5:+.1f}%"
            )
            if avg_r20 is not None:
                line += f" | 평균 r20 {avg_r20:+.1f}%"
            lines.append(line)
        return "\n".join(lines)


_tracker: Optional[CounterfactualTracker] = None


def get_counterfactual_tracker() -> CounterfactualTracker:
    global _tracker
    if _tracker is None:
        _tracker = CounterfactualTracker()
    return _tracker

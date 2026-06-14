"""전문가 의견 파일 영속화 — ~/.cache/ai_trader/experts/"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from .types import ExpertOpinion


_STORE_DIR = Path.home() / ".cache" / "ai_trader" / "experts"


def _ensure_dir() -> Path:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORE_DIR


def save_opinion(opinion: ExpertOpinion) -> None:
    """의견을 일별 파일에 append"""
    try:
        path = _ensure_dir() / f"{opinion.expert}_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(opinion.to_dict(), ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[opinion_store] 저장 실패 ({opinion.expert}): {e}")


def load_latest(expert: str, date: Optional[str] = None) -> Optional[ExpertOpinion]:
    """해당 날짜의 마지막 의견 로드"""
    try:
        d = date or datetime.now().strftime("%Y-%m-%d")
        path = _STORE_DIR / f"{expert}_{d}.jsonl"
        if not path.exists():
            return None
        last_line = ""
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_line = line
        if not last_line:
            return None
        return ExpertOpinion.from_dict(json.loads(last_line))
    except Exception as e:
        logger.warning(f"[opinion_store] 로드 실패 ({expert}): {e}")
        return None


def load_all_today() -> Dict[str, ExpertOpinion]:
    """오늘 자 모든 전문가 의견 (각 전문가의 마지막 것)"""
    result: Dict[str, ExpertOpinion] = {}
    if not _STORE_DIR.exists():
        return result
    today = datetime.now().strftime("%Y-%m-%d")
    for path in _STORE_DIR.glob(f"*_{today}.jsonl"):
        expert = path.stem.rsplit("_", 1)[0]
        op = load_latest(expert, today)
        if op is not None:
            result[expert] = op
    return result


def list_recent(expert: str, days: int = 7) -> List[ExpertOpinion]:
    """최근 N일 의견 (시간순)"""
    from datetime import timedelta
    out: List[ExpertOpinion] = []
    today = datetime.now().date()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        path = _STORE_DIR / f"{expert}_{d}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(ExpertOpinion.from_dict(json.loads(line)))
        except Exception:
            continue
    out.sort(key=lambda o: o.issued_at)
    return out

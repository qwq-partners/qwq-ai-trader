"""전문가 위키 ingest — ~/.cache/ai_trader/wiki/experts/

각 전문가가 일별 의견을 마크다운으로 누적한다.
주간 토요일 lint + 위키 페이지 컨텍스트로 진화 시스템에 합류.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from loguru import logger

from .types import ExpertOpinion


_WIKI_ROOT = Path.home() / ".cache" / "ai_trader" / "wiki" / "experts"
_MAX_LINES_PER_PAGE = 500   # 토요일 lint에서 잘라냄
_TRIM_TO_LINES = 300        # lint 시 남기는 줄 수


def _page_path(expert: str) -> Path:
    """전문가별 위키 페이지 경로"""
    _WIKI_ROOT.mkdir(parents=True, exist_ok=True)
    return _WIKI_ROOT / f"{expert}.md"


_lock = asyncio.Lock()


async def ingest_opinion(opinion: ExpertOpinion) -> None:
    """전문가 의견을 위키에 append (비차단)"""
    if not opinion or not opinion.is_valid:
        return

    async with _lock:
        try:
            path = _page_path(opinion.expert)
            ts = opinion.issued_at.strftime("%Y-%m-%d %H:%M")
            entry = (
                f"\n## {ts} ({opinion.regime_bias.value}, score={opinion.score:+d}, conf={opinion.confidence:.2f})\n"
                + "\n".join(f"- {f}" for f in opinion.key_findings)
                + (f"\n- 섹터: {', '.join(opinion.affected_sectors)}\n" if opinion.affected_sectors else "\n")
            )

            # 헤더 없으면 추가
            if not path.exists():
                header = f"# {opinion.expert} Wiki\n\n자동 생성된 전문가 의견 누적 페이지.\n"
                path.write_text(header, encoding="utf-8")

            with path.open("a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.warning(f"[wiki] ingest 실패 ({opinion.expert}): {e}")


def get_context(expert: str, max_chars: int = 2000) -> str:
    """진화 시스템에 주입할 위키 컨텍스트 (최근 N자)"""
    try:
        path = _page_path(expert)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[-max_chars:] if len(text) > max_chars else text
    except Exception:
        return ""


def lint_all() -> List[str]:
    """주간 토요일 — 오래된 줄 트리밍, 오래된 페이지 식별"""
    if not _WIKI_ROOT.exists():
        return []
    warnings: List[str] = []
    cutoff = datetime.now() - timedelta(days=14)

    for path in _WIKI_ROOT.glob("*.md"):
        try:
            stat = path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if mtime < cutoff:
                warnings.append(f"{path.name}: 14일 무갱신 (마지막 {mtime.date()})")

            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > _MAX_LINES_PER_PAGE:
                # 헤더 4줄 + 최근 _TRIM_TO_LINES 줄만 보관
                header = lines[:4]
                tail = lines[-_TRIM_TO_LINES:]
                path.write_text("\n".join(header + tail), encoding="utf-8")
                logger.info(f"[wiki] lint trimmed {path.name}: {len(lines)} → {len(header)+len(tail)}")
        except Exception as e:
            warnings.append(f"{path.name}: lint 실패 {e}")
    return warnings

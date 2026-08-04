"""
원자적 파일 쓰기 헬퍼 (2026-08-04)

상태 파일이 쓰기 도중 크래시로 파손되면 재시작 시 fail-open 동작
(daily_stats 장중 리셋, evolved_overrides 전체 default 회귀 등)으로 번진다.
tmp 파일에 쓴 뒤 os.replace()로 교체해 "완전한 파일 또는 이전 파일"만 남긴다.
"""

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    """tmp + fsync + os.replace 원자적 텍스트 쓰기 (디렉토리 자동 생성)

    2026-08-05 P2: os.fsync 추가 — rename 자체는 원자적이어도 데이터가
    디스크에 내려가기 전 전원 유실 시 빈/파손 파일이 남을 수 있다.
    예외 시 tmp 잔재도 정리 (다음 쓰기 오염 방지). 디렉토리 fsync는 생략.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:   # 한글 포함 — locale 무관 명시
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        # 성공 시 tmp는 이미 rename되어 없음(missing_ok) — 실패 시 잔재 제거
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, data: Any, **json_kwargs) -> None:
    """원자적 JSON 쓰기"""
    json_kwargs.setdefault("ensure_ascii", False)
    atomic_write_text(Path(path), json.dumps(data, **json_kwargs))

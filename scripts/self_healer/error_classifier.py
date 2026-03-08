"""오류 분류 + 컨텍스트 추출 모듈"""

import re
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml


class Tier(Enum):
    NOISE = "noise"
    T1 = "t1"      # 자동 수정
    T2 = "t2"      # 수정 + 승인
    T3 = "t3"      # 알림만


@dataclass
class ErrorContext:
    """분류된 오류 정보"""
    tier: Tier
    error_type: str           # 예: AttributeError, RuntimeError
    error_message: str        # 전체 오류 메시지
    description: str          # 패턴 설명
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    stack_trace: str = ""
    recent_logs: str = ""     # 오류 전후 로그
    raw_line: str = ""        # 원본 로그 라인
    matched_pattern: str = ""


@dataclass
class CompiledPattern:
    regex: re.Pattern
    description: str
    tier: Tier
    extract_file: bool = False
    raw_pattern: str = ""


class ErrorClassifier:
    """로그 라인을 분류하고 컨텍스트를 추출"""

    # 스택 트레이스에서 파일/라인 추출
    _TRACEBACK_FILE_RE = re.compile(
        r'File "(/home/user/projects/qwq-ai-trader/(?:src|scripts)/[^"]+\.py)", line (\d+)'
    )
    # 일반 에러 타입 추출
    _ERROR_TYPE_RE = re.compile(
        r'\b((?:Syntax|Indentation|Import|ModuleNotFound|Attribute|Name|Key|Type|Runtime|Value|Index|Zero[Dd]ivision|Unbound[Ll]ocal|Recursion|Stop[Ii]teration|Assertion|Connection[Rr]efused|OS|Memory|Timeout|Permission|FileNotFound|ssl\.SSL)Error)\b'
    )

    def __init__(self, patterns_path: Optional[str] = None):
        if patterns_path is None:
            patterns_path = os.path.join(os.path.dirname(__file__), "patterns.yaml")
        self._patterns: list[CompiledPattern] = []
        self._load_patterns(patterns_path)

    def _load_patterns(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # 로드 순서: noise → t1 → t2 → t3
        for tier_name in ("noise", "t1", "t2", "t3"):
            tier = Tier(tier_name)
            for entry in raw.get(tier_name, []):
                try:
                    compiled = CompiledPattern(
                        regex=re.compile(entry["pattern"]),
                        description=entry.get("description", ""),
                        tier=tier,
                        extract_file=entry.get("extract_file", False),
                        raw_pattern=entry["pattern"],
                    )
                    self._patterns.append(compiled)
                except re.error as e:
                    print(f"[self_healer] 패턴 컴파일 오류: {entry['pattern']} → {e}")

    def classify(self, log_line: str, log_buffer: list[str] | None = None) -> Optional[ErrorContext]:
        """로그 라인을 분류하고 ErrorContext 반환. 매칭 없으면 None."""
        for cp in self._patterns:
            m = cp.regex.search(log_line)
            if not m:
                continue

            if cp.tier == Tier.NOISE:
                return None  # 무시

            # 에러 타입 추출
            error_type_match = self._ERROR_TYPE_RE.search(log_line)
            error_type = error_type_match.group(1) if error_type_match else "UnknownError"

            # 파일/라인 추출
            file_path, line_number = self._extract_file_info(log_line, log_buffer)

            # 스택 트레이스 조합
            stack_trace = self._extract_stack_trace(log_buffer) if log_buffer else ""

            # 최근 로그
            recent_logs = ""
            if log_buffer:
                recent_logs = "\n".join(log_buffer[-30:])

            return ErrorContext(
                tier=cp.tier,
                error_type=error_type,
                error_message=log_line.strip(),
                description=cp.description,
                file_path=file_path,
                line_number=line_number,
                stack_trace=stack_trace,
                recent_logs=recent_logs,
                raw_line=log_line,
                matched_pattern=cp.raw_pattern,
            )

        return None  # 패턴 미매칭

    def _extract_file_info(
        self, log_line: str, log_buffer: list[str] | None
    ) -> tuple[Optional[str], Optional[int]]:
        """스택 트레이스에서 프로젝트 소스 파일 경로와 라인 번호 추출"""
        # 현재 라인에서 먼저 시도
        m = self._TRACEBACK_FILE_RE.search(log_line)
        if m:
            return m.group(1), int(m.group(2))

        # 버퍼에서 마지막 매칭 (가장 안쪽 프레임)
        if log_buffer:
            for line in reversed(log_buffer[-50:]):
                m = self._TRACEBACK_FILE_RE.search(line)
                if m:
                    return m.group(1), int(m.group(2))

        return None, None

    def _extract_stack_trace(self, log_buffer: list[str] | None) -> str:
        """로그 버퍼에서 Traceback 블록 추출"""
        if not log_buffer:
            return ""

        trace_lines: list[str] = []
        in_traceback = False

        for line in log_buffer[-80:]:
            if "Traceback (most recent call last)" in line:
                in_traceback = True
                trace_lines = [line]
            elif in_traceback:
                trace_lines.append(line)
                # 에러 라인 (들여쓰기 없는 Exception 라인)으로 끝
                if line.strip() and not line.startswith(" ") and "Error" in line and len(trace_lines) > 2:
                    in_traceback = False

        return "\n".join(trace_lines) if trace_lines else ""

    def check_repeated(self, error_key: str, history: dict[str, list[float]], threshold: int = 3, window_secs: float = 1800) -> bool:
        """동일 오류가 window_secs 내에 threshold 횟수 이상 반복인지 확인"""
        import time
        now = time.time()
        timestamps = history.get(error_key, [])
        recent = [t for t in timestamps if now - t < window_secs]
        return len(recent) >= threshold

    @staticmethod
    def error_key(ctx: ErrorContext) -> str:
        """오류 고유 키 생성 (중복 검사용)"""
        parts = [ctx.error_type]
        if ctx.file_path:
            parts.append(os.path.basename(ctx.file_path))
        if ctx.line_number:
            parts.append(str(ctx.line_number))
        return ":".join(parts)

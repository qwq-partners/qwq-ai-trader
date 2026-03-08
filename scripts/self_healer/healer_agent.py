"""Claude Code 호출 + 결과 검증 모듈"""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from error_classifier import ErrorContext, Tier


PROJECT_DIR = "/home/user/projects/qwq-ai-trader"
MAX_CLAUDE_TIMEOUT_SECS = 300


@dataclass
class HealResult:
    success: bool
    summary: str = ""
    commit_hash: str = ""
    diff: str = ""
    cannot_fix_reason: str = ""
    elapsed_secs: float = 0.0
    stdout: str = ""


def build_prompt(ctx: ErrorContext) -> str:
    """Claude Code에 전달할 프롬프트 생성"""
    file_info = ""
    if ctx.file_path:
        file_info = f"- 파일: {ctx.file_path}"
        if ctx.line_number:
            file_info += f":{ctx.line_number}"

    stack_section = ""
    if ctx.stack_trace:
        stack_section = f"\n[스택 트레이스]\n{ctx.stack_trace}\n"

    logs_section = ""
    if ctx.recent_logs:
        logs_section = f"\n[최근 로그 (오류 전 30줄)]\n{ctx.recent_logs}\n"

    file_path_for_check = ctx.file_path or "<수정한 파일>"

    if ctx.tier == Tier.T3:
        # T3: 분석만
        return f"""다음 오류가 qwq-ai-trader 봇에서 발생했습니다.

[오류 정보]
- 시간: {time.strftime('%Y-%m-%d %H:%M:%S')}
- 오류 타입: {ctx.error_type}
{file_info}
- 오류 메시지: {ctx.error_message}
{stack_section}{logs_section}
작업:
1. 위 오류의 근본 원인을 분석하세요
2. 수정 방안을 제시하되, 코드를 직접 수정하지 마세요
3. 분석 결과를 마지막에 출력하세요: ANALYSIS: <분석 내용>

주의:
- 코드 수정 금지 (분석만)
- git commit 금지
- systemctl restart 금지"""

    # T1/T2: 수정 수행
    return f"""다음 오류가 qwq-ai-trader 봇에서 발생했습니다.

[오류 정보]
- 시간: {time.strftime('%Y-%m-%d %H:%M:%S')}
- 오류 타입: {ctx.error_type}
{file_info}
- 오류 메시지: {ctx.error_message}
{stack_section}{logs_section}
작업:
1. 위 오류의 근본 원인을 분석하세요
2. 관련 파일을 읽고 해당 부분을 수정하세요
3. 수정 후 python3 -c "import ast; ast.parse(open('{file_path_for_check}').read())" 로 문법 확인
4. git add -A && git commit -m "fix: [자가수정] {ctx.error_type} — {ctx.error_message[:50]}"
5. 수정한 내용을 한 줄로 요약해서 마지막에 출력하세요: SUMMARY: <내용>

주의:
- 수정 범위는 오류와 직접 관련된 코드만
- systemctl restart는 하지 말 것 (자가수정 에이전트가 처리)
- 불확실하면 수정하지 말고 CANNOT_FIX: <이유> 출력
- 반드시 한국어로 응답"""


def call_claude_code(prompt: str, timeout: int = MAX_CLAUDE_TIMEOUT_SECS) -> HealResult:
    """Claude Code를 non-interactive 모드로 실행"""
    start = time.time()

    try:
        result = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "-p", prompt],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if result.returncode != 0 and not stdout:
            return HealResult(
                success=False,
                summary=f"Claude Code 실행 실패 (rc={result.returncode}): {stderr[:500]}",
                elapsed_secs=elapsed,
                stdout=stdout,
            )

        return _parse_result(stdout, elapsed)

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return HealResult(
            success=False,
            summary=f"Claude Code 타임아웃 ({timeout}초 초과)",
            elapsed_secs=elapsed,
        )
    except Exception as e:
        elapsed = time.time() - start
        return HealResult(
            success=False,
            summary=f"Claude Code 호출 오류: {e}",
            elapsed_secs=elapsed,
        )


def _parse_result(stdout: str, elapsed: float) -> HealResult:
    """Claude Code 출력에서 SUMMARY / CANNOT_FIX / ANALYSIS 추출"""

    # CANNOT_FIX 체크
    cannot_fix = re.search(r"CANNOT_FIX:\s*(.+)", stdout)
    if cannot_fix:
        return HealResult(
            success=False,
            cannot_fix_reason=cannot_fix.group(1).strip(),
            elapsed_secs=elapsed,
            stdout=stdout,
        )

    # ANALYSIS 체크 (T3)
    analysis = re.search(r"ANALYSIS:\s*(.+)", stdout, re.DOTALL)
    if analysis:
        return HealResult(
            success=True,
            summary=analysis.group(1).strip()[:2000],
            elapsed_secs=elapsed,
            stdout=stdout,
        )

    # SUMMARY 체크 (T1/T2)
    summary_match = re.search(r"SUMMARY:\s*(.+)", stdout)
    summary = summary_match.group(1).strip() if summary_match else "수정 완료 (요약 없음)"

    # 커밋 해시 추출
    commit_hash = _get_latest_commit_hash()

    # diff 추출
    diff = _get_latest_diff()

    return HealResult(
        success=True,
        summary=summary,
        commit_hash=commit_hash,
        diff=diff,
        elapsed_secs=elapsed,
        stdout=stdout,
    )


def _get_latest_commit_hash() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _get_latest_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD~1..HEAD"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.stdout


def verify_syntax(file_path: str) -> bool:
    """수정된 파일 문법 검증"""
    if not file_path or not os.path.isfile(file_path):
        return True  # 파일 없으면 스킵

    result = subprocess.run(
        ["python3", "-c", f"import ast; ast.parse(open('{file_path}').read())"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0

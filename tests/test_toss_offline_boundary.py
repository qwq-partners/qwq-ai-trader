"""Toss Phase 1 replay CLI의 오프라인 경계 계약."""

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "replay_toss_shadow.py"
FIXTURES = ROOT / "tests" / "fixtures" / "toss"


def _offline_env():
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "TZ": "Asia/Seoul",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOSS_API": "1",
    }


def test_cli_help_exposes_only_manifest_and_input_without_live_or_credential_options():
    # live/credential 옵션을 추가하거나 help가 실행 중 인증으로 향하면 실패한다.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        env=_offline_env(),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--manifest" in result.stdout
    assert "--input" in result.stdout
    assert "--live" not in result.stdout
    assert "credential" not in result.stdout.lower()
    assert "token" not in result.stdout.lower()
    assert result.stderr == ""


def test_cli_reads_synthetic_files_and_writes_a_json_report_only_to_stdout(tmp_path):
    # cache/result 파일을 만들거나 TOSS_API=1로 live를 암묵 활성화하면 실패한다.
    manifest = tmp_path / "manifest.json"
    pairs = tmp_path / "pairs.json"
    manifest.write_text((FIXTURES / "phase1_manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    pairs.write_text((FIXTURES / "phase1_pairs.json").read_text(encoding="utf-8"), encoding="utf-8")
    before = sorted(path.name for path in tmp_path.iterdir())

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--input", str(pairs)],
        cwd=ROOT,
        env=_offline_env(),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert report["attempted_pairs"] == 6
    assert report["valid_pairs"] == 2
    assert report["p95_difference_pct"] == 1.0
    assert report["synthetic_only"] is True
    assert report["production_eligible"] is False

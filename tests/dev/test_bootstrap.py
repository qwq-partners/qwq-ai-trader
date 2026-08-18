import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/bootstrap.sh"
TOOLS = ("git", "node", "npm", "gh", "claude", "codex")


def run_bootstrap(
    tmp_path: Path,
    python_version: str = "Python 3.12.3",
    omitted_tool: str | None = None,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in TOOLS:
        if name == omitted_tool:
            continue
        tool = fake_bin / name
        tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)

    python = fake_bin / "python-under-test"
    python.write_text(
        f"#!/usr/bin/env bash\necho '{python_version}'\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "QWQ_BOOTSTRAP_CHECK_ONLY": "1",
        "QWQ_PYTHON_BIN": str(python),
        "QWQ_VENV_DIR": str(tmp_path / "venv"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_bootstrap_accepts_python_311_or_newer(tmp_path):
    result = run_bootstrap(tmp_path)

    assert result.returncode == 0, result.stderr


def test_bootstrap_rejects_old_python(tmp_path):
    result = run_bootstrap(tmp_path, python_version="Python 3.10.14")

    assert result.returncode != 0
    assert "Python 3.11 이상" in result.stderr


def test_bootstrap_reports_missing_required_tool(tmp_path):
    result = run_bootstrap(tmp_path, omitted_tool="codex")

    assert result.returncode != 0
    assert "codex" in result.stderr


def test_check_only_does_not_replace_existing_venv(tmp_path):
    marker = tmp_path / "venv" / "keep"
    marker.parent.mkdir()
    marker.write_text("preserve", encoding="utf-8")

    result = run_bootstrap(tmp_path)

    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8") == "preserve"

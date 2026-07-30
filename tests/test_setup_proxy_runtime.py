"""TDD test: setup_proxy_runtime.py must be importable and --help must exit 0."""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_setup_proxy_runtime_help():
    """Running setup_proxy_runtime.py --help from repo root must succeed."""
    result = subprocess.run(
        [sys.executable, "custom_scripts/setup_proxy_runtime.py", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    msg = (
        "Expected exit 0, got %d\n"
        "STDOUT:\n%s\n"
        "STDERR:\n%s"
    ) % (result.returncode, result.stdout, result.stderr)
    assert result.returncode == 0, msg

"""Independent check run before the agent's `done` is accepted.

The model's claim that it is finished is not evidence. Before `done` is
accepted, the package is installed into a fresh environment and the agent's
own tests are run against the INSTALLED package, from a separate folder.

Why a separate folder: inside the work folder, Python imports the source
files directly, so a package whose pyproject.toml doesn't include its code
still passes. That exact failure (phone-number, 3/3 baseline runs) is what
this check exists to catch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

STEP_TIMEOUT = 180   # seconds per step
REPORT_CHARS = 3000  # tail of output sent back to the model (pytest's summary is at the end)

# Generated or local-only folders that shouldn't be copied or installed from.
_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", "*.egg-info", "build", "dist", ".venv", "venv", ".git"
)


@dataclass
class VerifyResult:
    passed: bool
    report: str


def _clean_env() -> dict[str, str]:
    """Environment without anything that could point Python at the source folder."""
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run a command (no shell), return (exit code, combined output)."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=STEP_TIMEOUT
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {STEP_TIMEOUT}s"


def _copy_tests(src: Path, dest: Path) -> bool:
    """Copy the agent's tests into dest. Returns True if any test files were found."""
    found = False
    tests_dir = src / "tests"
    if tests_dir.is_dir():
        shutil.copytree(tests_dir, dest / "tests", ignore=_IGNORE)
        found = any(tests_dir.rglob("test_*.py")) or any(tests_dir.rglob("*_test.py"))
    for pattern in ("test_*.py", "*_test.py"):
        for f in src.glob(pattern):
            shutil.copy(f, dest / f.name)
            found = True
    conftest = src / "conftest.py"
    if conftest.is_file():
        shutil.copy(conftest, dest / "conftest.py")
    return found


def verify_package(work_dir: Path) -> VerifyResult:
    """Install the package in a fresh venv and run its tests against the install."""
    work_dir = Path(work_dir).resolve()
    env = _clean_env()

    with tempfile.TemporaryDirectory(prefix="verify_") as tmp_name:
        tmp = Path(tmp_name)

        # Install from a copy, so the check leaves no build files in the work folder.
        source = tmp / "source"
        shutil.copytree(work_dir, source, ignore=_IGNORE)

        venv = tmp / "venv"
        rc, out = _run([sys.executable, "-m", "venv", str(venv)], tmp, env)
        if rc != 0:
            return VerifyResult(False, "Could not create a fresh environment:\n" + out[-REPORT_CHARS:])
        python = str(venv / ("Scripts" if os.name == "nt" else "bin") / "python")

        rc, out = _run([python, "-m", "pip", "install", "."], source, env)
        if rc != 0:
            return VerifyResult(
                False,
                "`pip install .` failed in a fresh environment:\n" + out[-REPORT_CHARS:],
            )

        rc, out = _run([python, "-m", "pip", "install", "pytest"], tmp, env)
        if rc != 0:
            return VerifyResult(False, "Could not install pytest for the check:\n" + out[-REPORT_CHARS:])

        tests_copy = tmp / "tests_copy"
        tests_copy.mkdir()
        if not _copy_tests(work_dir, tests_copy):
            return VerifyResult(
                False, "No tests found. Expected a tests/ folder or test_*.py files."
            )

        rc, out = _run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], tests_copy, env)
        if rc == 0:
            return VerifyResult(
                True, "Check passed: installed in a fresh environment; tests pass against the installed package."
            )
        if rc == 5:  # pytest: no tests collected
            return VerifyResult(False, "pytest collected no tests.")
        return VerifyResult(
            False,
            "Tests failed when run against the INSTALLED package, from a folder outside your "
            "source tree. Only what `pip install .` actually installs is importable here.\n\n"
            + out[-REPORT_CHARS:],
        )
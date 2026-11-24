"""grade.py — independent grader for one agent run.

Input:  a finished work folder and a spec folder.
Output: a result dict.  No model calls.

Four checks, run in a fresh venv:
  1. build   — pip wheel . --no-deps
  2. install — pip install . into the fresh venv
  3. own     — pytest in the work folder (recorded, NOT in the headline)
  4. hidden  — hidden_test.py copied to a separate temp folder, run against
               the installed package

Headline per run: checks 1, 2, and 4 all pass.

Three outcomes:
  scored     — all checks ran (pass or fail)
  faulted    — infrastructure broke (network, timeout, harness error)
               A missing pyproject.toml is NOT a fault; installability is
               required by the spec, so it is a legitimate failure.
  timed_out  — a check exceeded GRADE_TIMEOUT seconds

Exit-code rule: pytest exit code 5 ("no tests collected") counts as FAILURE.
This is commented explicitly because it is easy to miss and silently inflates
the pass rate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

GRADE_TIMEOUT = 120  # seconds per check


def _run(cmd: list[str], cwd: Path, env: dict | None = None,
         timeout: int = GRADE_TIMEOUT) -> tuple[int, str]:
    """Run a command, return (returncode, combined_output).

    Merges stdout and stderr so the caller always has the full picture.
    Returns returncode=-1 on TimeoutExpired.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        combined = result.stdout
        if result.stderr:
            combined += "\n[stderr]\n" + result.stderr
        return result.returncode, combined.strip()
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except Exception as e:
        return -2, f"harness error: {type(e).__name__}: {e}"


def grade(work_dir: Path, spec_dir: Path) -> dict[str, Any]:
    """Grade one agent run.

    Args:
        work_dir:  the folder the agent wrote its package into
        spec_dir:  the eval spec folder (must contain hidden_test.py)

    Returns:
        A dict with all result fields.  Never raises.
    """
    work_dir = Path(work_dir).resolve()
    spec_dir = Path(spec_dir).resolve()
    hidden_test = spec_dir / "hidden_test.py"

    result: dict[str, Any] = {
        "work_dir": str(work_dir),
        "spec": spec_dir.name,
        "status": "scored",
        "build": False,
        "install": False,
        "own_tests": False,
        "own_tests_exit": None,
        "own_passed": 0,
        "own_total": 0,
        "hidden_tests": False,
        "hidden_exit": None,
        "hidden_passed": 0,
        "hidden_total": 0,
        "all_pass": False,
        "false_success": None,   # filled in by the runner, not the grader
        "error_detail": {},
    }

    # ------------------------------------------------------------------ #
    # Sanity checks                                                        #
    # ------------------------------------------------------------------ #
    if not work_dir.is_dir():
        result["status"] = "faulted"
        result["error_detail"]["setup"] = f"work_dir does not exist: {work_dir}"
        return result

    if not hidden_test.is_file():
        result["status"] = "faulted"
        result["error_detail"]["setup"] = f"hidden_test.py not found: {hidden_test}"
        return result

    # ------------------------------------------------------------------ #
    # Create a fresh venv                                                  #
    # ------------------------------------------------------------------ #
    venv_dir = Path(tempfile.mkdtemp(prefix="grade_venv_"))
    try:
        rc, out = _run(
            [sys.executable, "-m", "venv", "--clear", str(venv_dir)],
            cwd=work_dir,
        )
        if rc != 0:
            result["status"] = "faulted"
            result["error_detail"]["venv"] = out
            return result

        # Locate pip and python inside the venv
        if sys.platform == "win32":
            venv_python = venv_dir / "Scripts" / "python.exe"
            venv_pip    = venv_dir / "Scripts" / "pip.exe"
        else:
            venv_python = venv_dir / "bin" / "python"
            venv_pip    = venv_dir / "bin" / "pip"

        # Install pytest into the venv (needed for checks 3 and 4).
        # Treat a failure here as a fault (network/infra), not a model failure.
        # --index-url bypasses any corporate package feed configured in pip.ini.
        rc, out = _run(
            [str(venv_pip), "install", "pytest", "--quiet",
             "--index-url", "https://pypi.org/simple/"],
            cwd=work_dir,
        )
        if rc != 0:
            result["status"] = "faulted"
            result["error_detail"]["pytest_install"] = out
            return result

        # ---------------------------------------------------------------- #
        # Check 1: build                                                    #
        # ---------------------------------------------------------------- #
        rc, out = _run(
            [str(venv_pip), "wheel", ".", "--no-deps", "--wheel-dir",
             str(venv_dir / "wheels"), "--quiet",
             "--index-url", "https://pypi.org/simple/"],
            cwd=work_dir,
        )
        result["build"] = (rc == 0)
        if rc != 0:
            result["error_detail"]["build"] = out[-3000:]

        # ---------------------------------------------------------------- #
        # Check 2: install                                                  #
        # ---------------------------------------------------------------- #
        rc, out = _run(
            [str(venv_pip), "install", ".", "--quiet",
             "--index-url", "https://pypi.org/simple/"],
            cwd=work_dir,
        )
        result["install"] = (rc == 0)
        if rc != 0:
            result["error_detail"]["install"] = out[-3000:]

        # ---------------------------------------------------------------- #
        # Check 3: agent's own tests (recorded, NOT in headline)           #
        # Run from the work_dir so the agent's test discovery works.       #
        # ---------------------------------------------------------------- #
        rc, out = _run(
            [str(venv_python), "-m", "pytest", "--tb=short", "-q"],
            cwd=work_dir,
        )
        result["own_tests_exit"] = rc
        # Exit code 5 = no tests collected → treat as failure
        result["own_tests"] = (rc == 0)
        result["error_detail"]["own_tests_raw"] = out[-2000:] if rc != 0 else ""
        # Parse passed/total from pytest output
        result["own_passed"], result["own_total"] = _parse_pytest_counts(out)

        # ---------------------------------------------------------------- #
        # Check 4: hidden tests against the INSTALLED package              #
        # Copy hidden_test.py to a SEPARATE temp folder so Python cannot   #
        # import from the source tree.  This is the critical isolation:    #
        # if we ran from work_dir, a broken install would still pass       #
        # because Python finds the source directory on sys.path.           #
        # ---------------------------------------------------------------- #
        hidden_dir = Path(tempfile.mkdtemp(prefix="grade_hidden_"))
        try:
            shutil.copy(hidden_test, hidden_dir / hidden_test.name)
            rc, out = _run(
                [str(venv_python), "-m", "pytest", hidden_test.name,
                 "--tb=short", "-q"],
                cwd=hidden_dir,
            )
            result["hidden_exit"] = rc
            # Exit code 5 = no tests collected → treat as failure
            result["hidden_tests"] = (rc == 0)
            if rc != 0:
                result["error_detail"]["hidden_tests_raw"] = out[-3000:]
            result["hidden_passed"], result["hidden_total"] = _parse_pytest_counts(out)
        finally:
            shutil.rmtree(hidden_dir, ignore_errors=True)

    finally:
        shutil.rmtree(venv_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Headline                                                             #
    # ------------------------------------------------------------------ #
    result["all_pass"] = result["build"] and result["install"] and result["hidden_tests"]

    return result


def _parse_pytest_counts(output: str) -> tuple[int, int]:
    """Extract (passed, total) from pytest's summary line.

    Handles lines like:
      "5 passed in 0.12s"
      "3 passed, 2 failed in 0.45s"
      "FAILED ... - 1 failed in 0.10s"
    Returns (0, 0) if the line is not found.
    """
    import re
    passed = 0
    failed = 0
    for line in reversed(output.splitlines()):
        m_pass   = re.search(r"(\d+) passed", line)
        m_fail   = re.search(r"(\d+) failed", line)
        m_error  = re.search(r"(\d+) error", line)
        if m_pass or m_fail or m_error:
            passed = int(m_pass.group(1)) if m_pass else 0
            failed = int(m_fail.group(1)) if m_fail else 0
            failed += int(m_error.group(1)) if m_error else 0
            return passed, passed + failed
    return 0, 0


# ------------------------------------------------------------------ #
# Gate helpers (used by the test suite and by run_evals.py)           #
# ------------------------------------------------------------------ #

def run_oracle_gate(spec_dir: Path) -> dict[str, Any]:
    """Grade the oracle (reference solution) for a spec.

    The oracle is expected to:
      - build:   pass
      - install: pass
      - hidden:  pass
      - own:     FAIL (the oracle has no tests of its own; this is expected)

    Returns the grade result dict, plus an 'oracle_gate_pass' key.
    """
    spec_dir = Path(spec_dir).resolve()
    oracle_py = spec_dir / "oracle.py"
    hidden_test = spec_dir / "hidden_test.py"

    if not oracle_py.is_file():
        return {"oracle_gate_pass": False, "error": "oracle.py not found"}
    if not hidden_test.is_file():
        return {"oracle_gate_pass": False, "error": "hidden_test.py not found"}

    # Determine the module name from the hidden_test import line
    module_name = _infer_module_name(hidden_test)
    if not module_name:
        return {"oracle_gate_pass": False, "error": "could not infer module name from hidden_test.py"}

    # Build a minimal installable folder in a temp dir
    oracle_dir = Path(tempfile.mkdtemp(prefix="oracle_"))
    try:
        # Copy oracle.py as the module file
        shutil.copy(oracle_py, oracle_dir / f"{module_name}.py")

        # Write a minimal pyproject.toml
        pyproject = oracle_dir / "pyproject.toml"
        pyproject.write_text(
            f'[build-system]\n'
            f'requires = ["setuptools>=61"]\n'
            f'build-backend = "setuptools.build_meta"\n\n'
            f'[project]\n'
            f'name = "{module_name.replace("_", "-")}"\n'
            f'version = "0.0.0"\n\n'
            f'[tool.setuptools]\n'
            f'py-modules = ["{module_name}"]\n',
            encoding="utf-8",
        )

        result = grade(oracle_dir, spec_dir)
        # Oracle is expected to fail own_tests (it has none); that's fine.
        # Gate passes if build + install + hidden all pass.
        gate_pass = result["build"] and result["install"] and result["hidden_tests"]
        result["oracle_gate_pass"] = gate_pass
        return result
    finally:
        shutil.rmtree(oracle_dir, ignore_errors=True)


def run_empty_folder_gate(spec_dir: Path) -> dict[str, Any]:
    """Grade an empty folder.  Every check must fail (score 0).

    Returns the grade result dict, plus an 'empty_gate_pass' key.
    """
    empty_dir = Path(tempfile.mkdtemp(prefix="empty_gate_"))
    try:
        result = grade(empty_dir, spec_dir)
        # Gate passes if nothing passes
        gate_pass = not (result["build"] or result["install"] or result["hidden_tests"])
        result["empty_gate_pass"] = gate_pass
        return result
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)


def _infer_module_name(hidden_test: Path) -> str | None:
    """Read the first 'from <module> import' line and return the module name."""
    import re
    for line in hidden_test.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^from\s+(\w+)\s+import", line)
        if m:
            return m.group(1)
    return None


# ------------------------------------------------------------------ #
# CLI: run gates directly                                             #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run grader gates")
    parser.add_argument("--oracle", metavar="SPEC_DIR",
                        help="Run oracle gate for a spec dir")
    parser.add_argument("--empty", metavar="SPEC_DIR",
                        help="Run empty-folder gate for a spec dir")
    parser.add_argument("--grade", nargs=2, metavar=("WORK_DIR", "SPEC_DIR"),
                        help="Grade a work folder against a spec")
    args = parser.parse_args()

    if args.oracle:
        r = run_oracle_gate(Path(args.oracle))
        print(json.dumps(r, indent=2))
        sys.exit(0 if r.get("oracle_gate_pass") else 1)

    elif args.empty:
        r = run_empty_folder_gate(Path(args.empty))
        print(json.dumps(r, indent=2))
        sys.exit(0 if r.get("empty_gate_pass") else 1)

    elif args.grade:
        r = grade(Path(args.grade[0]), Path(args.grade[1]))
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["all_pass"] else 1)

    else:
        parser.print_help()
        sys.exit(1)

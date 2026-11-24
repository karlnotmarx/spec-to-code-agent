"""run_evals.py — run the spec-agent against all eval specs and grade results.

Usage:
    uv run python evals/run_evals.py --label baseline
    uv run python evals/run_evals.py --label after --repeats 3

Each run:
  1. Creates /tmp/evals/<label>/<spec>-<n>/ with work/ and venv/ subdirs.
  2. Installs pytest into the run venv (so the agent's own `pytest` works).
  3. Starts the agent as a subprocess with PATH pointing to the run venv first,
     so the agent's `pip install` lands in the run venv, not the host environment.
  4. Times the run; kills it after RUN_TIMEOUT seconds.
  5. Grades the work folder with grade.py.
  6. Appends the record to evals/results/<label>.json.
  7. After all runs, writes evals/results/<label>.md with a summary table.

Design notes:
  - The agent is called as a subprocess (not imported) so it cannot share state
    with the harness.  This also means runs can be parallelised safely.
  - Per-run venv on PATH: the agent's pip install goes into the run venv, not
    the host conda base.  Without this, run 1 installs a package globally and
    run 2 may import a stale version from a deleted temp folder.
  - `uv run` is NOT used to call the agent because uv puts the project venv
    first on PATH, which would bypass the per-run venv.  We call the entry
    point binary directly instead.
  - Timing is recorded but treated as a rough guide only: parallel runs share
    the API rate limit and the network, so wall time varies.  Use iterations
    as the cost measure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (all relative to this file's location, i.e. evals/)
# ---------------------------------------------------------------------------
EVALS_DIR   = Path(__file__).resolve().parent
PROJECT_DIR = EVALS_DIR.parent
SPECS_DIR   = EVALS_DIR / "specs"
RESULTS_DIR = EVALS_DIR / "results"

# The agent entry point binary inside the project venv
if sys.platform == "win32":
    AGENT_BIN = PROJECT_DIR / ".venv" / "Scripts" / "spec-agent.exe"
    PIP_BIN_SUBDIR = "Scripts"
    PYTHON_BIN = "python.exe"
    PIP_BIN    = "pip.exe"
else:
    AGENT_BIN = PROJECT_DIR / ".venv" / "bin" / "spec-agent"
    PIP_BIN_SUBDIR = "bin"
    PYTHON_BIN = "python"
    PIP_BIN    = "pip"

RUN_TIMEOUT   = 15 * 60   # 15 minutes per agent run
GRADE_TIMEOUT = 120        # seconds per grader check (passed to grade.py)

SPECS = ["transpose", "book-store", "bowling", "phone-number"]


def run_all(label: str, repeats: int = 3, max_workers: int = 4) -> list[dict]:
    """Run all specs × repeats, up to max_workers in parallel."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Export it before running evals.")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ...

def _make_run_venv(run_dir: Path) -> Path:
    """Create a fresh venv in run_dir/venv and install pytest into it.

    Returns the venv directory path.
    Raises RuntimeError if setup fails.
    """
    venv_dir = run_dir / "venv"
    python = sys.executable

    result = subprocess.run(
        [python, "-m", "venv", "--clear", str(venv_dir)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"venv creation failed: {result.stderr}")

    pip = venv_dir / PIP_BIN_SUBDIR / PIP_BIN
    result = subprocess.run(
        [str(pip), "install", "pytest", "--quiet",
         "--index-url", "https://pypi.org/simple/"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pytest install failed: {result.stderr[-2000:]}")

    return venv_dir


def _run_agent(spec_name: str, run_dir: Path, venv_dir: Path,
               agent_claimed_success: list, label: str) -> dict:
    """Run the agent for one spec in one repeat.

    Returns a partial result dict (without grade fields).
    """
    work_dir = run_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_md  = SPECS_DIR / spec_name / "spec.md"

    # Build an environment where the run venv comes first on PATH.
    # This ensures the agent's `pip install` lands in the run venv.
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")}
    venv_bin = str(venv_dir / PIP_BIN_SUBDIR)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(venv_dir)

    start = time.monotonic()
    agent_exit = None
    iterations = None
    timed_out = False
    faulted = False
    fault_reason = ""

    try:
        proc = subprocess.Popen(
            [str(AGENT_BIN), str(spec_md), str(work_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        try:
            stdout, _ = proc.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            timed_out = True

        agent_exit = proc.returncode
        # Parse iterations from the CLI output line:
        #   "✓ Completed in N iterations ..."  or
        #   "✗ Failed after N iterations ..."
        import re
        m = re.search(r"(?:Completed in|Failed after)\s+(\d+)\s+iteration", stdout)
        if m:
            iterations = int(m.group(1))

        # Detect tracebacks as faults
        if "Traceback (most recent call last)" in stdout and agent_exit != 0:
            faulted = True
            fault_reason = "traceback in agent output"

        # Save stdout for inspection
        (run_dir / "agent_stdout.txt").write_text(stdout, encoding="utf-8")

    except Exception as e:
        faulted = True
        fault_reason = f"{type(e).__name__}: {e}"
        agent_exit = -1

    wall_seconds = round(time.monotonic() - start, 1)

    return {
        "spec": spec_name,
        "label": label,
        "agent_exit": agent_exit,
        "agent_claimed_success": (agent_exit == 0),
        "iterations": iterations,
        "wall_seconds": wall_seconds,
        "timed_out": timed_out,
        "agent_faulted": faulted,
        "fault_reason": fault_reason,
    }


def run_one(spec_name: str, repeat: int, label: str,
            tmp_base: Path) -> dict:
    """Run and grade one (spec, repeat) pair.  Returns a full result dict."""
    run_dir = tmp_base / f"{spec_name}-{repeat}"
    run_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "spec": spec_name,
        "repeat": repeat,
        "label": label,
        "status": "scored",
    }

    # Create per-run venv
    try:
        venv_dir = _make_run_venv(run_dir)
    except RuntimeError as e:
        result["status"] = "faulted"
        result["fault_reason"] = str(e)
        return result

    # Run the agent
    agent_result = _run_agent(spec_name, run_dir, venv_dir, [], label)
    result.update(agent_result)

    if result.get("timed_out"):
        result["status"] = "timed_out"
        return result

    if result.get("agent_faulted"):
        result["status"] = "faulted"
        return result

    # Grade the work folder
    work_dir = run_dir / "work"
    spec_dir = SPECS_DIR / spec_name

    # Import grade from the same directory as this file
    sys.path.insert(0, str(EVALS_DIR))
    from grade import grade as grade_fn  # noqa: PLC0415

    grade_result = grade_fn(work_dir, spec_dir)
    result.update(grade_result)

    # false_success: agent said success but grader says not all_pass
    result["false_success"] = (
        result["agent_claimed_success"] and not result.get("all_pass", False)
    )

    return result


def run_all(label: str, repeats: int = 3, max_workers: int = 4) -> list[dict]:
    """Run all specs × repeats, up to max_workers in parallel."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    tmp_base = Path(tempfile.mkdtemp(prefix=f"evals_{label}_"))
    print(f"Run directory: {tmp_base}")
    print(f"Specs: {SPECS}")
    print(f"Repeats: {repeats}  Workers: {max_workers}")
    print()

    tasks = [
        (spec, rep)
        for spec in SPECS
        for rep in range(1, repeats + 1)
    ]

    results = []
    json_path = RESULTS_DIR / f"{label}.json"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(run_one, spec, rep, label, tmp_base): (spec, rep)
            for spec, rep in tasks
        }
        for future in as_completed(futures):
            spec, rep = futures[future]
            try:
                r = future.result()
            except Exception as e:
                r = {
                    "spec": spec, "repeat": rep, "label": label,
                    "status": "faulted", "fault_reason": str(e),
                }
            results.append(r)
            _print_progress(r)

            # Append to JSON file as we go (so partial results survive a crash)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)

    # Write summary markdown
    md_path = RESULTS_DIR / f"{label}.md"
    md_path.write_text(_make_summary(results, label), encoding="utf-8")
    print(f"\nResults: {json_path}")
    print(f"Summary: {md_path}")
    return results


def _print_progress(r: dict) -> None:
    spec    = r.get("spec", "?")
    rep     = r.get("repeat", "?")
    status  = r.get("status", "?")
    claimed = r.get("agent_claimed_success", "?")
    all_p   = r.get("all_pass", "?")
    false_s = r.get("false_success", "?")
    iters   = r.get("iterations", "?")
    wall    = r.get("wall_seconds", "?")
    print(f"  {spec:15s} rep={rep}  status={status:8s}  "
          f"claimed={claimed}  all_pass={all_p}  "
          f"false_success={false_s}  iters={iters}  {wall}s")


def _make_summary(results: list[dict], label: str) -> str:
    lines = [
        f"# Eval results: {label}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "| Spec | Rep | Status | Claimed | All-pass | False-success | Own-pass | Hidden-pass | Iters | Secs |",
        "|------|-----|--------|---------|----------|---------------|----------|-------------|-------|------|",
    ]
    for r in sorted(results, key=lambda x: (x.get("spec",""), x.get("repeat", 0))):
        own_p   = f"{r.get('own_passed',0)}/{r.get('own_total',0)}"
        hid_p   = f"{r.get('hidden_passed',0)}/{r.get('hidden_total',0)}"
        lines.append(
            f"| {r.get('spec','?'):15s} | {r.get('repeat','?')} "
            f"| {r.get('status','?'):8s} "
            f"| {str(r.get('agent_claimed_success','?')):5s} "
            f"| {str(r.get('all_pass','?')):5s} "
            f"| {str(r.get('false_success','?')):5s} "
            f"| {own_p:8s} "
            f"| {hid_p:11s} "
            f"| {str(r.get('iterations','?')):5s} "
            f"| {r.get('wall_seconds','?')} |"
        )

    # Totals
    scored  = [r for r in results if r.get("status") == "scored"]
    faulted = [r for r in results if r.get("status") == "faulted"]
    timed   = [r for r in results if r.get("status") == "timed_out"]
    all_p   = sum(1 for r in scored if r.get("all_pass"))
    false_s = sum(1 for r in scored if r.get("false_success"))
    claimed = sum(1 for r in scored if r.get("agent_claimed_success"))

    lines += [
        "",
        f"**Scored:** {len(scored)} / {len(results)}  "
        f"**Faulted:** {len(faulted)}  "
        f"**Timed out:** {len(timed)}",
        "",
        f"**All-pass (build + install + hidden tests):** {all_p} / {len(scored)}",
        f"**Agent claimed success:** {claimed} / {len(scored)}",
        f"**False success (claimed but not all-pass):** {false_s} / {len(scored)}",
        "",
        f"*{len(results)} runs across {len(SPECS)} specs × {len(results)//len(SPECS) if SPECS else '?'} repeats.*",
        f"*Resolution: ~{100//len(results) if results else '?'} percentage points per run.*",
        f"*Only differences larger than ~20 points are meaningful at this sample size.*",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run eval suite")
    parser.add_argument("--label",    required=True, help="Label for this run (e.g. 'baseline')")
    parser.add_argument("--repeats",  type=int, default=3, help="Repeats per spec (default: 3)")
    parser.add_argument("--workers",  type=int, default=4, help="Parallel workers (default: 4)")
    parser.add_argument("--specs",    nargs="+", default=None,
                        help="Subset of specs to run (default: all)")
    args = parser.parse_args()

    if args.specs:
        SPECS[:] = args.specs

    results = run_all(args.label, repeats=args.repeats, max_workers=args.workers)
    sys.exit(0)

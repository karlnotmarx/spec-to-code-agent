# Spec-to-Code Agent: making "done" mean done

## The problem

Coding agents report success based on what the model says, not on what it built. An agent that says "done" may have produced a package that doesn't install, can't be imported, or fails its own spec. When agents run unattended, that gap is the main risk: failures look like successes.

This project takes an LLM agent that turns a Markdown spec into an installable Python package, measures how often its "success" is false, and fixes it.

## Approach

1. **Measure first.** A grader builds and installs each result in a fresh environment, then runs independent hidden tests (Exercism exercises) against the *installed* package. It makes no model calls, so there's no LLM judge and no self-report. The grader was checked on known-good (reference solution) and known-bad (empty folder) inputs before any agent run.
2. **Baseline.** 4 exercises × 3 runs. The agent claimed success on 12/12, but only 9/12 actually worked.
3. **Fix the success logic, test-first.**
   - Text-only or cut-off replies no longer count as success.
   - `done` is only accepted after the package installs into a fresh environment and the agent's tests pass from a separate folder against the installed package. Otherwise the error goes back to the model.
4. **Harden.** The API key is hidden from agent shell commands, and setup errors fail fast instead of counting as agent failures.
5. **Rerun** the same evaluation, with the same model and the same grader.

## Results

| | Before | After |
|---|---|---|
| All-pass (builds, installs, hidden tests pass) | 9/12 | **11/12** |
| False success (claimed success, failed the check) | 3/12 | **1/12** |

The remaining false success is the limit of self-verification: the agent's own tests agreed with its own bug. Only independent tests catch that.

The full write-up is in **[WRITEUP.md](WRITEUP.md)**: design, grader validation, observations, isolation limits, cost (verification about doubles wall time) and next steps. The commit history follows the order of the work: grader → baseline → failing tests → fixes → rerun.

## Run it

```bash
uv sync --all-extras
uv run python -m pytest -q
```

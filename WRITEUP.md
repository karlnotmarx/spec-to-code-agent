# Spec-to-Code Agent

An LLM agent that takes a Markdown specification and builds an installable Python package from it, plus an evaluation harness that measures whether the packages it produces actually work.

This started as a one-shot agent written by an AI. This write-up covers what I changed, why, and how I measured the effect.

## The problem

Coding agents report success based on what the model says, not on what it built. An agent that says "done" may have produced a package that doesn't build, install, import, or pass its spec. When agents run unattended, that gap is the main risk: failures look like successes.

## Approach

Measure with an independent grader that builds and installs each result in a fresh environment, then runs hidden tests against the installed package, so there's no LLM judge and no self-report. Take a baseline, fix the success logic test-first, harden the setup, then rerun the same evaluation with the same model and grader.

---

## How to run

### Setup

```bash
uv sync --all-extras
export ANTHROPIC_API_KEY="<your key>"
```

`ANTHROPIC_BASE_URL` can optionally point at a compatible proxy. The model can be chosen with `--model`.

### Run the agent

```bash
uv run spec-agent examples/calculator.md output/
```

Options: `--model` (default `evals-anthropic/claude-sonnet-4-6`), `--max-iterations` (default 50), `-v` for verbose logs.

### Run the tests

```bash
uv run python -m pytest -q
```

46 tests. `tests/test_verify.py` builds real packages, so it needs network access and takes about 20 seconds.

### Run the evaluation

```bash
uv run python evals/run_evals.py --label mylabel
```

Runs the agent on 4 exercises, 3 times each, 4 in parallel, and grades every result independently. Results go to `evals/results/<label>.json` and a summary table in `evals/results/<label>.md`. Useful options: `--specs transpose`, `--repeats 1`, `--workers 1`.

Check the grader itself (no API calls):

```bash
uv run python evals/grade.py --oracle evals/specs/transpose   # reference solution: must pass
uv run python evals/grade.py --empty evals/specs/transpose    # empty folder: must fail
```

---

## What I focused on, and why

The goal is a package that **can be built, tested, installed, and run.** The original agent never checked any of that. It reported `success=True` whenever:

- the model called `done` (believed without checking), or
- the model replied with text and no tool call (treated as finished, even if nothing had been built), including replies cut off by the token limit.

A test in the original suite asserted the second case as correct behaviour.

So "success" meant "the model said so." I focused on making success mean what success should mean, and on measuring the difference.

---

## Results

Same 4 exercises, 3 runs each, same model (`evals-anthropic/claude-sonnet-4-6`), graded by the same independent checker.

| | Baseline (original agent) | After fixes |
|---|---|---|
| **All-pass** (builds, installs, hidden tests pass) | 9/12 | **11/12** |
| **Agent claimed success** | 12/12 | 12/12 |
| **False success** (claimed success, but failed the check) | 3/12 | **1/12** |
| Mean iterations per run | 11.0 | 14.5 |
| Mean wall time per run (rough, runs in parallel) | ~57 s | ~129 s |

Per exercise (all-pass):

| Exercise | Baseline | After |
|---|---|---|
| `transpose` | 3/3 | 2/3 |
| `book-store` | 3/3 | 3/3 |
| `bowling` | 3/3 | 3/3 |
| `phone-number` | **0/3** | **3/3** |

**Sample size:** 12 runs per condition, so each run moves a rate by about 8 points. Only large differences are meaningful. The headline metric is a pass@1 rate over 3 runs per exercise: the agent's user gets one run, not the best of several.

**Cost:** iterations went up about 30% and wall time roughly doubled. Checking the package before accepting `done` adds an install and test cycle, and each rejected `done` adds more rounds. Run-to-run variation also plays a part: `bowling` passed in both conditions but went from 10–11 iterations to 17–26. The runner didn't record how many `done` calls were rejected per run, so I can't split the increase between the check and variation. For this task, catching broken packages is worth it, but it's a real trade-off.

The two fixes after the "after" run (startup key check, environment filter) change how the agent fails, not what it builds, so they're not reflected in these numbers.

---

## What I changed

In commit order. Each fix has tests; the success-logic fixes started as failing tests.

### 1. Default model name

`DEFAULT_MODEL` was `claude-sonnet-4-20250514`. The proxy rejects unprefixed names (`403 team_model_access_denied`), so the agent couldn't make a single API call with its defaults. Changed to `evals-anthropic/claude-sonnet-4-6`.

### 2. Evaluation harness (`evals/`)

- **Specs** (`evals/specs/`): 4 Python exercises from the [polyglot benchmark](https://github.com/Aider-AI/polyglot-benchmark) (from Exercism). Each has the exercise instructions as `spec.md`, the exercise's own test file as `hidden_test.py`, and the reference solution as `oracle.py`.
- **Grader** (`evals/grade.py`): four checks in a fresh environment, no model calls. See [How I assessed quality](#how-i-assessed-quality).
- **Runner** (`evals/run_evals.py`): runs the agent as a separate process, each run in its own temp folder and its own Python environment, with a 15-minute limit, then grades the result.

### 3. Text-only and cut-off replies no longer count as success

A reply with no tool call now ends the run with `success=False` and a status: `no_done_signal`, or `truncated` if the reply was cut off by the token limit. Every result now has a status saying how the run ended.

### 4. `done` is checked before it's accepted (`spec_agent/verify.py`)

When the model calls `done`, the agent:

1. installs the package with `pip install .` into a fresh environment,
2. copies the agent's own tests to a **separate empty folder** and runs them there, so they can only import the **installed** package.

If the check fails, the error goes back to the model as the result of `done`, and the loop continues. After 3 failed checks the run ends as `verification_failed`. `success` is only true for `verified`.

Running the tests from a separate folder is the important part. The baseline's `phone-number` failures (below) passed the agent's own tests from inside its source folder; a check that ran there would have accepted the same broken package.

### 5. Clear error for a missing API key

A missing key used to crash on the first API call with a 25-line traceback (a `TypeError` from the SDK, raised before any network call). `Agent.create()` now checks for the key and raises a clear error; the CLI prints one line and exits with code 1.

### 6. The API key is no longer visible to agent commands

`execute_command` passed the whole environment to every shell command, so any command could print `ANTHROPIC_API_KEY`. Commands now get a short allowlist (`PATH`, `HOME`, `TMPDIR`, locale variables, `VIRTUAL_ENV`). `VIRTUAL_ENV` has to stay: the eval runner uses it to keep each run's installs isolated. A test checks that a fake key set in the environment doesn't appear in the output of `env`.

### 7. The runner stops if the key is missing

After a change, a missing key produced a clean exit instead of a traceback, so the runner counted it as an agent failure rather than a setup problem. The runner now checks for the key once, before starting any runs.

---

## How I assessed quality

### Execution, not self-report, not an LLM judge

The grader decides whether a run passed by **running** the result:

1. **Build:** `pip wheel . --no-deps`
2. **Install:** `pip install .` into a fresh environment (a normal install, not editable)
3. **Agent's own tests:** `pytest` in the agent's folder. Recorded, but **not** part of the score
4. **Hidden tests:** the exercise's own test file, copied to a separate empty folder and run against the installed package

**A run passes if 1, 2 and 4 pass.** pytest's exit code 5 ("no tests collected") counts as a failure.

**Why the agent's own tests aren't the score:** the same model writes the code and the tests from one reading of the spec. If it misreads the spec, both share the mistake and still agree. This showed up in the data (see observations): runs with 19/19 or 42/42 own tests passing had hidden-test failures or couldn't be imported at all.

**Why hidden tests:** they were written by Exercism's maintainers long before this agent existed, so nothing the agent did could have shaped them.

**Why no LLM judge:** the code can be run, so there's no need to have a model predict what it does. And the bug being fixed is "believing a claim instead of checking"; a judge would bring the same problem into the measurement.

### Interface notes

The hidden tests import specific names (for example `from phone_number import PhoneNumber`). Exercism's instructions don't state these, because learners get a starter file with the names already in it. I didn't give the agent the starter file, so each `spec.md` ends with a short **Interface** section listing the module name, function or class names, every method or attribute the tests use, and input/output formats. It says nothing about how to solve the problem. A real client ordering a package would specify the same things.

### Checking the grader

Before any agent runs, the grader was run on inputs with known answers:

| Input | Build | Install | Hidden tests |
|---|---|---|---|
| Reference solution, `transpose` | ✓ | ✓ | 12/12 |
| Reference solution, `book-store` | ✓ | ✓ | 20/20 |
| Reference solution, `bowling` | ✓ | ✓ | 31/31 |
| Reference solution, `phone-number` | ✓ | ✓ | 21/21 |
| Empty folder | ✗ | ✗ | ✗ |

The reference solution is packaged as an ordinary folder and graded exactly like the agent's output. The empty folder's own-tests check returns pytest exit code 5 and is correctly scored as a failure, and its hidden test fails with `ModuleNotFoundError`, showing it can only reach an installed package.

**What this doesn't prove:** the reference solution never reads `spec.md`, so a passing oracle says nothing about whether the spec tells the agent everything the tests need. That gap caught me out (see below); I then checked each spec against its test file's imports, attributes and error messages.

### Exercise choice

| Exercise | Why |
|---|---|
| `transpose` | Easy. If it fails, suspect the harness first |
| `book-store` | Medium; the obvious approach is wrong |
| `bowling` | Hard; state across calls |
| `phone-number` | A class read through attributes, with error cases |

Two plain functions, a class with methods, a class with attributes: 84 hidden tests in total. I left out `calculator.md` (no independent tests exist for it; I used it as a manual smoke test).

---

## Observations

**The agent's own tests agreed with its bugs.**

- *Baseline, `phone-number`, 3/3 runs:* the package built and installed, but the hidden tests couldn't import it (`No module named 'phone_number'`). The agent wrote a single-file module (`phone_number.py`) but configured `[tool.setuptools.packages.find]`, which only finds package **folders**, so the installed package was empty. Its own 42–52 tests passed because they ran from its source folder, where the file was found directly. After fix 4, the check caught this every time and the agent fixed its packaging.
- *After, `transpose`, 1 run:* own tests 19/19, hidden tests 8/12. The agent's implementation stripped trailing spaces the exercise requires, and its own tests asserted the same wrong behaviour. The fix can't catch this, since it runs the agent's own tests; only independent tests can. This isn't a regression (the baseline got lucky on this exercise with 3 samples), but it's a clear limit of self-verification.

**My own specs were wrong at first.** The first baseline scored 6/12: `book-store` and `phone-number` failed 3/3, identically. The cause was my spec files, not the agent: `phone-number`'s Interface section never saved, and `book-store`'s never said prices are in cents, while the instructions' examples are all in dollars. The agent's own tests passed 100% on those runs too. Identical failures across repeats were the clue; real agent mistakes vary. I fixed the specs and reran the baseline; the results above are from the corrected specs.

**The same wrong build backend, three times.** In three separate manual runs of `calculator.md`, the model wrote `build-backend = "setuptools.backends.legacy:build"`, which doesn't exist (the correct value is `setuptools.build_meta`). With no memory between runs, the same invented string each time suggests a consistent gap in the model's knowledge, not a random slip. It recovered after pip's error each time on macOS. A one-line prompt hint would probably remove it; I didn't test that.

**Installs leaked into my own environment.** Run by hand, the agent's `pip install -e .` went into my conda base environment. The eval runner prevents this by giving each run its own environment first on `PATH`; after a smoke run, nothing new appeared in conda base.

**Hangs and stalls.** On macOS against the proxy, one model call took 5 minutes with no timeout set. Earlier, on Windows against a different endpoint, a `pip install` run by the agent hung for hours, and the 60-second command timeout didn't stop it. I didn't reproduce this on macOS and didn't confirm the cause.

**Two fixes interacted.** Once the agent gave a clean error for a missing key, the runner stopped recognising it as a setup fault and counted it as an agent failure. Found on a smoke run, fixed in the runner.

---

## Isolation: what's protected and what isn't

**Protected now:**

- The API key isn't visible to agent commands.
- The file tools (`write_file`, `read_file`, `list_files`) are restricted to the work folder.
- In evals, each run's installs go into that run's own environment.

**Not protected:**

- `execute_command` runs any shell command with my user's permissions. It can read or change files anywhere I can. The original blocklist catches five exact strings (`rm -rf /`, `sudo `, …) and is easy to get around (`rm -fr /`, for example).
- The file tools' path check compares text, not folders, so a sibling folder with a matching prefix (`/tmp/run1` vs `/tmp/run10`) gets through.
- Nothing restricts network access. The hidden tests are public on GitHub, so an agent could in principle look them up.

A blocklist can't be made safe. The real fix is running commands in a container with no access to the host filesystem or network.

---

## Development methodology

- **Order:** read the original code and wrote down findings; ran it by hand; built the grader, proved it with the reference and empty-folder checks, built the runner, ran the baseline; then made fixes, writing failing tests first for the success logic; reran the same evaluation. The commit history follows this order.
- **Tools:** Claude (via opencode) to write the harness and the fixes; a second Claude session to review plans and fact-check claims before they went into code or notes. The agent under test is Sonnet 4.6 via the proxy.
- **Time:** Built over a few sessions (about 4 hours of active work).
- **Same model family:** every model involved was Claude, so the tools that helped design the evaluation share blind spots with the agent being evaluated (what to test, what counts as "done"). The scoring itself makes no model calls: it runs third-party tests against the installed package, so it can't share the agent's biases. The risk is in the design choices, which I mitigated by checking the grader on known-good and known-bad inputs, and by verifying claims against actual runs rather than accepting them. Using a different model family for review would be a cheap improvement.
- **Substantial prompts:** mostly short, task-by-task instructions ("write a grader with these four checks," "why did this run fail," "is this claim supported by the output"), rather than one large prompt. The most useful pattern was asking the reviewing session to check a proposed plan against the goal and the observed results before building it.
- **Where I corrected the AI assistants** (examples):
  - **The AI assistant suggested** running the agent's tests inside the agent's own folder. The baseline showed this would have passed the broken `phone-number` package, so I had the tests run from a separate folder instead.
  - **An eval plan written by the AI assistant** claimed one of my runs had "failed to install, run pytest from the working directory, and called done." None of my runs did that, so I removed the claim.
  - **Notes written by the AI assistant** quoted precise statistics from research papers that I couldn't verify, so I left them out.
  - **The first version of the runner, written by the AI assistant,** treated any run whose log contained the word "Traceback" as an infrastructure fault. pip's error messages contain that word too, so real agent failures would have been hidden. I changed it to only treat API errors as faults.
  - **A shell command the AI assistant gave me** wrote a placeholder instead of my real API key into my shell settings. The proxy's error (`expected to start with 'sk-'`) made the cause obvious, and I fixed it.

---

## Limitations and next steps

### What I'd do differently

Build the grader and run a baseline in the first hour, before reading the code in depth. The measurements found things reading didn't (the packaging bug, the gaps in my own specs), and they'd have focused the review. I'd also develop on macOS or Linux from the start, and write spec files with a script rather than pasting them into the terminal.

### Next improvements

- Treat a text-only reply as a possible `done` and run the same check, so success depends on the check rather than on the model choosing the right tool. The cost is that the model loses a way to say "I'm not finished yet", so it's worth measuring before adopting.
- Fix the path check to compare folders, not strings.
- Kill the whole process group on command timeout, so child processes can't hang or linger.
- Set a timeout on API calls and turn API errors into a clear status instead of a traceback (a model call stalled for 5 minutes with no timeout).
- Keep each run's agent log alongside the results (temp folders are currently deleted), and record how many `done` calls were rejected per run.
- Add tests for the eval runner (`run_evals.py`). The agent has tests and the grader has its known-good/known-bad checks, but the runner that produced every number above was only checked by hand, through smoke runs.
- Share test fixtures instead of repeating them per file.
- Test a one-line prompt hint for the build backend the model kept getting wrong, and rerun everything with 5 repeats instead of 3.

### Longer-term direction

Run commands in a container with no access to the host or the network. Build a larger evaluation set: more exercises, other languages, and full multi-module packages rather than single functions. Track results over time in CI so every change is measured. Record token usage and cost per run, and compare models. And explore checks that don't rely on the agent's own tests at all, for example generating tests from the spec separately from the code, since this run showed self-written tests agreeing with their own bugs.
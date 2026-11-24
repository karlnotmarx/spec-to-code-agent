"""Fix 2: done is only accepted if the package passes an independent check.

The verifier is replaced with a fake here, so these tests are fast and make
no network calls. test_verify.py tests the real check.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from spec_agent.agent import Agent, AgentConfig
from spec_agent.verify import VerifyResult

import pytest


@pytest.fixture(autouse=True)
def _dummy_api_key(monkeypatch):
    """Agent.create() requires a key; tests use a fake client anyway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

def _done(n: int) -> SimpleNamespace:
    """A model reply that calls done."""
    block = SimpleNamespace(type="tool_use", id=f"done-{n}", name="done",
                            input={"summary": "all good"})
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def _agent(work_dir: Path, replies: list, verifier, **config) -> Agent:
    agent = Agent.create(work_dir=work_dir, config=AgentConfig(**config))
    agent.client = MagicMock()
    agent.client.messages.create.side_effect = replies
    agent.verifier = verifier
    return agent


def test_done_is_accepted_when_the_check_passes(tmp_path: Path):
    agent = _agent(tmp_path / "work", [_done(1)], lambda wd: VerifyResult(True, "ok"))

    result = agent.run("spec")

    assert result.success
    assert result.status == "verified"


def test_failed_check_is_sent_back_and_the_agent_continues(tmp_path: Path):
    checks = iter([
        VerifyResult(False, "ModuleNotFoundError: No module named 'phone_number'"),
        VerifyResult(True, "ok"),
    ])
    agent = _agent(tmp_path / "work", [_done(1), _done(2)], lambda wd: next(checks))

    result = agent.run("spec")

    assert result.success
    assert result.iterations == 2
    # messages: [0] spec, [1] first reply, [2] results for the first reply
    first_done_result = agent.messages[2]["content"][0]
    assert first_done_result["is_error"] is True
    assert "No module named 'phone_number'" in first_done_result["content"]


def test_gives_up_after_repeated_failed_checks(tmp_path: Path):
    agent = _agent(
        tmp_path / "work",
        [_done(n) for n in range(1, 10)],
        lambda wd: VerifyResult(False, "tests failed"),
        max_verify_attempts=3,
    )

    result = agent.run("spec")

    assert not result.success
    assert result.status == "verification_failed"
    assert result.iterations == 3
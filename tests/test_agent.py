"""Tests for the agent loop.

These tests use a mock LLM client to verify the agent's control flow
without making real API calls. This lets us test:
- The iteration loop and stop condition
- Tool call dispatch and result handling
- Error recovery behavior
- Maximum iteration limits
- Startup checks
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from spec_agent.agent import Agent, AgentConfig
from spec_agent.verify import VerifyResult


# ---------------------------------------------------------------------------
# Fixtures applied to every test in this file
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _passing_verifier(monkeypatch):
    """These tests are about the loop, not packaging. Fake a passing check."""
    monkeypatch.setattr(
        "spec_agent.agent.verify_package", lambda work_dir: VerifyResult(True, "ok")
    )


@pytest.fixture(autouse=True)
def _dummy_api_key(monkeypatch):
    """Agent.create() now requires a key; tests use a fake client anyway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# Helpers for building fake model replies
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _make_tool_use_block(
    tool_id: str, name: str, input_data: dict[str, Any]
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_data)


def _make_response(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks))


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workspace"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentLoop:
    """Test the core agent iteration loop."""

    def test_single_iteration_done(self, work_dir: Path):
        """Agent calls done on first iteration — should complete immediately."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            _make_text_block("I'll finish now."),
            _make_tool_use_block("t1", "done", {"summary": "Nothing to do"}),
        )

        agent = Agent(
            config=AgentConfig(max_iterations=10),
            client=mock_client,
            tool_executor=MagicMock(),
        )
        result = agent.run("Test spec")

        assert result.success
        assert result.iterations == 1
        assert "Nothing to do" in result.summary

    def test_write_then_done(self, work_dir: Path):
        """Agent writes a file then signals done."""
        mock_client = MagicMock()

        # First call: write a file
        # Second call: signal done
        mock_client.messages.create.side_effect = [
            _make_response(
                _make_tool_use_block(
                    "t1", "write_file", {"path": "hello.py", "content": "print('hi')"}
                ),
            ),
            _make_response(
                _make_tool_use_block("t2", "done", {"summary": "Created hello.py"}),
            ),
        ]

        agent = Agent.create(work_dir=work_dir)
        agent.client = mock_client

        result = agent.run("Create hello.py")

        assert result.success
        assert result.iterations == 2
        assert result.tool_calls == 1  # write_file only; done doesn't count
        assert (work_dir / "hello.py").read_text() == "print('hi')"

    def test_max_iterations_reached(self, work_dir: Path):
        """Agent that never calls done should stop at max iterations."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            _make_tool_use_block("t1", "list_files", {"path": "."}),
        )

        agent = Agent.create(work_dir=work_dir, config=AgentConfig(max_iterations=3))
        agent.client = mock_client

        result = agent.run("Infinite loop spec")

        assert not result.success
        assert result.iterations == 3
        assert "maximum iterations" in result.summary.lower()

    def test_text_only_reply_is_not_success(self, work_dir: Path):
        """A text-only reply (no tool calls, no done) must not count as success."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            _make_text_block("I'm done thinking."),
        )

        agent = Agent.create(work_dir=work_dir)
        agent.client = mock_client

        result = agent.run("Simple spec")
        assert not result.success
        assert result.iterations == 1

    def test_tool_error_continues_loop(self, work_dir: Path):
        """Tool errors should not crash the agent — they're fed back to the LLM."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            # First: try to read a non-existent file
            _make_response(
                _make_tool_use_block("t1", "read_file", {"path": "nope.txt"}),
            ),
            # Second: recover and signal done
            _make_response(
                _make_tool_use_block("t2", "done", {"summary": "Recovered from error"}),
            ),
        ]

        agent = Agent.create(work_dir=work_dir)
        agent.client = mock_client

        result = agent.run("Read missing file spec")
        assert result.success
        assert result.iterations == 2

    def test_truncated_reply_is_not_success(self, work_dir: Path):
        """A reply cut off by max_tokens must not count as success."""
        mock_client = MagicMock()
        response = _make_response(_make_text_block("Writing the parser now: def pars"))
        response.stop_reason = "max_tokens"
        mock_client.messages.create.return_value = response

        agent = Agent.create(work_dir=work_dir)
        agent.client = mock_client

        result = agent.run("Simple spec")
        assert not result.success


class TestAgentConfig:
    def test_defaults(self):
        config = AgentConfig()
        assert config.max_iterations == 50
        assert config.max_tokens == 16384
        assert "claude" in config.model.lower() or "sonnet" in config.model.lower()

    def test_custom_values(self):
        config = AgentConfig(model="test-model", max_iterations=5, max_tokens=1024)
        assert config.model == "test-model"
        assert config.max_iterations == 5
        assert config.max_tokens == 1024


class TestStartup:
    def test_missing_api_key_gives_clear_error(self, tmp_path: Path, monkeypatch):
        """A missing key fails at construction with a clear message, not a traceback."""
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
            Agent.create(work_dir=tmp_path)
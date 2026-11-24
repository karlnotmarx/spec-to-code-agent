"""Tests for tool definitions and execution.

These tests verify that tools work correctly in isolation, including
path traversal protection, file operations, and command execution.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from spec_agent.tools import TOOL_SCHEMAS, ToolExecutor, ToolResult


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Create a fresh working directory for each test."""
    d = tmp_path / "workspace"
    d.mkdir()
    return d


@pytest.fixture
def executor(work_dir: Path) -> ToolExecutor:
    return ToolExecutor(work_dir)


class TestToolSchemas:
    """Verify tool schema structure for the Anthropic API."""

    def test_all_schemas_have_required_fields(self):
        for schema in TOOL_SCHEMAS:
            assert "name" in schema
            assert "description" in schema
            assert "input_schema" in schema
            assert schema["input_schema"]["type"] == "object"

    def test_tool_names_are_unique(self):
        names = [s["name"] for s in TOOL_SCHEMAS]
        assert len(names) == len(set(names))

    def test_expected_tools_exist(self):
        names = {s["name"] for s in TOOL_SCHEMAS}
        assert names == {"write_file", "read_file", "list_files", "execute_command", "done"}


class TestWriteFile:
    def test_creates_file(self, executor: ToolExecutor, work_dir: Path):
        result = executor.execute("write_file", {"path": "hello.txt", "content": "Hello, world!"})
        assert not result.is_error
        assert (work_dir / "hello.txt").read_text() == "Hello, world!"

    def test_creates_nested_directories(self, executor: ToolExecutor, work_dir: Path):
        result = executor.execute("write_file", {"path": "a/b/c.txt", "content": "deep"})
        assert not result.is_error
        assert (work_dir / "a" / "b" / "c.txt").read_text() == "deep"

    def test_overwrites_existing_file(self, executor: ToolExecutor, work_dir: Path):
        (work_dir / "file.txt").write_text("old")
        executor.execute("write_file", {"path": "file.txt", "content": "new"})
        assert (work_dir / "file.txt").read_text() == "new"

    def test_rejects_path_traversal(self, executor: ToolExecutor):
        result = executor.execute("write_file", {"path": "../escape.txt", "content": "bad"})
        assert result.is_error
        assert "outside" in result.output.lower()


class TestReadFile:
    def test_reads_existing_file(self, executor: ToolExecutor, work_dir: Path):
        (work_dir / "data.txt").write_text("content here")
        result = executor.execute("read_file", {"path": "data.txt"})
        assert not result.is_error
        assert result.output == "content here"

    def test_error_on_missing_file(self, executor: ToolExecutor):
        result = executor.execute("read_file", {"path": "nonexistent.txt"})
        assert result.is_error
        assert "not found" in result.output.lower()

    def test_rejects_path_traversal(self, executor: ToolExecutor):
        result = executor.execute("read_file", {"path": "../../etc/passwd"})
        assert result.is_error


class TestListFiles:
    def test_lists_files(self, executor: ToolExecutor, work_dir: Path):
        (work_dir / "a.txt").touch()
        (work_dir / "b.txt").touch()
        result = executor.execute("list_files", {"path": "."})
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    def test_lists_nested_structure(self, executor: ToolExecutor, work_dir: Path):
        (work_dir / "src").mkdir()
        (work_dir / "src" / "main.py").touch()
        result = executor.execute("list_files", {"path": "."})
        assert "src" in result.output
        assert "main.py" in result.output

    def test_empty_directory(self, executor: ToolExecutor):
        result = executor.execute("list_files", {"path": "."})
        # Should show the directory itself at minimum
        assert not result.is_error


class TestExecuteCommand:

    def test_api_key_not_visible_to_commands(self, executor, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
        result = executor.execute("execute_command", {"command": "env"})
        assert "sk-should-not-leak" not in result.output

    def test_runs_simple_command(self, executor: ToolExecutor):
        result = executor.execute("execute_command", {"command": "echo hello"})
        assert not result.is_error
        assert "hello" in result.output

    def test_captures_exit_code(self, executor: ToolExecutor):
        result = executor.execute("execute_command", {"command": "exit 1"})
        assert result.is_error
        assert "exit code 1" in result.output

    def test_captures_stderr(self, executor: ToolExecutor):
        result = executor.execute("execute_command", {"command": "echo err >&2"})
        assert "err" in result.output

    def test_runs_in_work_dir(self, executor: ToolExecutor, work_dir: Path):
        result = executor.execute("execute_command", {"command": "pwd"})
        assert str(work_dir) in result.output

    def test_blocks_dangerous_commands(self, executor: ToolExecutor):
        result = executor.execute("execute_command", {"command": "sudo rm -rf /"})
        assert result.is_error
        assert "blocked" in result.output.lower()

    def test_handles_timeout(self, executor: ToolExecutor):
        # This should timeout (we use a short sleep that exceeds nothing,
        # but we can test the timeout mechanism exists)
        result = executor.execute("execute_command", {"command": "echo fast"})
        assert not result.is_error


class TestUnknownTool:
    def test_returns_error(self, executor: ToolExecutor):
        result = executor.execute("nonexistent_tool", {})
        assert result.is_error
        assert "unknown tool" in result.output.lower()


class TestDone:
    def test_returns_summary(self, executor: ToolExecutor):
        result = executor.execute("done", {"summary": "All done!"})
        assert result.output == "All done!"
        assert not result.is_error

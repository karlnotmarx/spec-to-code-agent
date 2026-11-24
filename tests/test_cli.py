"""Tests for the CLI argument parsing and entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_agent.cli import main, parse_args


class TestParseArgs:
    def test_required_args(self, tmp_path: Path):
        args = parse_args(["spec.md", str(tmp_path)])
        assert args.spec_file == Path("spec.md")
        assert args.work_dir == tmp_path

    def test_optional_model(self, tmp_path: Path):
        args = parse_args(["spec.md", str(tmp_path), "--model", "test-model"])
        assert args.model == "test-model"

    def test_optional_max_iterations(self, tmp_path: Path):
        args = parse_args(["spec.md", str(tmp_path), "--max-iterations", "10"])
        assert args.max_iterations == 10

    def test_verbose_flag(self, tmp_path: Path):
        args = parse_args(["spec.md", str(tmp_path), "-v"])
        assert args.verbose is True

    def test_defaults(self, tmp_path: Path):
        args = parse_args(["spec.md", str(tmp_path)])
        assert args.model is None
        assert args.max_iterations is None
        assert args.verbose is False


class TestMain:
    def test_missing_spec_file(self, tmp_path: Path):
        result = main(["nonexistent.md", str(tmp_path)])
        assert result == 1

    def test_empty_spec_file(self, tmp_path: Path):
        spec = tmp_path / "empty.md"
        spec.write_text("")
        result = main([str(spec), str(tmp_path / "out")])
        assert result == 1

    @patch("spec_agent.cli.Agent")
    def test_successful_run(self, mock_agent_cls: MagicMock, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\nBuild something.")

        mock_agent = MagicMock()
        mock_agent.run.return_value = MagicMock(
            success=True, summary="Done", iterations=1, tool_calls=2
        )
        mock_agent_cls.create.return_value = mock_agent

        result = main([str(spec), str(tmp_path / "out")])
        assert result == 0
        mock_agent.run.assert_called_once()

    @patch("spec_agent.cli.Agent")
    def test_failed_run(self, mock_agent_cls: MagicMock, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\nBuild something.")

        mock_agent = MagicMock()
        mock_agent.run.return_value = MagicMock(
            success=False, summary="Failed", iterations=50, tool_calls=100
        )
        mock_agent_cls.create.return_value = mock_agent

        result = main([str(spec), str(tmp_path / "out")])
        assert result == 1

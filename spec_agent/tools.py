"""Tool definitions and execution for the coding agent.

Tools are the agent's interface to the environment. Each tool is defined as an
Anthropic tool schema and has a corresponding execution function. We keep the
tool set intentionally small and general-purpose:

- write_file: Create or overwrite files in the working directory
- read_file: Read file contents (for verifying what was written or reading generated output)
- list_files: Explore directory structure
- execute_command: Run shell commands (build, test, install) inside the sandbox
- done: Signal that the task is complete

This minimal set covers the full "write code → run it → read output → iterate"
loop without giving the agent unnecessary capabilities.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Only these variables are passed to agent commands. Everything else,
# including ANTHROPIC_API_KEY, stays out of reach of the agent's shell.
# VIRTUAL_ENV must stay: the eval runner uses it to keep installs isolated.
_ALLOWED_ENV_KEYS = {
    "PATH", "HOME", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    "VIRTUAL_ENV",
}

# Maximum output length from command execution to avoid flooding context
MAX_COMMAND_OUTPUT = 10_000
# Command timeout in seconds
COMMAND_TIMEOUT = 60

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file at the given path relative to the working directory. "
            "Creates parent directories automatically. Use this to write source code, "
            "configuration files, tests, and any other project files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the working directory (e.g. 'src/main.py')",
                },
                "content": {
                    "type": "string",
                    "description": "The full file content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file at the given path relative to the working directory. "
            "Use this to verify written files, inspect error logs, or read generated output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the working directory",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories at a path relative to the working directory. "
            "Returns a recursive listing up to 3 levels deep. Use this to understand "
            "project structure or verify that files were created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to list (defaults to '.' for the root)",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
    {
        "name": "execute_command",
        "description": (
            "Execute a shell command in the working directory. Use this to run builds, "
            "tests, install dependencies, or verify that the project works. Commands run "
            f"with a {COMMAND_TIMEOUT}s timeout and output is truncated to {MAX_COMMAND_OUTPUT} chars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute (e.g. 'python -m pytest')",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "done",
        "description": (
            "Signal that the task is complete. Call this when the specification has been "
            "fully implemented, tests pass, and the package is ready. Include a brief "
            "summary of what was created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of the completed work",
                },
            },
            "required": ["summary"],
        },
    },
]


@dataclass
class ToolResult:
    """Result of executing a tool."""

    output: str
    is_error: bool = False


class ToolExecutor:
    """Executes tools within a sandboxed working directory.

    All file operations are restricted to the working directory to prevent
    the agent from modifying files outside its designated workspace.
    """

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> ToolResult:
        """Dispatch a tool call to the appropriate handler."""
        handlers = {
            "write_file": self._write_file,
            "read_file": self._read_file,
            "list_files": self._list_files,
            "execute_command": self._execute_command,
            "done": self._done,
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(output=f"Unknown tool: {tool_name}", is_error=True)

        try:
            return handler(tool_input)
        except Exception as e:
            return ToolResult(output=f"Tool error: {type(e).__name__}: {e}", is_error=True)

    def _resolve_path(self, relative_path: str) -> Path:
        """Resolve a relative path and verify it stays within the working directory."""
        resolved = (self.work_dir / relative_path).resolve()
        if not str(resolved).startswith(str(self.work_dir)):
            raise ValueError(
                f"Path '{relative_path}' resolves outside the working directory"
            )
        return resolved

    def _write_file(self, inputs: dict[str, Any]) -> ToolResult:
        path = self._resolve_path(inputs["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inputs["content"], encoding="utf-8")
        return ToolResult(output=f"Written {len(inputs['content'])} bytes to {inputs['path']}")

    def _read_file(self, inputs: dict[str, Any]) -> ToolResult:
        path = self._resolve_path(inputs["path"])
        if not path.is_file():
            return ToolResult(output=f"File not found: {inputs['path']}", is_error=True)
        content = path.read_text(encoding="utf-8")
        return ToolResult(output=content)

    def _list_files(self, inputs: dict[str, Any]) -> ToolResult:
        rel_path = inputs.get("path", ".")
        path = self._resolve_path(rel_path)
        if not path.is_dir():
            return ToolResult(output=f"Not a directory: {rel_path}", is_error=True)

        lines: list[str] = []
        for root, dirs, files in os.walk(path):
            depth = str(Path(root).resolve()).replace(str(path.resolve()), "").count(os.sep)
            if depth >= 3:
                dirs.clear()
                continue
            indent = "  " * depth
            dir_name = Path(root).name if depth > 0 else rel_path
            lines.append(f"{indent}{dir_name}/")
            for f in sorted(files):
                lines.append(f"{indent}  {f}")
            dirs[:] = sorted(dirs)

        return ToolResult(output="\n".join(lines) if lines else "(empty directory)")

    def _execute_command(self, inputs: dict[str, Any]) -> ToolResult:
        command = inputs["command"]

        # Block commands that could escape the working directory
        dangerous_patterns = ["rm -rf /", "rm -rf ~", "sudo ", "mkfs", "dd if="]
        for pattern in dangerous_patterns:
            if pattern in command:
                return ToolResult(
                    output=f"Blocked potentially dangerous command containing '{pattern}'",
                    is_error=True,
                )

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.work_dir,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                env={**{k: v for k, v in os.environ.items() if k in _ALLOWED_ENV_KEYS},
                    "PYTHONDONTWRITEBYTECODE": "1"},            
                )
        except subprocess.TimeoutExpired:
            return ToolResult(
                output=f"Command timed out after {COMMAND_TIMEOUT}s: {command}",
                is_error=True,
            )

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")

        output = "\n".join(output_parts) or "(no output)"

        if len(output) > MAX_COMMAND_OUTPUT:
            output = output[:MAX_COMMAND_OUTPUT] + f"\n... (truncated, {len(output)} total chars)"

        if result.returncode != 0:
            output = f"[exit code {result.returncode}]\n{output}"
            return ToolResult(output=output, is_error=True)

        return ToolResult(output=output)

    def _done(self, inputs: dict[str, Any]) -> ToolResult:
        return ToolResult(output=inputs.get("summary", "Done"))

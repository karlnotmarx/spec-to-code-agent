"""Core agent loop: the orchestrator that drives spec-to-code generation.

The agent follows the classic agentic loop:
1. Send the specification + conversation history to the LLM
2. If the LLM returns tool calls, execute them and append results
3. If the LLM returns a stop signal (the 'done' tool), exit
4. Repeat until done or max iterations reached

The loop is intentionally simple — complexity lives in the system prompt
(which teaches the LLM *how* to be a good coding agent) and in the tools
(which give it the ability to act on the environment).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic

from .tools import TOOL_SCHEMAS, ToolExecutor, ToolResult
from .verify import VerifyResult, verify_package

logger = logging.getLogger(__name__)

# Sensible defaults
DEFAULT_MODEL = "evals-anthropic/claude-sonnet-4-6"
MAX_ITERATIONS = 50

SYSTEM_PROMPT = """\
You are an expert software engineer acting as an autonomous coding agent. Your task \
is to implement a complete, working software package based on a specification provided \
by the user.

## Your approach

1. **Plan first**: Before writing any code, briefly outline your implementation plan. \
Identify the key components, file structure, and any decisions you need to make.

2. **Implement incrementally**: Write files one at a time. Start with project \
configuration (e.g., pyproject.toml), then core modules, then tests.

3. **Verify as you go**: After writing code, run it. Execute tests after writing them. \
Fix any errors before moving on. Don't assume code works — prove it.

4. **Handle errors gracefully**: If a command fails or a test breaks, read the error \
output carefully, diagnose the issue, and fix it. Don't give up on the first error.

5. **Complete the loop**: When everything works — code is written, tests pass, the \
package installs and runs — call the `done` tool with a summary.

## Guidelines

- Write clean, idiomatic code with clear structure.
- Include all necessary configuration files (pyproject.toml, etc.) so the package is \
installable.
- Write meaningful tests that cover the requirements, including edge cases and error \
handling.
- Follow the specification precisely. Implement all stated requirements.
- If the spec is ambiguous, make a reasonable choice and document it.
- Do NOT use `eval()` or other unsafe patterns unless explicitly required.
- Prefer standard library solutions when they suffice.
"""


@dataclass
class AgentConfig:
    """Configuration for the agent."""

    model: str = DEFAULT_MODEL
    max_iterations: int = MAX_ITERATIONS
    max_tokens: int = 16384
    max_verify_attempts: int = 3  # rejected done calls before giving up


@dataclass
class AgentResult:
    """Result of an agent run.

    status says how the run ended:
      "verified"             the model called done AND the package passed the check
      "verification_failed"  done was rejected max_verify_attempts times
      "no_done_signal"       the model replied with text only, no tool call
      "truncated"            the reply was cut off by max_tokens before any tool call
      "max_iterations"       the iteration limit was reached

    success is True only for "verified".
    """

    success: bool
    summary: str
    iterations: int
    tool_calls: int
    status: str = ""


@dataclass
class Agent:
    """LLM-powered coding agent that generates packages from specifications.

    The agent maintains a conversation with the LLM, providing it with tools
    to write files, execute commands, and explore the working directory. It
    runs in a loop until the LLM signals completion or the iteration limit
    is reached.
    """

    config: AgentConfig
    client: anthropic.Anthropic
    tool_executor: ToolExecutor
    messages: list[dict[str, Any]] = field(default_factory=list)
    verifier: Callable[[Path], VerifyResult] | None = None

    @classmethod
    def create(
        cls,
        work_dir: Path,
        config: AgentConfig | None = None,
    ) -> Agent:
        """Factory method to create a fully-wired agent."""
        config = config or AgentConfig()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it before running spec-agent."
            )
        client = anthropic.Anthropic()
        tool_executor = ToolExecutor(work_dir)
        return cls(
            config=config,
            client=client,
            tool_executor=tool_executor,
            verifier=verify_package,
        )

    def run(self, specification: str) -> AgentResult:
        """Run the agent loop to implement the given specification.

        Args:
            specification: The plain-language specification to implement.

        Returns:
            AgentResult with success status, summary, and statistics.
        """
        self.messages = [
            {
                "role": "user",
                "content": (
                    f"Please implement the following specification as a complete, "
                    f"working, installable package.\n\n"
                    f"<specification>\n{specification}\n</specification>"
                ),
            }
        ]

        total_tool_calls = 0
        verifier = self.verifier or verify_package
        failed_checks = 0
        last_report = ""

        for iteration in range(1, self.config.max_iterations + 1):
            logger.info("Iteration %d/%d", iteration, self.config.max_iterations)

            response = self._call_llm()

            # Process the response blocks
            tool_uses = []
            for block in response.content:
                if block.type == "text":
                    logger.info("Agent: %s", block.text[:200])
                elif block.type == "tool_use":
                    tool_uses.append(block)

            if not tool_uses:
                # A reply with no tool call is not a completion signal.
                # It may be mid-thought, a question nobody will answer, or cut off.
                if getattr(response, "stop_reason", None) == "max_tokens":
                    status = "truncated"
                    summary = "Model reply was cut off (max_tokens) before any tool call."
                else:
                    status = "no_done_signal"
                    summary = "Model stopped without calling done."
                return AgentResult(
                    success=False,
                    summary=summary,
                    iterations=iteration,
                    tool_calls=total_tool_calls,
                    status=status,
                )

            # Append the assistant message as-is
            self.messages.append({"role": "assistant", "content": response.content})

            # Execute each tool and collect results
            tool_results = []
            done_summary = None

            for tool_use in tool_uses:
                logger.info(
                    "Tool call: %s(%s)",
                    tool_use.name,
                    _summarize_input(tool_use.input),
                )

                if tool_use.name == "done":
                    # done is a claim, not evidence. Check the package before
                    # accepting it; if the check fails, send the failure back
                    # as the result of done and keep going.
                    check = verifier(self.tool_executor.work_dir)
                    logger.info("Verification on done: passed=%s", check.passed)
                    if check.passed:
                        done_summary = tool_use.input.get("summary", "Done")
                        content, is_error = check.report, False
                    else:
                        failed_checks += 1
                        last_report = check.report
                        content = (
                            "done was rejected: the package did not pass an "
                            "independent check.\n\n"
                            f"{check.report}\n\n"
                            "Fix the problem, then call done again."
                        )
                        is_error = True
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
                    continue

                result = self.tool_executor.execute(tool_use.name, tool_use.input)
                total_tool_calls += 1

                logger.info(
                    "Tool result: %s (error=%s, %d chars)",
                    result.output[:100],
                    result.is_error,
                    len(result.output),
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result.output,
                        "is_error": result.is_error,
                    }
                )

            self.messages.append({"role": "user", "content": tool_results})

            if done_summary is not None:
                return AgentResult(
                    success=True,
                    summary=done_summary,
                    iterations=iteration,
                    tool_calls=total_tool_calls,
                    status="verified",
                )

            if failed_checks >= self.config.max_verify_attempts:
                return AgentResult(
                    success=False,
                    summary=(
                        f"done was rejected {failed_checks} times. Last check:\n"
                        f"{last_report[-1000:]}"
                    ),
                    iterations=iteration,
                    tool_calls=total_tool_calls,
                    status="verification_failed",
                )

        return AgentResult(
            success=False,
            summary=f"Reached maximum iterations ({self.config.max_iterations})",
            iterations=self.config.max_iterations,
            tool_calls=total_tool_calls,
            status="max_iterations",
        )

    def _call_llm(self) -> anthropic.types.Message:
        """Make a single LLM API call with the current conversation state."""
        return self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=self.messages,
        )


def _summarize_input(tool_input: dict[str, Any]) -> str:
    """Create a brief summary of tool input for logging."""
    parts = []
    for key, value in tool_input.items():
        if isinstance(value, str) and len(value) > 60:
            parts.append(f"{key}='{value[:60]}...'")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)
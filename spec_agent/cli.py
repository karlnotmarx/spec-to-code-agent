"""Command-line interface for spec-agent.

Usage:
    spec-agent <specification-file> <work-dir> [--model MODEL] [--max-iterations N] [-v]

The CLI is deliberately simple: point it at a spec and a directory, and it
produces a working package. Verbosity and model selection are the only knobs
exposed — everything else is handled by sensible defaults.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .agent import Agent, AgentConfig


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="spec-agent",
        description="Generate a software package from a plain-language specification.",
    )
    parser.add_argument(
        "spec_file",
        type=Path,
        help="Path to the specification file (Markdown)",
    )
    parser.add_argument(
        "work_dir",
        type=Path,
        help="Working directory where the package will be created",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Anthropic model to use (default: evals-anthropic/claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum agent loop iterations (default: 50)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate inputs
    if not args.spec_file.is_file():
        print(f"Error: Specification file not found: {args.spec_file}", file=sys.stderr)
        return 1

    specification = args.spec_file.read_text(encoding="utf-8")
    if not specification.strip():
        print("Error: Specification file is empty", file=sys.stderr)
        return 1

    # Build config, only overriding defaults for explicitly-provided args
    config_kwargs = {}
    if args.model is not None:
        config_kwargs["model"] = args.model
    if args.max_iterations is not None:
        config_kwargs["max_iterations"] = args.max_iterations
    config = AgentConfig(**config_kwargs)

    work_dir = args.work_dir.resolve()
    print(f"Specification: {args.spec_file}")
    print(f"Working directory: {work_dir}")
    print(f"Model: {config.model}")
    print()

    try:
        agent = Agent.create(work_dir=work_dir, config=config)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    result = agent.run(specification)

    print()
    if result.success:
        print(f"✓ Completed in {result.iterations} iterations ({result.tool_calls} tool calls)")
        print(f"  {result.summary}")
        return 0
    else:
        print(f"✗ Failed after {result.iterations} iterations ({result.tool_calls} tool calls)")
        print(f"  {result.summary}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

# CLI Calculator

## Overview

Create a command-line calculator application as an installable Python package.

## Requirements

- The program accepts a mathematical expression as a single string argument.
- It evaluates the expression and prints the result.
- Supported operations: addition (+), subtraction (-), multiplication (*), division (/)
- Supports parentheses for grouping: `"(2 + 3) * 4"` → `20`
- Supports integer and floating-point numbers.
- Division should return a float when the result is not a whole number.

## Examples

```
$ calc "2 + 3"
5
$ calc "10 / 3"
3.3333333333333335
$ calc "(2 + 3) * (4 - 1)"
15
```

## Package structure

- Installable via `pip install -e .`
- Runnable as `python -m calculator <expression>` after installation
- Include a `pyproject.toml` with project metadata

## Error handling

- If no expression is provided, print usage and exit with code 1.
- If the expression is invalid (syntax error, division by zero), print a clear error message and exit with code 1.
- Do NOT use `eval()` on raw input — implement a safe expression parser.

## Testing

- Include unit tests using `pytest`.
- Test cases should cover:
  - Basic arithmetic: addition, subtraction, multiplication, division
  - Parenthesized expressions
  - Floating-point results
  - Error cases: empty input, invalid syntax, division by zero

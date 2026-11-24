"""The real check, on a tiny package that reproduces the baseline bug.

Slow (~20-40 s): creates venvs and installs from PyPI.
"""

from __future__ import annotations

from pathlib import Path

from spec_agent.verify import verify_package

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "phone-number"
version = "0.0.0"

{packaging}
"""


def _make_package(root: Path, packaging: str) -> Path:
    root.mkdir()
    (root / "phone_number.py").write_text("def area_code():\n    return '223'\n")
    (root / "pyproject.toml").write_text(PYPROJECT.format(packaging=packaging))
    (root / "tests").mkdir()
    (root / "tests" / "test_phone.py").write_text(
        "from phone_number import area_code\n\n"
        "def test_area_code():\n"
        "    assert area_code() == '223'\n"
    )
    return root


def test_rejects_a_package_that_installs_without_its_code(tmp_path: Path):
    # The exact baseline bug: a single-file module, but packages.find only
    # finds folders, so the installed package is empty.
    work = _make_package(
        tmp_path / "work",
        '[tool.setuptools.packages.find]\nwhere = ["."]\ninclude = ["phone_number*"]',
    )

    result = verify_package(work)

    assert not result.passed
    assert "phone_number" in result.report


def test_accepts_a_correctly_packaged_module(tmp_path: Path):
    work = _make_package(tmp_path / "work", '[tool.setuptools]\npy-modules = ["phone_number"]')

    result = verify_package(work)

    assert result.passed, result.report


def test_rejects_a_package_with_no_tests(tmp_path: Path):
    work = _make_package(tmp_path / "work", '[tool.setuptools]\npy-modules = ["phone_number"]')
    for f in (work / "tests").iterdir():
        f.unlink()
    (work / "tests").rmdir()

    result = verify_package(work)

    assert not result.passed
    assert "No tests found" in result.report
"""Guard the things this repository must not contain.

The project is a generic sample. Employer names, machine model numbers,
real addresses, and secrets do not belong in it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".txt", ".example"}
TEXT_NAMES = {
    "Dockerfile",
    "LICENSE",
    ".env.example",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _banned_tokens() -> tuple[str, ...]:
    # Built at runtime so this file does not contain the tokens it searches for.
    return ("ro" + "mi", "cm" + "20", "ro" + "misa")


def _files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            found.append(path)
    return found


def test_tree_has_no_employer_or_machine_identifiers() -> None:
    banned = _banned_tokens()
    for path in _files():
        text = path.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in text, f"{path} contains {token!r}"


def test_tree_has_no_unexpected_ip_addresses() -> None:
    for path in _files():
        text = path.read_text(encoding="utf-8")
        unexpected = sorted(set(IPV4.findall(text)) - ALLOWED_IPS)
        assert unexpected == [], f"{path} contains {unexpected}"


def test_yaml_files_do_not_assign_a_password() -> None:
    for path in _files():
        if path.suffix not in {".yaml", ".yml"}:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            assert re.match(r"\s*password\s*:", code, re.IGNORECASE) is None, path

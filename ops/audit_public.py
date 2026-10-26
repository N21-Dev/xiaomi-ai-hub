#!/usr/bin/env python3
"""Check publishable files without printing suspected secret values."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {"data", "archives", ".venv", "node_modules", "__pycache__", "out", "build"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite3", ".pem", ".key", ".jks", ".keystore", ".p12", ".apk", ".mp4"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".html", ".css", ".js", ".cjs", ".java", ".xml", ".yml", ".yaml", ".json", ".toml", ".sh", ".sql", ".example", ""}
LOCAL_PATH = re.compile(r"(?:/home/[^/\s]+/(?:code|apps)/|/vol\d+/\d+/|[A-Z]:[/\\]Users[/\\])")
TOKENS = re.compile(r"(?:" + "sk" + r"-[A-Za-z0-9_-]{24,}|gh" + r"[pousr]_[A-Za-z0-9_]{25,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def files() -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True)
    if result.returncode == 0:
        return [ROOT / name for name in result.stdout.decode().split("\0") if name]
    return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.relative_to(ROOT).parts]


def audit() -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for path in files():
        relative = path.relative_to(ROOT)
        name = str(relative)
        reason = None
        if path.is_symlink():
            reason = "symlink"
        elif FORBIDDEN_PARTS.intersection(relative.parts):
            reason = "runtime/generated directory"
        elif path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name in {".env", ".mi.json"}:
            reason = "private/generated file"
        elif path.name.startswith(".env.") and not path.name.endswith(".example"):
            reason = "private environment"
        elif relative.parts[0] == "config" and path.name != "health.yaml" and not path.name.endswith(".example"):
            reason = "private configuration"
        elif path.name.startswith(("HANDOFF-", "MIGRATION-")):
            reason = "deployment history"
        if reason:
            problems.append({"path": name, "rule": reason})
            continue
        if not path.exists():
            problems.append({"path": name, "rule": "missing tracked file"})
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            problems.append({"path": name, "rule": "unexpected large file"})
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            problems.append({"path": name, "rule": "unexpected binary content"})
            continue
        if LOCAL_PATH.search(content):
            problems.append({"path": name, "rule": "local deployment path"})
        if TOKENS.search(content):
            problems.append({"path": name, "rule": "possible credential"})
    return problems


def main() -> None:
    problems = audit()
    print(json.dumps({"files_checked": len(files()), "findings": problems}, indent=2))
    if problems:
        raise SystemExit(1)
    print("Public source checks passed; run Gitleaks separately before publishing.")


if __name__ == "__main__":
    main()

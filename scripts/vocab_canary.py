#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml"]
# ///
"""CI canary: vocab_check.py must flag a planted term and fail on a termless registry."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

CHECK = Path(__file__).with_name("vocab_check.py")
CONFIG = "---\nrule_overrides:\n  R51:\n    enabled: true\n    vocabulary_skill: skills/vocab/\n---\n"
REGISTRY = """scopes:
  - id: internal
    paths:
      - docs/
verbs:
  internal:
    - {key}: ship
      deprecated:
        - deploy out
"""


def run(root: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), *paths], cwd=root, capture_output=True, text=True
    )


def fixture(root: Path, key: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".claude").mkdir()
    (root / ".claude/nlpm.local.md").write_text(CONFIG)
    (root / "skills/vocab").mkdir(parents=True)
    (root / "skills/vocab/registry.yaml").write_text(REGISTRY.format(key=key))
    (root / "docs").mkdir()
    (root / "docs/in-scope.md").write_text("Please deploy out the build.\n")
    (root / "notes.md").write_text("Please deploy out the build.\n")


def expect(ok: bool, message: str, failures: list[str]) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "good"
        good.mkdir()
        fixture(good, "canonical")
        result = run(good, "docs/in-scope.md")
        expect(result.returncode == 1 and "deploy out" in result.stderr, "planted term in scope is flagged", failures)
        result = run(good, "notes.md")
        expect(result.returncode == 0, "out-of-scope Markdown is not checked", failures)

        bad = Path(tmp) / "bad"
        bad.mkdir()
        fixture(bad, "name")
        result = run(bad, "docs/in-scope.md")
        expect(result.returncode == 2 and "zero canonical terms" in result.stderr, "name: registry fails loudly", failures)

        (good / "skills/vocab/registry.yaml").unlink()
        result = run(good, "docs/in-scope.md")
        expect(result.returncode == 2 and "missing" in result.stderr, "missing registry fails loudly", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

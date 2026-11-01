#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml"]
# ///
"""Flag deprecated vocabulary in prose files (.md, .html, .htm, .txt).

Registries: ~/.agents/skills/.vocabulary/registry.yaml (global) and
<repo>/.agents/vocabulary.yaml (repo; its presence makes findings blocking).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

GLOBAL_REGISTRY = Path.home() / ".agents/skills/.vocabulary/registry.yaml"
REPO_REGISTRY = ".agents/vocabulary.yaml"
PROSE_GLOBS = ("**/*.md", "**/*.html", "**/*.htm", "**/*.txt")
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
HTML_CODE = re.compile(
    r"<(script|style|pre|code|svg)\b.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL
)
HTML_TAG = re.compile(r"<[^>]*>", re.DOTALL)
HTML_ENTITY = re.compile(r"&[#\w]+;")


@dataclass
class Rules:
    deprecated: dict[str, str]
    include: list[str]
    exclude: list[str]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def load(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"vocab: {path} must be a mapping")
    return data


def entries(node) -> list[dict]:
    if isinstance(node, list):
        return [e for item in node for e in entries(item)]
    if isinstance(node, dict):
        if "canonical" in node:
            return [node]
        return [e for value in node.values() for e in entries(value)]
    return []


def build_rules(global_reg: dict, repo_reg: dict) -> Rules:
    deprecated: dict[str, str] = {}
    for reg in (global_reg, repo_reg):
        for entry in entries(reg.get("verbs")) + entries(reg.get("nouns")):
            for term in entry.get("deprecated") or []:
                deprecated[str(term).lower()] = str(entry["canonical"])
    for term in repo_reg.get("allow") or []:
        deprecated.pop(str(term).lower(), None)
    return Rules(
        deprecated=deprecated,
        include=list(PROSE_GLOBS) + list(repo_reg.get("paths") or []),
        exclude=list(global_reg.get("exclude") or []) + list(repo_reg.get("exclude") or []),
    )


def selected(path: str, rules: Rules) -> bool:
    p = PurePosixPath(path)
    return any(p.full_match(g) for g in rules.include) and not any(
        p.full_match(g) for g in rules.exclude
    )


def pattern(rules: Rules) -> re.Pattern[str]:
    terms = sorted(rules.deprecated, key=len, reverse=True)
    alternation = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in terms)
    return re.compile(rf"(?<![\w/.@-])({alternation})(?![\w/@-]|\.\w)", re.IGNORECASE)


def blank(match: re.Match[str]) -> str:
    return " " + "\n" * match[0].count("\n")


def prose_lines(name: str, text: str) -> dict[int, str]:
    if name.lower().endswith((".html", ".htm")):
        text = HTML_ENTITY.sub(" ", HTML_TAG.sub(blank, HTML_CODE.sub(blank, text)))
        return dict(enumerate(text.splitlines(), start=1))
    lines: dict[int, str] = {}
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines[number] = INLINE_CODE.sub("", line)
    return lines


def added_lines(root: Path, path: str) -> set[int]:
    diff = git(root, "diff", "--cached", "-U0", "--no-color", "--", path)
    added: set[int] = set()
    for match in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start, count = int(match[1]), int(match[2] or 1)
        added.update(range(start, start + count))
    return added


def candidates(root: Path, args: argparse.Namespace, rules: Rules) -> list[tuple[str, str, set[int] | None]]:
    if args.staged:
        names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
        return [
            (n, git(root, "show", f":{n}"), added_lines(root, n))
            for n in names
            if n and selected(n, rules)
        ]
    names = args.paths or git(root, "ls-files", "-z").split("\0")
    out = []
    for n in names:
        rel = Path(n).resolve().relative_to(root).as_posix() if Path(n).is_absolute() else n
        if rel and selected(rel, rules) and (root / rel).is_file():
            out.append((rel, (root / rel).read_text(encoding="utf-8", errors="replace"), None))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="check lines added in the index")
    parser.add_argument("paths", nargs="*", help="files to check (default: all tracked prose files)")
    args = parser.parse_args()

    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    except subprocess.CalledProcessError:
        return 0
    repo_path = root / REPO_REGISTRY
    rules = build_rules(load(GLOBAL_REGISTRY), load(repo_path))
    if not rules.deprecated:
        return 0
    if args.staged and not any(
        selected(n, rules)
        for n in git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
        if n
    ):
        return 0

    regex = pattern(rules)
    findings = []
    for name, text, only in candidates(root, args, rules):
        for number, line in prose_lines(name, text).items():
            if only is not None and number not in only:
                continue
            for match in regex.finditer(line):
                term = match[1]
                canonical = rules.deprecated[re.sub(r"\s+", " ", term.lower())]
                findings.append(f"{name}:{number}: '{term}' -> '{canonical}'")

    if not findings:
        return 0
    blocking = repo_path.is_file()
    label = "error" if blocking else "warning"
    print(f"vocab {label}: {len(findings)} deprecated term(s)", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    if blocking:
        print(f"Fix the wording, or edit {REPO_REGISTRY}. Bypass once: VOCAB=0 git commit ...", file=sys.stderr)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml"]
# ///
"""Flag deprecated vocabulary in prose files (.md, .html, .htm, .txt).

Registries: ~/.agents/skills/.vocabulary/registry.yaml (global, optional) and
<repo>/.agents/vocabulary.yaml (repo; its presence makes findings blocking).
A repo that enables R51 in .claude/nlpm.local.md must provide the NLPM registry
at <vocabulary_skill>/registry.yaml (terms use `canonical:`); only files under
its scopes[].paths are checked. A missing, unparseable, or termless enabled
registry exits 2.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

import yaml

GLOBAL_REGISTRY = Path.home() / ".agents/skills/.vocabulary/registry.yaml"
REPO_REGISTRY = ".agents/vocabulary.yaml"
NLPM_CONFIG = ".claude/nlpm.local.md"
PROSE_GLOBS = ("**/*.md", "**/*.html", "**/*.htm", "**/*.txt")
PROSE_SUFFIXES = (".md", ".html", ".htm", ".txt")
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
    scoped: bool
    blocking: bool
    source: str


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def load_optional(path: Path) -> dict:
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


def build_rules(
    global_reg: dict,
    repo_reg: dict,
    nlpm_reg: dict | None = None,
    nlpm_skill: str | None = None,
    source: str = REPO_REGISTRY,
) -> Rules:
    deprecated: dict[str, str] = {}
    for reg in (global_reg, repo_reg, nlpm_reg or {}):
        for entry in canonical_entries(reg):
            for term in entry.get("deprecated") or []:
                deprecated[str(term).lower()] = str(entry["canonical"])
    for term in repo_reg.get("allow") or []:
        deprecated.pop(str(term).lower(), None)
    exclude = list(global_reg.get("exclude") or []) + list(repo_reg.get("exclude") or [])
    if nlpm_reg is None:
        include = list(PROSE_GLOBS) + list(repo_reg.get("paths") or [])
    else:
        include = [p for scope in nlpm_reg["scopes"] for p in scope["paths"]]
        include += list(repo_reg.get("paths") or [])
        if nlpm_skill:
            exclude.append(nlpm_skill.rstrip("/") + "/**")
    return Rules(
        deprecated=deprecated,
        include=include,
        exclude=exclude,
        scoped=nlpm_reg is not None,
        blocking=bool(repo_reg) or nlpm_reg is not None,
        source=source,
    )


def fail(message: str) -> NoReturn:
    print(f"vocab error: {message}", file=sys.stderr)
    print("Bypass once: VOCAB=0 git commit ...", file=sys.stderr)
    raise SystemExit(2)


def canonical_entries(reg: dict) -> list[dict]:
    return entries(reg.get("verbs")) + entries(reg.get("nouns"))


def load_enabled(path: Path, label: str) -> dict:
    if not path.is_file():
        fail(f"{label} registry {path} is missing")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        fail(f"{label} registry {path} is not valid YAML: {err}")
    if not isinstance(data, dict):
        fail(f"{label} registry {path} must be a mapping")
    if not canonical_entries(data):
        fail(
            f"{label} registry {path} has zero canonical terms; "
            "entries under verbs/nouns need `canonical:` (NLPM schema), not `name:`"
        )
    return data


def nlpm_skill_dir(root: Path) -> str | None:
    config = root / NLPM_CONFIG
    if not config.is_file():
        return None
    match = re.match(r"---\s*\n(.*?)\n---", config.read_text(encoding="utf-8"), re.DOTALL)
    if not match:
        return None
    try:
        front = yaml.safe_load(match[1]) or {}
    except yaml.YAMLError as err:
        fail(f"{config} front matter is not valid YAML: {err}")
    r51 = ((front.get("rule_overrides") or {}).get("R51")) or {}
    if not r51.get("enabled"):
        return None
    skill = r51.get("vocabulary_skill")
    if not skill:
        fail(f"{config} enables R51 but sets no rule_overrides.R51.vocabulary_skill")
    return str(skill)


def repo_registries(root: Path) -> tuple[dict, dict | None, str | None, str]:
    legacy_path = root / REPO_REGISTRY
    legacy = load_enabled(legacy_path, "repo") if legacy_path.is_file() else {}
    skill = nlpm_skill_dir(root)
    if skill is None:
        return legacy, None, None, REPO_REGISTRY
    registry_path = root / skill / "registry.yaml"
    nlpm = load_enabled(registry_path, "NLPM")
    scopes = nlpm.get("scopes")
    if not (
        isinstance(scopes, list)
        and scopes
        and all(isinstance(s, dict) and isinstance(s.get("paths"), list) for s in scopes)
        and any(s["paths"] for s in scopes)
    ):
        fail(f"NLPM registry {registry_path} needs scopes[].paths; refusing to check unscoped Markdown")
    return legacy, nlpm, skill, registry_path.relative_to(root).as_posix()


def scope_match(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        return PurePosixPath(path).full_match(pattern)
    return path == pattern or path.startswith(pattern + "/")


def selected(path: str, rules: Rules) -> bool:
    if any(PurePosixPath(path).full_match(g) for g in rules.exclude):
        return False
    if rules.scoped:
        return path.lower().endswith(PROSE_SUFFIXES) and any(
            scope_match(path, g) for g in rules.include
        )
    return any(PurePosixPath(path).full_match(g) for g in rules.include)


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
    rules = build_rules(load_optional(GLOBAL_REGISTRY), *repo_registries(root))
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
    blocking = rules.blocking
    label = "error" if blocking else "warning"
    print(f"vocab {label}: {len(findings)} deprecated term(s)", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    if blocking:
        print(f"Fix the wording, or edit {rules.source}. Bypass once: VOCAB=0 git commit ...", file=sys.stderr)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())

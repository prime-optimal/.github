#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml"]
# ///
"""Select NLPM-scorable artifacts from a PR and render the advisory comment."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

MARKER = "<!-- nlpm-advisory -->"
ARTIFACT_GLOBS = (
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/GEMINI.md",
    "**/SKILL.md",
    "**/rules/*.md",
    "**/agents/*.md",
    "**/commands/*.md",
    "plugin.json",
    "**/.claude-plugin/plugin.json",
    "**/.codex-plugin/plugin.json",
    "**/hooks/hooks.json",
)
PROSE_SUFFIXES = {".md", ".html", ".htm", ".txt"}
CONFIG = ".claude/nlpm.local.md"


def frontmatter(path: Path) -> dict:
    if not path.is_file():
        return {}
    match = re.match(r"^---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
    return (yaml.safe_load(match[1]) or {}) if match else {}


def select(args: argparse.Namespace) -> int:
    root = Path(args.root)
    config = frontmatter(root / CONFIG)
    exclude = (config.get("discovery") or {}).get("exclude") or []
    chosen, prose = [], []
    for name in Path(args.changed).read_text(encoding="utf-8").split():
        p = PurePosixPath(name)
        if not (root / name).is_file() or any(p.full_match(g) for g in exclude):
            continue
        if any(p.full_match(g) for g in ARTIFACT_GLOBS):
            chosen.append(name)
        if p.suffix.lower() in PROSE_SUFFIXES:
            prose.append(name)
    Path(args.out).write_text("".join(f"{n}\n" for n in chosen), encoding="utf-8")
    Path(args.prose_out).write_text("".join(f"{n}\n" for n in prose), encoding="utf-8")
    print(f"count={len(chosen)}\nprose={len(prose)}")
    return 0


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def read_scores(path: Path, root: Path) -> tuple[list[dict], list[str]]:
    if not path.is_file():
        return [], []
    rows, problems = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if number == 1 and line.startswith("file\t"):
            continue
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3 or not parts[1].strip().isdigit():
            problems.append(f"unparseable score row {number}")
            continue
        name, score, quote = parts[0].strip(), int(parts[1]), parts[2].strip()
        findings = parts[3].strip() if len(parts) > 3 else ""
        target = root / name
        grounded = target.is_file() and len(quote) >= 24 and squash(quote) in squash(
            target.read_text(encoding="utf-8", errors="replace")
        )
        rows.append(dict(file=name, score=score, quote=quote, findings=findings, grounded=grounded))
    return rows, problems


def read_drift(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("terms"):
            out.append(record)
    return out


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(args: argparse.Namespace) -> int:
    root = Path(args.root)
    artifacts = [n for n in Path(args.artifacts).read_text(encoding="utf-8").split() if n]
    prose = [n for n in Path(args.prose).read_text(encoding="utf-8").split() if n] if Path(args.prose).is_file() else []
    vocab = Path(args.vocab).read_text(encoding="utf-8").strip() if Path(args.vocab).is_file() else ""
    rows, problems = read_scores(Path(args.scores), root)
    drift = read_drift(Path(args.drift))
    hidden = sum(1 for d in drift if str(d.get("confidence", "")).lower() == "low")
    drift = [d for d in drift if str(d.get("confidence", "")).lower() != "low"]
    has_registry = (root / ".agents/vocabulary.yaml").is_file()

    out = [MARKER, "## NLPM advisory", "", "_Advisory only; nothing here blocks the PR._", ""]

    out.append("### Vocabulary")
    if not has_registry:
        out.append("No `.agents/vocabulary.yaml`; the deterministic vocabulary check has nothing to enforce.")
    elif args.vocab_rc not in ("0", "1"):
        out.append(f"Vocabulary check errored (exit {args.vocab_rc}).")
        if vocab:
            out += ["```text", vocab, "```"]
    elif vocab:
        out += ["Deprecated terms in tracked prose (`.md`/`.html`/`.txt`):", "```text", vocab, "```"]
    else:
        out.append("Clean: no deprecated terms in tracked prose.")
    out.append("")

    out.append("### Artifact scores")
    if not artifacts:
        out.append("No agent artifacts changed in this PR; scoring skipped.")
    elif args.score_outcome == "no-token":
        out.append("Skipped: the repo has no `CLAUDE_CODE_OAUTH_TOKEN` secret.")
    elif not rows:
        out.append(f"Scoring produced no results (step outcome: {args.score_outcome}).")
    else:
        out += ["| File | Score | Top findings | Grounded |", "| --- | ---: | --- | :---: |"]
        scored = {r["file"] for r in rows}
        for r in sorted(rows, key=lambda r: r["score"]):
            mark = "yes" if r["grounded"] else "no: quote not found"
            out.append(f"| `{r['file']}` | {r['score']} | {cell(r['findings']) or '-'} | {mark} |")
        for name in artifacts:
            if name not in scored:
                out.append(f"| `{name}` | - | not scored | - |")
        if any(not r["grounded"] for r in rows):
            out.append("")
            out.append("Rows marked _quote not found_ cite text that is not in the file; treat those scores as unverified.")
    for problem in problems:
        out.append(f"- {problem}")
    out.append("")

    out.append("### Vocabulary drift candidates")
    if not prose:
        out.append("No prose changed in this PR; drift scan skipped.")
    elif args.drift_outcome in ("no-token", "skipped"):
        out.append("Not run.")
    elif not drift and args.drift_outcome != "success":
        out.append(f"Drift scan produced no results (step outcome: {args.drift_outcome}).")
    elif not drift:
        out.append("None beyond the terms already in `.agents/vocabulary.yaml`.")
    else:
        out += ["| Terms | Suggested | Kind | Confidence | Evidence |", "| --- | --- | --- | --- | --- |"]
        for d in drift[:20]:
            terms = ", ".join(f"`{t}`" for t in d.get("terms", []))
            out.append(
                f"| {terms} | `{cell(str(d.get('suggested_canonical', '')))}` | {d.get('disposition', '')} "
                f"| {d.get('confidence', '')} | {cell(str(d.get('evidence', '')))} |"
            )
        out += ["", "Curate real drift into `.agents/vocabulary.yaml` (`deprecated`) or `rejected`."]
    if prose and hidden:
        out += ["", f"_{hidden} low-confidence candidate(s) hidden; see `drift.jsonl` in the workflow run artifacts._"]

    if args.run_url:
        out += ["", f"[Workflow run]({args.run_url})"]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("select", help="filter PR-changed files to scorable artifacts")
    s.add_argument("--root", default=".")
    s.add_argument("--changed", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--prose-out", required=True)
    s.set_defaults(func=select)

    r = sub.add_parser("render", help="render the advisory PR comment")
    r.add_argument("--root", default=".")
    r.add_argument("--artifacts", required=True)
    r.add_argument("--scores", required=True)
    r.add_argument("--prose", default="")
    r.add_argument("--drift", required=True)
    r.add_argument("--vocab", required=True)
    r.add_argument("--vocab-rc", default="0")
    r.add_argument("--score-outcome", default="skipped")
    r.add_argument("--drift-outcome", default="skipped")
    r.add_argument("--run-url", default="")
    r.set_defaults(func=render)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

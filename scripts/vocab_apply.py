#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml", "ruamel.yaml"]
# ///
"""Apply vocabulary recommendations: update .agents/vocabulary.yaml and rewrite tracked prose."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab_check as vc  # noqa: E402

MAX_WORDS = 4


def phrase(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if text and len(text.split()) <= MAX_WORDS and "`" not in text else ""


def read_recs(path: Path, known: set[str]) -> list[dict]:
    recs = []
    if not path.is_file():
        return recs
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        reason = re.sub(r"\s+", " ", str(rec.get("reason", ""))).strip()
        if rec.get("decision") == "reject":
            term = phrase(rec.get("term"))
            if term and term.lower() not in known:
                recs.append(dict(terms=rec.get("terms", []), decision="reject", term=term, reason=reason))
                known.add(term.lower())
            continue
        if rec.get("decision") != "deprecate":
            continue
        mappings = []
        for m in rec.get("mappings") or []:
            src, dst = phrase(m.get("from")), phrase(m.get("to"))
            if src and dst and src.lower() != dst.lower() and src.lower() not in known:
                mappings.append({"from": src, "to": dst, "count": 0})
                known.add(src.lower())
        if mappings:
            recs.append(dict(terms=rec.get("terms", []), decision="deprecate", mappings=mappings, reason=reason))
    return recs


def update_registry(path: Path, recs: list[dict]) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    data = yaml.load(path.read_text(encoding="utf-8")) if path.is_file() else None
    if data is None:
        data = {"version": 1}
    nouns = data.setdefault("nouns", {})
    group = next(iter(nouns), "general") if hasattr(nouns, "keys") else "general"
    bucket = nouns.setdefault(group, [])
    by_canonical = {str(e.get("canonical")).lower(): e for e in bucket if isinstance(e, dict)}
    rejected = data.setdefault("rejected", [])
    for rec in recs:
        if rec["decision"] == "reject":
            rejected.append({"term": rec["term"], "reason": rec["reason"] or "Rejected by NLPM advisory."})
            continue
        for m in rec["mappings"]:
            entry = by_canonical.get(m["to"].lower())
            if entry is None:
                deprecated = CommentedSeq()
                deprecated.fa.set_flow_style()
                entry = {"canonical": m["to"], "deprecated": deprecated}
                bucket.append(entry)
                by_canonical[m["to"].lower()] = entry
            entry.setdefault("deprecated", []).append(m["from"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


def cased(source: str, target: str) -> str:
    if len(source) > 1 and source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def rewrite(name: str, text: str, regex: re.Pattern[str], mapping: dict[str, dict]) -> str:
    def swap(match: re.Match[str]) -> str:
        m = mapping[re.sub(r"\s+", " ", match[1].lower())]
        m["count"] += 1
        return cased(match[1], m["to"])

    if name.lower().endswith((".html", ".htm")):
        protected = [m.span() for m in vc.HTML_CODE.finditer(text)]
        protected += [
            m.span() for m in vc.HTML_TAG.finditer(text) if not any(a <= m.start() < b for a, b in protected)
        ]
        chunks, cursor = [], 0
        for start, end in sorted(protected):
            if start < cursor:
                continue
            chunks += [regex.sub(swap, text[cursor:start]), text[start:end]]
            cursor = end
        return "".join(chunks + [regex.sub(swap, text[cursor:])])
    out, in_fence = [], False
    for line in text.splitlines(keepends=True):
        if vc.FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        pieces = re.split(f"({vc.INLINE_CODE.pattern})", line)
        out.append("".join(p if vc.INLINE_CODE.fullmatch(p) else regex.sub(swap, p) for p in pieces))
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(vc.git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    registry = root / vc.REPO_REGISTRY
    repo_reg = vc.load(registry)
    rules = vc.build_rules(vc.load(vc.GLOBAL_REGISTRY), repo_reg)
    known = set(rules.deprecated) | {str(r.get("term", "")).lower() for r in repo_reg.get("rejected") or []}
    known |= {str(e.get("canonical", "")).lower() for e in vc.entries(repo_reg.get("nouns")) + vc.entries(repo_reg.get("verbs"))}
    allowed = {str(t).lower() for t in repo_reg.get("allow") or []}

    recs = read_recs(Path(args.recs), known)
    result = {"applied": recs, "files": []}
    if recs:
        update_registry(registry, recs)
        mapping = {
            m["from"].lower(): m
            for rec in recs
            if rec["decision"] == "deprecate"
            for m in rec["mappings"]
            if m["from"].lower() not in allowed
        }
        if mapping:
            regex = vc.pattern(vc.Rules(deprecated={k: v["to"] for k, v in mapping.items()}, include=[], exclude=[]))
            for name in vc.git(root, "ls-files", "-z").split("\0"):
                path = root / name
                if not name or not vc.selected(name, rules) or not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                new = rewrite(name, text, regex, mapping)
                if new != text:
                    path.write_text(new, encoding="utf-8")
                    result["files"].append(name)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"recommendations={len(recs)}\nfiles={len(result['files'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

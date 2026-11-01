# NLPM advisory workflow

## What runs on a pull request

| Part | Engine | Scope | Output |
| --- | --- | --- | --- |
| Vocabulary check | `scripts/vocab_check.py` (deterministic, no LLM) | All tracked `.md`, `.html`, `.htm`, `.txt` | Deprecated terms with `file:line` and the canonical term |
| Artifact scores | `claude-code-action` with the subscription token and the pinned NLPM rubric | Agent artifacts changed in the PR (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `SKILL.md`, `rules/`, `agents/`, `commands/`, plugin manifests, `hooks.json`), minus `discovery.exclude` in `.claude/nlpm.local.md` | Score, top findings, and a verbatim quote per file |
| Drift candidates | Separate Claude step, NLPM `vocab-drift-scanner` | Prose (`.md`/`.html`/`.htm`/`.txt`) changed in the PR, minus `discovery.exclude`; the rest of the repo only as evidence | New medium/high-confidence term clusters not already in `.agents/vocabulary.yaml`; low-confidence ones are counted and kept in the `nlpm-advisory` run artifact |
| Drift fixes | Claude step (`recommend-model`), then `scripts/vocab_apply.py` (deterministic) | Medium/high-confidence drift candidates | Canonical term, narrowest safe phrase mappings, and a one-line reason; committed to the PR branch |

The comment is updated in place on every push. Each Claude step runs only when its inputs changed (agent artifacts for scoring, prose for drift) and the repo has a `CLAUDE_CODE_OAUTH_TOKEN` secret.

Each score row cites a quote; the render step checks the quote exists in the file and marks the row unverified when it does not.

## How vocabulary is maintained

Two separate jobs:

1. **Discovery (LLM, advisory).** The drift scan finds term pairs used for the same thing, such as `simulated`/`demo`. It suggests; it never enforces.
2. **Enforcement (deterministic).** `vocab_check.py` only knows the terms someone wrote into `.agents/vocabulary.yaml`. It flags new lines with deprecated terms on commit, and the whole repo in CI. It never discovers new drift.

The loop is automated. For each medium/high drift candidate, the recommend step decides `deprecate` (canonical term plus the narrowest phrases safe to replace everywhere, e.g. `live Twitch` -> `real Twitch` instead of `live` -> `real`) or `reject`. `vocab_apply.py` writes the decision into `.agents/vocabulary.yaml`, rewrites tracked prose (skipping fenced code, inline code, and HTML tags/code), and the workflow commits it to the PR branch as `github-actions[bot]`. The comment shows `Terms | Use | Why | Applied` with the commit link; evidence sits in a collapsed block.

The commit is pushed with the workflow token, so it does not run the workflow again. GitHub may list an `action_required` run on the bot commit; you do not need to approve it, and if you do, the job skips itself because the bot started it, so the comment stays as it was. Pull before your next push. To undo a call, revert that commit; to stop a pair coming back, list the term under `rejected`. The drift scan skips anything already in the registry. Fork PRs cannot be pushed to; the comment then shows the recommendations as `not pushed`.

Registry shape:

```yaml
version: 1
nouns:
  brand:
    - canonical: K-Lab
      deprecated: [klab]
rejected:
  - term: broadcast
    reason: Means a live Twitch broadcast, not a stream.
exclude:
  - "vendor/**"
```

A repo with `.agents/vocabulary.yaml` blocks local commits that add deprecated terms (`VOCAB=0 git commit` bypasses once). The optional global registry at `~/.agents/skills/.vocabulary/registry.yaml` adds terms everywhere as warnings.

## Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `tools-ref` | `main` | Ref of this repo to load scripts from |
| `nlpm-ref` | pinned `xiaolai/nlpm` commit | Rubric version; bump deliberately |
| `score-model` | `sonnet` | Model that scores changed artifacts; rubric judgment needs a stronger model |
| `drift-model` | `haiku` | Model that scans vocabulary drift; term clustering is cheap work |
| `recommend-model` | `sonnet` | Model that picks the canonical term and safe phrases for each drift candidate |
| `apply-drift` | `true` | Commit recommended fixes to the PR branch; the caller must grant `contents: write` |

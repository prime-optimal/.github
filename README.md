# .github

Shared GitHub Actions workflows and tooling for prime-optimal repos.

- Repo: https://github.com/prime-optimal/.github
- Default labels: `feat`, `chore`, `fix`
- Local clone: `~/Working/prime-optimal-github`. The global `vocab-pre-commit` git hook and the `vocab:check` task in `~/.agents/skills` run `scripts/vocab_check.py` from that clone.

## NLPM advisory workflow

`.github/workflows/nlpm-advisory.yml` posts one sticky PR comment and never blocks the PR. Details: [docs/nlpm-advisory.md](docs/nlpm-advisory.md).

Add it to a repo with `.github/workflows/nlpm.yml`:

```yaml
name: nlpm
on:
  pull_request:
permissions:
  contents: read
  pull-requests: write
jobs:
  nlpm:
    uses: prime-optimal/.github/.github/workflows/nlpm-advisory.yml@main
    secrets: inherit
```

Then add the subscription token as a repo secret:

```sh
claude setup-token
gh secret set CLAUDE_CODE_OAUTH_TOKEN -R prime-optimal/<repo>
```

## Scripts

| Script | Purpose |
| --- | --- |
| `scripts/vocab_check.py` | Deterministic vocabulary check (local hook and CI) |
| `scripts/nlpm_comment.py` | Selects changed agent artifacts and renders the advisory comment |

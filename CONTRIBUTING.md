# Contributing to arxiv-mcp-pro

Thanks for contributing! This guide covers local setup, the required checks, and how changes
are reviewed. See [`CLAUDE.md`](CLAUDE.md) for the architecture overview.

## Development setup

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[test]"      # dev + test dependencies
```

Install the git hooks (both the pre-commit and pre-push stages):

```bash
pre-commit install --install-hooks -t pre-commit -t pre-push
```

> **The hooks need a resolvable `python` on `PATH`.** The hooks are `language: system` — they
> run `black`, the gate scripts, and (on push) `pytest` from whatever `python` is active.
> **Activate the venv (`source .venv/bin/activate`) before any `git commit` or `git push`**,
> or the hooks fail to find their tools. The pre-push hook runs the full test suite, so a push
> takes as long as the tests do.

## Running tests

```bash
python -m pytest                              # full suite with coverage
python -m pytest tests/tools/test_search.py   # a single file
```

## Pull request expectations

- **All 8 required checks must be green**: `gates`, `lint`, and the six `test (…)` matrix legs
  (Python 3.11 / 3.12 × Ubuntu / macOS / Windows).
- **Resolve every review conversation** before merge — conversation resolution is required.
- **`main` is protected**, including for admins — all changes land through a PR, never a direct
  push.
- **Add a `CHANGELOG.md` entry** under `[Unreleased]` for any user-visible change (new/changed
  behavior, fixes, config). Pure-internal changes (CI, tests, docs) may note it there too.
- Keep commits and PR descriptions plain — conventional-commit style is welcome.

## Review process

Changes are reviewed per [`docs/governance/review-protocol.md`](docs/governance/review-protocol.md):
review effort is routed by risk, and whoever wrote a change does not sign off on it. Reviewer
findings (human or automated — CodeRabbit, Codex, Copilot) are handled with the
[`.claude/skills/pr-review-triage`](.claude/skills/pr-review-triage/) skill, which defines the
per-finding verdict vocabulary and the resolve-before-merge mechanics.

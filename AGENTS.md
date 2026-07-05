# AGENTS.md

Guidance for AI coding agents (Codex, Claude Code, etc.) working in this repo.

**`CLAUDE.md` is canonical** — read it first for architecture, the tools layer, and
configuration. This file is the short operational summary; when the two overlap, defer to
`CLAUDE.md`.

## Key commands

```bash
# Setup (Python 3.11+)
uv venv
source .venv/bin/activate
uv pip install -e ".[test]"      # dev + test dependencies

# Test
python -m pytest                 # full suite with coverage
python -m pytest tests/tools/test_search.py   # a single file

# Run the server
python -m arxiv_mcp_server        # or the console script: arxiv-mcp-pro
```

## Hooks need a resolvable Python (important)

The pre-commit / pre-push hooks are `language: system` — they invoke `black`, `pytest`, and
the gate scripts from whatever `python` is on `PATH`. Activate the project venv **before any
`git commit` or `git push`**, or the hooks fail to find their tools:

```bash
source .venv/bin/activate
```

The `pre-push` hook runs the full pytest suite; expect a push to take as long as the tests do.

## Review

Changes are reviewed per `docs/governance/review-protocol.md` (risk-routed; producer never
signs off on their own change). The `.claude/skills/pr-review-triage/` skill defines the
per-finding verdict vocabulary and the resolve-before-merge mechanics. Add a `CHANGELOG.md`
`[Unreleased]` entry for any user-visible change.

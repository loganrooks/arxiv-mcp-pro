# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`arxiv-mcp-pro` is the standalone, citation-capable evolution of
[`arxiv-mcp-server`](https://github.com/blazickjp/arxiv-mcp-server) by Joseph Blazick
(Pearl Labs). Releases up to and including v0.5.0 were published under the original name.

## [Unreleased]

Repo polish after the v0.7.0 PyPI release, plus reliability/ergonomics fixes
driven by multi-agent field use.

### Added
- Contributor scaffolding: `CONTRIBUTING.md`, GitHub issue templates (bug / feature),
  a pull request template, and a Dependabot config (weekly `pip` + `github-actions`
  updates). Also a committed `.claude/settings.json` with a minimal permissions allowlist.
- **Cross-process arXiv rate pacing** (B17): the arXiv-API request paths now pace through a
  lock file in the storage dir (`arxiv_api.lock`), so multiple sessions on one machine that
  share a storage dir stay under arXiv's ≈1-request/3s per-IP limit — the failure mode where
  a fleet of parallel agents drew sustained HTTP 429 cooldowns. Paced paths: `search_papers`
  (both the arxiv-library and raw-HTTP/date routes), `get_abstract`, `watch_topic` /
  `check_alerts` (via `_raw_arxiv_search`), and `download_paper`'s PDF-fallback metadata
  fetch. A 429/503 publishes a shared cooldown (`arxiv_api.cooldown`) so sibling lanes back
  off together rather than each rediscovering the limit. New `ARXIV_MIN_REQUEST_INTERVAL`
  knob (default `3`s; `0` disables all pacing including the lock/cooldown files). Fail-open:
  any pacer error degrades to in-process pacing and never breaks a request. See the README
  "Parallel / multi-agent use" note; multiple machines behind one IP remain uncoordinated.
  *Not yet paced (tracked follow-up):* the semantic-index metadata fetches —
  `download_paper`'s deferred background re-index, `semantic_search`'s on-demand fetch of a
  missing source paper, and the `reindex` loop (each uses a fresh `arxiv.Client()`).

### Changed
- **`read_paper` / `download_paper` now cap the default content return** at
  `CONTENT_DEFAULT_MAX_CHARS` (60000 chars) when `max_chars` is omitted (B12).
  Uncapped whole-paper defaults (~137k chars observed) overflowed MCP clients'
  per-tool-output limits and blocked the read path entirely. Responses have
  always carried `is_truncated` / `next_start`, so capped reads are pageable;
  an explicit `max_chars` still wins, and setting the env var to `0` (or any
  non-positive value) restores the legacy full-content default. Note: a
  schema-violating explicit `max_chars` (`0`, negative — the schema minimum
  is 1) is ignored and now receives the default cap, where it previously
  received full content.
- CI tuning: added `concurrency` (auto-cancel superseded runs) to the `CI`, `Lint`,
  and `Run Tests` workflows, and pip dependency caching to `CI` and `Lint` (the `Run
  Tests` matrix installs via `uv`, so pip caching does not apply — a uv cache is a
  follow-up); removed the dead `master` branch triggers and leftover template comments.
- **Slimmer install**: dropped `aiohttp`, `python-dotenv`, `anyio`, and `sse-starlette`
  from the runtime dependencies (none were imported anywhere; `anyio`/`sse-starlette`
  still arrive via `mcp` and dotenv support via `pydantic-settings`, so behavior is
  unchanged) and pinned `arxiv>=2.1,<3` — arxiv 4.x pulls in lxml (~20 MB compiled)
  and had never been what the lockfile and test suite ran against. A clean
  `pip install` shrinks from ~77 MB / 46 packages to ~53 MB / 40 packages.

### Fixed
- `search_papers` now documents which timestamp `date_from`/`date_to` bind to: arXiv's
  `submittedDate`, the original (v1) submission time — which can differ from the arXiv-ID
  prefix month and the latest-version date on cross-listed/revised papers, so strict
  windows should be widened slightly and hits verified against their `published` field
  (B18).
- README no longer claims a PyPI package is "planned for a future release" — the package
  shipped to PyPI on 2026-07-05. Install docs now lead with `pip install arxiv-mcp-pro` /
  `uvx arxiv-mcp-pro`, a PyPI version badge is added, the macOS `.mcpb` desktop bundle on
  GitHub Releases is referenced, and the stale "until the PyPI package ships" MCP-config
  note is gone.
- Agent docs corrected: `CLAUDE.md` mis-expanded MCP as "Message Control Protocol"
  (→ *Model Context Protocol*), listed only 4 of the ~10 tool modules, and documented
  non-existent `ARXIV_*`-prefixed env vars; `AGENTS.md` (previously stale instructions for a
  removed tool) is now a lean, tracked agent guide.
- `semantic_search`'s missing-dependency hint (tool description + runtime error) now
  gives the published-package command (`pip install "arxiv-mcp-pro[pro]"`) instead of
  the source-checkout-only `uv pip install -e ".[pro]"`, which fails for pip/uvx
  installs (B15).
- `search_papers`' non-date (arxiv-library) path no longer free-rides outside the rate
  pacer — it previously read the pacer clock but never acquired the lock or updated the
  timestamp, so those searches were effectively unpaced (B17).
- arXiv rate-limit errors now respect a short `Retry-After` header (≤30s → one retry) and
  otherwise fail fast with an honest, actionable message (naming the server's requested
  delay, or noting observed cooldowns can reach ~3 minutes under parallel use) instead of
  the previous hardcoded "wait 60 seconds" (B17).

## [0.7.0] - 2026-06-27

First release published to PyPI under the **`arxiv-mcp-pro`** name (via PyPI
Trusted Publishing / OIDC). `pip install arxiv-mcp-pro`.

### Added
- **`library_influence`** — a descriptive influence panel over your *downloaded*
  library (C5). Builds the induced citation subgraph across local papers (no
  database), then ranks them by personalised PageRank alongside global/local
  citation counts, author pedigree (max co-author h-index), and a has-code
  signal. The load-bearing column is `local_vs_global_delta` — where a paper's
  standing *inside* your corpus disagrees with its global citation footprint
  (a corpus "hidden gem"). Reuses `citation_graph`'s Semantic Scholar pacing /
  backoff / optional API key, fetching references and counts via the
  `/paper/batch` endpoint (chunked at 100 ids). Descriptive only — the
  predictive layer stays gated on a pre-registered backtest. Requires the
  opt-in `[influence]` extra (`pip install "arxiv-mcp-pro[influence]"`,
  pulls `networkx`+`scipy`); the base install is unaffected, and the tool
  degrades gracefully with an install hint when the extra is absent.

### Changed
- **Release pipeline wired** — `publish.yml` now uses PyPI Trusted Publishing
  (OIDC) instead of a stored API token; `lint.yml` is check-only (`black
  --check`) and no longer auto-commits formatting back to a protected branch.
- Trimmed the published **sdist** to source + essential metadata (was sweeping in
  repo/agent internals like `CLAUDE.md`, `.claude/`, `.github/`); the wheel was
  already scoped to the package.

### Removed
- Dropped `black` from the **runtime** dependencies (it is a dev tool, used only
  via pre-commit / the `dev` extra — it was never imported at runtime).

## [0.6.0] - 2026-06-26

First release under the **`arxiv-mcp-pro`** name — the project detached from the upstream
fork network and rebranded. Theme: token-frugal citation tooling and reliability hygiene on
an enforced-by-construction CI/review foundation.

### Added
- **`citation_graph` uplift** — opt-in `limit`/`offset`/`compact` pagination and a
  `counts_only` mode that returns true Semantic Scholar scalar totals
  (`total_citations`/`total_references`) at near-zero token cost; optional
  `SEMANTIC_SCHOLAR_API_KEY` (x-api-key) and a configurable request-pacing interval.
- **`semantic_search` uplift** — opt-in `offset`/`compact` pagination (default output is
  byte-for-byte unchanged).
- Paginated paper-content responses (`start`/`max_chars`) for `download_paper` /
  `read_paper`.

### Changed
- **Rebranded to `arxiv-mcp-pro`** — package name, primary console script `arxiv-mcp-pro`
  (with an `arxiv-mcp-server` back-compat alias), branding, funding and security metadata.
  Joseph Blazick retained as original author (Apache-2.0 lineage). The `arxiv_mcp_server`
  import package name is unchanged.
- `citation_graph` now retries with backoff on HTTP 429 and caps returned edges; in the
  graph modes `citation_count`/`reference_count` count the edges returned (use `counts_only`
  for true totals).

### Fixed
- **`check_alerts` per-topic resilience** — one topic's transient failure no longer aborts
  the whole batch; `last_checked` advances per successful topic via an incremental atomic
  save, and a failed topic carries an `error` field and retries next run (B6).
- **`watched_topics.json` atomic write** (`temp` + `fsync` + `os.replace`) — an interrupted
  save can no longer truncate the file and drop all watches (B5).
- `citation_graph` API-key hygiene — key restricted to printable ASCII and sanitized before
  use (no-leak).
- `get_abstract` — dropped a dead `_last_request_time` import (B9).
- arXiv PDF downloads are streamed via `httpx` to avoid truncated files.

### Infrastructure
- Enforcement-by-construction gates: `black`, secret-scan, and citation / claim-ledger lints
  run via pre-commit and GitHub Actions, with a cross-platform test matrix
  (Python 3.11 / 3.12 × ubuntu / macOS / windows).
- Tiered code-review protocol plus a branch-protection-enforced cross-vendor (Codex) merge
  gate for higher-risk changes.

> Distribution note: v0.6.0 is tagged from source. A published PyPI package
> (`uvx arxiv-mcp-pro`) and a one-click Claude Desktop `.mcpb` bundle are planned for a
> future release; both publish workflows fire on a published GitHub Release.

## [0.5.0] - 2026-05-18

_Published as `arxiv-mcp-server`._

### Added
- Streamable HTTP transport (#94).
- Claude Desktop MCPB packaging (#87).
- Tool annotations (#102); trusted-publishing-to-PyPI CI.

### Fixed
- MCP error flag set on tool error payloads (#95).
- Closed MCP tool schemas and aligned server metadata (#104).
- Alert response-shape test coverage (#100).

## [0.4.12] and earlier

Published as `arxiv-mcp-server` by Joseph Blazick. See the
[commit history](https://github.com/loganrooks/arxiv-mcp-pro/commits/main) for details.

[Unreleased]: https://github.com/loganrooks/arxiv-mcp-pro/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/loganrooks/arxiv-mcp-pro/releases/tag/v0.7.0
[0.6.0]: https://github.com/loganrooks/arxiv-mcp-pro/releases/tag/v0.6.0
[0.5.0]: https://github.com/loganrooks/arxiv-mcp-pro/releases/tag/v0.5.0

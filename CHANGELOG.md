# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`arxiv-mcp-pro` is the standalone, citation-capable evolution of
[`arxiv-mcp-server`](https://github.com/blazickjp/arxiv-mcp-server) by Joseph Blazick
(Pearl Labs). Releases up to and including v0.5.0 were published under the original name.

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

[0.6.0]: https://github.com/loganrooks/arxiv-mcp-pro/releases/tag/v0.6.0
[0.5.0]: https://github.com/loganrooks/arxiv-mcp-pro/releases/tag/v0.5.0

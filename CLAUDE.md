# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Environment Setup
```bash
# Create and activate virtual environment
uv venv
source .venv/bin/activate

# Install with test dependencies
uv pip install -e ".[test]"
```

### Testing
```bash
# Run all tests with coverage
python -m pytest

# Run specific test file
python -m pytest tests/tools/test_search.py

# Run tests with verbose output
python -m pytest -v
```

### Running the Server
```bash
# Run as module
python -m arxiv_mcp_server

# Or via console script (pyproject [project.scripts])
arxiv-mcp-pro
# `arxiv-mcp-server` is a back-compat alias for the same entry point
```

## Architecture Overview

This is an **MCP (Model Context Protocol) server** that provides AI models access to arXiv research papers. The codebase follows a modular architecture with four main layers:

### Core Components

1. **Server Layer** (`server.py`): Main MCP server implementation that handles tool registration and request routing
2. **Tools Layer** (`tools/`): Each module registers one or more MCP tools (the `name="…"` in each `types.Tool`):
   - `search.py` → `search_papers`: advanced arXiv search with category/date/boolean filters and the 3s rate limiter
   - `download.py` → `download_paper`: fetch a paper (HTML-first, PDF fallback), store it locally, return paginated content
   - `list_papers.py` → `list_papers`: list locally downloaded papers (arXiv IDs)
   - `read_paper.py` → `read_paper`: read a downloaded paper's markdown with `start`/`max_chars` pagination
   - `get_abstract.py` → `get_abstract`: fetch a paper's abstract/metadata without downloading the full text
   - `semantic_search.py` → `semantic_search` + `reindex`: similarity search over locally downloaded papers, plus a manual re-index tool
   - `citation_graph.py` → `citation_graph`: references + citing papers via Semantic Scholar (pagination / `compact` / `counts_only` modes)
   - `influence.py` → `library_influence`: descriptive influence panel (C5) — personalised PageRank over the induced citation subgraph of your local library
   - `alerts.py` → `watch_topic` + `check_alerts`: register topic watches and poll for newly published papers
   - `content.py`: internal helper (not a tool) for bounded/paginated content payloads shared by download/read
3. **Resource Management** (`resources/papers.py`): `PaperManager` class handles paper storage, PDF-to-markdown conversion using pymupdf4llm, and local caching
4. **Configuration** (`config.py`): Pydantic-based settings with environment variable support

### Key Design Patterns

- **MCP Protocol Compliance**: All tools follow MCP specification with proper type definitions
- **Async-First**: Built on asyncio with aiofiles for non-blocking I/O operations
- **Storage Strategy**: Papers downloaded as PDFs, converted to markdown, stored locally with PDF cleanup
- **Error Handling**: Comprehensive error handling with user-friendly messages throughout tool chain

### Configuration

Storage path is set via the `--storage-path` CLI flag (default: `~/.arxiv-mcp-server/papers`).
Everything else is read from environment variables whose names match the `Settings` fields in
`config.py` — pydantic-settings, **no prefix** (not `ARXIV_*`) — all optional:
- `MAX_RESULTS`: search results limit (default: 50)
- `REQUEST_TIMEOUT`: arXiv API timeout in seconds (default: 60)
- `TRANSPORT` / `HOST` / `PORT`: transport and HTTP bind (defaults: `stdio` / `127.0.0.1` / `8000`)
- `SEMANTIC_SCHOLAR_API_KEY`, `SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL`, `CITATION_MAX_EDGES`: citation/influence tuning

See the README configuration table for the full list.

### Testing Strategy

Tests use pytest with async support and comprehensive mocking:
- `conftest.py` provides shared fixtures for mock arXiv papers and HTTP responses
- Tests cover both unit-level tool functionality and integration scenarios
- Mock-based approach avoids external API calls during testing
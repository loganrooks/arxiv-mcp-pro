"""Tests for citation graph tool."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from arxiv_mcp_server.tools import citation_graph
from arxiv_mcp_server.tools.citation_graph import handle_citation_graph


@pytest.mark.asyncio
async def test_citation_graph_success():
    """Citation graph should return citations and references with normalized fields."""
    mock_payload = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
        "citations": [
            {
                "paperId": "citing-1",
                "title": "Citing Paper",
                "year": 2025,
                "authors": [{"name": "Author B"}],
                "externalIds": {"ArXiv": "2501.00001"},
            }
        ],
        "references": [
            {
                "paperId": "ref-1",
                "title": "Referenced Paper",
                "year": 2020,
                "authors": [{"name": "Author C"}],
                "externalIds": {"ArXiv": "2001.00001"},
            }
        ],
    }

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = mock_payload

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert payload["citation_count"] == 1
    assert payload["reference_count"] == 1
    assert payload["citations"][0]["arxiv_id"] == "2501.00001"


def _legacy_mock_payload():
    """Shared legacy nested payload (mirrors test_citation_graph_success)."""
    return {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
        "citations": [
            {
                "paperId": "citing-1",
                "title": "Citing Paper",
                "year": 2025,
                "authors": [{"name": "Author B"}],
                "externalIds": {"ArXiv": "2501.00001"},
            }
        ],
        "references": [
            {
                "paperId": "ref-1",
                "title": "Referenced Paper",
                "year": 2020,
                "authors": [{"name": "Author C"}],
                "externalIds": {"ArXiv": "2001.00001"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_citation_graph_default_unchanged():
    """Default call (no new params) must still take the legacy nested path.

    Asserts: indent=2 output, edges include authors + external_ids, single
    nested request (one client.get).
    """
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    text = response[0].text
    # Legacy path uses indent=2 -> newlines present in the rendered JSON.
    assert "\n" in text
    # Legacy path makes exactly one (nested) request.
    assert mock_client.get.await_count == 1

    payload = json.loads(text)
    assert "pagination" not in payload
    citation_edge = payload["citations"][0]
    assert "authors" in citation_edge
    assert "external_ids" in citation_edge
    assert citation_edge["arxiv_id"] == "2501.00001"
    reference_edge = payload["references"][0]
    assert "authors" in reference_edge
    assert "external_ids" in reference_edge

    # Golden byte-for-byte: pin the EXACT default output so a future change to
    # the legacy path cannot silently alter it (backward-compat guarantee).
    expected = {
        "status": "success",
        "paper": {
            "paper_id": "root-paper",
            "arxiv_id": "2401.12345",
            "title": "Root Paper",
            "year": 2024,
            "authors": ["Author A"],
            "external_ids": {"ArXiv": "2401.12345"},
        },
        "citation_count": 1,
        "reference_count": 1,
        "citations": [
            {
                "paper_id": "citing-1",
                "title": "Citing Paper",
                "year": 2025,
                "authors": ["Author B"],
                "external_ids": {"ArXiv": "2501.00001"},
                "arxiv_id": "2501.00001",
            }
        ],
        "references": [
            {
                "paper_id": "ref-1",
                "title": "Referenced Paper",
                "year": 2020,
                "authors": ["Author C"],
                "external_ids": {"ArXiv": "2001.00001"},
                "arxiv_id": "2001.00001",
            }
        ],
    }
    assert text == json.dumps(expected, indent=2)


@pytest.mark.asyncio
async def test_citation_graph_compact():
    """Compact opt-in path: minified output, stripped edges, pagination block."""
    # Call order in the implementation: root metadata, /citations, /references.
    root_response = MagicMock()
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.raise_for_status = MagicMock()
    citations_response.json.return_value = {
        "offset": 0,
        "next": 5,
        "data": [
            {
                "citingPaper": {
                    "paperId": "citing-1",
                    "title": "Citing Paper",
                    "year": 2025,
                    "authors": [{"name": "Author B"}],
                    "externalIds": {"ArXiv": "2501.00001"},
                }
            }
        ],
    }

    references_response = MagicMock()
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {
        "offset": 0,
        "next": 5,
        "data": [
            {
                "citedPaper": {
                    "paperId": "ref-1",
                    "title": "Referenced Paper",
                    "year": 2020,
                    "authors": [{"name": "Author C"}],
                    "externalIds": {"ArXiv": "2001.00001"},
                }
            }
        ],
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "compact": True, "limit": 5}
        )

    text = response[0].text
    # Minified output: no newline.
    assert "\n" not in text

    payload = json.loads(text)
    assert payload["status"] == "success"

    citation_edge = payload["citations"][0]
    assert set(citation_edge.keys()) == {"paper_id", "arxiv_id", "title", "year"}
    assert citation_edge["arxiv_id"] == "2501.00001"

    reference_edge = payload["references"][0]
    assert set(reference_edge.keys()) == {"paper_id", "arxiv_id", "title", "year"}

    # Compact root paper has no authors/external_ids.
    assert set(payload["paper"].keys()) == {"paper_id", "arxiv_id", "title", "year"}

    assert "pagination" in payload
    assert payload["pagination"]["limit"] == 5
    assert payload["pagination"]["citations"]["offset"] == 0
    assert payload["pagination"]["references"]["offset"] == 0
    assert payload["pagination"]["citations"]["next"] == 5
    assert payload["pagination"]["citations"]["returned"] == 1


@pytest.mark.asyncio
async def test_citation_graph_paginated_full():
    """Paginated non-compact path: full edges, indent=2, offset propagated."""
    # Call order in the implementation: root metadata, /citations, /references.
    root_response = MagicMock()
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.raise_for_status = MagicMock()
    citations_response.json.return_value = {
        "offset": 5,
        "next": 10,
        "data": [
            {
                "citingPaper": {
                    "paperId": "citing-1",
                    "title": "Citing Paper",
                    "year": 2025,
                    "authors": [{"name": "Author B"}],
                    "externalIds": {"ArXiv": "2501.00001"},
                }
            }
        ],
    }

    references_response = MagicMock()
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {
        "offset": 5,
        "data": [
            {
                "citedPaper": {
                    "paperId": "ref-1",
                    "title": "Referenced Paper",
                    "year": 2020,
                    "authors": [{"name": "Author C"}],
                    "externalIds": {"ArXiv": "2001.00001"},
                }
            }
        ],
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "limit": 5, "offset": 5}
        )

    text = response[0].text
    # Non-compact path uses indent=2 -> newlines present.
    assert "\n" in text

    payload = json.loads(text)
    assert payload["pagination"]["citations"]["offset"] == 5
    assert payload["pagination"]["references"]["offset"] == 5
    assert payload["pagination"]["limit"] == 5

    citation_edge = payload["citations"][0]
    assert "authors" in citation_edge
    assert citation_edge["authors"] == ["Author B"]
    assert "external_ids" in citation_edge

    # next absent on last page -> None.
    assert payload["pagination"]["references"]["next"] is None


@pytest.mark.asyncio
async def test_citation_graph_http_error():
    """Citation graph should surface HTTP API errors."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = Exception("boom")

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    assert response[0].text.startswith("Error:")


@pytest.mark.asyncio
async def test_citation_graph_offset_only_uses_legacy():
    """`offset` alone must NOT trigger pagination (backward-compat trap, FIX A).

    Asserts the legacy path: exactly ONE client.get await, indent=2 output
    (newline present), and no `pagination` key.
    """
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345", "offset": 5})

    text = response[0].text
    # Legacy path makes exactly one (nested) request.
    assert mock_client.get.await_count == 1
    # Legacy path uses indent=2 -> newlines present in the rendered JSON.
    assert "\n" in text

    payload = json.loads(text)
    assert "pagination" not in payload


@pytest.mark.asyncio
async def test_citation_graph_compact_default_limit():
    """`compact` with no `limit` must default the page limit to 100 (FIX A path)."""
    root_response = MagicMock()
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.raise_for_status = MagicMock()
    citations_response.json.return_value = {"offset": 0, "next": 100, "data": []}

    references_response = MagicMock()
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {"offset": 0, "data": []}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "compact": True}
        )

    payload = json.loads(response[0].text)
    assert payload["pagination"]["limit"] == 100


@pytest.mark.asyncio
async def test_citation_graph_paginated_http_error():
    """Paginated path surfaces HTTP errors with no partial result (FIX D)."""
    root_response = MagicMock()
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    # The /citations response raises on raise_for_status.
    failing_response = MagicMock()
    failing_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=MagicMock()
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[root_response, failing_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    text = response[0].text
    assert text.startswith("Error:")
    # No partial result emitted.
    assert "pagination" not in text


@pytest.mark.asyncio
async def test_citation_graph_paginated_empty_data():
    """Empty /citations data: count 0, no crash, `next` None when absent (FIX D)."""
    root_response = MagicMock()
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.raise_for_status = MagicMock()
    # No `next` key -> should normalize to None.
    citations_response.json.return_value = {"offset": 0, "data": []}

    references_response = MagicMock()
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {
        "offset": 0,
        "next": 5,
        "data": [
            {
                "citedPaper": {
                    "paperId": "ref-1",
                    "title": "Referenced Paper",
                    "year": 2020,
                    "authors": [{"name": "Author C"}],
                    "externalIds": {"ArXiv": "2001.00001"},
                }
            }
        ],
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    payload = json.loads(response[0].text)
    assert payload["citation_count"] == 0
    assert payload["citations"] == []
    assert payload["pagination"]["citations"]["next"] is None


@pytest.mark.asyncio
async def test_citation_graph_limit_offset_clamped():
    """Out-of-range limit/offset are clamped in code (FIX B).

    limit=99999 -> 1000, offset=-5 -> 0, reflected in the request URLs.
    """
    root_response = MagicMock()
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.raise_for_status = MagicMock()
    citations_response.json.return_value = {"offset": 0, "data": []}

    references_response = MagicMock()
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {"offset": 0, "data": []}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph(
            {
                "paper_id": "2401.12345",
                "limit": 99999,
                "offset": -5,
                "compact": True,
            }
        )

    # Inspect the awaited request URLs (positional arg 0 of each client.get call).
    awaited_urls = [call.args[0] for call in mock_client.get.call_args_list]
    # The two paged endpoints (/citations, /references) must carry the clamped
    # values. The root metadata request carries neither.
    paged_urls = [u for u in awaited_urls if "limit=" in u]
    assert paged_urls, "expected paged endpoint URLs with limit/offset"
    for url in paged_urls:
        assert "limit=1000" in url
        assert "offset=0" in url


def _paginated_mocks(citations_next):
    """Build (root, citations, references) response mocks for the paginated path."""
    root = MagicMock()
    root.raise_for_status = MagicMock()
    root.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }
    citations = MagicMock()
    citations.raise_for_status = MagicMock()
    citations.json.return_value = {
        "offset": 0,
        "next": citations_next,
        "data": [{"citingPaper": {"paperId": "c1", "title": "C", "year": 2025}}],
    }
    references = MagicMock()
    references.raise_for_status = MagicMock()
    references.json.return_value = {"offset": 0, "data": []}
    return root, citations, references


@pytest.mark.asyncio
async def test_citation_graph_pagination_next_offset_roundtrip():
    """The `next` cursor from page 1 is usable as the `offset` for page 2.

    Pins the README's documented paging loop: read pagination.citations.next,
    feed it back as `offset`, and the next request URL carries that offset.
    """
    # Page 1: limit=5, offset 0 -> citations.next == 5.
    root1, cit1, ref1 = _paginated_mocks(citations_next=5)
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[root1, cit1, ref1])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        page1 = await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    next_cursor = json.loads(page1[0].text)["pagination"]["citations"]["next"]
    assert next_cursor == 5

    # Page 2: feed next_cursor back as offset; the citations URL must carry it.
    root2, cit2, ref2 = _paginated_mocks(citations_next=None)
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[root2, cit2, ref2])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph(
            {"paper_id": "2401.12345", "limit": 5, "offset": next_cursor}
        )

    citations_urls = [
        c.args[0] for c in mock_client.get.call_args_list if "/citations" in c.args[0]
    ]
    assert citations_urls and f"offset={next_cursor}" in citations_urls[0]


@pytest.mark.asyncio
async def test_citation_graph_old_style_id_quoted():
    """Old-style arXiv IDs contain a slash (e.g. hep-th/9901001); it must be
    percent-encoded so it is not treated as a URL path separator."""
    root, cit, ref = _paginated_mocks(citations_next=None)
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[root, cit, ref])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "hep-th/9901001", "limit": 5})

    urls = [c.args[0] for c in mock_client.get.call_args_list]
    assert urls, "expected requests to be made"
    for u in urls:
        # The id's slash is encoded (%2F); the raw `hep-th/9901001` never appears.
        assert "hep-th%2F9901001" in u
        assert "hep-th/9901001" not in u


@pytest.mark.asyncio
async def test_citation_graph_compact_strict_bool():
    """A non-bool truthy `compact` (e.g. the string "false") must NOT enable the
    compact/paginated path — only a real JSON true does (defense-in-depth)."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "compact": "false"}
        )

    # Legacy path: exactly one request, no pagination block.
    assert mock_client.get.await_count == 1
    assert "pagination" not in json.loads(response[0].text)


@pytest.mark.asyncio
async def test_citation_graph_retries_on_429():
    """A transient 429 is retried; the subsequent 200 succeeds (FIX C2)."""
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {}

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = _legacy_mock_payload()

    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[rate_limited, ok_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    # First call hit 429, second call returned the 200 payload.
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_citation_graph_429_exhausted():
    """A 429 that survives all retries surfaces as an Error envelope (FIX C2).

    max_retries defaults to 4 -> 1 initial + 4 retries == 5 awaited GETs.
    """
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {}
    rate_limited.raise_for_status.side_effect = httpx.HTTPStatusError(
        "rate limited", request=MagicMock(), response=MagicMock()
    )

    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=rate_limited)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    assert response[0].text.startswith("Error:")
    # 1 initial GET + max_retries (4) retries == 5.
    assert mock_client.get.await_count == 5


@pytest.mark.asyncio
async def test_citation_graph_retry_after_header():
    """A numeric Retry-After header drives the backoff delay (FIX C2)."""
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {"Retry-After": "7"}

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = _legacy_mock_payload()

    sleep_mock = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=sleep_mock),
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[rate_limited, ok_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345"})

    sleep_mock.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_citation_graph_output_cap(monkeypatch):
    """An output cap truncates each direction and flags `truncated` (FIX C2)."""
    monkeypatch.setattr(citation_graph.settings, "CITATION_MAX_EDGES", 1)

    payload = _legacy_mock_payload()
    payload["citations"].append(
        {
            "paperId": "citing-2",
            "title": "Citing Paper 2",
            "year": 2025,
            "authors": [{"name": "Author D"}],
            "externalIds": {"ArXiv": "2501.00002"},
        }
    )
    payload["references"].append(
        {
            "paperId": "ref-2",
            "title": "Referenced Paper 2",
            "year": 2019,
            "authors": [{"name": "Author E"}],
            "externalIds": {"ArXiv": "2001.00002"},
        }
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = payload

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    result = json.loads(response[0].text)
    assert result["citation_count"] == 1
    assert result["reference_count"] == 1
    assert result["truncated"] is True
    assert len(result["citations"]) == 1
    assert len(result["references"]) == 1


@pytest.mark.asyncio
async def test_citation_graph_cap_unset_no_key():
    """With the default cap (None), no `truncated` key appears (golden contract)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    assert "truncated" not in json.loads(response[0].text)


@pytest.mark.asyncio
async def test_citation_graph_retries_on_5xx():
    """A transient 503 is retried; the subsequent 200 legacy payload succeeds."""
    server_error = MagicMock()
    server_error.status_code = 503
    server_error.headers = {}

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = _legacy_mock_payload()

    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[server_error, ok_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_citation_graph_retries_on_transport_error():
    """A transport error (ConnectError) is retried; the next 200 succeeds."""
    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = _legacy_mock_payload()

    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[httpx.ConnectError("boom"), ok_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_citation_graph_retry_after_clamped():
    """An absurd Retry-After is clamped to MAX_RETRY_DELAY, not slept literally."""
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {"Retry-After": "99999"}

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = _legacy_mock_payload()

    sleep_mock = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=sleep_mock),
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[rate_limited, ok_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345"})

    # Clamped to MAX_RETRY_DELAY (16.0), NOT the literal 99999.
    sleep_mock.assert_awaited_once_with(16.0)


@pytest.mark.asyncio
async def test_citation_graph_backoff_jitter():
    """With no Retry-After, the backoff uses jittered random.uniform."""
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {}

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = _legacy_mock_payload()

    sleep_mock = AsyncMock()
    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=sleep_mock),
        patch(
            "arxiv_mcp_server.tools.citation_graph.random.uniform", return_value=0.5
        ) as uniform_mock,
    ):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[rate_limited, ok_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345"})

    uniform_mock.assert_called()
    sleep_mock.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_citation_graph_paginated_retries():
    """The paginated path retries a 429 on a sub-request and still succeeds."""
    root_response = MagicMock()
    root_response.status_code = 200
    root_response.headers = {}
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_429 = MagicMock()
    citations_429.status_code = 429
    citations_429.headers = {}

    citations_ok = MagicMock()
    citations_ok.status_code = 200
    citations_ok.headers = {}
    citations_ok.raise_for_status = MagicMock()
    citations_ok.json.return_value = {
        "offset": 0,
        "next": 5,
        "data": [
            {
                "citingPaper": {
                    "paperId": "citing-1",
                    "title": "Citing Paper",
                    "year": 2025,
                    "authors": [{"name": "Author B"}],
                    "externalIds": {"ArXiv": "2501.00001"},
                }
            }
        ],
    }

    references_response = MagicMock()
    references_response.status_code = 200
    references_response.headers = {}
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {"offset": 0, "data": []}

    with (
        patch("httpx.AsyncClient") as mock_client_class,
        patch("arxiv_mcp_server.tools.citation_graph.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client = AsyncMock()
        # root, citations(429), citations(200 retry), references.
        mock_client.get = AsyncMock(
            side_effect=[
                root_response,
                citations_429,
                citations_ok,
                references_response,
            ]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert "pagination" in payload
    assert payload["citation_count"] == 1


@pytest.mark.asyncio
async def test_citation_graph_paginated_no_cap(monkeypatch):
    """The output cap must NOT apply in the paginated path (cursor integrity)."""
    monkeypatch.setattr(citation_graph.settings, "CITATION_MAX_EDGES", 1)

    root_response = MagicMock()
    root_response.status_code = 200
    root_response.headers = {}
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.status_code = 200
    citations_response.headers = {}
    citations_response.raise_for_status = MagicMock()
    citations_response.json.return_value = {
        "offset": 0,
        "next": 5,
        "data": [
            {
                "citingPaper": {
                    "paperId": "citing-1",
                    "title": "Citing Paper",
                    "year": 2025,
                    "authors": [{"name": "Author B"}],
                    "externalIds": {"ArXiv": "2501.00001"},
                }
            },
            {
                "citingPaper": {
                    "paperId": "citing-2",
                    "title": "Citing Paper 2",
                    "year": 2025,
                    "authors": [{"name": "Author D"}],
                    "externalIds": {"ArXiv": "2501.00002"},
                }
            },
        ],
    }

    references_response = MagicMock()
    references_response.status_code = 200
    references_response.headers = {}
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {
        "offset": 0,
        "next": 5,
        "data": [
            {
                "citedPaper": {
                    "paperId": "ref-1",
                    "title": "Referenced Paper",
                    "year": 2020,
                    "authors": [{"name": "Author C"}],
                    "externalIds": {"ArXiv": "2001.00001"},
                }
            },
            {
                "citedPaper": {
                    "paperId": "ref-2",
                    "title": "Referenced Paper 2",
                    "year": 2019,
                    "authors": [{"name": "Author E"}],
                    "externalIds": {"ArXiv": "2001.00002"},
                }
            },
        ],
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    payload = json.loads(response[0].text)
    # Cap (1) is NOT applied in the paginated path: full page returned, no flag.
    assert "truncated" not in payload
    assert payload["citation_count"] == 2
    assert payload["reference_count"] == 2
    assert len(payload["citations"]) == 2
    assert len(payload["references"]) == 2


@pytest.mark.asyncio
async def test_citation_graph_negative_cap_ignored(monkeypatch):
    """A negative cap is treated as "no cap" (no negative-slice truncation)."""
    monkeypatch.setattr(citation_graph.settings, "CITATION_MAX_EDGES", -1)

    payload = _legacy_mock_payload()
    payload["citations"].append(
        {
            "paperId": "citing-2",
            "title": "Citing Paper 2",
            "year": 2025,
            "authors": [{"name": "Author D"}],
            "externalIds": {"ArXiv": "2501.00002"},
        }
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = payload

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    result = json.loads(response[0].text)
    # Negative cap == no cap: both citations returned, no truncation flag.
    assert "truncated" not in result
    assert result["citation_count"] == 2
    assert len(result["citations"]) == 2


@pytest.mark.asyncio
async def test_citation_graph_sends_api_key(monkeypatch):
    """When SEMANTIC_SCHOLAR_API_KEY is set, the legacy path sends it as the
    `x-api-key` header on the S2 request."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", "secret-key"
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345"})

    sent_headers = mock_client.get.call_args.kwargs["headers"]
    assert sent_headers.get("x-api-key") == "secret-key"


@pytest.mark.asyncio
async def test_citation_graph_no_api_key_no_header(monkeypatch):
    """With no API key configured (default None), the `headers` kwarg carries no
    `x-api-key` (byte-for-byte unauthenticated behavior is preserved)."""
    # Self-contained: pin the precondition rather than relying on import-time state.
    monkeypatch.setattr(citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", None)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345"})

    sent_headers = mock_client.get.call_args.kwargs["headers"]
    assert "x-api-key" not in sent_headers
    assert sent_headers == {}


@pytest.mark.asyncio
async def test_citation_graph_api_key_paginated(monkeypatch):
    """When the API key is set, ALL THREE paginated sub-requests (root,
    /citations, /references) carry the `x-api-key` header."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", "secret-key"
    )

    root_response = MagicMock()
    root_response.status_code = 200
    root_response.headers = {}
    root_response.raise_for_status = MagicMock()
    root_response.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }

    citations_response = MagicMock()
    citations_response.status_code = 200
    citations_response.headers = {}
    citations_response.raise_for_status = MagicMock()
    citations_response.json.return_value = {"offset": 0, "data": []}

    references_response = MagicMock()
    references_response.status_code = 200
    references_response.headers = {}
    references_response.raise_for_status = MagicMock()
    references_response.json.return_value = {"offset": 0, "data": []}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[root_response, citations_response, references_response]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    assert mock_client.get.await_count == 3
    for call in mock_client.get.call_args_list:
        assert call.kwargs["headers"].get("x-api-key") == "secret-key"


def _legacy_200_mock():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()
    return mock_response


@pytest.mark.asyncio
async def test_citation_graph_malformed_key_ignored(monkeypatch):
    """A malformed key with an INTERIOR illegal header char (which strip cannot
    salvage) is dropped, NOT sent to the HTTP layer. This prevents the key from
    leaking back through an h11 'Illegal header value' exception into logs /
    returned text. (A merely trailing newline is stripped and still sent — see
    test_citation_graph_whitespace_key_stripped.)"""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", "secret\nkey"
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_legacy_200_mock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    # No malformed header reaches httpx, the call succeeds, and the key value
    # never appears in the returned text.
    sent_headers = mock_client.get.call_args.kwargs["headers"]
    assert "x-api-key" not in sent_headers
    assert sent_headers == {}
    assert "secret" not in response[0].text  # key fragment never leaks
    assert json.loads(response[0].text)["status"] == "success"


@pytest.mark.asyncio
async def test_citation_graph_whitespace_key_stripped(monkeypatch):
    """A key with surrounding whitespace is stripped before being sent."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", "  realkey  "
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_legacy_200_mock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345"})

    assert mock_client.get.call_args.kwargs["headers"]["x-api-key"] == "realkey"


@pytest.mark.asyncio
async def test_citation_graph_nonascii_key_ignored(monkeypatch, caplog):
    """A non-ASCII key (e.g. U+2028) is dropped before reaching httpx, and the
    warning never echoes the key value (self-contained no-leak guarantee)."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", "secret\u2028key"
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_legacy_200_mock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        with caplog.at_level("WARNING"):
            response = await handle_citation_graph({"paper_id": "2401.12345"})

    assert mock_client.get.call_args.kwargs["headers"] == {}
    assert "secret" not in response[0].text
    assert "secret" not in caplog.text
    assert json.loads(response[0].text)["status"] == "success"


@pytest.mark.asyncio
async def test_citation_graph_api_key_paginated_unset(monkeypatch):
    """With no key, ALL THREE paginated sub-requests carry no `x-api-key`."""
    monkeypatch.setattr(citation_graph.settings, "SEMANTIC_SCHOLAR_API_KEY", None)

    root = MagicMock()
    root.status_code = 200
    root.headers = {}
    root.raise_for_status = MagicMock()
    root.json.return_value = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "authors": [{"name": "Author A"}],
        "externalIds": {"ArXiv": "2401.12345"},
    }
    page = MagicMock()
    page.status_code = 200
    page.headers = {}
    page.raise_for_status = MagicMock()
    page.json.return_value = {"offset": 0, "data": []}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[root, page, page])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await handle_citation_graph({"paper_id": "2401.12345", "limit": 5})

    assert mock_client.get.await_count == 3
    for call in mock_client.get.call_args_list:
        assert "x-api-key" not in call.kwargs["headers"]


@pytest.mark.asyncio
async def test_citation_graph_cap_zero(monkeypatch):
    """A cap of 0 is a real "zero edges" request: empty lists + truncated when
    edges existed (distinct from None/negative = no cap)."""
    monkeypatch.setattr(citation_graph.settings, "CITATION_MAX_EDGES", 0)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()  # 1 citation + 1 ref

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph({"paper_id": "2401.12345"})

    result = json.loads(response[0].text)
    assert result["citation_count"] == 0
    assert result["reference_count"] == 0
    assert result["citations"] == []
    assert result["references"] == []
    assert result["truncated"] is True


# --- C4a: counts_only mode (true scalar totals) ----------------------------


@pytest.mark.asyncio
async def test_citation_graph_counts_only():
    """counts_only returns the paper's TRUE scalar totals via ONE endpoint, no edges.

    Pins the F1 fix: `citation_count` here is S2's authoritative citationCount
    (180624 for 1706.03762), NOT the page-capped edge count of the graph modes.
    """
    counts_payload = {
        "paperId": "root-paper",
        "title": "Attention Is All You Need",
        "year": 2017,
        "externalIds": {"ArXiv": "1706.03762"},
        "citationCount": 180624,
        "referenceCount": 41,
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = counts_payload

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "1706.03762", "counts_only": True}
        )

    # One endpoint only (no root + /citations + /references fan-out).
    assert mock_client.get.await_count == 1
    # The request asks S2 for the scalar count fields.
    url = mock_client.get.call_args.args[0]
    assert "citationCount" in url and "referenceCount" in url

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert payload["counts_only"] is True
    # TRUE totals, under distinct keys (not the graph modes' citation_count).
    assert payload["total_citations"] == 180624
    assert payload["total_references"] == 41
    assert "citation_count" not in payload
    assert payload["paper"]["arxiv_id"] == "1706.03762"
    # No edge lists / pagination block in counts mode.
    assert "citations" not in payload
    assert "references" not in payload
    assert "pagination" not in payload


@pytest.mark.asyncio
async def test_citation_graph_counts_only_precedence():
    """counts_only takes precedence over limit/compact: one endpoint, no edges."""
    counts_payload = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "externalIds": {"ArXiv": "2401.12345"},
        "citationCount": 5,
        "referenceCount": 3,
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = counts_payload

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {
                "paper_id": "2401.12345",
                "counts_only": True,
                "limit": 5,
                "compact": True,
            }
        )

    # counts_only wins over the paginated/compact path.
    assert mock_client.get.await_count == 1
    payload = json.loads(response[0].text)
    assert payload["counts_only"] is True
    assert "pagination" not in payload
    assert payload["total_citations"] == 5


@pytest.mark.asyncio
async def test_citation_graph_counts_only_strict_bool():
    """A non-bool truthy counts_only (string "true") must NOT enable counts mode;
    it falls through to the legacy graph path (defense-in-depth, like compact)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _legacy_mock_payload()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "counts_only": "true"}
        )

    payload = json.loads(response[0].text)
    # Legacy graph path, not counts mode.
    assert "counts_only" not in payload
    assert "citations" in payload


@pytest.mark.asyncio
async def test_citation_graph_counts_only_null_counts():
    """Missing S2 counts (null) pass through as null without crashing."""
    counts_payload = {
        "paperId": "root-paper",
        "title": "Root Paper",
        "year": 2024,
        "externalIds": {"ArXiv": "2401.12345"},
        # citationCount / referenceCount intentionally absent.
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = counts_payload

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "counts_only": True}
        )

    payload = json.loads(response[0].text)
    assert payload["total_citations"] is None
    assert payload["total_references"] is None


@pytest.mark.asyncio
async def test_citation_graph_counts_only_http_error():
    """A counts-mode HTTP error (e.g. 404 not-found) surfaces via the same Error
    envelope as the other paths — regression guard for the shared try/except."""
    failing = MagicMock()
    failing.status_code = 200
    failing.headers = {}
    failing.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found", request=MagicMock(), response=MagicMock()
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=failing)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        response = await handle_citation_graph(
            {"paper_id": "2401.12345", "counts_only": True}
        )

    assert response[0].text.startswith("Error:")


# --- B11: request pacing ----------------------------------------------------


@pytest.mark.asyncio
async def test_pace_request_disabled_by_default(monkeypatch):
    """Default interval (0.0) -> pacing is a no-op (asyncio.sleep never awaited)."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL", 0.0
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(citation_graph.asyncio, "sleep", sleep_mock)

    await citation_graph._pace_request()

    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_pace_request_spaces_consecutive_calls(monkeypatch):
    """A positive interval makes a call wait when a prior request was recent (B11).

    Uses the real monotonic clock (patching the global time.monotonic would break
    asyncio's event loop); seeds `_next_request_time` ~1s ahead and asserts the
    pacer sleeps a positive, bounded amount.
    """
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL", 1.0
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(citation_graph.asyncio, "sleep", sleep_mock)
    # Pretend the previous request scheduled the next-allowed time ~1s out.
    citation_graph._next_request_time = time.monotonic() + 1.0
    try:
        await citation_graph._pace_request()
    finally:
        citation_graph._next_request_time = 0.0

    sleep_mock.assert_awaited_once()
    slept = sleep_mock.await_args.args[0]
    assert 0 < slept <= 1.0


@pytest.mark.asyncio
async def test_pace_request_clamps_huge_interval(monkeypatch):
    """A misconfigured huge interval is clamped to MAX_PACE_INTERVAL, so a single
    pacing wait cannot hang the tool (mirrors the Retry-After clamp)."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL", 3600.0
    )
    citation_graph._next_request_time = 0.0
    citation_graph._pace_lock = None
    citation_graph._pace_loop = None
    sleep_mock = AsyncMock()
    monkeypatch.setattr(citation_graph.asyncio, "sleep", sleep_mock)
    try:
        await citation_graph._pace_request()  # schedules next at now + clamp
        await citation_graph._pace_request()  # must wait <= clamp, NOT 3600
    finally:
        citation_graph._next_request_time = 0.0
        citation_graph._pace_lock = None
        citation_graph._pace_loop = None

    sleep_mock.assert_awaited_once()
    assert sleep_mock.await_args.args[0] <= citation_graph.MAX_PACE_INTERVAL


@pytest.mark.asyncio
async def test_pace_request_wait_clamped_even_if_schedule_drifts(monkeypatch):
    """The *wait* is clamped, not just the configured interval: even if
    `_next_request_time` sits pathologically far ahead (schedule drift, or the
    float round-up where `now + 30.0` lands a few ULPs above 30 — the CI flake
    `assert 30.00000000000003 <= 30.0`), a single pacing sleep never exceeds
    MAX_PACE_INTERVAL."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL", 1.0
    )
    citation_graph._pace_lock = None
    citation_graph._pace_loop = None
    sleep_mock = AsyncMock()
    monkeypatch.setattr(citation_graph.asyncio, "sleep", sleep_mock)
    citation_graph._next_request_time = time.monotonic() + 10_000.0
    try:
        await citation_graph._pace_request()
    finally:
        citation_graph._next_request_time = 0.0
        citation_graph._pace_lock = None
        citation_graph._pace_loop = None

    sleep_mock.assert_awaited_once()
    # Exactly the clamp: min(~9999.9, 30.0) is 30.0 bit-for-bit, so == proves
    # the clamp engaged (<= would also pass if the pacer regressed to sleep 0).
    assert sleep_mock.await_args.args[0] == citation_graph.MAX_PACE_INTERVAL


@pytest.mark.asyncio
async def test_pace_request_non_finite_interval_is_noop(monkeypatch):
    """A non-finite interval (inf / nan) disables pacing instead of hanging."""
    sleep_mock = AsyncMock()
    monkeypatch.setattr(citation_graph.asyncio, "sleep", sleep_mock)
    for bad in (float("inf"), float("nan")):
        monkeypatch.setattr(
            citation_graph.settings, "SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL", bad
        )
        await citation_graph._pace_request()
    sleep_mock.assert_not_awaited()


def test_pace_request_survives_loop_change(monkeypatch):
    """The per-loop pace lock must not raise 'bound to a different event loop'
    when pacing runs under two different event loops (cross-vendor finding).

    A module-global asyncio.Lock created once would bind to the first loop and
    raise on the second asyncio.run; the lazy per-loop lock must not."""
    monkeypatch.setattr(
        citation_graph.settings, "SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL", 0.01
    )
    citation_graph._next_request_time = 0.0
    citation_graph._pace_lock = None
    citation_graph._pace_loop = None

    async def two_contending_calls():
        # gather() contends the lock so it actually binds to this loop.
        await asyncio.gather(
            citation_graph._pace_request(), citation_graph._pace_request()
        )

    try:
        asyncio.run(two_contending_calls())  # loop A binds the lock
        asyncio.run(two_contending_calls())  # loop B must not raise
    finally:
        citation_graph._next_request_time = 0.0
        citation_graph._pace_lock = None
        citation_graph._pace_loop = None

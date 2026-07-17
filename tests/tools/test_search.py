"""Tests for paper search functionality.

B16 unified search onto the raw-HTTP path (there is no longer an arxiv-package
branch in handle_search), so these tests mock at the httpx layer rather than the
arxiv package. `respx` is not a dependency, so the stub patches `httpx.AsyncClient`
directly and returns canned Atom XML.
"""

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock
from arxiv_mcp_server.tools import handle_search
from arxiv_mcp_server.tools.search import (
    _validate_categories,
    _raw_arxiv_search,
    _parse_arxiv_atom_response,
)

# ---------------------------------------------------------------------------
# Helpers: build canned Atom feeds and stub the httpx client
# ---------------------------------------------------------------------------


def _atom_feed(
    entries=1,
    total_results=None,
    categories=("cs.AI",),
    paper_id="2103.12345",
    title="Test Paper",
):
    """Build a minimal arXiv Atom feed with ``entries`` entries and an optional
    feed-level ``opensearch:totalResults`` (omitted entirely when None)."""
    total_xml = (
        f"<opensearch:totalResults>{total_results}</opensearch:totalResults>"
        if total_results is not None
        else ""
    )
    primary = f'<arxiv:primary_category term="{categories[0]}"/>' if categories else ""
    cats = "".join(f'<category term="{c}"/>' for c in categories)
    entry_xml = "".join(f"""
        <entry>
            <id>http://arxiv.org/abs/{paper_id}v1</id>
            <title>{title}</title>
            <summary>Test abstract</summary>
            <published>2023-06-15T00:00:00Z</published>
            <author><name>Test Author</name></author>
            {primary}
            {cats}
            <link title="pdf" href="http://arxiv.org/pdf/{paper_id}v1"/>
        </entry>""" for _ in range(entries))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
        {total_xml}
        {entry_xml}
    </feed>"""


def _stub_httpx(monkeypatch, xml_text):
    """Patch ``httpx.AsyncClient`` so ``_raw_arxiv_search``'s GET returns canned
    Atom XML. Returns the mock client so a test can read the outgoing URL from
    ``mock_client.get.call_args``.
    """
    mock_response = MagicMock()
    mock_response.text = xml_text
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("httpx.AsyncClient", MagicMock(return_value=mock_client))
    return mock_client


def _outgoing_url(mock_client):
    """The URL string passed to the (single) stubbed GET."""
    return mock_client.get.call_args[0][0]


# ---------------------------------------------------------------------------
# handle_search behaviour (all via the unified raw-HTTP path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_search(monkeypatch):
    """Test basic paper search functionality."""
    _stub_httpx(monkeypatch, _atom_feed(entries=1))
    result = await handle_search({"query": "test query", "max_results": 1})

    assert len(result) == 1
    content = json.loads(result[0].text)
    # No opensearch:totalResults in the feed → total_results falls back to page size.
    assert content["total_results"] == 1
    assert content["returned"] == 1
    paper = content["papers"][0]
    assert paper["id"] == "2103.12345"
    assert paper["title"] == "Test Paper"
    assert "resource_uri" in paper


@pytest.mark.asyncio
async def test_search_with_categories(monkeypatch):
    """Test paper search with category filtering (categories parsed from the feed)."""
    _stub_httpx(monkeypatch, _atom_feed(entries=1, categories=("cs.AI", "cs.LG")))
    result = await handle_search(
        {"query": "test query", "categories": ["cs.AI", "cs.LG"], "max_results": 1}
    )

    content = json.loads(result[0].text)
    assert content["papers"][0]["categories"] == ["cs.AI", "cs.LG"]


@pytest.mark.asyncio
async def test_search_with_dates(monkeypatch):
    """Test paper search with date filtering (still the raw HTTP path)."""
    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1, paper_id="2301.00001"))

    result = await handle_search(
        {
            "query": "test query",
            "date_from": "2022-01-01",
            "date_to": "2024-01-01",
            "max_results": 1,
        }
    )

    content = json.loads(result[0].text)
    assert content["total_results"] == 1
    assert content["returned"] == 1
    assert len(content["papers"]) == 1
    # The date clause is present and its '+TO+' is not percent-encoded.
    assert "+TO+" in _outgoing_url(mock_client)


@pytest.mark.asyncio
async def test_search_with_invalid_dates():
    """Test search with invalid date formats (fails before any HTTP call)."""
    result = await handle_search(
        {"query": "test query", "date_from": "invalid-date", "max_results": 1}
    )

    assert "Error:" in result[0].text


@pytest.mark.asyncio
async def test_categories_anded_with_query_in_url(monkeypatch):
    """B16 (5a): a non-date query + categories joins the query group and the
    category group with an explicit +AND+, so the category filter is strict — a
    bare space is NOT AND on the arXiv API."""
    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))
    await handle_search(
        {"query": "survey", "categories": ["cs.HC", "cs.CY"], "max_results": 5}
    )

    url = _outgoing_url(mock_client)
    assert "search_query=" in url
    assert "+AND+" in url
    # Groups are AND-joined; parens are left literal by the encode replace-chain.
    assert "(survey)+AND+(cat:cs.HC+OR+cat:cs.CY)" in url


@pytest.mark.asyncio
async def test_quoted_phrase_preserved_in_url(monkeypatch):
    """B16 (5b): a quoted phrase survives the encode replace-chain — quotes are
    preserved and the interior space becomes '+' (accepted identically by the live
    API, measured 2026-07-17)."""
    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))
    await handle_search({"query": 'ti:"transformer architecture"', "max_results": 5})

    url = _outgoing_url(mock_client)
    assert 'ti:"transformer+architecture"' in url


@pytest.mark.asyncio
async def test_total_results_from_opensearch(monkeypatch):
    """B16 (5c): opensearch:totalResults becomes total_results; returned is the
    page size."""
    _stub_httpx(monkeypatch, _atom_feed(entries=2, total_results=144990))
    result = await handle_search(
        {"query": "survey", "categories": ["cs.HC"], "max_results": 2}
    )

    content = json.loads(result[0].text)
    assert content["total_results"] == 144990
    assert content["returned"] == 2
    assert len(content["papers"]) == 2


@pytest.mark.asyncio
async def test_total_results_falls_back_to_returned(monkeypatch):
    """B16 (5d): a feed without opensearch:totalResults falls total_results back to
    the number of papers returned."""
    _stub_httpx(monkeypatch, _atom_feed(entries=3, total_results=None))
    result = await handle_search({"query": "test", "max_results": 5})

    content = json.loads(result[0].text)
    assert content["total_results"] == 3
    assert content["returned"] == 3


@pytest.mark.asyncio
async def test_date_and_non_date_share_one_path(monkeypatch):
    """B16 (5e): a plain query and a date-filtered query both route through the raw
    httpx path; the deleted arxiv-package client (get_arxiv_client) is never run."""
    import arxiv_mcp_server.config as config

    sentinel = MagicMock(side_effect=AssertionError("arxiv-package path must not run"))
    monkeypatch.setattr(config, "get_arxiv_client", sentinel)

    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))

    await handle_search({"query": "test", "max_results": 1})
    await handle_search({"query": "test", "date_from": "2020-01-01", "max_results": 1})

    # Both calls hit the same httpx stub (one GET each) — one shared code path.
    assert mock_client.get.await_count == 2
    sentinel.assert_not_called()


def test_validate_categories():
    """Test category validation function."""
    # Valid categories
    assert _validate_categories(["cs.AI", "cs.LG"])
    assert _validate_categories(["math.CO", "physics.gen-ph"])

    # Invalid categories
    assert not _validate_categories(["invalid.category"])
    assert not _validate_categories(["cs.AI", "invalid.test"])


def test_parse_arxiv_atom_response():
    """Test parsing of arXiv Atom XML response (now returns (papers, total))."""
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
        <entry>
            <id>http://arxiv.org/abs/2301.00001v1</id>
            <title>Test Paper Title</title>
            <summary>This is a test abstract.</summary>
            <published>2023-01-01T00:00:00Z</published>
            <author><name>John Doe</name></author>
            <author><name>Jane Smith</name></author>
            <arxiv:primary_category term="cs.AI"/>
            <category term="cs.AI"/>
            <category term="cs.LG"/>
            <link title="pdf" href="http://arxiv.org/pdf/2301.00001v1"/>
        </entry>
    </feed>"""

    results, total = _parse_arxiv_atom_response(sample_xml)
    # Feed carries no opensearch:totalResults → total is None.
    assert total is None
    assert len(results) == 1
    paper = results[0]
    assert paper["id"] == "2301.00001"
    assert paper["title"] == "Test Paper Title"
    assert paper["abstract"] == "[EXTERNAL CONTENT] This is a test abstract."
    assert paper["authors"] == ["John Doe", "Jane Smith"]
    assert "cs.AI" in paper["categories"]
    assert paper["resource_uri"] == "arxiv://2301.00001"


def test_parse_arxiv_atom_response_total_results():
    """The feed-level opensearch:totalResults is parsed into the second element."""
    xml_with_total = _atom_feed(entries=2, total_results=4148)
    results, total = _parse_arxiv_atom_response(xml_with_total)
    assert total == 4148
    assert len(results) == 2


@pytest.mark.asyncio
async def test_raw_arxiv_search_builds_correct_url():
    """Test that raw search builds correct URL with date filters."""
    import httpx

    # Mock the httpx client
    mock_response = MagicMock()
    mock_response.text = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
    </feed>"""
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        await _raw_arxiv_search(
            query="LLM",
            max_results=5,
            date_from="2023-01-01",
            date_to="2023-12-31",
            categories=["cs.AI"],
        )

        # Check that the URL was constructed with unencoded +TO+
        call_args = mock_client.get.call_args
        url = call_args[0][0]
        assert "+TO+" in url  # Critical: must not be encoded as %2B
        assert "submittedDate:" in url
        assert "20230101" in url
        assert "20231231" in url


@pytest.mark.asyncio
async def test_raw_arxiv_search_return_total_shape():
    """return_total=True yields (papers, total); the default stays a bare list so
    existing callers (alerts.py) are unaffected."""
    _mock_response = MagicMock()
    _mock_response.text = _atom_feed(entries=1, total_results=99)
    _mock_response.status_code = 200
    _mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        papers_only = await _raw_arxiv_search(query="x")
        assert isinstance(papers_only, list)
        assert len(papers_only) == 1

        papers, total = await _raw_arxiv_search(query="x", return_total=True)
        assert isinstance(papers, list)
        assert total == 99


@pytest.mark.asyncio
async def test_search_with_invalid_categories():
    """Test search with invalid categories (rejected before any HTTP call)."""
    result = await handle_search(
        {
            "query": "test query",
            "categories": ["invalid.category"],
            "max_results": 1,
        }
    )

    assert "Error: Invalid category" in result[0].text


@pytest.mark.asyncio
async def test_search_empty_query(monkeypatch):
    """Test search with empty query but categories."""
    _stub_httpx(monkeypatch, _atom_feed(entries=1))
    result = await handle_search(
        {"query": "", "categories": ["cs.AI"], "max_results": 1}
    )

    # Should still work with just categories.
    content = json.loads(result[0].text)
    assert "papers" in content


@pytest.mark.asyncio
async def test_search_no_criteria_error():
    """Empty query with no categories/dates → a clear 'no criteria' error."""
    result = await handle_search({"query": "", "max_results": 1})
    assert "Error:" in result[0].text
    assert "No search criteria" in result[0].text


@pytest.mark.asyncio
async def test_search_rate_limit_error_surfaces(monkeypatch):
    """A RuntimeError from the rate-limited GET surfaces as an error message
    instead of crashing the handler."""

    async def _raise_rate_limit(client, url):
        raise RuntimeError(
            "arXiv is rate limiting this IP. Wait at least 60s before retrying."
        )

    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search._rate_limited_get", _raise_rate_limit
    )

    result = await handle_search({"query": "test", "max_results": 1})
    assert "Error:" in result[0].text
    assert "rate limiting" in result[0].text


@pytest.mark.asyncio
async def test_search_max_results_limiting(monkeypatch):
    """max_results is clamped to settings.MAX_RESULTS in the outgoing URL."""
    from arxiv_mcp_server.tools.search import settings

    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))
    result = await handle_search({"query": "test", "max_results": 1000})

    content = json.loads(result[0].text)
    assert "papers" in content
    assert f"max_results={settings.MAX_RESULTS}" in _outgoing_url(mock_client)


@pytest.mark.asyncio
async def test_search_max_results_passed_through(monkeypatch):
    """A requested max_results within the cap is passed through to the arXiv URL."""
    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))
    await handle_search({"query": "test", "max_results": 5})

    assert "max_results=5" in _outgoing_url(mock_client)


@pytest.mark.asyncio
async def test_search_sort_by_relevance(monkeypatch):
    """Test search with relevance sorting (default)."""
    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))
    result = await handle_search({"query": "test", "sort_by": "relevance"})

    content = json.loads(result[0].text)
    assert "papers" in content
    assert "sortBy=relevance" in _outgoing_url(mock_client)


@pytest.mark.asyncio
async def test_search_sort_by_date(monkeypatch):
    """Test search with date sorting."""
    mock_client = _stub_httpx(monkeypatch, _atom_feed(entries=1))
    result = await handle_search({"query": "test", "sort_by": "date"})

    content = json.loads(result[0].text)
    assert "papers" in content
    assert "sortBy=submittedDate" in _outgoing_url(mock_client)


@pytest.mark.asyncio
async def test_search_no_query_optimization(mock_client):
    """Test that queries are not automatically modified."""
    from arxiv_mcp_server.tools.search import _optimize_query

    # Test that complex queries are not mangled
    complex_query = "graph neural networks message passing attention mechanism"
    optimized = _optimize_query(complex_query)
    assert optimized == complex_query

    # Test that field-specific queries are preserved
    field_query = 'ti:"graph neural networks"'
    optimized = _optimize_query(field_query)
    assert optimized == field_query

    # Test that boolean queries are preserved
    bool_query = "machine learning AND deep learning"
    optimized = _optimize_query(bool_query)
    assert optimized == bool_query

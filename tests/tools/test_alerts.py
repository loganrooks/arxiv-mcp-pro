"""Tests for research alert tools."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arxiv_mcp_server.tools import alerts as alerts_module


@pytest.fixture
def alerts_test_env(monkeypatch, temp_storage_path):
    """Configure alerts module to use temporary storage."""
    monkeypatch.setattr(
        alerts_module.settings,
        "_get_storage_path_from_args",
        lambda: Path(temp_storage_path),
    )


def _stub_httpx(monkeypatch, xml_text):
    """Patch ``httpx.AsyncClient`` so the REAL ``_raw_arxiv_search`` (reached via
    check_alerts) sees a canned Atom response instead of the network. Returns the
    mock client so a test can assert whether a GET was actually attempted."""
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


_ERROR_FEED = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
        <opensearch:totalResults>1</opensearch:totalResults>
        <entry>
            <id>https://arxiv.org/api/errors</id>
            <title>Error</title>
            <summary>Invalid query string: '( )'</summary>
        </entry>
    </feed>"""


@pytest.mark.asyncio
async def test_watch_topic_persists_topic(alerts_test_env):
    """watch_topic should persist watched topic payloads."""
    response = await alerts_module.handle_watch_topic(
        {"topic": "multi-agent systems", "categories": ["cs.AI"]}
    )

    assert len(response) >= 1
    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert "topic" in payload
    assert isinstance(payload["topic"], dict)
    assert payload["topic"]["topic"] == "multi-agent systems"


@pytest.mark.asyncio
async def test_check_alerts_returns_new_papers(monkeypatch, alerts_test_env):
    """check_alerts should return new papers and update last_checked."""

    async def _mock_raw_search(**kwargs):
        return [
            {
                "id": "2501.00001",
                "title": "New Paper",
                "authors": ["A"],
                "abstract": "x",
                "categories": ["cs.AI"],
                "published": "2025-01-01T00:00:00Z",
                "url": "https://arxiv.org/pdf/2501.00001",
                "resource_uri": "arxiv://2501.00001",
            }
        ]

    monkeypatch.setattr(alerts_module, "_raw_arxiv_search", _mock_raw_search)

    await alerts_module.handle_watch_topic({"topic": "agents"})
    response = await alerts_module.handle_check_alerts({})

    assert len(response) >= 1
    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert payload["checked_topics"] == 1
    assert "alerts" in payload
    assert len(payload["alerts"]) >= 1
    assert "new_paper_count" in payload["alerts"][0]
    assert payload["alerts"][0]["new_paper_count"] == 1


def test_save_watches_atomic_roundtrip_no_temp_leftovers(alerts_test_env):
    """_save_watches persists atomically: content round-trips and no temp files remain.

    Regression guard for B5 — the write goes through a temp file + os.replace, so an
    interrupted write can never truncate the live file, and a successful write must
    not leave a stray .tmp behind.
    """
    payload = {"topics": [{"topic": "atomic-write", "categories": ["cs.DC"]}]}
    alerts_module._save_watches(payload)

    # Content round-trips through the loader.
    assert alerts_module._load_watches() == payload

    # The atomic rename leaves no stray temp files in the storage dir.
    storage_dir = alerts_module._watch_file_path().parent
    leftovers = [p.name for p in storage_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"atomic write left temp files: {leftovers}"


@pytest.mark.asyncio
async def test_check_alerts_handles_partial_paper_fields(monkeypatch, alerts_test_env):
    """check_alerts must not raise KeyError when a paper entry is missing optional fields."""

    async def _mock_partial(**kwargs):
        return [
            {
                "id": "2501.00002",
                "title": "Sparse Paper",
                # "authors", "abstract", "url", "resource_uri" intentionally absent
                "categories": ["cs.AI"],
                "published": "2025-01-01T00:00:00Z",
            }
        ]

    monkeypatch.setattr(alerts_module, "_raw_arxiv_search", _mock_partial)

    await alerts_module.handle_watch_topic({"topic": "agents"})
    response = await alerts_module.handle_check_alerts({})

    assert len(response) >= 1
    payload = json.loads(response[0].text)
    assert "status" in payload


@pytest.mark.asyncio
async def test_check_alerts_isolates_per_topic_failure(monkeypatch, alerts_test_env):
    """One topic's search failure must not abort the batch or roll back the
    last_checked advance of topics that already succeeded (B6 / F7 regression).

    Before the fix, the whole loop ran under one try/except with a single
    post-loop save, so any topic's failure skipped _save_watches entirely and
    every topic re-reported its papers on the next run.
    """

    async def _mock_raw_search(**kwargs):
        if kwargs.get("query") == "flaky":
            raise RuntimeError("arXiv 429 rate limit")
        return [
            {
                "id": "2501.00010",
                "title": "Good Paper",
                "authors": ["A"],
                "abstract": "x",
                "categories": ["cs.AI"],
                "published": "2025-01-01T00:00:00Z",
                "url": "https://arxiv.org/pdf/2501.00010",
                "resource_uri": "arxiv://2501.00010",
            }
        ]

    monkeypatch.setattr(alerts_module, "_raw_arxiv_search", _mock_raw_search)

    # Register the FAILING topic first: the loop must continue PAST an early
    # failure to reach the later success. This is the discriminating F7 case —
    # the old single-try/post-loop-save code aborted the whole batch on the
    # first exception, losing every topic's advance.
    await alerts_module.handle_watch_topic({"topic": "flaky"})
    await alerts_module.handle_watch_topic({"topic": "good"})

    response = await alerts_module.handle_check_alerts({})
    payload = json.loads(response[0].text)

    # The batch completes (not an error envelope) despite one topic failing.
    assert payload["status"] == "success"
    alerts_by_topic = {a["topic"]: a for a in payload["alerts"]}
    assert alerts_by_topic["good"]["new_paper_count"] == 1
    assert "error" not in alerts_by_topic["good"]
    assert "error" in alerts_by_topic["flaky"]
    assert alerts_by_topic["flaky"]["new_paper_count"] == 0

    # The successful topic's last_checked was persisted; the failed one's was
    # left untouched so it retries (and does not silently skip) next run.
    saved = {t["topic"]: t for t in alerts_module._load_watches()["topics"]}
    assert saved["good"].get("last_checked") is not None
    assert saved["flaky"].get("last_checked") is None


@pytest.mark.asyncio
async def test_check_alerts_error_feed_is_per_topic_error(monkeypatch, alerts_test_env):
    """B16 P2-1 (b): an arXiv API-error feed (HTTP-200 /api/errors) for a topic
    surfaces as that topic's `error` field, and the batch still completes — the
    real _raw_arxiv_search + parse run against a stubbed httpx client."""
    await alerts_module.handle_watch_topic({"topic": "( )"})

    _stub_httpx(monkeypatch, _ERROR_FEED)
    response = await alerts_module.handle_check_alerts({})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    alert = payload["alerts"][0]
    assert "error" in alert
    assert "arXiv API error" in alert["error"]
    assert "Invalid query string" in alert["error"]
    assert alert["new_paper_count"] == 0

    # Error feed did not advance the topic's last_checked (it retries next run).
    saved = alerts_module._load_watches()["topics"][0]
    assert saved.get("last_checked") is None


@pytest.mark.asyncio
async def test_check_alerts_stored_bad_category_no_network(
    monkeypatch, alerts_test_env
):
    """B16 P2-2 (d): a malicious category already in storage (watch_topic would now
    reject it, but an old/hand-edited file could carry one) is caught by the
    _raw_arxiv_search grammar backstop → per-topic error, and NO network GET is
    attempted for that topic."""
    alerts_module._save_watches(
        {
            "topics": [
                {
                    "topic": "agents",
                    "categories": ["cs.AI OR all:*"],
                    "max_results": 5,
                    "last_checked": None,
                }
            ]
        }
    )

    mock_client = _stub_httpx(monkeypatch, "<feed/>")
    response = await alerts_module.handle_check_alerts({})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    alert = payload["alerts"][0]
    assert "error" in alert
    assert "Invalid category" in alert["error"]
    assert alert["new_paper_count"] == 0
    # The backstop raised before any request was issued.
    assert mock_client.get.await_count == 0


@pytest.mark.asyncio
async def test_watch_topic_rejects_bad_category(alerts_test_env):
    """B16 P2-2 (e): watch_topic rejects a malformed/injection category at SAVE
    time, so a bad value is never persisted."""
    response = await alerts_module.handle_watch_topic(
        {"topic": "agents", "categories": ["cs.AI&max_results=1000"]}
    )

    text = response[0].text
    assert "Error" in text
    assert "Invalid category" in text
    # Nothing was persisted.
    assert alerts_module._load_watches()["topics"] == []


@pytest.mark.asyncio
async def test_watch_topic_accepts_valid_category(alerts_test_env):
    """Sibling to (e): a well-formed category still saves normally."""
    response = await alerts_module.handle_watch_topic(
        {"topic": "agents", "categories": ["cs.AI", "cs.LG"]}
    )
    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert payload["topic"]["categories"] == ["cs.AI", "cs.LG"]

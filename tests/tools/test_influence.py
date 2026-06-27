"""Tests for the library_influence (C5) tool.

All network and filesystem I/O is injected/monkeypatched: these tests make NO
real Semantic Scholar or arXiv calls. networkx is required (the `influence` /
`test` extras provide it); the pure ranking core is tested directly with
hand-built fixtures.
"""

import json

import pytest

from arxiv_mcp_server.tools import influence
from arxiv_mcp_server.tools.influence import (
    build_influence_rows,
    handle_library_influence,
    _normalize_arxiv_id,
    _text_has_code,
    _build_has_code_map,
)

nx = pytest.importorskip("networkx")


# ---------------------------------------------------------------------------
# Fixtures / stubs (no network, no real PaperManager)
# ---------------------------------------------------------------------------


class StubManager:
    """In-memory PaperManager double: serves list_papers + get_paper_content."""

    def __init__(self, papers, content=None, raise_for=None):
        self._papers = list(papers)
        self._content = content or {}
        self._raise_for = set(raise_for or [])

    async def list_papers(self):
        return list(self._papers)

    async def get_paper_content(self, arxiv_id):
        if arxiv_id in self._raise_for:
            raise ValueError(f"Paper {arxiv_id} not found in storage")
        return self._content.get(arxiv_id, "")


def _three_paper_fixture():
    """A -> B, A -> C, B -> C induced graph with a built-in disagreement.

    In-degrees: C=2, B=1, A=0  => local pagerank C > B > A.
    Global citations: A=1000, B=50, C=2 => global rank A > B > C.
    So C is the corpus hidden gem (top locally, bottom globally).
    """
    s2_data = {
        "A": {
            "title": "Paper A",
            "citationCount": 1000,
            "influentialCitationCount": 100,
            "authors": [{"hIndex": 30}, {"hIndex": 5}],
            "references": [
                {"externalIds": {"ArXiv": "B"}},
                {"externalIds": {"ArXiv": "C"}},
                # A non-local reference must NOT create an edge (v1: no expansion).
                {"externalIds": {"ArXiv": "Z-not-local"}},
                # A reference with no ArXiv id is skipped.
                {"externalIds": {"DOI": "10.x/y"}},
            ],
        },
        "B": {
            "title": "Paper B",
            "citationCount": 50,
            "influentialCitationCount": 10,
            "authors": [{"hIndex": 12}],
            "references": [{"externalIds": {"ArXiv": "C"}}],
        },
        "C": {
            "title": "Paper C",
            "citationCount": 2,
            "influentialCitationCount": 0,
            "authors": [{"hIndex": 3}],
            "references": [],
        },
    }
    has_code_map = {"A": True, "B": False, "C": False}
    return ["A", "B", "C"], s2_data, has_code_map


# ---------------------------------------------------------------------------
# Pure ranking core
# ---------------------------------------------------------------------------


def test_build_rows_ranking_and_delta():
    """Induced edges, in-degrees, pagerank ordering and the disagreement delta."""
    library_ids, s2_data, has_code_map = _three_paper_fixture()
    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=20)

    assert result["library_size"] == 3
    # Only B and C are local references of A (Z-not-local is dropped); plus B->C.
    assert result["graph"] == {"nodes": 3, "edges": 3}

    rows = {row["arxiv_id"]: row for row in result["papers"]}

    # In-degrees in the induced subgraph.
    assert rows["C"]["local_citations"] == 2
    assert rows["B"]["local_citations"] == 1
    assert rows["A"]["local_citations"] == 0

    # PageRank ordering: C > B > A.
    assert rows["C"]["pagerank"] > rows["B"]["pagerank"] > rows["A"]["pagerank"]
    # Sorted by pagerank desc -> C is first.
    assert result["papers"][0]["arxiv_id"] == "C"

    # Disagreement delta = global_rank - pagerank_rank (rank 1 = best).
    # pagerank ranks: C=1, B=2, A=3 ; global ranks: A=1, B=2, C=3.
    # C (local_citations 2) and B (1) have in-library citations -> real deltas.
    assert rows["C"]["local_vs_global_delta"] == 2  # 3 - 1 : hidden gem (positive)
    assert rows["B"]["local_vs_global_delta"] == 0  # 2 - 2
    # A has 0 in-library citations (PageRank dangling floor) -> null delta (FIX 1),
    # not the alphabetical-noise value its ordinal ranks would otherwise produce.
    assert rows["A"]["local_citations"] == 0
    assert rows["A"]["local_vs_global_delta"] is None

    # Pass-through signal columns.
    assert rows["A"]["global_citations"] == 1000
    assert rows["A"]["influential_citations"] == 100
    assert rows["A"]["max_author_hindex"] == 30  # max(30, 5)
    assert rows["A"]["has_code"] is True
    assert rows["C"]["has_code"] is False
    assert rows["C"]["title"] == "Paper C"


def test_build_rows_output_keys_exact():
    """Each row carries exactly the documented column set."""
    library_ids, s2_data, has_code_map = _three_paper_fixture()
    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=20)
    expected_keys = {
        "arxiv_id",
        "title",
        "pagerank",
        "local_citations",
        "global_citations",
        "influential_citations",
        "max_author_hindex",
        "has_code",
        "local_vs_global_delta",
    }
    for row in result["papers"]:
        assert set(row.keys()) == expected_keys
    # Top-level shape.
    assert set(result.keys()) == {"library_size", "graph", "papers", "notes"}


def test_build_rows_empty_library():
    """No papers -> empty graph, no rows, no crash."""
    result = build_influence_rows([], {}, {}, top_k=20)
    assert result["library_size"] == 0
    assert result["graph"] == {"nodes": 0, "edges": 0}
    assert result["papers"] == []
    assert result["notes"] == []


def test_build_rows_edgeless_uniform_pagerank():
    """Nodes but no induced edges -> uniform pagerank, no crash."""
    library_ids = ["A", "B", "C", "D"]
    # No references at all => zero edges.
    s2_data = {
        aid: {"title": aid, "citationCount": 1, "references": []} for aid in library_ids
    }
    has_code_map = {aid: False for aid in library_ids}

    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=20)
    assert result["graph"]["edges"] == 0
    for row in result["papers"]:
        # Uniform 1/N = 0.25, rounded to 6 dp.
        assert row["pagerank"] == 0.25
        assert row["local_citations"] == 0
        # Every paper is at the dangling floor -> null delta (FIX 1).
        assert row["local_vs_global_delta"] is None
    # ...and a note explains why.
    assert any("local_citations == 0" in note for note in result["notes"])


def test_build_rows_paper_missing_from_s2():
    """A paper absent from S2 stays a node with null signals and is noted."""
    library_ids = ["A", "B"]
    s2_data = {
        "A": {
            "title": "Paper A",
            "citationCount": 5,
            "influentialCitationCount": 1,
            "authors": [{"hIndex": 7}],
            "references": [{"externalIds": {"ArXiv": "B"}}],
        },
        "B": None,  # absent from Semantic Scholar
    }
    has_code_map = {"A": False, "B": False}
    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=20)

    rows = {row["arxiv_id"]: row for row in result["papers"]}
    assert rows["B"]["global_citations"] is None
    assert rows["B"]["influential_citations"] is None
    assert rows["B"]["max_author_hindex"] is None
    assert rows["B"]["title"] == ""
    # B is still a node and receives the induced edge A->B.
    assert rows["B"]["local_citations"] == 1
    # Null global count -> null delta (not an inflated positive).
    assert rows["B"]["local_vs_global_delta"] is None
    # A note flags the missing paper.
    assert any("Semantic Scholar" in note for note in result["notes"])


def test_build_rows_null_citation_null_delta():
    """A paper PRESENT on S2 but with a null citationCount gets a null delta.

    Mutual citation (A<->B) gives BOTH papers an in-library citation, so this
    isolates the null-COUNT rule from the zero-in-degree rule (FIX 1): B's delta
    is null purely because its global citationCount is null, not because it is
    uncited locally. A (which has a count and is cited) gets a real int delta."""
    library_ids = ["A", "B"]
    s2_data = {
        "A": {
            "title": "Paper A",
            "citationCount": 100,
            "references": [{"externalIds": {"ArXiv": "B"}}],
        },
        # Present record, cited locally (A->B... and B->A below), but no count.
        "B": {
            "title": "Paper B",
            "citationCount": None,
            "references": [{"externalIds": {"ArXiv": "A"}}],
        },
    }
    has_code_map = {"A": False, "B": False}
    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=20)

    rows = {row["arxiv_id"]: row for row in result["papers"]}
    # Both papers are cited within the library (mutual citation).
    assert rows["A"]["local_citations"] == 1
    assert rows["B"]["local_citations"] == 1
    # B: present record, null count -> null delta (the null-COUNT rule alone).
    assert rows["B"]["global_citations"] is None
    assert rows["B"]["local_vs_global_delta"] is None
    # A: has a count AND an in-library citation -> a real integer delta.
    assert isinstance(rows["A"]["local_vs_global_delta"], int)


def test_build_rows_versioned_reference_builds_edge():
    """A VERSIONED S2 reference id still matches a canonical library node (FIX 2).

    S2 may return `references.externalIds.ArXiv` with a version suffix; without
    normalizing the reference side of the join the induced edge is silently
    dropped (defeating the module's version-robustness on one side)."""
    library_ids = ["2401.0001", "2401.0002"]  # canonical (unversioned) nodes
    s2_data = {
        "2401.0001": {
            "title": "One",
            "citationCount": 5,
            # The reference id is VERSIONED but must match node "2401.0002".
            "references": [{"externalIds": {"ArXiv": "2401.0002v1"}}],
        },
        "2401.0002": {"title": "Two", "citationCount": 2, "references": []},
    }
    has_code_map = {"2401.0001": False, "2401.0002": False}
    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=20)

    assert result["graph"]["edges"] == 1
    rows = {row["arxiv_id"]: row for row in result["papers"]}
    assert rows["2401.0002"]["local_citations"] == 1
    # 0002 has an in-library citation and a global count -> a real int delta.
    assert isinstance(rows["2401.0002"]["local_vs_global_delta"], int)


def test_build_rows_versioned_self_reference_no_loop():
    """A versioned self-reference must not create a self-loop (canonical guard, FIX 2)."""
    library_ids = ["2401.0001"]
    s2_data = {
        "2401.0001": {
            "title": "One",
            "citationCount": 1,
            # A versioned reference to ITSELF -> canonical self-loop, suppressed.
            "references": [{"externalIds": {"ArXiv": "2401.0001v2"}}],
        }
    }
    result = build_influence_rows(library_ids, s2_data, {"2401.0001": False}, top_k=20)
    assert result["graph"]["edges"] == 0
    assert result["graph"]["nodes"] == 1


def test_build_rows_top_k_truncation():
    """top_k truncates to the top-ranked rows (by pagerank desc)."""
    library_ids, s2_data, has_code_map = _three_paper_fixture()
    result = build_influence_rows(library_ids, s2_data, has_code_map, top_k=1)
    assert len(result["papers"]) == 1
    # Highest pagerank (C) survives truncation.
    assert result["papers"][0]["arxiv_id"] == "C"
    # graph stats reflect the FULL induced graph, not the truncated rows.
    assert result["graph"]["nodes"] == 3


# ---------------------------------------------------------------------------
# has_code scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("See our repo at https://GitHub.com/foo/bar", True),
        ("Hosted on gitlab.com/foo", True),
        ("indexed on paperswithcode.com", True),
        ("Our code will be released soon.", True),
        ("The implementation is available upon request.", True),
        ("Code available at the project page.", True),
        ("No links or repositories here, just prose about results.", False),
        ("", False),
    ],
)
def test_text_has_code(text, expected):
    assert _text_has_code(text) is expected


# ---------------------------------------------------------------------------
# arXiv version normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2401.12345v2", "2401.12345"),  # new-scheme versioned
        ("2401.12345", "2401.12345"),  # already bare
        ("2401.12345v12", "2401.12345"),  # multi-digit version
        ("hep-th/9901001v3", "hep-th/9901001"),  # old-scheme versioned
        ("hep-th/9901001", "hep-th/9901001"),  # old-scheme bare
    ],
)
def test_normalize_arxiv_id(raw, expected):
    assert _normalize_arxiv_id(raw) == expected


@pytest.mark.asyncio
async def test_build_has_code_map_reads_by_stem(tmp_path):
    """has_code keys by the canonical id but reads the file by the local stem."""
    (tmp_path / "2401.0001v2.md").write_text("see https://github.com/org/repo")

    class FileManager:
        async def get_paper_content(self, stem):
            path = tmp_path / f"{stem}.md"
            if not path.exists():
                raise ValueError("missing")
            return path.read_text()

    # Canonical id "2401.0001" -> versioned stem "2401.0001v2".
    has_code = await _build_has_code_map(
        ["2401.0001"], FileManager(), id_to_stem={"2401.0001": "2401.0001v2"}
    )
    assert has_code == {"2401.0001": True}


@pytest.mark.asyncio
async def test_build_has_code_map_reads_markdown(tmp_path):
    """_build_has_code_map scans real markdown and is resilient to read errors."""
    # Write temp markdown files and serve them through a stub manager.
    code_md = tmp_path / "A.md"
    code_md.write_text("# Paper A\n\nCode: https://github.com/org/repo\n")
    plain_md = tmp_path / "B.md"
    plain_md.write_text("# Paper B\n\nA purely theoretical contribution.\n")

    class FileManager:
        async def get_paper_content(self, arxiv_id):
            path = tmp_path / f"{arxiv_id}.md"
            if not path.exists():
                raise ValueError("missing")
            return path.read_text()

    has_code = await _build_has_code_map(["A", "B", "C-missing"], FileManager())
    assert has_code == {"A": True, "B": False, "C-missing": False}


# ---------------------------------------------------------------------------
# Handler wiring (PaperManager + S2 fetch injected; no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_library_influence_happy_path(monkeypatch):
    """Full handler: stub PaperManager + injected S2 batch, success envelope."""
    library_ids, s2_data, _ = _three_paper_fixture()
    content = {
        "A": "code at https://github.com/org/a",
        "B": "no code here",
        "C": "no code here",
    }
    monkeypatch.setattr(
        influence, "_get_paper_manager", lambda: StubManager(library_ids, content)
    )

    async def fake_fetch(ids):
        return {aid: s2_data.get(aid) for aid in ids}

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({})
    payload = json.loads(response[0].text)

    assert payload["status"] == "success"
    assert payload["library_size"] == 3
    assert payload["graph"] == {"nodes": 3, "edges": 3}
    rows = {row["arxiv_id"]: row for row in payload["papers"]}
    assert rows["A"]["has_code"] is True
    assert rows["B"]["has_code"] is False
    assert payload["papers"][0]["arxiv_id"] == "C"


@pytest.mark.asyncio
async def test_handle_library_influence_paper_ids_filter(monkeypatch):
    """`paper_ids` restricts the node set to the requested subset."""
    library_ids, s2_data, _ = _three_paper_fixture()
    monkeypatch.setattr(
        influence, "_get_paper_manager", lambda: StubManager(library_ids)
    )

    captured = {}

    async def fake_fetch(ids):
        captured["ids"] = list(ids)
        return {aid: s2_data.get(aid) for aid in ids}

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({"paper_ids": ["A", "C"]})
    payload = json.loads(response[0].text)

    assert payload["library_size"] == 2
    assert sorted(captured["ids"]) == ["A", "C"]
    returned_ids = {row["arxiv_id"] for row in payload["papers"]}
    assert returned_ids == {"A", "C"}


@pytest.mark.asyncio
async def test_handle_library_influence_compact(monkeypatch):
    """compact=True -> minified JSON (no newlines), still valid + correct status."""
    library_ids, s2_data, _ = _three_paper_fixture()
    monkeypatch.setattr(
        influence, "_get_paper_manager", lambda: StubManager(library_ids)
    )

    async def fake_fetch(ids):
        return {aid: s2_data.get(aid) for aid in ids}

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({"compact": True})
    text = response[0].text
    assert "\n" not in text  # minified
    payload = json.loads(text)
    assert payload["status"] == "success"


@pytest.mark.asyncio
async def test_handle_library_influence_top_k(monkeypatch):
    """Handler honors top_k truncation end-to-end."""
    library_ids, s2_data, _ = _three_paper_fixture()
    monkeypatch.setattr(
        influence, "_get_paper_manager", lambda: StubManager(library_ids)
    )

    async def fake_fetch(ids):
        return {aid: s2_data.get(aid) for aid in ids}

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({"top_k": 2})
    payload = json.loads(response[0].text)
    assert len(payload["papers"]) == 2
    # graph reflects the full library even when rows are truncated.
    assert payload["graph"]["nodes"] == 3


@pytest.mark.asyncio
async def test_handle_library_influence_networkx_absent(monkeypatch):
    """When networkx is missing, the handler returns the dependency error envelope."""
    monkeypatch.setattr(influence, "nx", None)
    response = await handle_library_influence({})
    payload = json.loads(response[0].text)
    assert payload["status"] == "error"
    assert "influence" in payload["message"]
    assert "pip install" in payload["message"]


@pytest.mark.asyncio
async def test_handle_library_influence_scipy_absent(monkeypatch):
    """networkx-present/scipy-absent yields the SAME helpful hint (FIX 4), not a
    raw 'No module named scipy' (which is what nx.pagerank would raise)."""
    monkeypatch.setattr(influence, "_scipy", None)
    response = await handle_library_influence({})
    payload = json.loads(response[0].text)
    assert payload["status"] == "error"
    assert "influence" in payload["message"]
    assert "pip install" in payload["message"]


@pytest.mark.asyncio
async def test_handle_empty_paper_ids_empty_panel(monkeypatch):
    """An explicit `paper_ids: []` restricts to nothing -> empty success panel (FIX 8)."""
    library_ids, s2_data, _ = _three_paper_fixture()
    monkeypatch.setattr(
        influence, "_get_paper_manager", lambda: StubManager(library_ids)
    )

    async def fake_fetch(ids):
        return {aid: s2_data.get(aid) for aid in ids}

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({"paper_ids": []})
    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert payload["library_size"] == 0
    assert payload["papers"] == []
    assert payload["graph"] == {"nodes": 0, "edges": 0}


@pytest.mark.asyncio
async def test_handle_library_influence_error_envelope(monkeypatch):
    """An unexpected failure surfaces the JSON error envelope, not a raw raise."""

    class BoomManager:
        async def list_papers(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(influence, "_get_paper_manager", lambda: BoomManager())
    response = await handle_library_influence({})
    payload = json.loads(response[0].text)
    assert payload["status"] == "error"
    assert "boom" in payload["message"]


# ---------------------------------------------------------------------------
# arXiv version normalization through the handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_version_normalization_builds_edges(monkeypatch):
    """Versioned local stems query S2 unversioned, match references, get signals.

    Local stems are versioned (`...v2`/`...v1`) but S2 and its reference ids are
    unversioned. The canonical ids must (1) be what S2 is queried with, (2)
    match the induced edge, (3) carry non-null S2 signals, and (4) appear as the
    output arxiv_id."""
    stems = ["2401.0001v2", "2401.0002v1"]
    monkeypatch.setattr(influence, "_get_paper_manager", lambda: StubManager(stems))

    captured = {}

    async def fake_fetch(ids):
        captured["ids"] = list(ids)
        # Keyed by canonical (unversioned) ids; 0001 references 0002.
        return {
            "2401.0001": {
                "title": "Paper One",
                "citationCount": 10,
                "references": [{"externalIds": {"ArXiv": "2401.0002"}}],
            },
            "2401.0002": {
                "title": "Paper Two",
                "citationCount": 3,
                "references": [],
            },
        }

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({})
    payload = json.loads(response[0].text)

    # S2 was queried with UNVERSIONED ids.
    assert sorted(captured["ids"]) == ["2401.0001", "2401.0002"]
    # The induced edge was built across canonical ids (0001 -> 0002).
    assert payload["graph"]["edges"] == 1
    rows = {row["arxiv_id"]: row for row in payload["papers"]}
    # Output uses canonical ids and carries non-null S2 signals.
    assert set(rows) == {"2401.0001", "2401.0002"}
    assert rows["2401.0002"]["local_citations"] == 1
    assert rows["2401.0001"]["global_citations"] == 10
    # Neither paper is reported missing (the versioned stems matched S2).
    assert not any("not found on Semantic Scholar" in n for n in payload["notes"])


@pytest.mark.asyncio
async def test_handle_paper_ids_matches_across_versions(monkeypatch):
    """An unversioned `paper_ids` value matches a versioned local stem."""
    stems = ["2401.0001v2", "2401.0002v1"]
    monkeypatch.setattr(influence, "_get_paper_manager", lambda: StubManager(stems))

    captured = {}

    async def fake_fetch(ids):
        captured["ids"] = list(ids)
        return {
            aid: {"title": aid, "citationCount": 1, "references": []} for aid in ids
        }

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    # User passes the UNVERSIONED id; it must match the `...v2` stem.
    response = await handle_library_influence({"paper_ids": ["2401.0001"]})
    payload = json.loads(response[0].text)

    assert captured["ids"] == ["2401.0001"]
    assert payload["library_size"] == 1
    assert payload["papers"][0]["arxiv_id"] == "2401.0001"


@pytest.mark.asyncio
async def test_handle_duplicate_versions_collapsed(monkeypatch):
    """Two stems normalizing to the same canon collapse to one node, with a note."""
    stems = ["2401.0001v1", "2401.0001v2"]  # both -> 2401.0001
    monkeypatch.setattr(influence, "_get_paper_manager", lambda: StubManager(stems))

    async def fake_fetch(ids):
        return {
            aid: {"title": aid, "citationCount": 1, "references": []} for aid in ids
        }

    monkeypatch.setattr(influence, "_fetch_s2_batch", fake_fetch)

    response = await handle_library_influence({})
    payload = json.loads(response[0].text)

    assert payload["library_size"] == 1
    assert payload["graph"]["nodes"] == 1
    assert payload["papers"][0]["arxiv_id"] == "2401.0001"
    assert any("collapsed" in note.lower() for note in payload["notes"])


# ---------------------------------------------------------------------------
# S2 batch fetch (httpx mocked -> no real network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_s2_batch_maps_entries(monkeypatch):
    """_fetch_s2_batch POSTs once, aligns the list response, maps nulls to None."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    # S2 returns a list aligned with input ids; a null entry = no record.
    mock_response.json.return_value = [
        {"title": "Paper A", "citationCount": 5},
        None,
    ]

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        result = await influence._fetch_s2_batch(["A", "B"])

    assert result["A"] == {"title": "Paper A", "citationCount": 5}
    assert result["B"] is None
    # One POST to the batch endpoint, with ARXIV:-prefixed ids in the body.
    assert mock_client.post.await_count == 1
    body = mock_client.post.call_args.kwargs["json"]
    assert body == {"ids": ["ARXIV:A", "ARXIV:B"]}


@pytest.mark.asyncio
async def test_fetch_s2_batch_empty_no_request():
    """An empty library issues no HTTP request."""
    from unittest.mock import patch

    with patch("httpx.AsyncClient") as mock_client_class:
        result = await influence._fetch_s2_batch([])

    assert result == {}
    mock_client_class.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_s2_batch_short_payload_trailing_none():
    """A batch response with FEWER entries than ids leaves trailing ids None."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    # Two ids requested, only ONE entry returned.
    mock_response.json.return_value = [{"title": "Paper A", "citationCount": 1}]

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        result = await influence._fetch_s2_batch(["A", "B"])

    assert result["A"] == {"title": "Paper A", "citationCount": 1}
    # The unmatched trailing id stays None rather than crashing.
    assert result["B"] is None


@pytest.mark.asyncio
async def test_fetch_s2_batch_non_list_payload_warns(caplog):
    """A non-list payload (e.g. an error dict) is treated as empty, with a warning
    (FIX 9) — never zipped over a dict's keys."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()
    # A dict, not the expected list.
    mock_response.json.return_value = {"error": "bad request"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        with caplog.at_level("WARNING"):
            result = await influence._fetch_s2_batch(["A", "B"])

    assert result == {"A": None, "B": None}
    assert "non-list" in caplog.text


@pytest.mark.asyncio
async def test_s2_post_retries_on_429(monkeypatch):
    """_s2_post retries a transient 429 and returns the subsequent 200."""
    from unittest.mock import AsyncMock, MagicMock

    # Mock out the backoff sleep so the retry is instant.
    monkeypatch.setattr(influence.asyncio, "sleep", AsyncMock())

    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {}

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.headers = {}

    client = AsyncMock()
    client.post = AsyncMock(side_effect=[rate_limited, ok_response])

    response = await influence._s2_post(
        client, "https://s2/batch", {"ids": ["ARXIV:A"]}
    )

    assert response is ok_response
    # First POST hit 429, second POST returned 200.
    assert client.post.await_count == 2

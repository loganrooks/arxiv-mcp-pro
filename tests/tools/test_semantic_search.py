"""Tests for semantic search and reindex tools."""

import json
from pathlib import Path

import pytest

from arxiv_mcp_server.tools import semantic_search as semantic_module

np = pytest.importorskip("numpy")


class DummyModel:
    """Deterministic embedding model for tests."""

    def encode(self, text, convert_to_numpy=True, normalize_embeddings=True):
        vector = np.array(
            [
                float("transformer" in text.lower()),
                float("vision" in text.lower()),
                float("graph" in text.lower()),
            ],
            dtype=np.float32,
        )
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return vector


@pytest.fixture
def semantic_test_env(monkeypatch, temp_storage_path):
    """Configure semantic search module to use a temporary index and dummy model."""
    monkeypatch.setattr(
        semantic_module.settings,
        "_get_storage_path_from_args",
        lambda: Path(temp_storage_path),
    )
    monkeypatch.setattr(semantic_module, "SentenceTransformer", object)
    monkeypatch.setattr(semantic_module, "_get_model", lambda: DummyModel())
    semantic_module._model = None


@pytest.mark.asyncio
async def test_semantic_search_free_text(semantic_test_env):
    """Semantic text query should rank closest abstract first."""
    semantic_module._upsert_index_record(
        paper_id="2401.00001",
        title="Vision Transformers",
        abstract="transformer model for vision",
        authors=["Author 1"],
        categories=["cs.CV"],
    )
    semantic_module._upsert_index_record(
        paper_id="2401.00002",
        title="Graph Methods",
        abstract="graph neural network approach",
        authors=["Author 2"],
        categories=["cs.LG"],
    )

    response = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 2}
    )

    payload = json.loads(response[0].text)
    assert payload["total_results"] == 2
    assert payload["papers"][0]["id"] == "2401.00001"


@pytest.mark.asyncio
async def test_semantic_search_by_paper_id(semantic_test_env):
    """similar-to-paper mode excludes the source paper from results."""
    semantic_module._upsert_index_record(
        paper_id="2402.00001",
        title="Transformer Baselines",
        abstract="transformer pretraining method",
        authors=["Author 1"],
        categories=["cs.LG"],
    )
    semantic_module._upsert_index_record(
        paper_id="2402.00002",
        title="Vision Transformer Variant",
        abstract="vision transformer architecture",
        authors=["Author 2"],
        categories=["cs.CV"],
    )

    response = await semantic_module.handle_semantic_search(
        {"paper_id": "2402.00001", "max_results": 3}
    )

    payload = json.loads(response[0].text)
    assert payload["mode"] == "similar_to_paper"
    assert all(p["id"] != "2402.00001" for p in payload["papers"])


def _index_three_papers():
    """Index three papers with deterministic, distinct DummyModel rankings.

    Against the query "vision transformer" (DummyModel vector ~ [1, 1, 0]):
      - 2403.00001 "transformer model for vision" -> [1, 1, 0] -> rank 1
      - 2403.00002 "vision graph methods"         -> [0, 1, 1] -> rank 2
      - 2403.00003 "graph neural network"         -> [0, 0, 1] -> rank 3
    """
    semantic_module._upsert_index_record(
        paper_id="2403.00001",
        title="Vision Transformers",
        abstract="transformer model for vision",
        authors=["Author 1"],
        categories=["cs.CV"],
    )
    semantic_module._upsert_index_record(
        paper_id="2403.00002",
        title="Vision Graphs",
        abstract="vision graph methods",
        authors=["Author 2"],
        categories=["cs.LG"],
    )
    semantic_module._upsert_index_record(
        paper_id="2403.00003",
        title="Graph Networks",
        abstract="graph neural network",
        authors=["Author 3"],
        categories=["cs.LG"],
    )


@pytest.mark.asyncio
async def test_semantic_search_compact_drops_abstract(semantic_test_env):
    """compact=True omits the abstract key and adds the pagination metadata."""
    _index_three_papers()

    response = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 3, "compact": True}
    )

    payload = json.loads(response[0].text)
    assert payload["total_results"] == 3
    for paper in payload["papers"]:
        assert "abstract" not in paper
        assert "id" in paper
        assert "title" in paper
        assert "score" in paper
    # Paginated mode metadata is present.
    assert payload["offset"] == 0
    assert payload["total_available"] == 3
    # Last page (3 of 3 returned) -> no further page.
    assert payload["next_offset"] is None


@pytest.mark.asyncio
async def test_semantic_search_offset_pages(semantic_test_env):
    """offset=1 returns the 2nd-ranked paper; next_offset advances correctly."""
    _index_three_papers()

    full = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 3}
    )
    full_payload = json.loads(full[0].text)
    second_ranked_id = full_payload["papers"][1]["id"]

    page = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 1, "offset": 1}
    )
    page_payload = json.loads(page[0].text)

    assert page_payload["total_results"] == 1
    assert page_payload["papers"][0]["id"] == second_ranked_id
    assert page_payload["offset"] == 1
    assert page_payload["total_available"] == 3
    # offset(1) + returned(1) = 2 < total_available(3) -> next page at 2.
    assert page_payload["next_offset"] == 2


@pytest.mark.asyncio
async def test_semantic_search_default_output_unchanged(semantic_test_env):
    """No offset/compact: abstracts present, no pagination metadata (legacy)."""
    _index_three_papers()

    response = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 3}
    )

    payload = json.loads(response[0].text)
    assert set(payload.keys()) == {"mode", "query", "total_results", "papers"}
    for paper in payload["papers"]:
        assert "abstract" in paper
    assert "offset" not in payload
    assert "total_available" not in payload
    assert "next_offset" not in payload


@pytest.mark.asyncio
async def test_semantic_search_next_offset_null_at_end(semantic_test_env):
    """An offset that reaches the last page yields next_offset is None."""
    _index_three_papers()

    response = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 1, "offset": 2}
    )

    payload = json.loads(response[0].text)
    assert payload["total_results"] == 1
    assert payload["offset"] == 2
    assert payload["total_available"] == 3
    # offset(2) + returned(1) = 3, not < total_available(3) -> end of results.
    assert payload["next_offset"] is None


@pytest.mark.asyncio
async def test_semantic_search_offset_past_end(semantic_test_env):
    """An offset beyond total_available returns an empty page, not an error or loop."""
    _index_three_papers()

    response = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 3, "offset": 99}
    )

    payload = json.loads(response[0].text)
    assert payload["total_results"] == 0
    assert payload["papers"] == []
    assert payload["offset"] == 99
    assert payload["total_available"] == 3
    # offset(99) is not < total_available(3) -> no next page (no infinite paging).
    assert payload["next_offset"] is None


@pytest.mark.asyncio
async def test_semantic_search_compact_with_offset(semantic_test_env):
    """compact and offset combine: dropped abstract AND correct pagination cursor."""
    _index_three_papers()

    response = await semantic_module.handle_semantic_search(
        {"query": "vision transformer", "max_results": 1, "offset": 1, "compact": True}
    )

    payload = json.loads(response[0].text)
    assert payload["total_results"] == 1
    assert "abstract" not in payload["papers"][0]
    assert payload["papers"][0]["id"]  # still carries identity fields
    assert payload["offset"] == 1
    assert payload["total_available"] == 3
    # offset(1) + returned(1) = 2 < total_available(3) -> next page at 2.
    assert payload["next_offset"] == 2


@pytest.mark.asyncio
async def test_reindex_uses_local_markdown_ids(
    monkeypatch, semantic_test_env, temp_storage_path
):
    """Reindex should walk local markdown files and attempt indexing each ID."""
    Path(temp_storage_path, "2301.00001.md").write_text("paper", encoding="utf-8")
    Path(temp_storage_path, "2301.00002.md").write_text("paper", encoding="utf-8")

    indexed_ids = []

    def _mock_index(paper_id):
        indexed_ids.append(paper_id)
        return True

    monkeypatch.setattr(semantic_module, "index_paper_by_id", _mock_index)

    response = await semantic_module.handle_reindex({"clear_existing": True})

    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert set(indexed_ids) == {"2301.00001", "2301.00002"}

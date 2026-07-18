"""Semantic search and indexing tools for the arXiv MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import arxiv
import mcp.types as types
from mcp.types import ToolAnnotations

from ..config import Settings
from .arxiv_pacing import pace_arxiv_request_sync, record_arxiv_request
from .list_papers import is_valid_arxiv_id

try:
    import numpy as np
except ImportError:  # pragma: no cover - handled gracefully in runtime checks
    np = None  # type: ignore[assignment]

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover - handled gracefully in runtime checks
    SentenceTransformer = None  # type: ignore[assignment]

try:
    from model2vec import StaticModel
except ImportError:  # pragma: no cover - handled gracefully in runtime checks
    StaticModel = None  # type: ignore[assignment]

logger = logging.getLogger("arxiv-mcp-pro")
settings = Settings()

# Default embedding model per backend (B23). The sentence-transformers backend
# keeps the original all-MiniLM-L6-v2 (384-dim) so indexes built before B23 stay
# valid; the lightweight model2vec backend uses potion-retrieval-32M (512-dim,
# retrieval-tuned, torch-free). `settings.EMBEDDING_MODEL` overrides the active
# backend's default (see _active_model_name).
_DEFAULT_MODEL_NAMES = {
    "sentence-transformers": "sentence-transformers/all-MiniLM-L6-v2",
    "model2vec": "minishlab/potion-retrieval-32M",
}
INDEX_DB_NAME = "semantic_index.db"

# Lazily-loaded embedding model, cached alongside the backend + model name that
# produced it. Caching the identity (not just the instance) lets _get_model
# reload when the active backend or configured model changes — e.g. a test
# flipping backends, or an operator setting EMBEDDING_MODEL — instead of
# returning a stale model of the wrong dimension. Tests reset `_model = None`
# (the fixture) to force a reload.
_model: Optional[Any] = None
_model_backend: Optional[str] = None
_model_name: Optional[str] = None

# Guards a running `reindex` against concurrent `semantic_search` reads. With
# `clear_existing=True`, `rebuild_index` commits `DELETE FROM semantic_index`
# then slowly re-indexes off the event loop (B20); without this lock a search
# landing mid-rebuild would read a just-cleared / partial corpus. Lazy + module
# level so it can rebind across event loops in tests, mirroring
# download.py's `_get_index_semaphore` (asyncio primitives only bind to a loop
# on their contended slow path, so uncontended callers are loop-agnostic).
_reindex_lock: Optional[asyncio.Lock] = None


def _get_reindex_lock() -> asyncio.Lock:
    """Return the module-level reindex lock, creating it lazily."""
    global _reindex_lock
    if _reindex_lock is None:
        _reindex_lock = asyncio.Lock()
    return _reindex_lock


@dataclass
class IndexedPaper:
    """Stored paper payload used for similarity ranking."""

    paper_id: str
    title: str
    abstract: str
    authors: List[str]
    categories: List[str]
    published: str
    score: float


semantic_search_tool = types.Tool(
    name="semantic_search",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Semantic similarity search over papers you have already downloaded locally via download_paper. "
        "Supports free-text queries (e.g. 'attention mechanisms for long sequences') or finding papers "
        "similar to a given paper_id. "
        "IMPORTANT: only searches your local downloaded collection — will return empty results if no papers "
        "have been downloaded yet. Use search_papers to find papers on arXiv, then download_paper to add "
        "them to the local index before using this tool. "
        "Opt-in pagination: set `offset` to page through ranked results (page size = max_results); "
        "set `compact` to drop the full abstract from each result and cut token cost. When either is set, "
        "the response adds `offset`/`total_available`/`next_offset`. Omit both for full, unpaged output. "
        'Requires the [pro] extra: pip install "arxiv-mcp-pro[pro]" '
        '(from a source checkout: uv pip install -e ".[pro]").'
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text semantic query.",
            },
            "paper_id": {
                "type": "string",
                "description": "Find papers semantically similar to this arXiv paper ID.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 0,
                "description": "Maximum number of results to return (default: 10).",
                "default": 10,
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Pagination offset into the ranked results (page size = max_results). Send `offset: 0` for page one WITH a cursor (`total_available`/`next_offset`), then follow `next_offset` for later pages. Omit `offset` entirely (with `compact` also unset) for legacy unpaged output and no cursor.",
            },
            "compact": {
                "type": "boolean",
                "description": "Drop the full `abstract` from each result to cut token cost; all other fields (id, title, authors, categories, published, score, resource_uri) are kept. Omit for full output.",
            },
        },
        "additionalProperties": False,
    },
)


reindex_tool = types.Tool(
    name="reindex",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False
    ),
    description="Rebuild the local semantic index for downloaded papers.",
    inputSchema={
        "type": "object",
        "properties": {
            "clear_existing": {
                "type": "boolean",
                "description": "If true, clear the existing index before rebuilding.",
                "default": True,
            }
        },
        "additionalProperties": False,
    },
)


def _dependency_error() -> Optional[str]:
    """Return a friendly dependency error if no embedding backend is available.

    Either backend satisfies the requirement (B23): the lightweight model2vec
    backend (`[pro]`) or the sentence-transformers parity backend (`[pro-st]`).
    numpy is required by both.
    """
    if np is None or (SentenceTransformer is None and StaticModel is None):
        return (
            "Pro feature dependency missing. Install the lightweight model2vec "
            'backend with `pip install "arxiv-mcp-pro[pro]"`, or the '
            "sentence-transformers backend (exact parity with the pre-0.9 "
            'embedding model) with `pip install "arxiv-mcp-pro[pro-st]"` '
            '(from a source checkout: `uv pip install -e ".[pro]"`).'
        )
    return None


def _db_path() -> Path:
    """Return the semantic index SQLite path."""
    return Path(settings.STORAGE_PATH) / INDEX_DB_NAME


def _connect() -> sqlite3.Connection:
    """Open SQLite connection and ensure schema exists."""
    conn = sqlite3.connect(_db_path())
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS semantic_index (
                paper_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                published TEXT,
                embedding BLOB NOT NULL,
                embedding_dim INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        # Records which embedding model built the current index (B23). Keyed
        # rows `embedding_model` (`<backend>:<model>`) and `embedding_dim` let
        # _index_compat_error refuse a search against vectors from a different
        # model — with a "run reindex" message — instead of silently ranking on
        # incompatible embeddings or crashing on a numpy shape mismatch.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
        conn.commit()
    except Exception:
        # Schema init failed after the connection opened — close it rather than
        # leak it. Callers use `with closing(_connect())`, which never receives
        # (and so never closes) the connection if _connect raises mid-setup.
        conn.close()
        raise
    return conn


def _active_backend() -> Optional[str]:
    """Return the active embedding backend, or None if none is installed.

    sentence-transformers WINS when importable (B23): an upgrader who already has
    it installed sees zero behavior change and their 384-dim index keeps working.
    Otherwise the lightweight model2vec backend is used. None means no backend is
    available (callers gate on _dependency_error first).
    """
    if SentenceTransformer is not None:
        return "sentence-transformers"
    if StaticModel is not None:
        return "model2vec"
    return None


def _active_model_name(backend: Optional[str] = None) -> Optional[str]:
    """Return the model name for the active backend, honoring EMBEDDING_MODEL.

    An operator override (`settings.EMBEDDING_MODEL`) replaces the backend's
    built-in default; otherwise the per-backend default from _DEFAULT_MODEL_NAMES
    is used. Returns None only when no backend is available.
    """
    if backend is None:
        backend = _active_backend()
    if backend is None:
        return None
    if settings.EMBEDDING_MODEL:
        return settings.EMBEDDING_MODEL
    return _DEFAULT_MODEL_NAMES.get(backend)


def _get_model() -> Any:
    """Load the active-backend embedding model lazily.

    Reload when the backend or model name changes (see the `_model_*` globals)
    so a switch never returns a stale model. The ST path keeps
    `SentenceTransformer(name, silent=True)`; the model2vec path uses
    `StaticModel.from_pretrained(name)`.
    """
    global _model, _model_backend, _model_name
    backend = _active_backend()
    name = _active_model_name(backend)
    if _model is None or _model_backend != backend or _model_name != name:
        logger.info("Loading semantic embedding model %s (backend=%s)", name, backend)
        if backend == "sentence-transformers":
            _model = SentenceTransformer(name, silent=True)
        elif backend == "model2vec":
            _model = StaticModel.from_pretrained(name)
        else:  # pragma: no cover - callers gate on _dependency_error first
            raise RuntimeError("No embedding backend available")
        _model_backend = backend
        _model_name = name
    return _model


def _embed_text(text: str) -> Any:
    """Create a normalized float32 embedding vector for a text payload.

    Contract (unchanged): returns an L2-normalized float32 numpy vector. The two
    backends reach it differently:
      - sentence-transformers: normalize at encode time.
      - model2vec: potion-retrieval-32M is configured with `normalize: true`, so
        `.encode` already returns an L2-normalized float32 vector — do NOT
        re-normalize. An empty string yields a zero vector (norm 0); dividing by
        that norm would inject NaN, so we leave it as-is. A zero vector is
        harmless for cosine ranking (it scores 0 against every candidate). In
        practice the zero-vector edge never reaches ranking: index_paper_from_result
        skips empty abstracts and handle_semantic_search rejects an empty query.
    """
    model = _get_model()
    if _active_backend() == "model2vec":
        return model.encode(text or "")
    return model.encode(text or "", convert_to_numpy=True, normalize_embeddings=True)


def _read_index_meta() -> Dict[str, str]:
    """Return the index_meta table as a dict (empty when unset/legacy index)."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT key, value FROM index_meta").fetchall()
    return {row["key"]: row["value"] for row in rows}


def _index_compat_error(query_vector: Optional[Any] = None) -> Optional[str]:
    """Return a friendly 'run reindex' error if the local index is incompatible
    with the active embedding model, else None.

    Two cheapest-first checks; either failing returns the same guidance:

    1. Model-identity (post-B23 indexes carry index_meta): if the stored
       `embedding_model` (`<backend>:<model>`) disagrees with the active
       backend:model, the vectors came from a different model — the search must
       not silently rank on incompatible embeddings. Fires in BOTH query and
       paper_id modes.
    2. Dimension: if a supplied query vector's dimension differs from the stored
       vectors', `matrix @ query_vector` in _rank_by_similarity would raise a
       numpy shape error. Guard it with the friendly message first. This also
       covers legacy pre-B23 indexes that have no index_meta (free-text query
       mode supplies a current-model vector); in paper_id mode the "query" vector
       is itself a stored row, so its dim matches by construction and this is a
       no-op.

    An empty index (no rows) is compatible with anything — returns None, so a
    fresh install never trips the guard.
    """
    with closing(_connect()) as conn:
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM index_meta").fetchall()
        }
        sample = conn.execute(
            "SELECT embedding_dim FROM semantic_index LIMIT 1"
        ).fetchone()

    if sample is None:
        return None

    reindex_msg = (
        "The local semantic index was built with a different embedding model "
        "than the one now active, so its stored vectors are incompatible. Run "
        "the `reindex` tool (clear_existing=true) to rebuild the index with the "
        "current model."
    )

    stored_model = meta.get("embedding_model")
    if stored_model is not None:
        current_model = f"{_active_backend()}:{_active_model_name()}"
        if stored_model != current_model:
            return reindex_msg

    if query_vector is not None:
        stored_dim = int(sample["embedding_dim"])
        query_dim = int(np.asarray(query_vector).shape[0])
        if query_dim != stored_dim:
            return reindex_msg

    return None


def _upsert_index_record(
    paper_id: str,
    title: str,
    abstract: str,
    authors: List[str],
    categories: List[str],
    published: str = "",
) -> bool:
    """Insert or update an index record for a paper."""
    dependency_error = _dependency_error()
    if dependency_error:
        logger.warning(dependency_error)
        return False

    embedding = _embed_text(abstract)
    embedding_array = np.asarray(embedding, dtype=np.float32)
    new_dim = int(embedding_array.shape[0])

    with closing(_connect()) as conn:
        # Refuse to write a vector whose dimension disagrees with the existing
        # index (B23). This is the SINGLE authoritative write path — download
        # auto-index, semantic_search's paper_id index-on-miss, AND rebuild's
        # per-paper upserts all route here — so refusing here makes mixed-dim
        # rows structurally impossible, which is precisely what lets the
        # search-side guard (_index_compat_error) trust that the whole index
        # shares one model/dimension. Without it, a legacy 384-dim index plus a
        # switch to a 512-dim backend would insert a 512-dim row AND overwrite
        # index_meta to the new model, defeating the model-identity check and
        # leaving _load_vectors -> np.vstack to raise a raw ValueError on the
        # mixed dims. All callers already handle a False return (download logs,
        # paper_id search returns "Could not index source paper").
        sample = conn.execute(
            "SELECT embedding_dim FROM semantic_index LIMIT 1"
        ).fetchone()
        if sample is not None and int(sample["embedding_dim"]) != new_dim:
            logger.warning(
                "Refusing to index %s: its embedding dimension (%d) differs from "
                "the existing index (%d) — the index was built with a different "
                "embedding model. Run the `reindex` tool (clear_existing=true) to "
                "rebuild it with the current model.",
                paper_id,
                new_dim,
                int(sample["embedding_dim"]),
            )
            return False

        conn.execute(
            """
            INSERT INTO semantic_index (
                paper_id, title, abstract, authors_json, categories_json,
                published, embedding, embedding_dim, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
                title=excluded.title,
                abstract=excluded.abstract,
                authors_json=excluded.authors_json,
                categories_json=excluded.categories_json,
                published=excluded.published,
                embedding=excluded.embedding,
                embedding_dim=excluded.embedding_dim,
                updated_at=excluded.updated_at
            """,
            (
                paper_id,
                title,
                abstract,
                json.dumps(authors),
                json.dumps(categories),
                published,
                embedding_array.tobytes(),
                new_dim,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        # Stamp which model produced these vectors (B23) so a later search can
        # detect a model switch and refuse rather than mis-rank. One tiny extra
        # write per upsert; the embedding just computed is authoritative for both
        # the model identity and the dimension.
        conn.execute(
            "INSERT INTO index_meta (key, value) VALUES ('embedding_model', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{_active_backend()}:{_active_model_name()}",),
        )
        conn.execute(
            "INSERT INTO index_meta (key, value) VALUES ('embedding_dim', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(new_dim),),
        )
        conn.commit()

    return True


def index_paper_by_id(paper_id: str) -> bool:
    """Fetch arXiv metadata by ID and add/update it in the semantic index.

    Blocking (network + embedding) — run it off the event loop
    (``asyncio.to_thread``); the async handlers in this module and download.py
    do. Pacing: the metadata fetch goes through the sync cross-process pacer
    (B20) — pacing is the pacer's job, not any client-internal delay. The
    ``arxiv.Client()`` is created per call because this function runs in worker
    threads, so no ``requests.Session`` is shared across threads; the shared
    ``get_arxiv_client()`` instance is used by the foreground paths.
    """
    try:
        client = arxiv.Client()
        pace_arxiv_request_sync()
        try:
            paper = next(client.results(arxiv.Search(id_list=[paper_id])))
        finally:
            # Even a failed attempt hit the network; record it so sibling
            # lanes pace off the same clock.
            record_arxiv_request()
    except StopIteration:
        logger.warning("Could not index paper %s: not found on arXiv", paper_id)
        return False
    except Exception as exc:
        logger.error("Could not fetch metadata for %s: %s", paper_id, exc)
        return False

    return index_paper_from_result(paper)


def index_paper_from_result(paper: Any) -> bool:
    """Index a paper from an arxiv.Result-like object."""
    try:
        paper_id = paper.get_short_id()
        title = paper.title or ""
        abstract = paper.summary or ""
        authors = [author.name for author in getattr(paper, "authors", [])]
        categories = list(getattr(paper, "categories", []) or [])
        published = ""
        if getattr(paper, "published", None) is not None:
            published = paper.published.isoformat()

        if not abstract.strip():
            logger.warning(
                "Skipping semantic indexing for %s: empty abstract", paper_id
            )
            return False

        return _upsert_index_record(
            paper_id=paper_id,
            title=title,
            abstract=abstract,
            authors=authors,
            categories=categories,
            published=published,
        )
    except Exception as exc:
        logger.error("Failed indexing paper from result: %s", exc)
        return False


def _load_vectors(exclude_paper_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load all vectors (optionally excluding one paper)."""
    with closing(_connect()) as conn:
        if exclude_paper_id:
            rows = conn.execute(
                "SELECT * FROM semantic_index WHERE paper_id != ?", (exclude_paper_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM semantic_index").fetchall()

    vectors: List[Dict[str, Any]] = []
    for row in rows:
        vector = np.frombuffer(
            row["embedding"], dtype=np.float32, count=row["embedding_dim"]
        )
        vectors.append(
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "abstract": row["abstract"],
                "authors": json.loads(row["authors_json"]),
                "categories": json.loads(row["categories_json"]),
                "published": row["published"] or "",
                "vector": vector,
            }
        )
    return vectors


def _rank_by_similarity(
    query_vector: Any,
    candidates: List[Dict[str, Any]],
    max_results: int,
    offset: int = 0,
) -> List[IndexedPaper]:
    """Compute cosine similarity (normalized vectors) and rank results."""
    if not candidates:
        return []

    matrix = np.vstack([candidate["vector"] for candidate in candidates])
    similarities = matrix @ np.asarray(query_vector, dtype=np.float32)

    ranked_indices = np.argsort(similarities)[::-1][offset : offset + max_results]
    ranked_results: List[IndexedPaper] = []

    for idx in ranked_indices:
        candidate = candidates[int(idx)]
        ranked_results.append(
            IndexedPaper(
                paper_id=candidate["paper_id"],
                title=candidate["title"],
                abstract=candidate["abstract"],
                authors=candidate["authors"],
                categories=candidate["categories"],
                published=candidate["published"],
                score=float(similarities[int(idx)]),
            )
        )

    return ranked_results


def _get_indexed_paper_vector(paper_id: str) -> Optional[Any]:
    """Fetch an indexed vector for a specific paper."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT embedding, embedding_dim FROM semantic_index WHERE paper_id = ?",
            (paper_id,),
        ).fetchone()

    if row is None:
        return None

    return np.frombuffer(row["embedding"], dtype=np.float32, count=row["embedding_dim"])


def rebuild_index(clear_existing: bool = True) -> Dict[str, Any]:
    """Rebuild semantic index from downloaded markdown papers."""
    dependency_error = _dependency_error()
    if dependency_error:
        return {"status": "error", "message": dependency_error}

    paper_ids = sorted(
        p.stem
        for p in Path(settings.STORAGE_PATH).glob("*.md")
        if is_valid_arxiv_id(p.stem)
    )

    if clear_existing:
        # A full rebuild is the documented fix for a model switch: drop the old
        # vectors AND the stale index_meta so the re-populated index reflects the
        # active model (the per-paper upserts below re-stamp meta).
        with closing(_connect()) as conn:
            conn.execute("DELETE FROM semantic_index")
            conn.execute("DELETE FROM index_meta")
            conn.commit()
    else:
        # Incremental (append/update) mode must not mix embedding models in one
        # index: appending current-model vectors onto rows of a different
        # dimension would crash ranking (np.vstack) and silently corrupt it
        # otherwise. Learn the active model's output dimension by embedding a
        # probe (the rebuild loads the model regardless) and refuse if it
        # disagrees with the stored vectors, pointing the user at clear_existing.
        with closing(_connect()) as conn:
            sample = conn.execute(
                "SELECT embedding_dim FROM semantic_index LIMIT 1"
            ).fetchone()
        if sample is not None:
            stored_dim = int(sample["embedding_dim"])
            active_dim = int(np.asarray(_embed_text("dimension probe")).shape[0])
            if active_dim != stored_dim:
                return {
                    "status": "error",
                    "message": (
                        f"The existing semantic index stores {stored_dim}-dim "
                        f"vectors but the active embedding model produces "
                        f"{active_dim}-dim vectors. Re-run reindex with "
                        "clear_existing=true to rebuild the index cleanly."
                    ),
                }

    indexed = 0
    failed: List[str] = []

    for paper_id in paper_ids:
        success = index_paper_by_id(paper_id)
        if success:
            indexed += 1
        else:
            failed.append(paper_id)

    return {
        "status": "success",
        "indexed": indexed,
        "failed": failed,
        "total_local_papers": len(paper_ids),
    }


async def handle_reindex(arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle reindex tool calls."""
    try:
        clear_existing = bool(arguments.get("clear_existing", True))
        # Off the event loop: rebuild_index makes one paced arXiv call per
        # local paper (N × the pacing interval under B20) plus embedding work.
        # Run inline, that would freeze every other tool for minutes.
        # Hold _reindex_lock so a concurrent semantic_search waits for the
        # rebuild rather than reading a just-cleared / partially-rebuilt index.
        #
        # Tie the lock's release to the WORKER's completion, not this
        # coroutine's. Cancelling `await asyncio.to_thread(...)` (client
        # disconnect / request timeout) does NOT stop the worker thread — a
        # plain `async with` would exit and release the lock while the thread
        # is still mid-`DELETE FROM semantic_index` + repopulate, letting a
        # semantic_search acquire the lock and read the cleared/partial corpus
        # (codex P2). Acquire manually; release only from the worker's
        # done-callback (runs on the loop, fires exactly once). asyncio.shield
        # keeps the worker uncancelled; on cancellation CancelledError
        # propagates out of the handler (BaseException on 3.11 — NOT caught by
        # `except Exception` below), while the thread runs on holding the lock.
        lock = _get_reindex_lock()
        await lock.acquire()
        try:
            worker = asyncio.ensure_future(
                asyncio.to_thread(rebuild_index, clear_existing)
            )
        except BaseException:
            lock.release()
            raise
        worker.add_done_callback(lambda _t: lock.release())
        result = await asyncio.shield(worker)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:
        logger.error("Reindex failed: %s", exc)
        return [types.TextContent(type="text", text=f"Error: {str(exc)}")]


async def handle_semantic_search(arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle semantic search queries and similar-paper lookups."""
    try:
        dependency_error = _dependency_error()
        if dependency_error:
            return [types.TextContent(type="text", text=f"Error: {dependency_error}")]

        query = (arguments.get("query") or "").strip()
        paper_id = (arguments.get("paper_id") or "").strip()
        # Clamp to [0, MAX_RESULTS]: a negative max_results would otherwise feed a
        # negative slice bound into _rank_by_similarity (offset:offset+max_results),
        # producing surprising pages/cursors. 0 stays valid (empty page).
        max_results = min(
            max(0, int(arguments.get("max_results", 10))), settings.MAX_RESULTS
        )
        # Capture whether `offset` was explicitly provided (even as 0): an
        # explicit offset opts into paginated mode so the client gets cursor
        # metadata to page forward. `offset` omitted (or null) stays legacy.
        offset_arg = arguments.get("offset")
        offset = max(0, int(offset_arg or 0))
        # Strict boolean (like citation_graph's `compact`/`counts_only`): a string
        # such as "false" is truthy under bool(), which would silently drop every
        # abstract and switch into paginated mode.
        compact = arguments.get("compact") is True

        if not query and not paper_id:
            return [
                types.TextContent(
                    type="text",
                    text="Error: Provide either `query` or `paper_id` for semantic_search.",
                )
            ]

        # Serialize the read/rank against a running reindex: searches wait for a
        # running rebuild rather than reading a just-cleared index (pre-B20, the
        # inline rebuild blocked the loop and serialized these de facto; the lock
        # restores those observable semantics while keeping OTHER tools
        # responsive). Holding it across the query-mode _embed_text too is
        # harmless and keeps the critical section a single block. Unlike
        # reindex, a plain `async with` is safe here: a cancelled search
        # releases the lock with no orphan worker mutating shared state — the
        # only write on this path is a single index_paper_by_id upsert, never a
        # clear+rebuild, so an early release cannot expose a cleared index.
        async with _get_reindex_lock():
            if paper_id:
                query_vector = _get_indexed_paper_vector(paper_id)
                if query_vector is None:
                    logger.info(
                        "Paper %s not indexed yet, attempting to fetch and index",
                        paper_id,
                    )
                    # Off the event loop: blocking network fetch, paced (B20).
                    if not await asyncio.to_thread(index_paper_by_id, paper_id):
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Error: Could not index source paper {paper_id}.",
                            )
                        ]
                    query_vector = _get_indexed_paper_vector(paper_id)

                candidates = _load_vectors(exclude_paper_id=paper_id)
                mode = "similar_to_paper"
                query_payload = paper_id
            else:
                query_vector = _embed_text(query)
                candidates = _load_vectors()
                mode = "semantic_query"
                query_payload = query

            # Refuse a search against an index built by a different embedding
            # model BEFORE _rank_by_similarity does `matrix @ query_vector`
            # (which raises a bare numpy shape error on a dim mismatch). Return
            # the friendly "run reindex" guidance instead (B23).
            compat_error = _index_compat_error(query_vector)
            if compat_error:
                return [types.TextContent(type="text", text=f"Error: {compat_error}")]

            ranked = _rank_by_similarity(
                query_vector, candidates, max_results=max_results, offset=offset
            )
            total_available = len(candidates)

        papers = []
        for paper in ranked:
            paper_dict = {
                "id": paper.paper_id,
                "title": paper.title,
                "abstract": paper.abstract,
                "authors": paper.authors,
                "categories": paper.categories,
                "published": paper.published,
                "score": round(paper.score, 6),
                "resource_uri": f"arxiv://{paper.paper_id}",
            }
            if compact:
                # pop (not del) so a future change making `abstract` conditional
                # can't raise a KeyError that the broad except below would swallow.
                paper_dict.pop("abstract", None)
            papers.append(paper_dict)

        # PAGINATED MODE = `offset` explicitly provided (even 0) or `compact`.
        # Only the default — `offset` omitted AND not compact — leaves the
        # response byte-for-byte identical to the legacy shape. An explicit
        # `offset: 0` is a deliberate opt-in: unlike citation_graph there is no
        # separate `limit` param to trigger pagination while starting at 0.
        paginated = offset_arg is not None or compact
        response = {
            "mode": mode,
            "query": query_payload,
            "total_results": len(ranked),
        }
        if paginated:
            next_offset = offset + len(ranked)
            response["offset"] = offset
            response["total_available"] = total_available
            # Only emit a cursor when this page actually returned results. An empty
            # page (e.g. max_results=0, or offset past the end) must not point a
            # client back at the same offset, or it loops forever.
            response["next_offset"] = (
                next_offset if ranked and next_offset < total_available else None
            )
        response["papers"] = papers

        return [types.TextContent(type="text", text=json.dumps(response, indent=2))]
    except Exception as exc:
        logger.error("Semantic search failed: %s", exc)
        return [types.TextContent(type="text", text=f"Error: {str(exc)}")]

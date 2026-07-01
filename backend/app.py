import asyncio
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load local env vars (e.g. SUPABASE_DB_URL) from .env.local so local runs mirror
# Render. override=False means real environment vars (Render's) always win.
load_dotenv(Path(__file__).parent.parent / ".env.local", override=False)

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.bfs import find_path
from backend.graph_backend import ALL_EDGE_TYPES, ALL_WORK_EDGE_TYPES, OpenAlexBackend, _is_work_id
from backend.models import AuthorResult, AuthorWork, PaginatedAuthors, PaginatedWorks
from backend.neighbor_store import (
    JsonNeighborStore,
    NeighborCache,
    NeighborStore,
    SupabaseNeighborStore,
)
from backend.openalex_client import OpenAlexClient, _short_id

log = logging.getLogger(__name__)

app = FastAPI(title="Researcher Degree of Separation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "DELETE"],
    allow_headers=["*"],
)

_KEY_PATH = Path(__file__).parent.parent / "api-keys.json"
_keys: dict = json.loads(_KEY_PATH.read_text()) if _KEY_PATH.exists() else {}

_client = OpenAlexClient()
_BACKEND = os.environ.get("BACKEND", "openalex")

# ── Neighbor cache ─────────────────────────────────────────────────────────────
# A bounded in-memory LRU (`NeighborCache`) fronts a durable NeighborStore, so the
# process footprint stays flat under load instead of holding the whole table
# resident. On an LRU miss the store is consulted per-id; only a true miss hits
# OpenAlex. Setting SUPABASE_DB_URL (or `supabase-db-url` in api-keys.json)
# selects the Postgres-backed store (survives Render's ephemeral FS); otherwise a
# local JSON file is used. NEIGHBOR_CACHE_MAX caps the resident entry count.

_CACHE_FILE = Path(__file__).parent.parent / "neighbor_cache.json"
_CACHE_MAX = int(os.environ.get("NEIGHBOR_CACHE_MAX", "10000"))


def _make_store() -> NeighborStore:
    dsn = _keys.get("supabase-db-url") or os.environ.get("SUPABASE_DB_URL")
    if dsn:
        return SupabaseNeighborStore(dsn)
    return JsonNeighborStore(_CACHE_FILE)


_store: NeighborStore = _make_store()
_cache = NeighborCache(_store, max_size=_CACHE_MAX)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _store.open()
    yield
    await _store.close()


app.router.lifespan_context = lifespan


def _make_backend(edge_types: set[str], work_edge_types: set[str] | None = None) -> OpenAlexBackend:
    if _BACKEND == "bigquery":
        from backend.bigquery_backend import BigQueryBackend
        project = _keys.get("gcp-project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise RuntimeError(
                "BigQuery backend requires gcp-project in api-keys.json "
                "or GOOGLE_CLOUD_PROJECT env var"
            )
        return BigQueryBackend(project, edge_types=edge_types)
    return OpenAlexBackend(
        _client,
        edge_types=edge_types,
        work_edge_types=work_edge_types,
        neighbor_cache=_cache,
    )


def _get_inst(author: dict) -> str | None:
    insts = author.get("last_known_institutions", [])
    return insts[0].get("display_name") if insts else None


async def _collect_path(
    backend: OpenAlexBackend,
    from_id: str,
    from_name: str,
    to_id: str,
) -> dict:
    """Run bidirectional BFS; return the found path's nodes/edges plus hop count.

    The returned dict carries the graph elements as well as the degree-of-separation
    metadata (found, hops, and both endpoint names) so the caller can emit a `path`
    SSE event without re-deriving any of it.
    """
    if _is_work_id(to_id):
        to_obj = await _client.get_work(to_id)
        to_name = to_obj.get("title", to_id)
    else:
        to_obj = await _client.get_author(to_id)
        to_name = to_obj.get("display_name", to_id)

    nodes: list[dict] = []
    edges: list[dict] = []
    steps: list[dict] = []   # ordered hops along the path (names + paper/label)
    found = False
    hops: int | None = None

    async for event in find_path(backend, from_id, from_name, to_id, to_name):
        if event.get("type") == "result" and event.get("found"):
            found = True
            hops = event.get("hops")
            path = event["path"]
            for i, step in enumerate(path):
                sid = step["author_id"]
                is_endpoint = sid in (from_id, to_id)
                # A work can only ever be a path endpoint (its only edges are to
                # its own authors/citers, never to another work) — never a mid-path node.
                node_type = "work" if _is_work_id(sid) else ("origin" if is_endpoint else "path")
                nodes.append({
                    "id": sid,
                    "name": step["author_name"],
                    "institution": None,
                    "works_count": 0,
                    "cited_by_count": 0,
                    "publication_year": None,
                    "type": node_type,
                    "depth": 0,
                })
                if i < len(path) - 1 and step.get("connection_to_next"):
                    nxt = path[i + 1]
                    edges.append({
                        "source": step["author_id"],
                        "target": nxt["author_id"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                        "direction": step.get("direction"),
                    })
                    steps.append({
                        "from_name": step["author_name"],
                        "to_name": nxt["author_name"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                        "direction": step.get("direction"),
                    })

    # The BFS only knows ids + names, so path nodes would otherwise render as
    # "0 works · 0 citations" with no institution. Backfill real metadata in a
    # single batched lookup before returning — authors via get_authors_batch,
    # and any work-typed endpoint (at most from_id/to_id, never a mid-path node)
    # via get_work, reusing the to_id fetch already done above where possible.
    author_node_ids = [n["id"] for n in nodes if n["type"] != "work"]
    if author_node_ids:
        authors = await _client.get_authors_batch(author_node_ids)
        meta = {_short_id(a["id"]): a for a in authors}
        for n in nodes:
            a = meta.get(n["id"])
            if a:
                n["institution"] = _get_inst(a)
                n["works_count"] = a.get("works_count", 0)
                n["cited_by_count"] = a.get("cited_by_count", 0)

    for n in nodes:
        if n["type"] != "work":
            continue
        w = to_obj if n["id"] == to_id else await _client.get_work(n["id"])
        n["cited_by_count"] = w.get("cited_by_count", 0)
        n["publication_year"] = w.get("publication_year")
        n["name"] = w.get("title", n["name"])

    return {
        "nodes": nodes,
        "edges": edges,
        "steps": steps,
        "found": found,
        "hops": hops,
        "from_id": from_id,
        "from_name": from_name,
        "to_id": to_id,
        "to_name": to_name,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/authors", response_model=PaginatedAuthors)
async def search_authors(
    q: str = Query(..., min_length=2),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=50),
):
    results, total = await _client.search_authors(q, page=page, per_page=per_page)
    total_pages = max(1, math.ceil(total / per_page))
    return PaginatedAuthors(
        results=results, page=page, per_page=per_page,
        total=total, total_pages=total_pages,
    )


@app.get("/api/works", response_model=PaginatedWorks)
async def search_works(
    q: str = Query(..., min_length=2),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=50),
):
    results, total = await _client.search_works(q, page=page, per_page=per_page)
    total_pages = max(1, math.ceil(total / per_page))
    return PaginatedWorks(
        results=results, page=page, per_page=per_page,
        total=total, total_pages=total_pages,
    )


@app.get("/api/authors/{author_id}/works", response_model=list[AuthorWork])
async def get_author_top_works(author_id: str, limit: int = Query(default=10, ge=1, le=25)):
    works = await _client.get_author_works(author_id, limit=limit)
    return [
        AuthorWork(
            id=_short_id(w["id"]),
            title=w.get("title") or "(untitled)",
            cited_by_count=w.get("cited_by_count", 0),
            publication_year=w.get("publication_year"),
            doi=w.get("doi"),
        )
        for w in works
    ]


@app.delete("/api/cache")
async def clear_cache():
    """Wipe the server-side neighbor cache (in-memory LRU + persisted store)."""
    await _cache.clear()
    return {"cleared": True}


@app.get("/api/path")
async def get_path(
    from_id: str = Query(..., alias="from"),
    to_id: str = Query(..., alias="to"),
    edges: list[str] = Query(default=list(ALL_EDGE_TYPES)),
):
    edge_types = {e for e in edges if e in ALL_EDGE_TYPES} or ALL_EDGE_TYPES

    async def event_stream():
        try:
            from_author = await _client.get_author(from_id)
            to_author = await _client.get_author(to_id)
            from_name = from_author.get("display_name", from_id)
            to_name = to_author.get("display_name", to_id)
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return

        backend = _make_backend(edge_types)
        try:
            async for event in find_path(backend, from_id, from_name, to_id, to_name):
                event_type = event.get("type", "progress")
                yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/graph/expand")
async def graph_expand(
    new_id: str = Query(...),
    origin_ids: str = Query(default=""),   # comma-sep existing origin IDs
    path_ids: str = Query(default=""),     # comma-sep existing path node IDs from client
    edges: list[str] = Query(default=list(ALL_EDGE_TYPES)),
    work_edges: list[str] = Query(default=list(ALL_WORK_EDGE_TYPES)),
    depth: int = Query(default=2, ge=0, le=4),   # neighborhood expansion depth (0 = path only)
    top_k: int = Query(default=8, ge=1, le=25),  # neighbors kept per expansion level
):
    from backend.graph_expand import expand_graph

    edge_types = {e for e in edges if e in ALL_EDGE_TYPES} or ALL_EDGE_TYPES
    work_edge_types = {e for e in work_edges if e in ALL_WORK_EDGE_TYPES} or ALL_WORK_EDGE_TYPES
    existing_origins = [x.strip() for x in origin_ids.split(",") if x.strip()]
    existing_path_ids = [x.strip() for x in path_ids.split(",") if x.strip()]

    async def event_stream():
        # Fetch the new origin's metadata — a work (paper) or an author.
        try:
            new_obj = await (
                _client.get_work(new_id) if _is_work_id(new_id) else _client.get_author(new_id)
            )
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return

        if _is_work_id(new_id):
            new_name = new_obj.get("title", new_id)
            new_node = {
                "id": new_id,
                "name": new_name,
                "cited_by_count": new_obj.get("cited_by_count", 0),
                "publication_year": new_obj.get("publication_year"),
                "type": "work",
                "depth": 0,
            }
        else:
            new_name = new_obj.get("display_name", new_id)
            new_node = {
                "id": new_id,
                "name": new_name,
                "institution": _get_inst(new_obj),
                "works_count": new_obj.get("works_count", 0),
                "cited_by_count": new_obj.get("cited_by_count", 0),
                "type": "origin",
                "depth": 0,
            }
        yield f"event: node\ndata: {json.dumps(new_node)}\n\n"

        backend = _make_backend(edge_types, work_edge_types)
        new_path_node_ids: list[str] = []

        # Find paths from new researcher to all existing origins in parallel
        if existing_origins:
            yield f"event: progress\ndata: {json.dumps({'message': f'Finding connections to {len(existing_origins)} existing researcher(s)…'})}\n\n"

            path_results = await asyncio.gather(*[
                _collect_path(backend, new_id, new_name, oid)
                for oid in existing_origins
            ], return_exceptions=True)

            for result in path_results:
                if isinstance(result, Exception):
                    log.warning("Path finding failed: %s", result)
                    continue
                pair_key = "||".join(sorted([result["from_id"], result["to_id"]]))
                for n in result["nodes"]:
                    if n["type"] == "path":
                        n = {**n, "path_pair": pair_key}
                        new_path_node_ids.append(n["id"])
                    yield f"event: node\ndata: {json.dumps(n)}\n\n"
                for e in result["edges"]:
                    yield f"event: edge\ndata: {json.dumps(e)}\n\n"
                path_event = {
                    k: result[k]
                    for k in ("from_id", "from_name", "to_id", "to_name", "hops", "found", "steps")
                }
                yield f"event: path\ndata: {json.dumps(path_event)}\n\n"

        # Balanced expansion around each researcher of interest (the origins). The
        # connecting-path nodes are passed as already-seen so they aren't re-grown.
        all_origins = [new_id] + existing_origins
        all_path_nodes = list(set(new_path_node_ids + existing_path_ids))

        if depth > 0:
            yield f"event: progress\ndata: {json.dumps({'message': 'Building neighborhood graph…'})}\n\n"

            async for event in expand_graph(
                backend, _client, all_origins,
                max_depth=depth, top_k=top_k, bridge_ids=all_path_nodes,
            ):
                event_type = event.get("type", "progress")
                yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

        yield f"event: done\ndata: {{}}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")

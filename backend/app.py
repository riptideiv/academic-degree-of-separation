import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.bfs import find_path
from backend.graph_backend import ALL_EDGE_TYPES, OpenAlexBackend
from backend.models import AuthorResult
from backend.openalex_client import OpenAlexClient, _short_id

log = logging.getLogger(__name__)

app = FastAPI(title="Researcher Degree of Separation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_KEY_PATH = Path(__file__).parent.parent / "api-keys.json"
_keys: dict = json.loads(_KEY_PATH.read_text()) if _KEY_PATH.exists() else {}

_client = OpenAlexClient()
_BACKEND = os.environ.get("BACKEND", "openalex")

# Shared ring cache — persists for the lifetime of the server process.
# Maps author_id → list[Connection] (all edge types stored, filtered on retrieval).
_neighbor_cache: dict = {}


def _make_backend(edge_types: set[str]) -> OpenAlexBackend:
    if _BACKEND == "bigquery":
        from backend.bigquery_backend import BigQueryBackend
        project = _keys.get("gcp-project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise RuntimeError(
                "BigQuery backend requires gcp-project in api-keys.json "
                "or GOOGLE_CLOUD_PROJECT env var"
            )
        return BigQueryBackend(project, edge_types=edge_types)
    return OpenAlexBackend(_client, edge_types=edge_types, neighbor_cache=_neighbor_cache)


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
    to_author = await _client.get_author(to_id)
    to_name = to_author.get("display_name", to_id)

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
                is_endpoint = step["author_id"] in (from_id, to_id)
                nodes.append({
                    "id": step["author_id"],
                    "name": step["author_name"],
                    "institution": None,
                    "works_count": 0,
                    "cited_by_count": 0,
                    "type": "origin" if is_endpoint else "path",
                    "depth": 0,
                })
                if i < len(path) - 1 and step.get("connection_to_next"):
                    nxt = path[i + 1]
                    edges.append({
                        "source": step["author_id"],
                        "target": nxt["author_id"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                    })
                    steps.append({
                        "from_name": step["author_name"],
                        "to_name": nxt["author_name"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                    })

    # The BFS only knows author ids + names, so path nodes would otherwise render
    # as "0 works · 0 citations" with no institution. Backfill real metadata in a
    # single batched lookup before returning.
    if nodes:
        authors = await _client.get_authors_batch([n["id"] for n in nodes])
        meta = {_short_id(a["id"]): a for a in authors}
        for n in nodes:
            a = meta.get(n["id"])
            if a:
                n["institution"] = _get_inst(a)
                n["works_count"] = a.get("works_count", 0)
                n["cited_by_count"] = a.get("cited_by_count", 0)

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


@app.get("/api/authors", response_model=list[AuthorResult])
async def search_authors(q: str = Query(..., min_length=2)):
    return await _client.search_authors(q)


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
    depth: int = Query(default=2, ge=0, le=4),   # neighborhood expansion depth (0 = path only)
    top_k: int = Query(default=8, ge=1, le=25),  # neighbors kept per expansion level
):
    from backend.graph_expand import expand_graph

    edge_types = {e for e in edges if e in ALL_EDGE_TYPES} or ALL_EDGE_TYPES
    existing_origins = [x.strip() for x in origin_ids.split(",") if x.strip()]
    existing_path_ids = [x.strip() for x in path_ids.split(",") if x.strip()]

    async def event_stream():
        # Fetch new researcher's metadata
        try:
            new_author = await _client.get_author(new_id)
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return

        new_node = {
            "id": new_id,
            "name": new_author.get("display_name", new_id),
            "institution": _get_inst(new_author),
            "works_count": new_author.get("works_count", 0),
            "cited_by_count": new_author.get("cited_by_count", 0),
            "type": "origin",
            "depth": 0,
        }
        yield f"event: node\ndata: {json.dumps(new_node)}\n\n"

        backend = _make_backend(edge_types)
        new_path_node_ids: list[str] = []

        # Find paths from new researcher to all existing origins in parallel
        if existing_origins:
            new_name = new_author.get("display_name", new_id)
            yield f"event: progress\ndata: {json.dumps({'message': f'Finding connections to {len(existing_origins)} existing researcher(s)…'})}\n\n"

            path_results = await asyncio.gather(*[
                _collect_path(backend, new_id, new_name, oid)
                for oid in existing_origins
            ], return_exceptions=True)

            for result in path_results:
                if isinstance(result, Exception):
                    log.warning("Path finding failed: %s", result)
                    continue
                for n in result["nodes"]:
                    if n["type"] == "path":
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

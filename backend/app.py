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
from backend.openalex_client import OpenAlexClient

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
) -> tuple[list[dict], list[dict]]:
    """Run bidirectional BFS; return (graph_nodes, graph_edges) for the found path."""
    to_author = await _client.get_author(to_id)
    to_name = to_author.get("display_name", to_id)

    nodes: list[dict] = []
    edges: list[dict] = []

    async for event in find_path(backend, from_id, from_name, to_id, to_name):
        if event.get("type") == "result" and event.get("found"):
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
                    edges.append({
                        "source": step["author_id"],
                        "target": path[i + 1]["author_id"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                    })

    return nodes, edges


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
                p_nodes, p_edges = result
                for n in p_nodes:
                    if n["type"] == "path":
                        new_path_node_ids.append(n["id"])
                    yield f"event: node\ndata: {json.dumps(n)}\n\n"
                for e in p_edges:
                    yield f"event: edge\ndata: {json.dumps(e)}\n\n"

        # BFS expansion from all origins + all path nodes (new and previously known)
        all_origins = [new_id] + existing_origins
        all_path_nodes = list(set(new_path_node_ids + existing_path_ids))
        start_nodes = all_origins + all_path_nodes

        yield f"event: progress\ndata: {json.dumps({'message': 'Building neighborhood graph…'})}\n\n"

        async for event in expand_graph(backend, _client, start_nodes):
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

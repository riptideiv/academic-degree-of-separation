import json
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


def _make_backend(edge_types: set[str]):
    if _BACKEND == "bigquery":
        from backend.bigquery_backend import BigQueryBackend
        project = _keys.get("gcp-project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise RuntimeError(
                "BigQuery backend requires gcp-project in api-keys.json "
                "or GOOGLE_CLOUD_PROJECT env var"
            )
        return BigQueryBackend(project, edge_types=edge_types)
    return OpenAlexBackend(_client, edge_types=edge_types)


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


_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")

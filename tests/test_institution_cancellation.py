"""Cancelling an Institution Explorer request must stop all of its API work."""

import asyncio
import importlib
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.openalex_client import CoauthorSummary


app_module = importlib.import_module("backend.app")


@pytest.mark.parametrize("phase", ["origin", "topic", "seed", "deep"])
async def test_cancelled_institution_request_drains_background_tasks(monkeypatch, phase):
    institution = {"id": "https://openalex.org/I1", "display_name": "University"}
    topic = {
        "id": "https://openalex.org/T1",
        "display_name": "Topic",
        "field": {"id": "https://openalex.org/fields/1", "display_name": "Field"},
    }
    origin = {
        "id": "https://openalex.org/A0", "display_name": "Origin",
        "topics": [topic], "last_known_institutions": [],
    }
    candidate = {
        "id": "https://openalex.org/A1", "display_name": "Candidate",
        "topics": [topic], "last_known_institutions": [institution],
        "works_count": 1, "cited_by_count": 1,
    }
    expected_calls = {
        "origin": ["citation", "origin"],
        "topic": ["citation", "topic", "hierarchy"],
        "seed": ["seed:A0", "seed:A1"],
        "deep": ["deep"],
    }[phase]
    started = {name: asyncio.Event() for name in expected_calls}
    finished = {name: asyncio.Event() for name in expected_calls}
    blocked_tasks = []
    release = asyncio.Event()

    async def block(name):
        blocked_tasks.append(asyncio.current_task())
        started[name].set()
        try:
            await release.wait()
        finally:
            finished[name].set()

    async def citation(*args, **kwargs):
        if phase in {"origin", "topic"}:
            await block("citation")
        return [candidate]

    async def authors(*args, **kwargs):
        if phase == "origin":
            await block("origin")
        return [origin]

    async def topics(*args, **kwargs):
        if phase == "topic":
            await block("topic")
        return []

    async def hierarchy(*args, **kwargs):
        if phase == "topic":
            await block("hierarchy")
        return []

    async def summary(author_id, **kwargs):
        if phase == "seed":
            await block(f"seed:{author_id}")
        return CoauthorSummary({}, complete=True)

    async def path(*args, **kwargs):
        await block("deep")
        return {"found": False, "search_complete": True}

    client = AsyncMock()
    client.get_institution_authors.side_effect = citation
    client.get_authors_batch.side_effect = authors
    client.get_institution_authors_by_topics.side_effect = topics
    client.get_institution_authors_by_hierarchy.side_effect = hierarchy
    client.get_coauthor_summary.side_effect = summary
    monkeypatch.setattr(app_module, "_client", client)
    monkeypatch.setattr(app_module, "get_effective_affiliation_overrides", lambda _: [])
    monkeypatch.setattr(app_module, "_make_backend", lambda _: object())
    monkeypatch.setattr(
        app_module, "_short_coauthor_paths", AsyncMock(return_value=({}, 0, True))
    )
    monkeypatch.setattr(app_module, "_collect_path_proposal", path)

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as http:
        request = asyncio.create_task(http.get(
            "/api/institution-suggestions?institution_id=I1&origin_ids=A0"
        ))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started.values())),
                timeout=1,
            )
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, timeout=1)

            # Cleanup is part of completing the request, not deferred work.
            assert all(event.is_set() for event in finished.values())
            assert all(task.done() for task in blocked_tasks)
        finally:
            release.set()
            for task in [request, *blocked_tasks]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(request, *blocked_tasks, return_exceptions=True)

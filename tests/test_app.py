import asyncio
import json
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from backend.app import (
    _SummarySeededBackend,
    _collect_path_proposal,
    _has_current_inst,
    _merge_affiliation_overrides,
    _ranked_author_payload,
    _short_coauthor_paths,
    app,
)
from backend.models import AuthorResult, Connection, WorkResult
from backend.neighbor_store import JsonNeighborStore, SupabaseNeighborStore


async def test_health_reports_json_store(tmp_path):
    with patch("backend.app._store", JsonNeighborStore(tmp_path / "cache.json")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "store": "json"}


async def test_health_reports_supabase_store():
    with patch("backend.app._store", SupabaseNeighborStore("postgresql://example")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "store": "supabase"}


async def test_summary_seeded_backend_uses_seed_roots_and_delegates_the_rest():
    class BaseBackend:
        def __init__(self):
            self.requested = []

        async def get_neighbors(self, author_id):
            return (await self.get_neighbors_batch([author_id]))[author_id]

        async def get_neighbors_batch(self, author_ids, cached_only=False):
            self.requested.append((author_ids, cached_only))
            return {
                author_id: [Connection(
                    target_author_id="A-DELEGATED",
                    target_name="Delegated",
                    connection_type="coauthor",
                    label="",
                )]
                for author_id in author_ids
            }

    base = BaseBackend()
    seed = Connection(
        target_author_id="A-SEED-NEIGHBOR",
        target_name="Seed Neighbor",
        connection_type="coauthor",
        label="",
    )
    backend = _SummarySeededBackend(
        base,
        {"A-SEED": [seed]},
        complete_seed_ids={"A-SEED"},
    )

    result = await backend.get_neighbors_batch(
        ["A-SEED", "A-OTHER"], cached_only=True
    )

    assert result["A-SEED"] == [seed]
    assert result["A-OTHER"][0].target_author_id == "A-DELEGATED"
    assert result.complete_ids == {"A-SEED", "A-OTHER"}
    assert base.requested == [(["A-OTHER"], True)]

    incomplete = _SummarySeededBackend(base, {"A-SEED": [seed]})
    incomplete_result = await incomplete.get_neighbors_batch(["A-SEED"])
    assert incomplete_result.complete_ids == set()


async def test_rank_path_proposal_skips_discarded_metadata_hydration():
    path = [
        {
            "author_id": "A1",
            "author_name": "Alice",
            "connection_to_next": "coauthor",
            "label": "Proposal paper",
            "direction": None,
        },
        {
            "author_id": "A2",
            "author_name": "Bob",
            "connection_to_next": None,
            "label": None,
            "direction": None,
        },
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": path, "hops": 1}

    with patch("backend.app.find_path", mock_find_path), patch(
        "backend.app._client"
    ) as client:
        result = await _collect_path_proposal(
            AsyncMock(), "A1", "Alice", "A2", max_depth=4, to_name="Bob"
        )

    assert result["found"] is True
    assert result["hops"] == 1
    assert result["steps"] == [{
        "from_id": "A1",
        "from_name": "Alice",
        "to_id": "A2",
        "to_name": "Bob",
        "type": "coauthor",
        "label": "Proposal paper",
        "direction": None,
    }]
    assert client.mock_calls == []


async def test_openalex_key_accepts_plain_text_payload():
    with patch("backend.app._client") as mock_client:
        mock_client.has_api_key = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/api/openalex-key",
                content="test-openalex-key",
                headers={"content-type": "text/plain"},
            )
    assert resp.status_code == 200
    assert resp.json() == {"configured": True}
    mock_client.set_api_key.assert_called_once_with("test-openalex-key")


async def test_search_authors_returns_results():
    mock_results = [
        AuthorResult(id="A1", display_name="Alice", institution="MIT", works_count=10)
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.search_authors = AsyncMock(return_value=(mock_results, 1))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors?q=Alice")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["display_name"] == "Alice"
    assert data["results"][0]["id"] == "A1"
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["total"] == 1
    assert data["total_pages"] == 1


async def test_search_authors_pagination_params():
    mock_results = [
        AuthorResult(id=f"A{i}", display_name=f"Author {i}", works_count=1)
        for i in range(20)
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.search_authors = AsyncMock(return_value=(mock_results, 45))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors?q=Alice&page=2&per_page=20")
    assert resp.status_code == 200
    data = resp.json()
    mock_client.search_authors.assert_awaited_once_with("Alice", page=2, per_page=20)
    assert data["page"] == 2
    assert data["total"] == 45
    assert data["total_pages"] == 3


async def test_search_authors_handles_openalex_rate_limit():
    request = httpx.Request("GET", "https://api.openalex.org/authors")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
    cached_results = [
        AuthorResult(id="A2", display_name="Cached Alice", institution=None, works_count=0)
    ]
    with patch("backend.app._client") as mock_client, patch("backend.app._local_index") as mock_index:
        mock_client.search_authors = AsyncMock(side_effect=exc)
        mock_index.search_authors.return_value = (cached_results, 1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors?q=Alice")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["display_name"] == "Cached Alice"
    assert data["total"] == 1
    assert "Advanced settings" in data["message"]


async def test_get_author_top_works_returns_results():
    mock_works = [
        {
            "id": "https://openalex.org/W1", "title": "Paper One", "cited_by_count": 50,
            "publication_year": 2020, "doi": "https://doi.org/10.1/abc",
        },
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.get_author_works = AsyncMock(return_value=mock_works)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors/A1/works?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "W1"
    assert data[0]["title"] == "Paper One"
    assert data[0]["cited_by_count"] == 50
    assert data[0]["publication_year"] == 2020
    assert data[0]["doi"] == "https://doi.org/10.1/abc"
    mock_client.get_author_works.assert_awaited_once_with("A1", limit=10)


async def test_get_conflated_author_works_uses_reviewed_scope():
    reviewed_works = [
        {
            "id": "https://openalex.org/W3139199066",
            "title": "Running it up the flagpole",
            "cited_by_count": 3,
            "publication_year": 2021,
            "authorships": [{"author": {
                "id": "https://openalex.org/A5072773992",
                "display_name": "Katrina Elliott",
            }}],
        },
        {
            "id": "https://openalex.org/W2891784578",
            "title": "Time Travel",
            "cited_by_count": 10,
            "publication_year": 2018,
            "authorships": [{"author": {
                "id": "https://openalex.org/A5072773992",
                "display_name": "Katrina Elliott",
            }}],
        },
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.get_works_batch = AsyncMock(return_value=reviewed_works)
        mock_client.get_author_works = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors/A5072773992/works?limit=1")

    assert resp.status_code == 200
    assert [work["id"] for work in resp.json()] == ["W2891784578"]
    requested = set(mock_client.get_works_batch.await_args.args[0])
    assert requested == {
        "W2891784578", "W2800467463", "W2345731203",
        "W3204177509", "W3139199066", "W3136126418",
    }
    mock_client.get_author_works.assert_not_awaited()


@pytest.mark.skip(reason="Replaced by graph-based institution suggestions")
async def test_institution_rank_sorts_by_closest_path():
    institution = {
        "id": "I1", "display_name": "Duke University",
        "country_code": "US", "works_count": 100, "cited_by_count": 1000,
    }
    target = AuthorResult(id="A9", display_name="David Chalmers", works_count=20)
    candidates = [
        {
            "id": "https://openalex.org/A1", "display_name": "Close Scholar",
            "works_count": 5, "cited_by_count": 20, "last_known_institutions": [
                {"id": "https://openalex.org/I9", "display_name": "Other Primary"},
                {"id": "https://openalex.org/I1", "display_name": "Duke University"},
            ],
            "affiliations": [{
                "institution": {"id": "https://openalex.org/I1", "display_name": "Duke University"},
                "years": [2025, 2023, 2024],
            }],
        },
        {
            "id": "https://openalex.org/A2", "display_name": "Far Scholar",
            "works_count": 7, "cited_by_count": 500, "last_known_institutions": [
                {"id": "https://openalex.org/I1", "display_name": "Duke University"},
            ],
        },
        {
            "id": "https://openalex.org/A9", "display_name": "David Chalmers",
            "works_count": 20, "cited_by_count": 800, "last_known_institutions": [],
        },
    ]

    async def mock_find_path(backend, source_id, source_name, target_id, target_name, max_depth=6):
        if source_id == "A1":
            yield {
                "type": "result", "found": True, "hops": 1,
                "path": [
                    {
                        "author_id": "A1", "author_name": "Close Scholar",
                        "connection_to_next": "coauthor", "label": "Paper", "direction": None,
                    },
                    {
                        "author_id": "A9", "author_name": "David Chalmers",
                        "connection_to_next": None, "label": None, "direction": None,
                    },
                ],
            }
        else:
            yield {"type": "result", "found": False, "reason": "No path found"}

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"):
        mock_client.search_institutions = AsyncMock(return_value=([institution], 1))
        mock_client.search_authors = AsyncMock(return_value=([target], 1))
        mock_client.get_institution_authors = AsyncMock(return_value=candidates)
        mock_client.get_author = AsyncMock(side_effect=lambda author_id: {"id": author_id, "display_name": author_id})
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        mock_client.get_author = AsyncMock(return_value={"display_name": "David Chalmers"})
        mock_client.get_authors_batch = AsyncMock(return_value=[])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/institution-rank?institution=Duke%20University&target=David%20Chalmers"
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["institution"]["display_name"] == "Duke University"
    assert data["target"]["display_name"] == "David Chalmers"
    assert data["primary_only"] is False
    assert data["include_unconnected"] is False
    assert data["searched_count"] == 2
    assert data["unconnected_count"] == 1
    assert [r["author"]["id"] for r in data["results"]] == ["A1"]
    assert data["results"][0]["found"] is True
    assert data["results"][0]["hops"] == 1
    assert data["results"][0]["steps"][0]["label"] == "Paper"
    assert data["results"][0]["matched_institution"] == "Duke University"
    assert data["results"][0]["author"]["institution"] == "Other Primary"
    assert data["results"][0]["author"]["openalex_url"] == "https://openalex.org/A1"
    assert data["results"][0]["affiliation_evidence"] == {
        "institution_id": "I1",
        "display_name": "Duke University",
        "years": [2025, 2024, 2023],
        "openalex_url": "https://openalex.org/I1",
    }
    mock_client.get_institution_authors.assert_awaited_once_with(
        "I1", limit=15, sort="cited_by_count:desc"
    )


@pytest.mark.skip(reason="Replaced by graph-based institution suggestions")
async def test_institution_rank_primary_only_filters_non_primary_affiliations():
    institution = {
        "id": "I1", "display_name": "Duke University",
        "country_code": "US", "works_count": 100, "cited_by_count": 1000,
    }
    target = AuthorResult(id="A9", display_name="David Chalmers", works_count=20)
    candidates = [
        {
            "id": "https://openalex.org/A1", "display_name": "Former Duke Scholar",
            "works_count": 5, "cited_by_count": 20, "last_known_institutions": [
                {"id": "https://openalex.org/I9", "display_name": "Other Primary"},
                {"id": "https://openalex.org/I1", "display_name": "Duke University"},
            ],
        },
        {
            "id": "https://openalex.org/A2", "display_name": "Current Duke Scholar",
            "works_count": 7, "cited_by_count": 500, "last_known_institutions": [
                {"id": "https://openalex.org/I1", "display_name": "Duke University"},
            ],
        },
    ]

    async def mock_find_path(backend, source_id, source_name, target_id, target_name, max_depth=6):
        yield {"type": "result", "found": False, "reason": "No path found"}

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"):
        mock_client.search_institutions = AsyncMock(return_value=([institution], 1))
        mock_client.search_authors = AsyncMock(return_value=([target], 1))
        mock_client.get_institution_authors = AsyncMock(return_value=candidates)
        mock_client.get_author = AsyncMock(return_value={"display_name": "David Chalmers"})
        mock_client.get_authors_batch = AsyncMock(return_value=[])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/institution-rank?institution=Duke%20University"
                "&target=David%20Chalmers&primary_only=true"
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["primary_only"] is True
    assert data["searched_count"] == 1
    assert data["unconnected_count"] == 1
    assert data["results"] == []
    mock_client.get_institution_authors.assert_awaited_once_with(
        "I1", limit=60, sort="cited_by_count:desc"
    )


@pytest.mark.skip(reason="Replaced by graph-based institution suggestions")
async def test_institution_rank_can_include_unconnected_results():
    institution = {
        "id": "I1", "display_name": "Duke University",
        "country_code": "US", "works_count": 100, "cited_by_count": 1000,
    }
    target = AuthorResult(id="A9", display_name="David Chalmers", works_count=20)
    candidates = [
        {
            "id": "https://openalex.org/A2", "display_name": "Far Scholar",
            "works_count": 7, "cited_by_count": 500, "last_known_institutions": [
                {"id": "https://openalex.org/I1", "display_name": "Duke University"},
            ],
        },
    ]

    async def mock_find_path(backend, source_id, source_name, target_id, target_name, max_depth=6):
        yield {"type": "result", "found": False, "reason": "No path found"}

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"):
        mock_client.search_institutions = AsyncMock(return_value=([institution], 1))
        mock_client.search_authors = AsyncMock(return_value=([target], 1))
        mock_client.get_institution_authors = AsyncMock(return_value=candidates)
        mock_client.get_author = AsyncMock(return_value={"display_name": "David Chalmers"})
        mock_client.get_authors_batch = AsyncMock(return_value=[])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/institution-rank?institution=Duke%20University"
                "&target=David%20Chalmers&include_unconnected=true"
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["include_unconnected"] is True
    assert data["unconnected_count"] == 1
    assert [r["author"]["id"] for r in data["results"]] == ["A2"]
    assert data["results"][0]["found"] is False


async def test_institution_suggestions_use_all_author_origins_and_coauthors_only():
    institution = {"id": "I1", "display_name": "Duke University"}
    candidates = [{
        "id": "https://openalex.org/A1", "display_name": "Local Scholar",
        "works_count": 5, "cited_by_count": 20, "orcid": "https://orcid.org/0000",
        "topics": [{"display_name": "Machine learning"}],
        "last_known_institutions": [{"id": "https://openalex.org/I1", "display_name": "Duke University"}],
    }]

    async def mock_collect_path(
        backend, source_id, source_name, target_id, max_depth=6, *, to_name=None
    ):
        if target_id == "A3":
            return {
                "found": True, "hops": 2,
                "steps": [{"from_id": "A1", "to_id": "AB", "type": "coauthor"}],
            }
        return {"found": False, "hops": None, "steps": []}

    async def mock_verify(path, candidate_profile, origin_profile):
        if not path.get("found"):
            return None
        return {
            **path,
            "steps": [{
                "from_id": "A1", "from_name": "Local Scholar",
                "to_id": "A3", "to_name": "A3", "type": "coauthor",
                "label": "Verified paper", "title": "Verified paper",
                "direction": None, "work_id": "W1",
                "work_url": "https://openalex.org/W1",
                "publication_year": 2024, "evidence_verified": True,
            }],
            "path_verified": True,
            "evidence_quality": 1.0,
            "evidence_query_complete": True,
        }

    with patch("backend.app._client") as mock_client, \
         patch("backend.app._short_coauthor_paths", AsyncMock(return_value=({}, 0, True))), \
         patch("backend.app._collect_path_proposal", AsyncMock(side_effect=mock_collect_path)), \
         patch("backend.app._verify_deep_coauthor_path", AsyncMock(side_effect=mock_verify)), \
         patch("backend.app._make_backend") as make_backend:
        mock_client.get_institution_authors = AsyncMock(return_value=candidates)
        mock_client.get_author = AsyncMock(side_effect=lambda author_id: {
            "id": author_id, "display_name": author_id,
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        mock_client.get_coauthor_summary = AsyncMock(return_value={})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/institution-suggestions?institution_id=I1&institution=Duke"
                "&origin_ids=A2&origin_ids=A3"
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["origin_ids"] == ["A2", "A3"]
    assert data["results"][0]["closest_origin_id"] == "A3"
    assert data["results"][0]["author"]["topics"] == ["Machine learning"]
    make_backend.assert_called_once_with({"coauthor"})
    mock_client.get_institution_authors.assert_awaited_once_with(
        "I1", limit=80, sort="cited_by_count:desc"
    )


async def test_institution_suggestions_return_verified_two_hop_join():
    institution = {"id": "I1", "display_name": "Example University"}
    candidates = [
        {
            "id": "https://openalex.org/A1",
            "display_name": "Local One",
            "works_count": 3,
            "cited_by_count": 5,
            "last_known_institutions": [institution],
        },
        {
            "id": "https://openalex.org/A2",
            "display_name": "Local Two",
            "works_count": 4,
            "cited_by_count": 6,
            "last_known_institutions": [institution],
        },
    ]
    def verified_path(candidate_id, candidate_name, bridge_id, bridge_name, suffix):
        return {
            "found": True, "hops": 2, "closest_origin_id": "A0",
            "reachable_origin_count": 1, "path_verified": True,
            "evidence_quality": 1.0,
            "steps": [
                {
                    "from_id": candidate_id, "from_name": candidate_name,
                    "to_id": bridge_id, "to_name": bridge_name,
                    "type": "coauthor", "label": f"Paper {suffix}a",
                    "title": f"Paper {suffix}a", "direction": None,
                    "work_id": f"W{suffix}a", "work_url": f"https://openalex.org/W{suffix}a",
                    "publication_year": 2023, "evidence_verified": True,
                },
                {
                    "from_id": bridge_id, "from_name": bridge_name,
                    "to_id": "A0", "to_name": "Origin",
                    "type": "coauthor", "label": f"Paper {suffix}b",
                    "title": f"Paper {suffix}b", "direction": None,
                    "work_id": f"W{suffix}b", "work_url": f"https://openalex.org/W{suffix}b",
                    "publication_year": 2024, "evidence_verified": True,
                },
            ],
        }
    short_paths = {
        "A1": verified_path("A1", "Local One", "AB1", "Bridge One", "1"),
        "A2": verified_path("A2", "Local Two", "AB2", "Bridge Two", "2"),
    }

    with patch("backend.app._client") as mock_client, \
         patch("backend.app._short_coauthor_paths", AsyncMock(return_value=(short_paths, 0, True))), \
         patch("backend.app._make_backend") as make_backend:
        mock_client.get_institution_authors = AsyncMock(return_value=candidates)
        mock_client.get_authors_batch = AsyncMock(return_value=[{
            "id": "https://openalex.org/A0",
            "display_name": "Origin",
        }])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                "/api/institution-suggestions?institution_id=I1"
                "&institution=Example%20University&origin_ids=A0&limit=2"
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["search_strategy"] == "balanced_verified_two_hop_join"
    assert data["timeout_count"] == 0
    assert data["error_count"] == 0
    assert {result["author"]["id"] for result in data["results"]} == {
        "A1",
        "A2",
    }
    assert all(result["hops"] == 2 for result in data["results"])
    paths = {
        result["author"]["id"]: result["steps"]
        for result in data["results"]
    }
    assert paths["A1"][0]["to_name"] == "Bridge One"
    assert paths["A2"][0]["to_name"] == "Bridge Two"
    assert all(step["evidence_verified"] for result in data["results"] for step in result["steps"])
    make_backend.assert_not_called()


async def test_reviewed_affiliation_reserves_slot_and_returns_official_evidence():
    institution = {
        "id": "https://openalex.org/I6902469",
        "display_name": "Brandeis University",
    }
    high_citation_authors = [
        {
            "id": f"https://openalex.org/A{1000 + index}",
            "display_name": f"Highly Cited {index}",
            "works_count": 100,
            "cited_by_count": 100_000 - index,
            "last_known_institutions": [institution],
        }
        for index in range(30)
    ]
    katrina = {
        "id": "https://openalex.org/A5072773992",
        "display_name": "Conflated Katrina Record",
        "works_count": 19,
        "cited_by_count": 87,
        "orcid": "https://orcid.org/unreviewed",
        "topics": [{"display_name": "Unrelated topic"}],
        "last_known_institutions": [{
            "id": "https://openalex.org/I154248400",
            "display_name": "The University of Queensland",
        }],
    }

    with patch("backend.app._client") as mock_client:
        mock_client.get_authors_batch = AsyncMock(return_value=[katrina])
        merged = await _merge_affiliation_overrides(
            high_citation_authors,
            "I6902469",
            effective_pool=30,
        )

    assert len(merged) == 30
    assert merged[0]["id"].endswith("A5072773992")
    assert all(not author["id"].endswith("A1029") for author in merged)
    payload = _ranked_author_payload(
        merged[0],
        {"id": "I6902469", "display_name": "Brandeis University"},
        found=False,
        hops=None,
        steps=[],
        closest_origin_id=None,
        reachable_origin_count=0,
    )
    assert payload["author"]["institution"] == "Brandeis University"
    assert payload["author"]["display_name"] == "Katrina Elliott"
    assert payload["author"]["works_count"] == 6
    assert payload["author"]["cited_by_count"] == 0
    assert payload["author"]["metrics_scoped"] is True
    assert payload["author"]["orcid"] is None
    assert payload["author"]["topics"] == []
    assert payload["affiliation_evidence"]["source"] == "official_university"
    assert payload["affiliation_evidence"]["source_url"] == (
        "https://scholarworks.brandeis.edu/esploro/profile/katrina_elliott"
    )


async def test_conflated_reviewed_author_does_not_trust_summary_without_exact_work():
    candidate = {
        "id": "https://openalex.org/A5072773992",
        "display_name": "Katrina Elliott",
    }

    async def summary(author_id, verified_work_ids=None, limit=200):
        if author_id == "A5103215889":
            # The broad OpenAlex record has a bogus direct Katrina edge, while Marc
            # Lange is a genuine bridge present in her reviewed philosophy works.
            assert verified_work_ids is None
            return {
                "A5072773992": {"name": "Katrina Elliott", "works_count": 1},
                "A5051014644": {"name": "Marc Lange", "works_count": 2},
            }
        assert author_id == "A5072773992"
        assert set(verified_work_ids) == {
            "W2891784578", "W2800467463", "W2345731203",
            "W3204177509", "W3139199066", "W3136126418",
        }
        return {
            "A5051014644": {
                "name": "Marc Lange",
                "works_count": 1,
                "label": "Running it up the flagpole",
            },
        }

    with patch("backend.app._client") as mock_client:
        mock_client.get_coauthor_summary = AsyncMock(side_effect=summary)
        mock_client.get_authors_batch = AsyncMock(return_value=[{
            "id": "https://openalex.org/A5103215889",
            "display_name": "David Chalmers",
        }])
        mock_client.get_coauthor_links = AsyncMock(return_value=[])
        paths, errors, complete = await _short_coauthor_paths(
            [candidate],
            ["A5103215889"],
            max_depth=2,
        )

    assert errors == 0
    assert complete is True
    assert paths == {}
    assert mock_client.get_coauthor_links.await_count >= 2


def test_current_institution_can_be_any_last_known_affiliation():
    author = {
        "last_known_institutions": [
            {"id": "https://openalex.org/I2", "display_name": "First"},
            {"id": "https://openalex.org/I1", "display_name": "Second"},
        ],
    }
    assert _has_current_inst(author, "I1") is True


async def test_institution_suggestions_caps_origin_fanout():
    with patch("backend.app._client") as mock_client, \
         patch("backend.app._make_backend"):
        mock_client.get_institution_authors = AsyncMock(return_value=[])
        params = "&".join(f"origin_ids=A{index}" for index in range(12))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                f"/api/institution-suggestions?institution_id=I1&institution=Test&{params}"
            )

    data = resp.json()
    assert data["origin_ids"] == [f"A{index}" for index in range(10)]
    assert data["omitted_origin_count"] == 2


async def test_search_works_returns_results():
    mock_results = [WorkResult(id="W1", title="Paper One", author_names=["Alice"])]
    with patch("backend.app._client") as mock_client:
        mock_client.search_works = AsyncMock(return_value=(mock_results, 1))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/works?q=test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["id"] == "W1"
    assert data["results"][0]["author_names"] == ["Alice"]
    assert data["total"] == 1
    assert data["total_pages"] == 1


async def test_search_institutions_returns_results():
    mock_results = [{
        "id": "I1",
        "display_name": "Duke University",
        "country_code": "US",
        "works_count": 123,
        "cited_by_count": 456,
    }]
    with patch("backend.app._client") as mock_client:
        mock_client.search_institutions = AsyncMock(return_value=(mock_results, 1))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/institutions?q=Duke&page=2&per_page=20")
    assert resp.status_code == 200
    data = resp.json()
    mock_client.search_institutions.assert_awaited_once_with("Duke", page=2, per_page=20)
    assert data["results"][0]["display_name"] == "Duke University"
    assert data["page"] == 2
    assert data["total"] == 1


async def test_graph_expand_emits_work_node_for_work_origin():
    """Adding a work (not an author) as the new origin emits a type='work' node."""
    async def mock_expand_graph(*args, **kwargs):
        return
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_work = AsyncMock(return_value={
            "title": "Some Paper", "cited_by_count": 42, "publication_year": 2019,
        })
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=W1") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    lines = full_text.splitlines()
    node_events = []
    for i, line in enumerate(lines):
        if line.strip() == "event: node":
            node_events.append(json.loads(lines[i + 1][5:].strip()))

    assert len(node_events) == 1
    n = node_events[0]
    assert n["id"] == "W1"
    assert n["type"] == "work"
    assert n["name"] == "Some Paper"
    assert n["cited_by_count"] == 42
    assert n["publication_year"] == 2019
    mock_client.get_author.assert_not_called()


async def test_collect_path_to_work_endpoint():
    """An existing work origin as the path target dispatches through get_work, not get_author."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "authorship", "label": "Some Paper"},
        {"author_id": "W1", "author_name": "Some Paper", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        return
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_author = AsyncMock(return_value={
            "display_name": "Alice", "works_count": 5, "cited_by_count": 10,
            "last_known_institutions": [],
        })
        mock_client.get_work = AsyncMock(return_value={
            "title": "Some Paper", "cited_by_count": 99, "publication_year": 2018,
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=W1") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    lines = full_text.splitlines()
    node_events = []
    for i, line in enumerate(lines):
        if line.strip() == "event: node":
            node_events.append(json.loads(lines[i + 1][5:].strip()))

    work_nodes = [n for n in node_events if n["id"] == "W1"]
    assert len(work_nodes) == 1
    assert work_nodes[0]["type"] == "work"
    assert work_nodes[0]["cited_by_count"] == 99
    assert work_nodes[0]["publication_year"] == 2018


async def test_search_authors_requires_q():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/authors")
    assert resp.status_code == 422


async def test_path_sse_streams_events():
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "progress", "message": "Searching..."}
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/path?from=A1&to=A2") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    events = []
    for line in full_text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))

    assert any(e.get("type") == "progress" for e in events)
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert result_events[0]["found"] is True

    # Also check event: lines to verify SSE event type names
    event_type_lines = [ln for ln in full_text.splitlines() if ln.startswith("event:")]
    event_type_names = [ln.split(":", 1)[1].strip() for ln in event_type_lines]
    assert "progress" in event_type_names
    assert "result" in event_type_names


async def test_path_sse_passes_edge_types_to_backend():
    captured = {}

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": False, "reason": "No path found"}

    class CapturingBackend:
        def __init__(self, client, edge_types=None, **kwargs):
            captured["edge_types"] = edge_types

        async def get_neighbors(self, author_id):
            return []

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend", CapturingBackend):
        mock_client.get_author = AsyncMock(return_value={"display_name": "Alice"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/path?from=A1&to=A2&edges=coauthor&edges=institution") as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_text():
                    pass

    assert captured["edge_types"] == {"coauthor", "institution"}


async def test_graph_expand_emits_path_event():
    """Adding a researcher with an existing origin streams a `path` event carrying hops."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "progress", "message": "Searching..."}
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        # Empty async generator, so the test skips real neighborhood expansion.
        return
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        # _collect_path backfills path-node metadata via a batched author lookup.
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=A2") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    assert "event: path" in full_text

    path_events = []
    lines = full_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "event: path":
            data_line = lines[i + 1]
            assert data_line.startswith("data:")
            path_events.append(json.loads(data_line[5:].strip()))

    assert len(path_events) == 1
    pe = path_events[0]
    assert pe["found"] is True
    assert pe["hops"] == 1
    assert pe["from_id"] == "A1"
    assert pe["to_id"] == "A2"
    # The ordered shortest-path steps (names + paper) are included for the sidebar.
    assert pe["steps"] == [
        {
            "from_id": "A1", "from_name": "Alice",
            "to_id": "A2", "to_name": "Bob",
            "type": "coauthor", "label": "Paper", "direction": None,
        }
    ]


async def test_path_sse_yields_app_error_on_exception():
    with patch("backend.app._client") as mock_client:
        mock_client.get_author = AsyncMock(side_effect=RuntimeError("API down"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/path?from=A1&to=A2") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    assert "event: app_error" in full_text
    for line in full_text.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            if "message" in data:
                assert "API down" in data["message"]
                break


async def test_graph_expand_failure_still_emits_paths_and_app_error():
    """A phase-1 expansion failure flushes gathered path results before app_error."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        raise RuntimeError("store down")
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=A2") as resp:
                assert resp.status_code == 200
                chunks = [c async for c in resp.aiter_text()]

    full_text = "".join(chunks)
    assert "event: path" in full_text
    assert "event: app_error" in full_text
    assert "store down" in full_text
    assert "event: done" not in full_text
    assert full_text.index("event: path") < full_text.index("event: app_error")


async def test_graph_expand_stitch_suppresses_already_streamed_edges():
    """The final stitch receives the canonical keys of every edge already streamed."""
    async def mock_expand_graph(*args, **kwargs):
        yield {
            "type": "expansion", "depth": 1,
            "nodes": [{"id": "A9", "name": "Nine", "type": "expansion", "depth": 1}],
            "edges": [{"source": "A1", "target": "A9", "type": "coauthor", "label": "", "direction": None}],
        }

    stitch_mock = AsyncMock(return_value=[])
    with patch("backend.app._client") as mock_client, \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", stitch_mock):
        mock_client.get_author = AsyncMock(return_value={"display_name": "Alice"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1") as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_text():
                    pass

    stitch_mock.assert_awaited_once()
    emitted = stitch_mock.await_args.args[2]
    assert ("A1", "A9", "coauthor") in emitted


async def test_graph_expand_emits_path_as_soon_as_search_finishes():
    """Path results flush between phase-1 expansion events once the gather is done."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        for depth in (1, 2, 3):
            yield {"type": "expansion", "depth": depth, "nodes": [], "edges": []}
            await asyncio.sleep(0.05)

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=A2") as resp:
                assert resp.status_code == 200
                chunks = [c async for c in resp.aiter_text()]

    lines = "".join(chunks).splitlines()
    path_indices = [i for i, ln in enumerate(lines) if ln.strip() == "event: path"]
    expansion_indices = [i for i, ln in enumerate(lines) if ln.strip() == "event: expansion"]
    assert len(path_indices) == 1
    assert path_indices[0] < expansion_indices[-1]


async def test_clear_cache_wipes_author_lru_too():
    with patch("backend.app._client") as mock_client, \
         patch("backend.app._cache") as mock_cache:
        mock_cache.clear = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.delete("/api/cache")
    assert resp.status_code == 200
    mock_cache.clear.assert_awaited_once()
    mock_client.clear_author_cache.assert_called_once()

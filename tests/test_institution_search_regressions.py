"""End-to-end regressions for Institution Explorer's bounded verified search.

These tests intentionally exercise the contracts that were missed by the original
top-cited-30 / raw-author-ID implementation.  Candidate discovery may be approximate,
but a displayed path must be backed by exact works and response coverage must describe
what was actually checked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from backend.app import _short_coauthor_paths, _verify_deep_coauthor_path, app
from backend.institution_ranking import (
    choose_deeper_search_shortlist,
    merge_balanced_candidate_pool,
)


INSTITUTION = {
    "id": "I1",
    "display_name": "Example University",
}


def _topic(
    topic_id: str,
    name: str,
    *,
    field_id: str,
    field_name: str,
) -> dict:
    return {
        "id": f"https://openalex.org/{topic_id}",
        "display_name": name,
        "field": {
            "id": f"https://openalex.org/{field_id}",
            "display_name": field_name,
        },
    }


PHILOSOPHY = _topic(
    "T-PHIL",
    "Philosophy of mind",
    field_id="F-PHIL",
    field_name="Philosophy",
)
GENETICS = _topic(
    "T-GEN",
    "Population genetics",
    field_id="F-GEN",
    field_name="Genetics",
)


def _author(
    author_id: str,
    name: str | None = None,
    *,
    topic: dict | None = None,
    citations: int = 0,
    institution: bool = True,
    **extra,
) -> dict:
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": name or author_id,
        "works_count": 1,
        "cited_by_count": citations,
        "topics": [topic] if topic else [],
        "last_known_institutions": [INSTITUTION] if institution else [],
        **extra,
    }


def _edge(
    left_id: str,
    left_name: str,
    right_id: str,
    right_name: str,
    work_id: str,
    *,
    topic: dict,
    title: str | None = None,
) -> dict:
    compact_topic = {
        "id": topic["id"].rsplit("/", 1)[-1],
        "name": topic["display_name"],
        "field_id": topic["field"]["id"].rsplit("/", 1)[-1],
        "field_name": topic["field"]["display_name"],
    }
    title = title or f"Evidence {work_id}"
    return {
        "left_id": left_id,
        "left_name": left_name,
        "right_id": right_id,
        "right_name": right_name,
        "work_id": work_id,
        "title": title,
        "label": title,
        "publication_year": 2024,
        "author_count": 2,
        "author_ids": [left_id, right_id],
        "topics": [compact_topic],
        "left_authorship": {
            "author_id": left_id,
            "display_name": left_name,
            "raw_author_name": left_name,
            "raw_affiliation_strings": [],
            "institution_ids": [],
            "institution_names": [],
            "orcid": None,
        },
        "right_authorship": {
            "author_id": right_id,
            "display_name": right_name,
            "raw_author_name": right_name,
            "raw_affiliation_strings": [],
            "institution_ids": [],
            "institution_names": [],
            "orcid": None,
        },
    }


def _verified_step(edge: dict) -> dict:
    work_id = edge["work_id"]
    return {
        "from_id": edge["left_id"],
        "from_name": edge["left_name"],
        "to_id": edge["right_id"],
        "to_name": edge["right_name"],
        "type": "coauthor",
        "label": edge["title"],
        "title": edge["title"],
        "direction": None,
        "work_id": work_id,
        "work_url": f"https://openalex.org/{work_id}",
        "publication_year": edge["publication_year"],
        "evidence_verified": True,
    }


def _ids(records: list[dict]) -> list[str]:
    return [record["id"].rsplit("/", 1)[-1] for record in records]


def test_balanced_pool_keeps_low_citation_topical_author_beyond_top_fifty():
    origin = _author(
        "A-ORIGIN",
        "Origin Philosopher",
        topic=PHILOSOPHY,
        institution=False,
    )
    citation_lane = [
        _author(f"A-{index:03d}", citations=100_000 - index)
        for index in range(1, 52)
    ]
    topical_author = _author(
        "A-051",
        "Low Citation Philosopher",
        topic=PHILOSOPHY,
        citations=1,
    )

    pool = merge_balanced_candidate_pool(
        [],
        [topical_author],
        citation_lane,
        origins=[origin],
        pool_size=50,
        topical_fraction=0.4,
    )

    assert len(pool) == 50
    assert "A-051" in _ids(pool)
    assert "A-050" not in _ids(pool)
    kept = next(author for author in pool if author["id"].endswith("A-051"))
    assert kept["_candidate_sources"] == ("topic", "citation")
    assert kept["_topic_similarity"] == 1.0


async def test_two_hop_path_requires_exact_work_evidence_on_both_legs():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Local Philosopher", topic=PHILOSOPHY)
    candidate_bridge = _edge(
        "A-BRIDGE",
        "Bridge Scholar",
        "A-CANDIDATE",
        "Local Philosopher",
        "W-CANDIDATE-BRIDGE",
        topic=PHILOSOPHY,
    )

    async def links(left_ids, right_ids):
        requested = set(left_ids) | set(right_ids)
        if {"A-BRIDGE", "A-CANDIDATE"} <= requested:
            return [candidate_bridge]
        # The origin summary proposed A-BRIDGE, but the exact origin/bridge works
        # query found nothing.  That proposal must never become a displayed leg.
        return []

    with patch("backend.app._client") as client:
        client.get_authors_batch = AsyncMock(return_value=[origin])
        client.get_coauthor_summary = AsyncMock(return_value={
            "A-BRIDGE": {"name": "Bridge Scholar", "works_count": 3},
        })
        client.get_coauthor_links = AsyncMock(side_effect=links)

        paths, errors, complete = await _short_coauthor_paths(
            [candidate], ["A-ORIGIN"], max_depth=2
        )

    assert paths == {}
    assert errors == 0
    assert complete is True
    assert any(
        {"A-ORIGIN", "A-BRIDGE"} <= (set(call.args[0]) | set(call.args[1]))
        for call in client.get_coauthor_links.await_args_list
    )


async def test_two_hop_path_rejects_merged_cross_domain_intermediary():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Local Geneticist", topic=GENETICS)
    philosophy_leg = _edge(
        "A-ORIGIN",
        "Origin Philosopher",
        "A-MERGED",
        "David Manley",
        "W-PHILOSOPHY",
        topic=PHILOSOPHY,
    )
    genetics_leg = _edge(
        "A-MERGED",
        "David Manley",
        "A-CANDIDATE",
        "Local Geneticist",
        "W-GENETICS",
        topic=GENETICS,
    )

    async def links(left_ids, right_ids):
        requested = set(left_ids) | set(right_ids)
        if {"A-ORIGIN", "A-CANDIDATE"} <= requested:
            return []
        if {"A-MERGED", "A-CANDIDATE"} <= requested:
            return [genetics_leg]
        if {"A-ORIGIN", "A-MERGED"} <= requested:
            return [philosophy_leg]
        return []

    with patch("backend.app._client") as client:
        client.get_authors_batch = AsyncMock(return_value=[origin])
        client.get_coauthor_summary = AsyncMock(return_value={
            "A-MERGED": {"name": "David Manley", "works_count": 2},
        })
        client.get_coauthor_links = AsyncMock(side_effect=links)

        paths, errors, complete = await _short_coauthor_paths(
            [candidate], ["A-ORIGIN"], max_depth=2
        )

    assert paths == {}
    assert errors == 0
    assert complete is True


async def test_deeper_bfs_proposal_also_rejects_merged_cross_domain_intermediary():
    """The slow fallback may propose IDs, but exact works still decide identity."""
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Local Geneticist", topic=GENETICS)
    philosophy_leg = _edge(
        "A-MERGED",
        "David Manley",
        "A-ORIGIN",
        "Origin Philosopher",
        "W-PHILOSOPHY",
        topic=PHILOSOPHY,
    )
    genetics_leg = _edge(
        "A-CANDIDATE",
        "Local Geneticist",
        "A-MERGED",
        "David Manley",
        "W-GENETICS",
        topic=GENETICS,
    )
    bfs_proposal = {
        "found": True,
        "hops": 2,
        "steps": [
            {
                "from_id": "A-CANDIDATE",
                "from_name": "Local Geneticist",
                "to_id": "A-MERGED",
                "to_name": "David Manley",
                "type": "coauthor",
            },
            {
                "from_id": "A-MERGED",
                "from_name": "David Manley",
                "to_id": "A-ORIGIN",
                "to_name": "Origin Philosopher",
                "type": "coauthor",
            },
        ],
    }

    with patch("backend.app._client") as client:
        client.get_coauthor_links = AsyncMock(
            return_value=[genetics_leg, philosophy_leg]
        )
        verified = await _verify_deep_coauthor_path(
            bfs_proposal,
            candidate,
            origin,
        )

    assert verified is None


async def test_reviewed_identity_scope_filters_exact_edges_to_reviewed_works():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-REVIEWED", "Reviewed Philosopher", topic=PHILOSOPHY)
    bogus = _edge(
        "A-REVIEWED",
        "Reviewed Philosopher",
        "A-ORIGIN",
        "Origin Philosopher",
        "W-BOGUS",
        topic=PHILOSOPHY,
        title="Conflated record paper",
    )
    reviewed = _edge(
        "A-REVIEWED",
        "Reviewed Philosopher",
        "A-ORIGIN",
        "Origin Philosopher",
        "W-REVIEWED",
        topic=PHILOSOPHY,
        title="Reviewed identity paper",
    )

    def scope(author_id):
        return frozenset({"W-REVIEWED"}) if author_id == "A-REVIEWED" else None

    with patch("backend.app._client") as client, patch(
        "backend.app.get_verified_work_ids", side_effect=scope
    ):
        client.get_authors_batch = AsyncMock(return_value=[origin])
        client.get_coauthor_summary = AsyncMock(return_value={
            "A-REVIEWED": {"name": "Reviewed Philosopher", "works_count": 2},
        })
        client.get_coauthor_links = AsyncMock(return_value=[bogus, reviewed])

        paths, errors, complete = await _short_coauthor_paths(
            [candidate], ["A-ORIGIN"], max_depth=2
        )

    assert errors == 0
    assert complete is True
    path = paths["A-REVIEWED"]
    assert path["hops"] == 1
    assert path["steps"][0]["work_id"] == "W-REVIEWED"
    assert path["steps"][0]["title"] == "Reviewed identity paper"
    assert path["steps"][0]["evidence_verified"] is True


def test_deeper_shortlist_is_bounded_and_not_citation_ranked():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidates = [
        _author(f"A-CITED-{index:03d}", citations=1_000_000 - index)
        for index in range(150)
    ]
    candidates.extend([
        _author("A-TOPICAL-2", topic=PHILOSOPHY, citations=0),
        _author("A-TOPICAL-1", topic=PHILOSOPHY, citations=1),
    ])

    shortlist = choose_deeper_search_shortlist(
        candidates,
        origins=[origin],
        limit=10,
    )

    assert len(shortlist) == 10
    assert _ids(shortlist)[:2] == ["A-TOPICAL-1", "A-TOPICAL-2"]
    assert "A-CITED-000" in _ids(shortlist)


async def test_two_short_results_do_not_skip_bounded_deeper_search():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidates = [
        _author(f"A-CANDIDATE-{index}", topic=PHILOSOPHY, citations=100 - index)
        for index in range(3)
    ]
    short_paths = {
        f"A-CANDIDATE-{index}": {
            "found": True,
            "hops": 1,
            "steps": [_verified_step(_edge(
                f"A-CANDIDATE-{index}",
                f"A-CANDIDATE-{index}",
                "A-ORIGIN",
                "Origin Philosopher",
                f"W-SHORT-{index}",
                topic=PHILOSOPHY,
            ))],
            "closest_origin_id": "A-ORIGIN",
            "reachable_origin_count": 1,
            "path_verified": True,
            "evidence_quality": 1.0,
        }
        for index in range(2)
    }

    async def no_deep_path(*args, **kwargs):
        return {"found": False, "hops": None, "steps": []}

    with patch("backend.app._client") as client, patch(
        "backend.app._short_coauthor_paths",
        AsyncMock(return_value=(short_paths, 0, True)),
    ), patch("backend.app._collect_path_proposal", AsyncMock(side_effect=no_deep_path)) as collect, patch(
        "backend.app._make_backend"
    ):
        client.get_institution_authors = AsyncMock(return_value=candidates)
        client.get_institution_authors_by_topics = AsyncMock(return_value=candidates)
        client.get_institution_authors_by_hierarchy = AsyncMock(return_value=candidates)
        client.get_authors_batch = AsyncMock(return_value=[origin])
        client.get_coauthor_summary = AsyncMock(return_value={})

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(
                "/api/institution-suggestions",
                params={
                    "institution_id": "I1",
                    "institution": "Example University",
                    "origin_ids": "A-ORIGIN",
                    "limit": 10,
                },
            )

    assert response.status_code == 200
    data = response.json()
    assert collect.await_count >= 1
    assert data["deeper_search_started_count"] == 1
    assert data["deeper_search_completed_count"] == 1
    assert data["deeper_search_skipped_count"] == 0
    assert data["candidate_pool_requested"] >= 3
    assert data["candidate_pool_effective"] == 3
    assert data["short_checked_count"] == 3
    assert data["coverage_complete"] is True
    assert all(result["path_verified"] is True for result in data["results"])
    assert all(result["verified_hops"] == 1 for result in data["results"])


async def test_failed_origin_query_never_claims_complete_coverage():
    origin_one = _author(
        "A-ORIGIN-1", "Origin One", topic=PHILOSOPHY, institution=False
    )
    origin_two = _author(
        "A-ORIGIN-2", "Origin Two", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Local Philosopher", topic=PHILOSOPHY)

    async def summary(author_id, verified_work_ids=None):
        if author_id == "A-ORIGIN-2":
            raise RuntimeError("one origin query failed")
        return {}

    with patch("backend.app._client") as client:
        client.get_coauthor_summary = AsyncMock(side_effect=summary)
        client.get_authors_batch = AsyncMock(return_value=[origin_one, origin_two])
        client.get_coauthor_links = AsyncMock(return_value=[])

        paths, errors, complete = await _short_coauthor_paths(
            [candidate], ["A-ORIGIN-1", "A-ORIGIN-2"], max_depth=2
        )

    assert paths == {}
    assert errors == 1
    assert complete is False

    with patch("backend.app._client") as client, patch(
        "backend.app._short_coauthor_paths",
        AsyncMock(return_value=({}, errors, complete)),
    ), patch("backend.app._collect_path_proposal", AsyncMock(side_effect=RuntimeError("failed"))), patch(
        "backend.app._make_backend"
    ):
        client.get_institution_authors = AsyncMock(return_value=[candidate])
        client.get_institution_authors_by_topics = AsyncMock(return_value=[candidate])
        client.get_institution_authors_by_hierarchy = AsyncMock(return_value=[candidate])
        client.get_authors_batch = AsyncMock(return_value=[origin_one, origin_two])
        client.get_coauthor_summary = AsyncMock(return_value={})

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(
                "/api/institution-suggestions",
                params=[
                    ("institution_id", "I1"),
                    ("institution", "Example University"),
                    ("origin_ids", "A-ORIGIN-1"),
                    ("origin_ids", "A-ORIGIN-2"),
                ],
            )

    assert response.status_code == 200
    data = response.json()
    assert data["error_count"] >= 1
    assert data["coverage_complete"] is False
    assert data["deeper_search_started_count"] == 1
    assert data["deeper_search_completed_count"] <= data["deeper_search_started_count"]
    assert data["candidate_source_counts"] == {
        "reviewed": 0,
        "topic": 1,
        "citation": 1,
    }


async def test_origin_summary_coauthor_cannot_self_validate_a_merged_identity():
    origin = _author(
        "A-ORIGIN", "Alex Smith", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Genetics Collaborator", topic=GENETICS)
    cross_domain_edge = _edge(
        "A-CANDIDATE",
        "Genetics Collaborator",
        "A-ORIGIN",
        "Alex Smith",
        "W-GENETICS",
        topic=GENETICS,
    )

    # The proposal summary is derived from the same potentially conflated author
    # record. It must not become independent identity evidence for the exact edge.
    with patch("backend.app._client") as client:
        client.get_coauthor_summary = AsyncMock(return_value={
            "A-CANDIDATE": {"name": "Genetics Collaborator", "works_count": 1}
        })
        client.get_authors_batch = AsyncMock(return_value=[origin])
        client.get_coauthor_links = AsyncMock(return_value=[cross_domain_edge])

        paths, errors, complete = await _short_coauthor_paths(
            [candidate], ["A-ORIGIN"], max_depth=1
        )

    assert paths == {}
    assert errors == 0
    assert complete is True


async def test_short_scan_exception_reports_zero_checked_candidates():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Local Philosopher", topic=PHILOSOPHY)

    with patch("backend.app._client") as client, patch(
        "backend.app._short_coauthor_paths",
        AsyncMock(side_effect=RuntimeError("exact scan failed")),
    ):
        client.get_institution_authors = AsyncMock(return_value=[candidate])
        client.get_institution_authors_by_topics = AsyncMock(return_value=[candidate])
        client.get_institution_authors_by_hierarchy = AsyncMock(return_value=[candidate])
        client.get_authors_batch = AsyncMock(return_value=[origin])

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(
                "/api/institution-suggestions",
                params={
                    "institution_id": "I1",
                    "institution": "Example University",
                    "origin_ids": "A-ORIGIN",
                    "max_depth": 2,
                },
            )

    assert response.status_code == 200
    data = response.json()
    assert data["short_checked_count"] == 0
    assert data["searched_count"] == 0
    assert data["coverage_complete"] is False
    assert data["error_count"] >= 1
    assert "one/two-hop scan did not complete" in data["coverage_note"]


async def test_total_budget_also_bounds_candidate_discovery():
    async def never_returns(*args, **kwargs):
        await asyncio.Event().wait()

    with patch("backend.app._client") as client, patch(
        "backend.app.RANK_TOTAL_TIMEOUT_S", 0.08
    ), patch("backend.app.RANK_DISCOVERY_TIMEOUT_S", 0.04), patch(
        "backend.app.RANK_ORIGIN_PROFILE_TIMEOUT_S", 0.02
    ):
        client.get_institution_authors = AsyncMock(side_effect=never_returns)
        client.get_authors_batch = AsyncMock(side_effect=never_returns)

        started = asyncio.get_running_loop().time()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(
                "/api/institution-suggestions",
                params={
                    "institution_id": "I1",
                    "institution": "Example University",
                    "origin_ids": "A-ORIGIN",
                },
            )
        elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 200
    assert elapsed < 0.3
    assert response.json()["timeout_count"] >= 2


async def test_incomplete_deep_ring_is_not_counted_as_completed_coverage():
    origin = _author(
        "A-ORIGIN", "Origin Philosopher", topic=PHILOSOPHY, institution=False
    )
    candidate = _author("A-CANDIDATE", "Local Philosopher", topic=PHILOSOPHY)

    with patch("backend.app._client") as client, patch(
        "backend.app._short_coauthor_paths",
        AsyncMock(return_value=({}, 0, True)),
    ), patch(
        "backend.app._collect_path_proposal",
        AsyncMock(return_value={
            "found": False,
            "hops": None,
            "steps": [],
            "search_complete": False,
        }),
    ), patch("backend.app._make_backend"):
        client.get_institution_authors = AsyncMock(return_value=[candidate])
        client.get_institution_authors_by_topics = AsyncMock(return_value=[candidate])
        client.get_institution_authors_by_hierarchy = AsyncMock(return_value=[candidate])
        client.get_authors_batch = AsyncMock(return_value=[origin])
        client.get_coauthor_summary = AsyncMock(return_value={})

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(
                "/api/institution-suggestions",
                params={
                    "institution_id": "I1",
                    "institution": "Example University",
                    "origin_ids": "A-ORIGIN",
                },
            )

    data = response.json()
    assert data["deeper_search_started_count"] == 1
    assert data["deeper_search_completed_count"] == 0
    assert data["coverage_complete"] is False
    assert "0 unmatched candidates completed" in data["coverage_note"]

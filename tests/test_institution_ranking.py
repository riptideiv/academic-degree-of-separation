from copy import deepcopy

import pytest

from backend.institution_ranking import (
    TopicMetadata,
    choose_deeper_search_shortlist,
    extract_topic_metadata,
    merge_balanced_candidate_pool,
    stable_result_sort_key,
    topic_similarity,
)


def author(
    author_id: str,
    *,
    topic_id: str | None = None,
    topic_name: str | None = None,
    subfield_id: str | None = None,
    subfield_name: str | None = None,
    field_id: str | None = None,
    field_name: str | None = None,
    citations: int = 0,
    **extra,
):
    result = {
        "id": f"https://openalex.org/{author_id}",
        "display_name": f"Author {author_id}",
        "cited_by_count": citations,
        **extra,
    }
    if any((topic_id, topic_name, subfield_id, subfield_name, field_id, field_name)):
        topic = {
            "id": f"https://openalex.org/{topic_id}" if topic_id else None,
            "display_name": topic_name,
            "subfield": {
                "id": f"https://openalex.org/subfields/{subfield_id}"
                if subfield_id
                else None,
                "display_name": subfield_name,
            },
            "field": {
                "id": f"https://openalex.org/fields/{field_id}"
                if field_id
                else None,
                "display_name": field_name,
            },
        }
        result["topics"] = [topic]
    return result


PHILOSOPHY_ORIGIN = author(
    "A_ORIGIN",
    topic_id="T1",
    topic_name=" Philosophy of Mind ",
    subfield_id="1211",
    subfield_name="Philosophy",
    field_id="12",
    field_name="Arts and Humanities",
)


def ids(records):
    return [record["id"].rstrip("/").rsplit("/", 1)[-1] for record in records]


def test_extract_topic_metadata_normalizes_full_hierarchy_and_legacy_strings():
    profile = deepcopy(PHILOSOPHY_ORIGIN)
    profile["topics"].append("  Consciousness\tStudies ")
    profile["subfields"] = [{"id": "S2", "display_name": " Cognitive Science "}]
    profile["fields"] = [{"id": "F2", "display_name": " Psychology "}]

    metadata = extract_topic_metadata(profile)

    assert metadata.topic_ids == frozenset({"T1"})
    assert metadata.topic_names == frozenset(
        {"philosophy of mind", "consciousness studies"}
    )
    assert metadata.subfield_ids == frozenset({"1211", "S2"})
    assert metadata.subfield_names == frozenset(
        {"philosophy", "cognitive science"}
    )
    assert metadata.field_ids == frozenset({"12", "F2"})
    assert metadata.field_names == frozenset(
        {"arts and humanities", "psychology"}
    )


def test_extract_topic_metadata_tolerates_missing_and_malformed_data():
    assert extract_topic_metadata(None).is_empty
    assert extract_topic_metadata({}).is_empty
    assert extract_topic_metadata({"topics": [None, 7, {}]}).is_empty
    assert extract_topic_metadata(TopicMetadata(topic_names=frozenset({"ethics"}))) == (
        TopicMetadata(topic_names=frozenset({"ethics"}))
    )


def test_topic_similarity_is_weighted_bounded_and_specificity_sensitive():
    exact = deepcopy(PHILOSOPHY_ORIGIN)
    field_only = author(
        "A_FIELD",
        topic_id="T_OTHER",
        topic_name="Ancient history",
        subfield_id="S_OTHER",
        subfield_name="History",
        field_id="12",
        field_name="Arts and Humanities",
    )

    assert topic_similarity(exact, [PHILOSOPHY_ORIGIN]) == pytest.approx(1.0)
    broad_score = topic_similarity(field_only, [PHILOSOPHY_ORIGIN])
    assert 0.0 < broad_score < 1.0
    assert topic_similarity({}, [PHILOSOPHY_ORIGIN]) == 0.0
    assert topic_similarity(exact, []) == 0.0


def test_topic_similarity_matches_any_of_multiple_origins_without_dilution():
    physics = author(
        "A_PHYSICS",
        topic_id="T9",
        topic_name="Radio astronomy",
        subfield_id="S9",
        subfield_name="Astronomy",
        field_id="F9",
        field_name="Physics",
    )
    candidate = deepcopy(physics)
    candidate["id"] = "https://openalex.org/A_CANDIDATE"

    assert topic_similarity(candidate, [PHILOSOPHY_ORIGIN, physics]) == 1.0
    assert topic_similarity(candidate, [PHILOSOPHY_ORIGIN]) == 0.0


def test_low_citation_topical_candidate_31_survives_balanced_cutoff():
    citation_candidates = [
        author(f"A{index:02d}", citations=10_000 - index)
        for index in range(1, 32)
    ]
    # This is the shape that used to fail: a direct/topical author sat just below
    # the citation cutoff, despite already carrying useful path-discovery data.
    topical_31 = author(
        "A31",
        topic_id="T1",
        topic_name="Philosophy of Mind",
        subfield_id="1211",
        subfield_name="Philosophy",
        field_id="12",
        field_name="Arts and Humanities",
        citations=1,
        direct_path={"hops": 1, "work_id": "W_DIRECT"},
    )

    pool = merge_balanced_candidate_pool(
        [],
        [topical_31],
        citation_candidates,
        origins=[PHILOSOPHY_ORIGIN],
        pool_size=30,
    )

    assert len(pool) == 30
    assert "A31" in ids(pool)
    assert "A30" not in ids(pool)
    kept = next(record for record in pool if record["id"].endswith("A31"))
    assert kept["direct_path"]["work_id"] == "W_DIRECT"
    assert kept["_topic_similarity"] == 1.0


def test_pool_deduplicates_sources_excludes_origins_and_does_not_mutate_inputs():
    reviewed = [{"id": "A1", "display_name": "Reviewed Name", "topics": []}]
    topical = [
        author("A1", topic_id="T1", topic_name="Philosophy of Mind"),
        author("A_ORIGIN", topic_id="T1", topic_name="Philosophy of Mind"),
    ]
    citations = [author("A1", citations=999), author("A2", citations=500)]
    before = deepcopy((reviewed, topical, citations))

    pool = merge_balanced_candidate_pool(
        reviewed,
        topical,
        citations,
        origins=[PHILOSOPHY_ORIGIN],
        origin_ids=["https://openalex.org/A_ORIGIN"],
        pool_size=5,
    )

    assert ids(pool) == ["A1", "A2"]
    assert pool[0]["display_name"] == "Reviewed Name"
    assert pool[0]["topics"][0]["id"].endswith("T1")
    assert pool[0]["_candidate_sources"] == ("reviewed", "topic", "citation")
    assert pool[0]["_reviewed_candidate"] is True
    assert (reviewed, topical, citations) == before


def test_reviewed_candidates_are_never_dropped_even_when_they_exceed_pool():
    reviewed = [author(f"A{index}") for index in range(5, 0, -1)]

    pool = merge_balanced_candidate_pool(
        reviewed, [], [author("A99", citations=1_000_000)], pool_size=3
    )

    assert ids(pool) == ["A1", "A2", "A3", "A4", "A5"]
    assert all(item["_reviewed_candidate"] for item in pool)


def test_balanced_pool_is_stable_when_all_input_lanes_are_reversed():
    reviewed = [author("A8"), author("A7")]
    topical = [
        author("A4", topic_similarity=0.4),
        author("A3", topic_similarity=0.8),
        author("A8", topic_similarity=0.1),
    ]
    citations = [
        author("A1", citations=100),
        author("A2", citations=200),
        author("A3", citations=5),
    ]

    forward = merge_balanced_candidate_pool(
        reviewed, topical, citations, pool_size=5, topical_fraction=0.4
    )
    reverse = merge_balanced_candidate_pool(
        reversed(reviewed),
        reversed(topical),
        reversed(citations),
        pool_size=5,
        topical_fraction=0.4,
    )

    assert forward == reverse
    assert ids(forward) == ["A8", "A7", "A3", "A4", "A2"]


def test_balanced_pool_handles_missing_topics_and_validates_bounds():
    pool = merge_balanced_candidate_pool(
        [], [{"id": "A1"}], [{"id": "A2", "cited_by_count": "bad"}], pool_size=2
    )
    assert set(ids(pool)) == {"A1", "A2"}
    assert all(item["_topic_similarity"] == 0.0 for item in pool)
    with pytest.raises(ValueError):
        merge_balanced_candidate_pool([], [], [], pool_size=-1)
    with pytest.raises(ValueError):
        merge_balanced_candidate_pool([], [], [], topical_fraction=1.1)


def test_stable_result_sort_key_uses_verified_path_reach_and_relevance_order():
    results = [
        {
            "author": {"id": "A4", "cited_by_count": 999_999},
            "hops": 2,
            "path_verified": False,
            "reachable_origin_count": 9,
            "topic_similarity": 1,
        },
        {
            "author": {"id": "A3"},
            "verified_hops": 3,
            "reachable_origin_count": 9,
            "topic_similarity": 1,
        },
        {
            "author": {"id": "A2"},
            "verified_hops": 2,
            "reachable_origin_count": 1,
            "topic_similarity": 0.9,
            "evidence_quality": 0.5,
        },
        {
            "author": {"id": "A1"},
            "verified_hops": 2,
            "reachable_origin_count": 2,
            "topic_similarity": 0.1,
            "evidence_quality": 0.1,
        },
    ]

    assert [
        item["author"]["id"]
        for item in sorted(reversed(results), key=stable_result_sort_key)
    ] == ["A1", "A2", "A3", "A4"]


def test_stable_result_sort_key_never_uses_citations_as_tiebreaker():
    low_cited = {
        "author": {"id": "A1", "cited_by_count": 0},
        "verified_hops": 2,
        "reachable_origin_count": 1,
        "topic_similarity": 0.5,
        "evidence_quality": 1,
    }
    high_cited = deepcopy(low_cited)
    high_cited["author"] = {"id": "A2", "cited_by_count": 10_000_000}

    assert sorted([high_cited, low_cited], key=stable_result_sort_key) == [
        low_cited,
        high_cited,
    ]


def test_deeper_shortlist_excludes_matches_and_prefers_topics_but_keeps_reviewed():
    candidates = [
        author("A_ORIGIN", topic_similarity=1),
        author("A_MATCHED", topic_similarity=1),
        author("A_TOPICAL", topic_similarity=0.95, citations=1),
        author("A_MIDDLE", topic_similarity=0.4, citations=2),
        author("A_CITED", topic_similarity=0, citations=999_999),
        author("A_REVIEWED", topic_similarity=0, citations=0),
    ]

    shortlist = choose_deeper_search_shortlist(
        candidates,
        origins=[PHILOSOPHY_ORIGIN],
        matched_ids=["A_MATCHED"],
        reviewed_ids=["A_REVIEWED"],
        limit=3,
    )

    assert ids(shortlist) == ["A_REVIEWED", "A_TOPICAL", "A_MIDDLE"]
    assert "A_ORIGIN" not in ids(shortlist)
    assert "A_MATCHED" not in ids(shortlist)


def test_deeper_shortlist_is_stable_supports_multiple_origins_and_reviewed_overflow():
    physics = author(
        "A_PHYSICS_ORIGIN", topic_id="T9", topic_name="Radio astronomy"
    )
    candidates = [
        author("A3", topic_id="T9", topic_name="Radio astronomy"),
        author("A2", topic_id="T1", topic_name="Philosophy of Mind"),
        author("A1"),
    ]

    forward = choose_deeper_search_shortlist(
        candidates,
        origins=[PHILOSOPHY_ORIGIN, physics],
        reviewed_ids=["A1", "A2"],
        limit=1,
    )
    reverse = choose_deeper_search_shortlist(
        reversed(candidates),
        origins=[physics, PHILOSOPHY_ORIGIN],
        reviewed_ids=["A2", "A1"],
        limit=1,
    )

    assert forward == reverse
    assert ids(forward) == ["A2", "A1"]
    assert forward[0]["_topic_similarity"] > 0


def test_deeper_shortlist_validates_limit():
    with pytest.raises(ValueError):
        choose_deeper_search_shortlist([], limit=-1)

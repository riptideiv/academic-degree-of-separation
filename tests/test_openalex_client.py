import pytest
import respx
import httpx
from backend.openalex_client import OpenAlexClient, _short_id, _FILTER_CHUNK, _chunks


@pytest.fixture
def api_key_file(monkeypatch):
    """Keep the historical fixture name while configuring the supported env source."""
    monkeypatch.setenv("OPENALEX_KEY", "test_key")


def test_short_id():
    assert _short_id("https://openalex.org/A12345") == "A12345"
    assert _short_id("https://openalex.org/W99999") == "W99999"


@respx.mock
async def test_search_authors(api_key_file):
    respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/A123",
                    "display_name": "Alice Smith",
                    "last_known_institutions": [{"display_name": "MIT"}],
                    "works_count": 42,
                }
            ]
        })
    )
    client = OpenAlexClient()
    results, total = await client.search_authors("Alice")
    assert len(results) == 1
    assert results[0].id == "A123"
    assert results[0].display_name == "Alice Smith"
    assert results[0].institution == "MIT"
    assert results[0].works_count == 42
    assert total == 1


@respx.mock
async def test_search_authors_no_institution(api_key_file):
    respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/A456",
                    "display_name": "Bob Jones",
                    "last_known_institutions": [],
                    "works_count": 5,
                }
            ]
        })
    )
    client = OpenAlexClient()
    results, _total = await client.search_authors("Bob")
    assert results[0].institution is None


@respx.mock
async def test_search_authors_pagination_params(api_key_file):
    route = respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={"results": [], "meta": {"count": 0}})
    )
    client = OpenAlexClient()
    await client.search_authors("Alice", page=3, per_page=20)
    request = route.calls.last.request
    params = dict(httpx.QueryParams(request.url.query))
    assert params["page"] == "3"
    assert params["per_page"] == "20"


@respx.mock
async def test_get_author(api_key_file):
    respx.get("https://api.openalex.org/authors/A123").mock(
        return_value=httpx.Response(200, json={
            "id": "https://openalex.org/A123",
            "display_name": "Alice Smith",
            "last_known_institutions": [{"id": "https://openalex.org/I1", "display_name": "MIT"}],
        })
    )
    client = OpenAlexClient()
    data = await client.get_author("A123")
    assert data["display_name"] == "Alice Smith"


@respx.mock
async def test_get_author_works(api_key_file):
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "Paper One",
                    "cited_by_count": 100,
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A123", "display_name": "Alice"}}
                    ],
                }
            ]
        })
    )
    client = OpenAlexClient()
    works = await client.get_author_works("A123")
    assert len(works) == 1
    assert works[0]["title"] == "Paper One"


@respx.mock
async def test_search_does_not_retry_on_429(api_key_file):
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(429)

    respx.get("https://api.openalex.org/authors").mock(side_effect=side_effect)
    client = OpenAlexClient()
    import pytest
    with pytest.raises(httpx.HTTPStatusError):
        await client.search_authors("test")
    assert call_count == 1


@respx.mock
async def test_get_citing_works(api_key_file):
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"id": "https://openalex.org/W2", "title": "Citing Paper", "authorships": []}
            ]
        })
    )
    client = OpenAlexClient()
    works = await client.get_citing_works("W1")
    assert len(works) == 1
    assert works[0]["title"] == "Citing Paper"


@respx.mock
async def test_get_institution_authors(api_key_file):
    route = respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"id": "https://openalex.org/A99", "display_name": "Carol", "last_known_institutions": [], "works_count": 3}
            ]
        })
    )
    client = OpenAlexClient()
    authors = await client.get_institution_authors("I1")
    assert len(authors) == 1
    assert authors[0]["display_name"] == "Carol"
    params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert "topics" in params["select"]
    assert "orcid" in params["select"]


@respx.mock
async def test_get_institution_authors_by_topics_filters_sorts_and_caches(api_key_file):
    route = respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={
            "results": [{
                "id": "https://openalex.org/A99",
                "display_name": "Carol",
                "last_known_institutions": [],
                "topics": [{"id": "https://openalex.org/T1", "display_name": "Mind"}],
                "orcid": "https://orcid.org/0000-0001-0000-0001",
                "works_count": 3,
                "cited_by_count": 7,
            }]
        })
    )
    client = OpenAlexClient()

    first = await client.get_institution_authors_by_topics(
        "https://openalex.org/I1", ["T2", "https://openalex.org/T1"], limit=25
    )
    second = await client.get_institution_authors_by_topics("I1", ["T1", "T2"], limit=25)

    assert route.call_count == 1
    params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert params["filter"] == "last_known_institutions.id:I1,topics.id:T1|T2"
    assert params["per_page"] == "25"
    assert params["sort"] == "cited_by_count:desc"
    assert "topics" in params["select"]
    assert "orcid" in params["select"]
    assert first[0]["display_name"] == "Carol"
    assert second == first


@respx.mock
async def test_get_institution_authors_by_hierarchy_groups_works_and_caches(api_key_file):
    works_route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "group_by": [
                {"key": "https://openalex.org/A2", "count": 4},
                {"key": "https://openalex.org/A1", "count": 2},
            ]
        })
    )
    authors_route = respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"id": "https://openalex.org/A1", "display_name": "Alice"},
                {"id": "https://openalex.org/A2", "display_name": "Bob"},
            ]
        })
    )
    client = OpenAlexClient()

    first = await client.get_institution_authors_by_hierarchy(
        "https://openalex.org/I1",
        subfield_ids=["https://openalex.org/subfields/1211", "1201"],
        field_ids=["12"],
        limit=25,
    )
    second = await client.get_institution_authors_by_hierarchy(
        "I1", subfield_ids=["1201", "1211"], field_ids=["99"], limit=25
    )

    assert works_route.call_count == 1
    assert authors_route.call_count == 1
    params = dict(httpx.QueryParams(works_route.calls.last.request.url.query))
    assert params["filter"] == (
        "authorships.institutions.id:I1,topics.subfield.id:1201|1211"
    )
    assert params["group_by"] == "authorships.author.id"
    assert params["per_page"] == "25"
    assert [author["display_name"] for author in first] == ["Bob", "Alice"]
    assert second == first


async def test_get_institution_authors_by_hierarchy_needs_a_hierarchy_id(api_key_file):
    client = OpenAlexClient()
    assert await client.get_institution_authors_by_hierarchy("I1") == []


@respx.mock
async def test_retry_exhaustion_raises(api_key_file):
    respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(429)
    )
    client = OpenAlexClient()
    import unittest.mock as mock
    with mock.patch("asyncio.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            await client.search_authors("test")


def test_chunks_helper():
    result = list(_chunks([1, 2, 3, 4, 5], 2))
    assert result == [[1, 2], [3, 4], [5]]

    result = list(_chunks([], 50))
    assert result == []

    result = list(_chunks([1, 2], 50))
    assert result == [[1, 2]]


@respx.mock
async def test_get_works_by_authors_chunks_large_list(api_key_file):
    """When author_ids exceeds FILTER_CHUNK, multiple API calls are made and results merged."""
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={
            "results": [{"id": f"https://openalex.org/W{call_count}", "title": f"Paper {call_count}", "authorships": []}]
        })

    respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()
    # Build a list larger than FILTER_CHUNK to force chunking
    author_ids = [f"A{i}" for i in range(_FILTER_CHUNK + 1)]
    works = await client.get_works_by_authors(author_ids)
    assert call_count == 2  # two chunks: FILTER_CHUNK + 1 remaining
    assert len(works) == 2  # one result per chunk merged
    assert works.complete is True


@respx.mock
async def test_get_works_by_authors_marks_full_top_page_incomplete(api_key_file):
    page = [
        {
            "id": f"https://openalex.org/W{index}",
            "title": f"Paper {index}",
            "authorships": [{
                "author": {
                    "id": "https://openalex.org/A1",
                    "display_name": "Alice",
                }
            }],
        }
        for index in range(50)
    ]
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": page,
            "meta": {"count": 51},
        })
    )
    client = OpenAlexClient()

    works = await client.get_works_by_authors(["A1"], limit=50)

    assert len(works) == 50
    assert works.complete is False


@respx.mock
async def test_get_works_by_authors_fails_closed_on_any_chunk_error(api_key_file):
    def handler(request):
        params = dict(httpx.QueryParams(request.url.query))
        if params["filter"].endswith(":A50"):
            return httpx.Response(500)
        return httpx.Response(200, json={
            "results": [{"id": "https://openalex.org/Wpartial", "authorships": []}]
        })

    respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_works_by_authors([f"A{i}" for i in range(_FILTER_CHUNK + 1)])


@respx.mock
async def test_get_works_batch(api_key_file):
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "Paper One",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}}
                    ],
                }
            ]
        })
    )
    client = OpenAlexClient()
    works = await client.get_works_batch(["W1"])
    assert len(works) == 1
    assert works[0]["title"] == "Paper One"


@respx.mock
async def test_get_work(api_key_file):
    respx.get("https://api.openalex.org/works/W1").mock(
        return_value=httpx.Response(200, json={
            "id": "https://openalex.org/W1",
            "title": "Paper One",
            "cited_by_count": 10,
        })
    )
    client = OpenAlexClient()
    data = await client.get_work("W1")
    assert data["title"] == "Paper One"


@respx.mock
async def test_search_works(api_key_file):
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "Paper One",
                    "publication_year": 2020,
                    "cited_by_count": 50,
                    "doi": "https://doi.org/10.1/abc",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                        {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                    ],
                }
            ],
            "meta": {"count": 1},
        })
    )
    client = OpenAlexClient()
    results, total = await client.search_works("test")
    assert total == 1
    assert len(results) == 1
    r = results[0]
    assert r.id == "W1"
    assert r.title == "Paper One"
    assert r.publication_year == 2020
    assert r.cited_by_count == 50
    assert r.doi == "https://doi.org/10.1/abc"
    assert r.author_names == ["Alice", "Bob"]


@respx.mock
async def test_get_author_works_includes_referenced_works(api_key_file):
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = OpenAlexClient()
    await client.get_author_works("A1")
    request = route.calls.last.request
    params = dict(httpx.QueryParams(request.url.query))
    assert "referenced_works" in params["select"]


@respx.mock
async def test_get_works_by_authors_includes_referenced_works(api_key_file):
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = OpenAlexClient()
    await client.get_works_by_authors(["A1"])
    request = route.calls.last.request
    params = dict(httpx.QueryParams(request.url.query))
    assert "referenced_works" in params["select"]
    assert "is_authors_truncated" in params["select"]


@respx.mock
async def test_get_authors_batch_chunks_large_list(api_key_file):
    """When author_ids exceeds FILTER_CHUNK, results from all chunks are combined."""
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={
            "results": [{"id": f"https://openalex.org/A{call_count}00", "display_name": f"Author {call_count}", "last_known_institutions": []}]
        })

    respx.get("https://api.openalex.org/authors").mock(side_effect=handler)
    client = OpenAlexClient()
    author_ids = [f"A{i}" for i in range(_FILTER_CHUNK + 1)]
    authors = await client.get_authors_batch(author_ids)
    assert call_count == 2
    assert len(authors) == 2


@respx.mock
async def test_get_authors_batch_caches_records(api_key_file):
    route = respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={
            "results": [{
                "id": "https://openalex.org/A1", "display_name": "Alice",
                "last_known_institutions": [], "cited_by_count": 5, "works_count": 2,
            }]
        })
    )
    client = OpenAlexClient()
    first = await client.get_authors_batch(["A1"])
    second = await client.get_authors_batch(["A1"])
    params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert route.call_count == 1  # second call is served from the LRU
    assert "topics" in params["select"]
    assert "affiliations" in params["select"]
    assert "orcid" in params["select"]
    assert first[0]["display_name"] == "Alice"
    assert second == first


@respx.mock
async def test_get_authors_batch_fetches_only_uncached_ids(api_key_file):
    def handler(request):
        params = dict(httpx.QueryParams(request.url.query))
        ids = params["filter"].split(":", 1)[1].split("|")
        return httpx.Response(200, json={
            "results": [
                {"id": f"https://openalex.org/{i}", "display_name": i,
                 "last_known_institutions": []}
                for i in ids
            ]
        })

    route = respx.get("https://api.openalex.org/authors").mock(side_effect=handler)
    client = OpenAlexClient()
    await client.get_authors_batch(["A1"])
    result = await client.get_authors_batch(["A1", "A2"])

    assert route.call_count == 2
    last_params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert last_params["filter"] == "ids.openalex:A2"  # A1 came from the LRU
    assert {a["display_name"] for a in result} == {"A1", "A2"}


@respx.mock
async def test_get_author_populates_and_reads_author_lru(api_key_file):
    single = respx.get("https://api.openalex.org/authors/A123").mock(
        return_value=httpx.Response(200, json={
            "id": "https://openalex.org/A123", "display_name": "Alice",
            "last_known_institutions": [], "works_count": 2, "cited_by_count": 5,
        })
    )
    batch = respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = OpenAlexClient()

    first = await client.get_author("A123")
    second = await client.get_author("A123")
    assert single.call_count == 1  # second read came from the LRU
    assert second == first

    # The batch path reuses the record get_author cached — no batch HTTP call.
    result = await client.get_authors_batch(["A123"])
    assert batch.call_count == 0
    assert result[0]["display_name"] == "Alice"


@respx.mock
async def test_clear_author_cache_forces_refetch(api_key_file):
    route = respx.get("https://api.openalex.org/authors/A123").mock(
        return_value=httpx.Response(200, json={
            "id": "https://openalex.org/A123", "display_name": "Alice",
        })
    )
    client = OpenAlexClient()
    await client.get_author("A123")
    client.clear_author_cache()
    await client.get_author("A123")
    assert route.call_count == 2


def test_concurrency_default_keyed(api_key_file, monkeypatch):
    monkeypatch.delenv("OPENALEX_CONCURRENCY", raising=False)
    client = OpenAlexClient()
    assert client._semaphore._value == 15


def test_concurrency_default_keyless(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENALEX_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_CONCURRENCY", raising=False)
    client = OpenAlexClient()
    assert client._semaphore._value == 8


def test_concurrency_env_override(api_key_file, monkeypatch):
    monkeypatch.setenv("OPENALEX_CONCURRENCY", "42")
    client = OpenAlexClient()
    assert client._semaphore._value == 42


def test_concurrency_invalid_env_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENALEX_KEY", raising=False)
    monkeypatch.setenv("OPENALEX_CONCURRENCY", "not-a-number")
    client = OpenAlexClient()
    assert client._semaphore._value == 8


def test_concurrency_zero_env_falls_back(api_key_file, monkeypatch):
    monkeypatch.setenv("OPENALEX_CONCURRENCY", "0")
    client = OpenAlexClient()
    assert client._semaphore._value == 15


@respx.mock
async def test_get_coauthor_summary_uses_group_by_removes_self_and_caches(api_key_file):
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "group_by": [
                {
                    "key": "https://openalex.org/A1",
                    "key_display_name": "Alice",
                    "count": 12,
                },
                {
                    "key": "https://openalex.org/A2",
                    "key_display_name": "Bob",
                    "count": 4,
                },
                {
                    "key": "https://openalex.org/A3",
                    "key_display_name": None,
                    "count": 1,
                },
            ]
        })
    )
    client = OpenAlexClient()

    first = await client.get_coauthor_summary("https://openalex.org/A1", limit=500)
    second = await client.get_coauthor_summary("A1", limit=500)

    assert route.call_count == 1
    params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert params["filter"] == "authorships.author.id:A1"
    assert params["group_by"] == "authorships.author.id"
    assert params["per_page"] == "200"
    assert "select" not in params
    assert first == {
        "A2": {"name": "Bob", "works_count": 4},
        "A3": {"name": "A3", "works_count": 1},
    }
    assert first.complete is True
    assert second == first


@respx.mock
async def test_get_coauthor_summary_marks_bounded_group_page_incomplete(api_key_file):
    groups = [
        {
            "key": f"https://openalex.org/A{index}",
            "key_display_name": f"Author {index}",
            "count": 1,
        }
        for index in range(200)
    ]
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "group_by": groups,
            "meta": {"group_count": 201},
        })
    )
    client = OpenAlexClient()

    result = await client.get_coauthor_summary("A999")

    assert len(result) == 200
    assert result.complete is False


@respx.mock
async def test_get_coauthor_summary_verified_scope_counts_only_requested_works(api_key_file):
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "First shared paper",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                        {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                        # Duplicate authorships must not inflate the work count.
                        {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                    ],
                },
                {
                    "id": "https://openalex.org/W2",
                    "title": "Second shared paper",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                        {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                        {"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}},
                    ],
                },
                {
                    "id": "https://openalex.org/W3",
                    "title": "Unrelated paper",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A4", "display_name": "Dan"}},
                        {"author": {"id": "https://openalex.org/A5", "display_name": "Eve"}},
                    ],
                },
            ]
        })
    )
    client = OpenAlexClient()

    first = await client.get_coauthor_summary("A1", ["W1", "W2", "W3"])
    second = await client.get_coauthor_summary("A1", ["W3", "W2", "W1"])

    assert route.call_count == 1
    params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert params["filter"] == "ids.openalex:W1|W2|W3"
    assert {
        coauthor_id: {
            key: entry[key]
            for key in ("name", "works_count", "label", "work_id")
        }
        for coauthor_id, entry in first.items()
    } == {
        "A2": {
            "name": "Bob",
            "works_count": 2,
            "label": "First shared paper",
            "work_id": "W1",
        },
        "A3": {
            "name": "Carol",
            "works_count": 1,
            "label": "Second shared paper",
            "work_id": "W2",
        },
    }
    assert first["A2"]["left_id"] == "A1"
    assert first["A2"]["right_id"] == "A2"
    assert first["A2"]["author_count"] == 3
    assert second == first
    assert first.complete is True


@respx.mock
async def test_get_coauthor_summary_does_not_cache_partial_verified_batch(api_key_file):
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        works = [{
            "id": "https://openalex.org/W1",
            "title": "Paper one",
            "authorships": [
                {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
            ],
        }]
        if call_count > 1:
            works.append({
                "id": "https://openalex.org/W2",
                "title": "Paper two",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                    {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                ],
            })
        return httpx.Response(200, json={"results": works})

    respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()

    with pytest.raises(RuntimeError, match="incomplete verified-work batch"):
        await client.get_coauthor_summary("A1", ["W1", "W2"])
    complete = await client.get_coauthor_summary("A1", ["W1", "W2"])

    assert call_count == 2
    assert complete["A2"]["works_count"] == 2


async def test_get_coauthor_summary_does_not_cache_exception(api_key_file, monkeypatch):
    from unittest.mock import AsyncMock

    client = OpenAlexClient()
    get_batch = AsyncMock(side_effect=[
        RuntimeError("temporary failure"),
        [{
            "id": "https://openalex.org/W1",
            "title": "Recovered paper",
            "authorships": [
                {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
            ],
        }],
    ])
    monkeypatch.setattr(client, "get_works_batch", get_batch)

    with pytest.raises(RuntimeError, match="temporary failure"):
        await client.get_coauthor_summary("A1", ["W1"])
    result = await client.get_coauthor_summary("A1", ["W1"])

    assert get_batch.await_count == 2
    assert result["A2"]["label"] == "Recovered paper"


@respx.mock
async def test_get_coauthor_links_paginates_verifies_and_caches(api_key_file):
    calls = []

    def handler(request):
        params = dict(httpx.QueryParams(request.url.query))
        calls.append(params)
        if params["cursor"] == "*":
            return httpx.Response(200, json={
                "results": [{
                    "id": "https://openalex.org/W1",
                    "title": "Alice and Carol",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                        {"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}},
                    ],
                }],
                "meta": {"next_cursor": "next-page"},
            })
        return httpx.Response(200, json={
            "results": [
                {
                    "id": "https://openalex.org/W2",
                    "title": "Bob and Dan",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                        {"author": {"id": "https://openalex.org/A4", "display_name": "Dan"}},
                    ],
                },
                # A false-positive API record is rejected by local membership checks.
                {
                    "id": "https://openalex.org/Wbad",
                    "title": "Not actually cross-set",
                    "authorships": [
                        {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                    ],
                },
            ],
            "meta": {"next_cursor": None},
        })

    route = respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()

    first = await client.get_coauthor_links(["A2", "A1"], ["A4", "A3"])
    second = await client.get_coauthor_links(["A1", "A2"], ["A3", "A4"])

    assert route.call_count == 2
    assert [call["cursor"] for call in calls] == ["*", "next-page"]
    assert all(
        call["filter"] == "authorships.author.id:A1|A2,authorships.author.id:A3|A4"
        for call in calls
    )
    assert all(call["per_page"] == "100" for call in calls)
    assert all("publication_year" in call["select"] for call in calls)
    assert all("topics" in call["select"] for call in calls)
    assert [
        {
            key: item[key]
            for key in (
                "left_id", "left_name", "right_id", "right_name", "label", "work_id"
            )
        }
        for item in first
    ] == [
        {
            "left_id": "A1",
            "left_name": "Alice",
            "right_id": "A3",
            "right_name": "Carol",
            "label": "Alice and Carol",
            "work_id": "W1",
        },
        {
            "left_id": "A2",
            "left_name": "Bob",
            "right_id": "A4",
            "right_name": "Dan",
            "label": "Bob and Dan",
            "work_id": "W2",
        },
    ]
    assert first[0]["title"] == "Alice and Carol"
    assert first[0]["author_count"] == 2
    assert first[0]["topics"] == []
    assert second == first
    assert first.complete is True


@respx.mock
async def test_get_coauthor_links_retains_work_and_endpoint_identity_metadata(api_key_file):
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [{
                "id": "https://openalex.org/W42",
                "title": "Conscious perception",
                "publication_year": 2023,
                "topics": [{
                    "id": "https://openalex.org/T100",
                    "display_name": "Conscious visual perception",
                    "subfield": {
                        "id": "https://openalex.org/subfields/2802",
                        "display_name": "Behavioral Neuroscience",
                    },
                    "field": {
                        "id": "https://openalex.org/fields/28",
                        "display_name": "Neuroscience",
                    },
                    "domain": {
                        "id": "https://openalex.org/domains/3",
                        "display_name": "Life Sciences",
                    },
                }],
                "authorships": [
                    {
                        "author": {
                            "id": "https://openalex.org/A1",
                            "display_name": "Andrew Example",
                            "orcid": "https://orcid.org/0000-0001-1111-1111",
                        },
                        "raw_author_name": "A. Example",
                        "raw_affiliation_strings": ["Mind Lab, Example University"],
                        "institutions": [{
                            "id": "https://openalex.org/I1",
                            "display_name": "Example University",
                        }],
                    },
                    {
                        "author": {
                            "id": "https://openalex.org/A2",
                            "display_name": "Beatrice Example",
                        },
                        "raw_author_name": "B. Example",
                        "raw_orcid": "https://orcid.org/0000-0002-2222-2222",
                        "affiliations": [{
                            "raw_affiliation_string": "Vision Centre",
                            "institution_ids": ["https://openalex.org/I2"],
                        }],
                        "institutions": [{
                            "id": "https://openalex.org/I2",
                            "display_name": "Vision Institute",
                        }],
                    },
                ],
            }],
            "meta": {"next_cursor": None},
        })
    )
    client = OpenAlexClient()

    evidence = await client.get_coauthor_links(["A1"], ["A2"])

    assert len(evidence) == 1
    edge = evidence[0]
    assert edge["work_id"] == "W42"
    assert edge["title"] == "Conscious perception"
    assert edge["publication_year"] == 2023
    assert edge["author_count"] == 2
    assert edge["author_ids"] == ["A1", "A2"]
    assert edge["topics"] == [{
        "id": "T100",
        "name": "Conscious visual perception",
        "subfield_id": "2802",
        "subfield_name": "Behavioral Neuroscience",
        "field_id": "28",
        "field_name": "Neuroscience",
        "domain_id": "3",
        "domain_name": "Life Sciences",
    }]
    assert edge["left_authorship"] == {
        "author_id": "A1",
        "display_name": "Andrew Example",
        "raw_author_name": "A. Example",
        "raw_affiliation_strings": ["Mind Lab, Example University"],
        "institution_ids": ["I1"],
        "institution_names": ["Example University"],
        "orcid": "https://orcid.org/0000-0001-1111-1111",
    }
    assert edge["right_authorship"]["raw_affiliation_strings"] == ["Vision Centre"]
    assert edge["right_authorship"]["institution_ids"] == ["I2"]
    assert edge["right_authorship"]["institution_names"] == ["Vision Institute"]
    assert edge["right_authorship"]["orcid"] == "https://orcid.org/0000-0002-2222-2222"


@respx.mock
async def test_get_coauthor_links_handles_overlap_and_deduplicates(api_key_file):
    work = {
        "id": "https://openalex.org/W1",
        "title": "Three-way paper",
        "authorships": [
            {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
            {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
            {"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}},
        ],
    }
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [work, work],
            "meta": {"next_cursor": None},
        })
    )
    client = OpenAlexClient()

    evidence = await client.get_coauthor_links(["A1", "A2"], ["A2", "A3"])

    assert [
        (item["left_id"], item["right_id"], item["work_id"])
        for item in evidence
    ] == [
        ("A1", "A2", "W1"),
        ("A1", "A3", "W1"),
        ("A2", "A3", "W1"),
    ]
    assert all(item["left_id"] != item["right_id"] for item in evidence)


@respx.mock
async def test_get_coauthor_links_chunks_both_filter_sides(api_key_file):
    filters = []

    def handler(request):
        params = dict(httpx.QueryParams(request.url.query))
        filters.append(params["filter"])
        return httpx.Response(200, json={
            "results": [],
            "meta": {"next_cursor": None},
        })

    respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()
    left = [f"A{i:03}" for i in range(_FILTER_CHUNK + 1)]
    right = [f"B{i:03}" for i in range(_FILTER_CHUNK + 1)]

    assert await client.get_coauthor_links(left, right) == []

    assert len(filters) == 4
    for filter_value in filters:
        left_filter, right_filter = filter_value.split(",")
        assert left_filter.startswith("authorships.author.id:")
        assert right_filter.startswith("authorships.author.id:")
        assert len(left_filter.split(":", 1)[1].split("|")) <= _FILTER_CHUNK
        assert len(right_filter.split(":", 1)[1].split("|")) <= _FILTER_CHUNK


@respx.mock
async def test_get_coauthor_links_does_not_cache_failure(api_key_file):
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={
            "results": [{
                "id": "https://openalex.org/W1",
                "title": "Recovered paper",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                    {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                ],
            }],
            "meta": {"next_cursor": None},
        })

    respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_coauthor_links(["A1"], ["A2"])
    result = await client.get_coauthor_links(["A1"], ["A2"])

    assert call_count == 2
    assert result[0]["work_id"] == "W1"


@respx.mock
async def test_get_coauthor_links_caps_cursor_scan_and_does_not_cache(api_key_file):
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={
            "results": [{
                "id": f"https://openalex.org/W{call_count}",
                "title": f"Paper {call_count}",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}},
                    {"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}},
                ],
            }],
            "meta": {"next_cursor": f"cursor-{call_count}"},
        })

    respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()

    first = await client.get_coauthor_links(["A1"], ["A2"])
    second = await client.get_coauthor_links(["A1"], ["A2"])

    assert call_count == 6
    assert len(first) == 3
    assert first.complete is False
    assert second.complete is False


@respx.mock
async def test_clear_author_cache_clears_coauthor_caches(api_key_file):
    def handler(request):
        params = dict(httpx.QueryParams(request.url.query))
        if "group_by" in params:
            return httpx.Response(200, json={"group_by": []})
        return httpx.Response(200, json={
            "results": [],
            "meta": {"next_cursor": None},
        })

    route = respx.get("https://api.openalex.org/works").mock(side_effect=handler)
    client = OpenAlexClient()

    await client.get_coauthor_summary("A1")
    await client.get_coauthor_links(["A1"], ["A2"])
    await client.get_coauthor_summary("A1")
    await client.get_coauthor_links(["A1"], ["A2"])
    assert route.call_count == 2

    client.clear_author_cache()
    await client.get_coauthor_summary("A1")
    await client.get_coauthor_links(["A1"], ["A2"])
    assert route.call_count == 4

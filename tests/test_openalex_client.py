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
    respx.get("https://api.openalex.org/authors").mock(
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
    assert route.call_count == 1  # second call is served from the LRU
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
    assert client._semaphore._value == 25


def test_concurrency_default_keyless(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENALEX_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_CONCURRENCY", raising=False)
    client = OpenAlexClient()
    assert client._semaphore._value == 25


def test_concurrency_env_override(api_key_file, monkeypatch):
    monkeypatch.setenv("OPENALEX_CONCURRENCY", "42")
    client = OpenAlexClient()
    assert client._semaphore._value == 42


def test_concurrency_invalid_env_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENALEX_KEY", raising=False)
    monkeypatch.setenv("OPENALEX_CONCURRENCY", "not-a-number")
    client = OpenAlexClient()
    assert client._semaphore._value == 25


def test_concurrency_zero_env_falls_back(api_key_file, monkeypatch):
    monkeypatch.setenv("OPENALEX_CONCURRENCY", "0")
    client = OpenAlexClient()
    assert client._semaphore._value == 25

import json
import pytest
import respx
import httpx
from pathlib import Path
from backend.openalex_client import OpenAlexClient, _short_id


@pytest.fixture
def api_key_file(tmp_path):
    f = tmp_path / "api-keys.json"
    f.write_text(json.dumps({"openalex-key": "test_key"}))
    return f


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
    client = OpenAlexClient(api_key_path=api_key_file)
    results = await client.search_authors("Alice")
    assert len(results) == 1
    assert results[0].id == "A123"
    assert results[0].display_name == "Alice Smith"
    assert results[0].institution == "MIT"
    assert results[0].works_count == 42


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
    client = OpenAlexClient(api_key_path=api_key_file)
    results = await client.search_authors("Bob")
    assert results[0].institution is None


@respx.mock
async def test_get_author(api_key_file):
    respx.get("https://api.openalex.org/authors/A123").mock(
        return_value=httpx.Response(200, json={
            "id": "https://openalex.org/A123",
            "display_name": "Alice Smith",
            "last_known_institutions": [{"id": "https://openalex.org/I1", "display_name": "MIT"}],
        })
    )
    client = OpenAlexClient(api_key_path=api_key_file)
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
    client = OpenAlexClient(api_key_path=api_key_file)
    works = await client.get_author_works("A123")
    assert len(works) == 1
    assert works[0]["title"] == "Paper One"


@respx.mock
async def test_retry_on_429(api_key_file):
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"results": []})

    respx.get("https://api.openalex.org/authors").mock(side_effect=side_effect)
    client = OpenAlexClient(api_key_path=api_key_file)
    import unittest.mock as mock
    with mock.patch("asyncio.sleep"):
        results = await client.search_authors("test")
    assert call_count == 2
    assert results == []


@respx.mock
async def test_get_citing_works(api_key_file):
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"id": "https://openalex.org/W2", "title": "Citing Paper", "authorships": []}
            ]
        })
    )
    client = OpenAlexClient(api_key_path=api_key_file)
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
    client = OpenAlexClient(api_key_path=api_key_file)
    authors = await client.get_institution_authors("I1")
    assert len(authors) == 1
    assert authors[0]["display_name"] == "Carol"


@respx.mock
async def test_retry_exhaustion_raises(api_key_file):
    respx.get("https://api.openalex.org/authors").mock(
        return_value=httpx.Response(429)
    )
    client = OpenAlexClient(api_key_path=api_key_file)
    import unittest.mock as mock
    with mock.patch("asyncio.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            await client.search_authors("test")

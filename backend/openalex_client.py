import asyncio
import json
import os
from pathlib import Path

import httpx

from backend.models import AuthorResult

API_BASE = "https://api.openalex.org"
_DEFAULT_KEY_PATH = Path(__file__).parent.parent / "api-keys.json"
_FILTER_CHUNK = 50  # max IDs per pipe-separated filter to stay within URL limits


def _short_id(openalex_url: str) -> str:
    return openalex_url.split("/")[-1]


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


class OpenAlexClient:
    def __init__(self, api_key_path: Path | None = None):
        api_key = os.environ.get("OPENALEX_KEY")
        if not api_key:
            path = api_key_path or _DEFAULT_KEY_PATH
            if path.exists():
                api_key = json.loads(path.read_text()).get("openalex-key", "")
        self._api_key = api_key or ""
        self._semaphore = asyncio.Semaphore(5)

    async def _get(self, url: str, params: dict) -> dict:
        if self._api_key:
            params = {**params, "api_key": self._api_key}
        for attempt in range(3):
            async with self._semaphore:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, params=params, timeout=30.0)
            if resp.status_code == 429:
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    async def search_authors(self, query: str, limit: int = 5) -> list[AuthorResult]:
        data = await self._get(f"{API_BASE}/authors", {"search": query, "per_page": limit})
        results = []
        for item in data.get("results", []):
            institution = None
            if item.get("last_known_institutions"):
                institution = item["last_known_institutions"][0].get("display_name")
            results.append(AuthorResult(
                id=_short_id(item["id"]),
                display_name=item["display_name"],
                institution=institution,
                works_count=item.get("works_count", 0),
            ))
        return results

    async def get_author(self, author_id: str) -> dict:
        return await self._get(f"{API_BASE}/authors/{author_id}", {})

    async def get_author_works(self, author_id: str, limit: int = 20) -> list[dict]:
        data = await self._get(f"{API_BASE}/works", {
            "filter": f"authorships.author.id:{author_id}",
            "per_page": limit,
            "sort": "cited_by_count:desc",
        })
        return data.get("results", [])

    async def get_citing_works(self, work_id: str, limit: int = 10) -> list[dict]:
        data = await self._get(f"{API_BASE}/works", {
            "filter": f"cites:{work_id}",
            "per_page": limit,
        })
        return data.get("results", [])

    async def get_institution_authors(self, institution_id: str, limit: int = 50) -> list[dict]:
        data = await self._get(f"{API_BASE}/authors", {
            "filter": f"last_known_institutions.id:{institution_id}",
            "per_page": limit,
            "sort": "works_count:desc",
        })
        return data.get("results", [])

    async def get_works_by_authors(self, author_ids: list[str], limit: int = 200) -> list[dict]:
        """Fetch top works for multiple authors; chunks large lists to avoid URL length limits."""
        if not author_ids:
            return []
        chunk_list = list(_chunks(author_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/works", {
                "filter": f"authorships.author.id:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "sort": "cited_by_count:desc",
                "select": "id,title,authorships",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

    async def get_citing_works_for_works(self, work_ids: list[str], limit: int = 200) -> list[dict]:
        """Fetch papers that cite any of the given works; chunks large lists."""
        if not work_ids:
            return []
        chunk_list = list(_chunks(work_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/works", {
                "filter": f"cites:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "select": "id,authorships,referenced_works",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

    async def get_authors_batch(self, author_ids: list[str]) -> list[dict]:
        """Fetch multiple author records by ID; chunks large lists."""
        if not author_ids:
            return []
        chunk_list = list(_chunks(author_ids, _FILTER_CHUNK))
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/authors", {
                "filter": f"ids.openalex:{'|'.join(chunk)}",
                "per_page": min(len(chunk), 200),
                "select": "id,display_name,last_known_institutions",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

    async def get_institution_authors_batch(self, institution_ids: list[str], limit: int = 200) -> list[dict]:
        """Fetch top authors across multiple institutions; chunks large lists."""
        if not institution_ids:
            return []
        chunk_list = list(_chunks(institution_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/authors", {
                "filter": f"last_known_institutions.id:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "sort": "works_count:desc",
                "select": "id,display_name,last_known_institutions",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

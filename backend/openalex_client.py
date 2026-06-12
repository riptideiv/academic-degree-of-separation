import asyncio
import json
from pathlib import Path

import httpx

from backend.models import AuthorResult

API_BASE = "https://api.openalex.org"
_DEFAULT_KEY_PATH = Path(__file__).parent.parent / "api-keys.json"


def _short_id(openalex_url: str) -> str:
    return openalex_url.split("/")[-1]


class OpenAlexClient:
    def __init__(self, api_key_path: Path | None = None):
        path = api_key_path or _DEFAULT_KEY_PATH
        self._api_key = json.loads(path.read_text())["openalex-key"]
        self._semaphore = asyncio.Semaphore(5)

    async def _get(self, url: str, params: dict) -> dict:
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

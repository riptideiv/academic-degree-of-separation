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
        keys: dict = {}
        path = api_key_path or _DEFAULT_KEY_PATH
        if path.exists():
            try:
                keys = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                keys = {}
        self._api_key = os.environ.get("OPENALEX_KEY") or keys.get("openalex-key", "") or ""
        # OpenAlex routes requests that identify themselves (mailto / descriptive
        # User-Agent) into a faster "polite pool" with more headroom. Configure via
        # OPENALEX_MAILTO env var or a "mailto" entry in api-keys.json.
        self._mailto = os.environ.get("OPENALEX_MAILTO") or keys.get("mailto", "") or ""
        self._semaphore = asyncio.Semaphore(5)
        # One shared client → connection pooling / keep-alive across the many calls
        # a single BFS makes. Created lazily so it binds to the running event loop.
        self._http: httpx.AsyncClient | None = None

    def _user_agent(self) -> str:
        ua = "researcher-degree-of-separation/1.0"
        return f"{ua} (mailto:{self._mailto})" if self._mailto else ua

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=30.0, headers={"User-Agent": self._user_agent()}
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _get(self, url: str, params: dict) -> dict:
        params = dict(params)
        if self._api_key:
            params["api_key"] = self._api_key
        if self._mailto:
            params.setdefault("mailto", self._mailto)
        client = await self._http_client()
        for attempt in range(3):
            async with self._semaphore:
                resp = await client.get(url, params=params)
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

    async def get_works_by_authors(self, author_ids: list[str], limit: int = 50) -> list[dict]:
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

    async def get_citing_works_for_works(self, work_ids: list[str], limit: int = 50) -> list[dict]:
        """Fetch papers that cite any of the given works; chunks large lists."""
        if not work_ids:
            return []
        chunk_list = list(_chunks(work_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/works", {
                "filter": f"cites:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "sort": "cited_by_count:desc",
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
                "select": "id,display_name,last_known_institutions,cited_by_count,works_count",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

    async def get_institution_authors_batch(self, institution_ids: list[str], limit: int = 50) -> list[dict]:
        """Fetch top authors across multiple institutions; chunks large lists."""
        if not institution_ids:
            return []
        chunk_list = list(_chunks(institution_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/authors", {
                "filter": f"last_known_institutions.id:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "sort": "cited_by_count:desc",
                "select": "id,display_name,last_known_institutions,cited_by_count,works_count",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

import asyncio
import json
import os
from collections import OrderedDict
from pathlib import Path

import httpx

from backend.models import AuthorResult, WorkResult

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
        # OpenAlex retired the "polite pool" in early 2025; mailto no longer affects
        # rate limits (higher limits now come from OPENALEX_KEY / the api_key param).
        # We still send a descriptive User-Agent / mailto as a courtesy identifier.
        # Configure via OPENALEX_MAILTO env var or a "mailto" entry in api-keys.json.
        self._mailto = os.environ.get("OPENALEX_MAILTO") or keys.get("mailto", "") or ""
        # Cap on concurrent OpenAlex requests. Each BFS level fans out many chunked
        # requests via asyncio.gather; too small a gate serializes them. Configurable
        # via OPENALEX_CONCURRENCY; otherwise key-aware (a key raises the daily budget,
        # so we can push more in flight — keyless stays conservative). The 429 retry
        # in _get absorbs the higher burst.
        self._semaphore = asyncio.Semaphore(self._concurrency_limit())
        # One shared client → connection pooling / keep-alive across the many calls
        # a single BFS makes. Created lazily so it binds to the running event loop.
        self._http: httpx.AsyncClient | None = None
        # Author-metadata LRU (id → author record). Expansion ranking re-fetches
        # the same author records every level/run; process-lifetime caching is
        # fine because citation counts drift slowly.
        self._author_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._author_cache_max = 50_000

    def _concurrency_limit(self) -> int:
        """Max concurrent OpenAlex requests. OPENALEX_CONCURRENCY overrides; else
        key-aware (15 with a key, 8 keyless)."""
        default = 15 if self._api_key else 8
        raw = os.environ.get("OPENALEX_CONCURRENCY")
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return default

    def _user_agent(self) -> str:
        ua = "researcher-degree-of-separation/1.0"
        return f"{ua} (mailto:{self._mailto})" if self._mailto else ua

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=30.0, http2=True, headers={"User-Agent": self._user_agent()}
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

    async def search_authors(
        self, query: str, page: int = 1, per_page: int = 20
    ) -> tuple[list[AuthorResult], int]:
        data = await self._get(f"{API_BASE}/authors", {
            "search": query,
            "page": page,
            "per_page": per_page,
            "select": "id,display_name,last_known_institutions,works_count,cited_by_count",
        })
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
                cited_by_count=item.get("cited_by_count", 0),
            ))
        total = data.get("meta", {}).get("count", len(results))
        return results, total

    async def get_author(self, author_id: str) -> dict:
        # Served from / written to the author LRU: the batch path caches
        # select-projected records whose fields are a superset of what
        # get_author's consumers read, and the full record cached here is a
        # superset of the projection, so the two shapes interchange safely.
        hit = self._author_cache.get(author_id)
        if hit is not None:
            self._author_cache.move_to_end(author_id)
            return hit
        author = await self._get(f"{API_BASE}/authors/{author_id}", {})
        self._author_cache[author_id] = author
        while len(self._author_cache) > self._author_cache_max:
            self._author_cache.popitem(last=False)
        return author

    def clear_author_cache(self) -> None:
        """Drop all cached author records (the /api/cache wipe calls this)."""
        self._author_cache.clear()

    async def get_author_works(self, author_id: str, limit: int = 20) -> list[dict]:
        data = await self._get(f"{API_BASE}/works", {
            "filter": f"authorships.author.id:{author_id}",
            "per_page": limit,
            "sort": "cited_by_count:desc",
            "select": "id,title,cited_by_count,publication_year,doi,referenced_works",
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
                "select": "id,title,authorships,referenced_works",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

    async def get_works_batch(self, work_ids: list[str], limit: int = 200) -> list[dict]:
        """Fetch multiple work records by ID (title + authorships); chunks large lists."""
        if not work_ids:
            return []
        chunk_list = list(_chunks(work_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/works", {
                "filter": f"ids.openalex:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "select": "id,title,authorships",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        combined: list[dict] = []
        for r in results:
            if not isinstance(r, Exception):
                combined.extend(r.get("results", []))
        return combined

    async def get_work(self, work_id: str) -> dict:
        return await self._get(f"{API_BASE}/works/{work_id}", {})

    async def search_works(
        self, query: str, page: int = 1, per_page: int = 20
    ) -> tuple[list[WorkResult], int]:
        data = await self._get(f"{API_BASE}/works", {
            "search": query,
            "page": page,
            "per_page": per_page,
            "select": "id,title,publication_year,cited_by_count,authorships,doi",
        })
        results = []
        for item in data.get("results", []):
            author_names = [
                a["author"]["display_name"]
                for a in item.get("authorships", [])
                if a.get("author") and a["author"].get("display_name")
            ]
            results.append(WorkResult(
                id=_short_id(item["id"]),
                title=item.get("title") or "(untitled)",
                publication_year=item.get("publication_year"),
                cited_by_count=item.get("cited_by_count", 0),
                author_names=author_names,
                doi=item.get("doi"),
            ))
        total = data.get("meta", {}).get("count", len(results))
        return results, total

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
        """Fetch multiple author records by ID; chunks large lists.

        Records are served from a bounded in-process LRU when possible — the
        expansion ranking asks for the same authors level after level and run
        after run, so this saves a full API round per level on warm paths.
        """
        if not author_ids:
            return []
        combined: list[dict] = []
        missing: list[str] = []
        for aid in dict.fromkeys(author_ids):
            hit = self._author_cache.get(aid)
            if hit is not None:
                self._author_cache.move_to_end(aid)
                combined.append(hit)
            else:
                missing.append(aid)
        if not missing:
            return combined

        chunk_list = list(_chunks(missing, _FILTER_CHUNK))
        results = await asyncio.gather(*[
            self._get(f"{API_BASE}/authors", {
                "filter": f"ids.openalex:{'|'.join(chunk)}",
                "per_page": min(len(chunk), 200),
                "select": "id,display_name,last_known_institutions,cited_by_count,works_count",
            })
            for chunk in chunk_list
        ], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                continue
            for author in r.get("results", []):
                self._author_cache[_short_id(author["id"])] = author
                self._author_cache.move_to_end(_short_id(author["id"]))
                combined.append(author)
        while len(self._author_cache) > self._author_cache_max:
            self._author_cache.popitem(last=False)
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

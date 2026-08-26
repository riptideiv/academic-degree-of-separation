import asyncio
import json
import os
from collections import OrderedDict

import httpx

from backend.models import AuthorResult, WorkResult

API_BASE = "https://api.openalex.org"
_FILTER_CHUNK = 50  # max IDs per pipe-separated filter to stay within URL limits
_COAUTHOR_LINK_PAGE_MAX = 3


def _short_id(openalex_url: str) -> str:
    return openalex_url.split("/")[-1]


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _unique_strings(values) -> list[str]:
    """Return non-empty strings once, preserving their source order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _authorship_identity(authorship: dict) -> dict | None:
    """Project the identity-bearing portion of an OpenAlex authorship."""
    author = authorship.get("author") or {}
    raw_author_id = author.get("id")
    if not raw_author_id:
        return None

    institution_ids: list[str] = []
    institution_names: list[str] = []
    for institution in authorship.get("institutions") or []:
        if not isinstance(institution, dict):
            continue
        raw_institution_id = institution.get("id")
        if raw_institution_id:
            institution_ids.append(_short_id(raw_institution_id))
        name = institution.get("display_name")
        if name:
            institution_names.append(name)

    raw_affiliation_strings = list(authorship.get("raw_affiliation_strings") or [])
    for affiliation in authorship.get("affiliations") or []:
        if not isinstance(affiliation, dict):
            continue
        raw_affiliation = affiliation.get("raw_affiliation_string")
        if raw_affiliation:
            raw_affiliation_strings.append(raw_affiliation)
        for raw_institution_id in affiliation.get("institution_ids") or []:
            if raw_institution_id:
                institution_ids.append(_short_id(raw_institution_id))

    return {
        "author_id": _short_id(raw_author_id),
        "display_name": author.get("display_name") or "",
        "raw_author_name": authorship.get("raw_author_name"),
        "raw_affiliation_strings": _unique_strings(raw_affiliation_strings),
        "institution_ids": _unique_strings(institution_ids),
        "institution_names": _unique_strings(institution_names),
        "orcid": (
            author.get("orcid")
            or authorship.get("raw_orcid")
            or authorship.get("orcid")
        ),
    }


def _work_topics(work: dict) -> list[dict]:
    """Keep the compact topic taxonomy needed for identity continuity checks."""
    topics: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for topic in work.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        raw_topic_id = topic.get("id")
        topic_id = _short_id(raw_topic_id) if raw_topic_id else ""
        name = topic.get("display_name") or ""
        key = (topic_id, name)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        item = {"id": topic_id, "name": name}
        for level in ("subfield", "field", "domain"):
            value = topic.get(level) or {}
            if not isinstance(value, dict):
                continue
            raw_level_id = value.get("id")
            item[f"{level}_id"] = _short_id(raw_level_id) if raw_level_id else ""
            item[f"{level}_name"] = value.get("display_name") or ""
        topics.append(item)
    return topics


def _work_edge_evidence(work: dict, left_id: str, right_id: str) -> dict | None:
    """Build one locally verified, metadata-rich coauthor edge from a work."""
    identities: dict[str, dict] = {}
    for authorship in work.get("authorships") or []:
        identity = _authorship_identity(authorship)
        if identity is None:
            continue
        # OpenAlex should emit one authorship per author.  If a malformed record
        # repeats an ID, keep the first stable identity instead of mixing rows.
        identities.setdefault(identity["author_id"], identity)

    left = identities.get(left_id)
    right = identities.get(right_id)
    if left is None or right is None or left_id == right_id:
        return None

    title = work.get("title") or "(untitled)"
    return {
        # Existing public keys remain unchanged.
        "left_id": left_id,
        "left_name": left.get("display_name") or left_id,
        "right_id": right_id,
        "right_name": right.get("display_name") or right_id,
        "label": title,
        "work_id": _short_id(work["id"]),
        # Rich positive evidence used by the identity/coherence validator.
        "title": title,
        "publication_year": work.get("publication_year"),
        "author_count": len(work.get("authorships") or []),
        "topics": _work_topics(work),
        "author_ids": list(identities),
        "left_authorship": left,
        "right_authorship": right,
    }


class CoauthorSummary(dict[str, dict]):
    """Bounded coauthor mapping with an explicit exhaustiveness signal."""

    def __init__(self, values: dict[str, dict], *, complete: bool):
        super().__init__(values)
        self.complete = complete


class AuthorWorksPage(list[dict]):
    """A bounded author-work sample with an explicit exhaustiveness signal."""

    def __init__(self, values: list[dict], *, complete: bool):
        super().__init__(values)
        self.complete = complete


class CoauthorLinks(list[dict]):
    """Positive publication evidence plus cursor-scan completeness."""

    def __init__(self, values: list[dict], *, complete: bool):
        super().__init__(values)
        self.complete = complete


class OpenAlexClient:
    def __init__(self):
        self._api_key = os.environ.get("OPENALEX_KEY", "")
        # OpenAlex retired the "polite pool" in early 2025; mailto no longer affects
        # rate limits (higher limits now come from OPENALEX_KEY / the api_key param).
        # We still send a descriptive User-Agent / mailto as a courtesy identifier.
        # Configure via OPENALEX_MAILTO.
        self._mailto = os.environ.get("OPENALEX_MAILTO", "")
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
        self._institution_authors_cache: "OrderedDict[tuple, list[dict]]" = OrderedDict()
        self._institution_authors_cache_max = 100
        self._search_cache: "OrderedDict[tuple, tuple[list, int]]" = OrderedDict()
        self._search_cache_max = 200
        self._coauthor_summary_cache: "OrderedDict[tuple, CoauthorSummary]" = OrderedDict()
        self._coauthor_summary_cache_max = 2_000
        self._coauthor_links_cache: "OrderedDict[tuple, CoauthorLinks]" = OrderedDict()
        self._coauthor_links_cache_max = 100

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def set_api_key(self, api_key: str) -> None:
        self._api_key = api_key.strip()
        self.clear_author_cache()

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

    async def _get(self, url: str, params: dict, max_attempts: int = 5) -> dict:
        params = dict(params)
        if self._api_key:
            params["api_key"] = self._api_key
        if self._mailto:
            params.setdefault("mailto", self._mailto)
        client = await self._http_client()
        for attempt in range(max_attempts):
            async with self._semaphore:
                resp = await client.get(url, params=params)
            if resp.status_code == 429:
                if self._is_budget_exhausted(resp):
                    resp.raise_for_status()
                if attempt < max_attempts - 1:
                    retry_after = resp.headers.get("Retry-After")
                    delay = min(float(retry_after), 5.0) if retry_after else min(2**attempt, 5.0)
                    await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    def _is_budget_exhausted(self, resp: httpx.Response) -> bool:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                if float(retry_after) > 60:
                    return True
            except ValueError:
                pass
        try:
            text = json.dumps(resp.json()).casefold()
        except Exception:
            text = resp.text.casefold()
        return "insufficient budget" in text or "$0 remaining" in text

    async def search_authors(
        self, query: str, page: int = 1, per_page: int = 20
    ) -> tuple[list[AuthorResult], int]:
        cache_key = ("authors", query.casefold(), page, per_page)
        cached = self._get_search_cache(cache_key)
        if cached is not None:
            return cached
        data = await self._get(f"{API_BASE}/authors", {
            "search": query,
            "page": page,
            "per_page": per_page,
            "select": "id,display_name,last_known_institutions,works_count,cited_by_count",
        }, max_attempts=1)
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
        value = (results, total)
        self._put_search_cache(cache_key, value)
        return value

    async def search_institutions(
        self, query: str, page: int = 1, per_page: int = 20
    ) -> tuple[list[dict], int]:
        cache_key = ("institutions", query.casefold(), page, per_page)
        cached = self._get_search_cache(cache_key)
        if cached is not None:
            return cached
        data = await self._get(f"{API_BASE}/institutions", {
            "search": query,
            "page": page,
            "per_page": per_page,
            "select": "id,display_name,country_code,works_count,cited_by_count",
        }, max_attempts=1)
        results = [
            {
                "id": _short_id(item["id"]),
                "display_name": item.get("display_name", ""),
                "country_code": item.get("country_code"),
                "works_count": item.get("works_count", 0),
                "cited_by_count": item.get("cited_by_count", 0),
            }
            for item in data.get("results", [])
        ]
        total = data.get("meta", {}).get("count", len(results))
        value = (results, total)
        self._put_search_cache(cache_key, value)
        return value

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
        self._institution_authors_cache.clear()
        self._search_cache.clear()
        self._coauthor_summary_cache.clear()
        self._coauthor_links_cache.clear()

    def _get_search_cache(self, key: tuple):
        hit = self._search_cache.get(key)
        if hit is not None:
            self._search_cache.move_to_end(key)
        return hit

    def _put_search_cache(self, key: tuple, value: tuple[list, int]) -> None:
        self._search_cache[key] = value
        self._search_cache.move_to_end(key)
        while len(self._search_cache) > self._search_cache_max:
            self._search_cache.popitem(last=False)

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

    async def get_institution_authors(
        self,
        institution_id: str,
        limit: int = 50,
        sort: str = "works_count:desc",
    ) -> list[dict]:
        """Fetch institution-associated authors, paging when limit exceeds 200."""
        if limit <= 0:
            return []
        cache_key = (institution_id, limit, sort)
        hit = self._institution_authors_cache.get(cache_key)
        if hit is not None:
            self._institution_authors_cache.move_to_end(cache_key)
            return hit
        per_page = min(limit, 200)
        pages = max(1, (limit + per_page - 1) // per_page)
        results: list[dict] = []
        for page in range(1, pages + 1):
            data = await self._get(f"{API_BASE}/authors", {
                "filter": f"last_known_institutions.id:{institution_id}",
                "page": page,
                "per_page": per_page,
                "sort": sort,
                "select": (
                    "id,display_name,last_known_institutions,affiliations,"
                    "cited_by_count,works_count,topics,orcid"
                ),
            })
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < per_page:
                break
        results = results[:limit]
        self._institution_authors_cache[cache_key] = results
        self._institution_authors_cache.move_to_end(cache_key)
        while len(self._institution_authors_cache) > self._institution_authors_cache_max:
            self._institution_authors_cache.popitem(last=False)
        return results

    async def get_institution_authors_by_topics(
        self,
        institution_id: str,
        topic_ids: list[str],
        limit: int = 50,
    ) -> list[dict]:
        """Fetch institution authors matching any requested OpenAlex topic.

        The institution and topic clauses use AND semantics, while IDs within
        each topic chunk use OpenAlex's pipe-separated OR syntax.  Topic lists
        are canonicalized for caching, and large lists are chunked to keep URLs
        bounded.  Failed chunks propagate so partial candidate pools are never
        mistaken for a complete negative result.
        """
        if limit <= 0:
            return []
        topics = sorted(set(
            _short_id(topic_id) for topic_id in topic_ids if topic_id
        ))
        if not topics:
            return []

        institution_id = _short_id(institution_id)
        cache_key = ("topics", institution_id, tuple(topics), limit)
        hit = self._institution_authors_cache.get(cache_key)
        if hit is not None:
            self._institution_authors_cache.move_to_end(cache_key)
            return hit

        per_chunk = min(limit, 200)
        responses = await asyncio.gather(*[
            self._get(f"{API_BASE}/authors", {
                "filter": (
                    f"last_known_institutions.id:{institution_id},"
                    f"topics.id:{'|'.join(topic_chunk)}"
                ),
                "per_page": per_chunk,
                "sort": "cited_by_count:desc",
                "select": (
                    "id,display_name,last_known_institutions,affiliations,"
                    "cited_by_count,works_count,topics,orcid"
                ),
            })
            for topic_chunk in _chunks(topics, _FILTER_CHUNK)
        ])

        # An author may match topics from more than one chunk. Keep one record,
        # then restore a deterministic global citation ranking.
        by_id: dict[str, dict] = {}
        for response in responses:
            for author in response.get("results", []):
                raw_author_id = author.get("id")
                if raw_author_id:
                    by_id.setdefault(_short_id(raw_author_id), author)
        results = sorted(
            by_id.values(),
            key=lambda author: (
                -author.get("cited_by_count", 0),
                _short_id(author.get("id", "")),
            ),
        )[:limit]

        self._institution_authors_cache[cache_key] = results
        self._institution_authors_cache.move_to_end(cache_key)
        while len(self._institution_authors_cache) > self._institution_authors_cache_max:
            self._institution_authors_cache.popitem(last=False)
        return results

    async def get_institution_authors_by_hierarchy(
        self,
        institution_id: str,
        *,
        subfield_ids: list[str] | None = None,
        field_ids: list[str] | None = None,
        limit: int = 80,
    ) -> list[dict]:
        """Discover relevant authors from institution-attributed topical works.

        Author records can only be filtered by exact ``topics.id``. That is often
        too narrow: nearby researchers may share a subfield while OpenAlex assigns
        different fine-grained topics. The Works endpoint supports hierarchy
        filters, so one grouped request proposes author IDs from institution works;
        a batched author lookup then supplies the profiles used by the caller to
        validate last-known affiliation and rank topic overlap.
        """
        if limit <= 0:
            return []
        institution_id = _short_id(institution_id)
        subfields = sorted(set(
            _short_id(value) for value in (subfield_ids or []) if value
        ))
        fields = sorted(set(
            _short_id(value) for value in (field_ids or []) if value
        ))
        if subfields:
            hierarchy_filter = "topics.subfield.id"
            hierarchy_ids = subfields
        elif fields:
            hierarchy_filter = "topics.field.id"
            hierarchy_ids = fields
        else:
            return []

        cache_key = (
            "hierarchy", institution_id, hierarchy_filter,
            tuple(hierarchy_ids), limit,
        )
        hit = self._institution_authors_cache.get(cache_key)
        if hit is not None:
            self._institution_authors_cache.move_to_end(cache_key)
            return hit

        data = await self._get(f"{API_BASE}/works", {
            "filter": (
                f"authorships.institutions.id:{institution_id},"
                f"{hierarchy_filter}:{'|'.join(hierarchy_ids)}"
            ),
            "group_by": "authorships.author.id",
            "per_page": min(limit, 100),
        })
        author_ids = list(dict.fromkeys(
            _short_id(group["key"])
            for group in data.get("group_by", [])
            if isinstance(group, dict) and group.get("key")
        ))[:limit]
        profiles = await self.get_authors_batch(author_ids)
        by_id = {
            _short_id(profile["id"]): profile
            for profile in profiles
            if isinstance(profile, dict) and profile.get("id")
        }
        results = [by_id[author_id] for author_id in author_ids if author_id in by_id]
        self._institution_authors_cache[cache_key] = results
        self._institution_authors_cache.move_to_end(cache_key)
        while len(self._institution_authors_cache) > self._institution_authors_cache_max:
            self._institution_authors_cache.popitem(last=False)
        return results

    async def get_works_by_authors(
        self,
        author_ids: list[str],
        limit: int = 50,
    ) -> AuthorWorksPage:
        """Fetch bounded top works and report whether every matching work fit.

        OpenAlex sorts each OR-filter chunk globally, so ``limit=50`` means 50
        works for the whole chunk rather than 50 per author.  Callers must not
        persist the returned sample as a complete author ring when ``complete``
        is false.
        """
        if not author_ids:
            return AuthorWorksPage([], complete=True)
        chunk_list = list(_chunks(author_ids, _FILTER_CHUNK))
        per_chunk = min(limit, 200)
        responses = await asyncio.gather(*[
            self._get(f"{API_BASE}/works", {
                "filter": f"authorships.author.id:{'|'.join(chunk)}",
                "per_page": per_chunk,
                "sort": "cited_by_count:desc",
                "select": (
                    "id,title,authorships,referenced_works,is_authors_truncated"
                ),
            })
            for chunk in chunk_list
        ])
        combined: list[dict] = []
        complete = True
        for response in responses:
            page = response.get("results", [])
            combined.extend(page)
            total = response.get("meta", {}).get("count")
            if total is not None:
                complete = complete and total <= len(page)
            elif len(page) >= per_chunk:
                # OpenAlex normally supplies meta.count.  A full page without it
                # is still potentially truncated, so fail conservative.
                complete = False
        return AuthorWorksPage(combined, complete=complete)

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
                "select": (
                    "id,title,authorships,is_authors_truncated,"
                    "cited_by_count,publication_year,doi,topics"
                ),
            })
            for chunk in chunk_list
        ])
        combined: list[dict] = []
        for r in results:
            combined.extend(r.get("results", []))
        return combined

    async def get_coauthor_summary(
        self,
        author_id: str,
        verified_work_ids: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, dict]:
        """Return coauthors keyed by short OpenAlex author ID.

        The default path uses OpenAlex's server-side ``group_by`` aggregation, so
        even prolific authors cost one request instead of fetching and parsing a
        citation-sorted work sample.  When ``verified_work_ids`` is provided, the
        result is deliberately scoped to those works and includes a representative
        work title as ``label`` when one is available.

        Values have ``name`` and shared ``works_count`` fields, plus the optional
        ``label`` field. Verified-scope entries additionally carry one complete
        representative work edge (the same shape as ``get_coauthor_links``).
        Verified batches fail closed: if every reviewed work is not returned, no
        partial identity ring is exposed or cached. The returned mapping has a
        ``complete`` attribute; normal group-by results are bounded to 200 groups
        and report ``False`` when more groups exist.
        """
        if limit <= 0:
            return CoauthorSummary({}, complete=True)

        author_id = _short_id(author_id)
        capped_limit = min(limit, 200)
        verified_ids: list[str] | None = None
        verified_key: tuple[str, ...] | None = None
        if verified_work_ids is not None:
            verified_ids = list(dict.fromkeys(
                _short_id(work_id) for work_id in verified_work_ids if work_id
            ))
            verified_key = tuple(sorted(verified_ids))

        cache_key = (author_id, verified_key, capped_limit)
        hit = self._coauthor_summary_cache.get(cache_key)
        if hit is not None:
            self._coauthor_summary_cache.move_to_end(cache_key)
            return hit

        complete = True
        if verified_ids is None:
            data = await self._get(f"{API_BASE}/works", {
                "filter": f"authorships.author.id:{author_id}",
                "group_by": "authorships.author.id",
                "per_page": capped_limit,
            })
            groups = data.get("group_by", [])
            group_count = data.get("meta", {}).get("group_count")
            complete = (
                group_count <= capped_limit
                if isinstance(group_count, int)
                else len(groups) < capped_limit
            )
            summary: dict[str, dict] = {}
            for group in groups:
                raw_id = group.get("key")
                if not raw_id:
                    continue
                coauthor_id = _short_id(raw_id)
                if coauthor_id == author_id:
                    continue
                summary[coauthor_id] = {
                    "name": group.get("key_display_name") or coauthor_id,
                    "works_count": group.get("count", 0),
                }
        elif not verified_ids:
            summary = {}
        else:
            works = await self.get_works_batch(verified_ids, limit=200)
            requested = set(verified_ids)
            returned = {
                _short_id(work["id"])
                for work in works
                if work.get("id")
            }
            # A successful response can still omit a requested ID. Detect that
            # condition so incomplete evidence never poisons the LRU.
            complete = requested.issubset(returned)
            if not complete:
                missing = ", ".join(sorted(requested - returned))
                raise RuntimeError(
                    f"OpenAlex returned an incomplete verified-work batch: {missing}"
                )
            if any(work.get("is_authors_truncated") for work in works):
                raise RuntimeError(
                    "A verified work has truncated authorships; refusing a partial identity ring"
                )

            summary = {}
            seen_works: set[str] = set()
            for work in works:
                raw_work_id = work.get("id")
                if not raw_work_id:
                    continue
                work_id = _short_id(raw_work_id)
                if work_id in seen_works:
                    continue
                seen_works.add(work_id)

                author_ids = list(dict.fromkeys(
                    identity["author_id"]
                    for authorship in work.get("authorships", [])
                    if (identity := _authorship_identity(authorship)) is not None
                ))
                if author_id not in author_ids:
                    continue

                for coauthor_id in author_ids:
                    if coauthor_id == author_id:
                        continue
                    edge = _work_edge_evidence(work, author_id, coauthor_id)
                    if edge is None:
                        continue
                    entry = summary.setdefault(coauthor_id, {
                        "name": edge["right_name"],
                        "works_count": 0,
                    })
                    entry["works_count"] += 1
                    if "work_id" not in entry:
                        entry.update(edge)

            summary = dict(sorted(
                summary.items(),
                key=lambda item: (-item[1]["works_count"], item[0]),
            )[:capped_limit])

        result = CoauthorSummary(summary, complete=complete)
        self._coauthor_summary_cache[cache_key] = result
        self._coauthor_summary_cache.move_to_end(cache_key)
        while len(self._coauthor_summary_cache) > self._coauthor_summary_cache_max:
            self._coauthor_summary_cache.popitem(last=False)
        return result

    async def get_coauthor_links(
        self,
        left_author_ids: list[str],
        right_author_ids: list[str],
    ) -> list[dict]:
        """Return exact publication evidence connecting two author-ID sets.

        Each side is OR-filtered in URL-safe chunks and the author filter is
        repeated so OpenAlex applies AND semantics between the sets.  Every
        returned work is checked locally before evidence is emitted; this also
        handles overlapping input sets without ever producing a self-link. Cursor
        scans are capped at three 100-work pages per chunk pair; a capped result is
        useful for exact positive evidence but is deliberately not cached.

        Existing evidence keys (``left_id``, ``left_name``, ``right_id``,
        ``right_name``, ``label``, and ``work_id``) are preserved. Records also
        retain publication year, author count, topics/taxonomy, all author IDs,
        and the identity-bearing authorship metadata for each endpoint. The
        returned list has a ``complete`` attribute.
        """
        left_ids = sorted(set(
            _short_id(author_id) for author_id in left_author_ids if author_id
        ))
        right_ids = sorted(set(
            _short_id(author_id) for author_id in right_author_ids if author_id
        ))
        if not left_ids or not right_ids:
            return CoauthorLinks([], complete=True)

        cache_key = (tuple(left_ids), tuple(right_ids))
        hit = self._coauthor_links_cache.get(cache_key)
        if hit is not None:
            self._coauthor_links_cache.move_to_end(cache_key)
            return hit

        async def fetch_chunk_pair(
            left_chunk: list[str],
            right_chunk: list[str],
        ) -> tuple[list[dict], bool]:
            cursor = "*"
            seen_cursors: set[str] = set()
            works: list[dict] = []
            complete = False
            for _ in range(_COAUTHOR_LINK_PAGE_MAX):
                data = await self._get(f"{API_BASE}/works", {
                    "filter": (
                        f"authorships.author.id:{'|'.join(left_chunk)},"
                        f"authorships.author.id:{'|'.join(right_chunk)}"
                    ),
                    "cursor": cursor,
                    "per_page": 100,
                    "select": (
                        "id,title,authorships,is_authors_truncated,"
                        "publication_year,topics"
                    ),
                })
                page = data.get("results", [])
                works.extend(page)
                next_cursor = data.get("meta", {}).get("next_cursor")
                if not next_cursor:
                    complete = True
                    break
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise RuntimeError("OpenAlex returned a non-advancing cursor")
                seen_cursors.add(cursor)
                cursor = next_cursor
            return works, complete

        chunk_pairs = [
            (left_chunk, right_chunk)
            for left_chunk in _chunks(left_ids, _FILTER_CHUNK)
            for right_chunk in _chunks(right_ids, _FILTER_CHUNK)
        ]
        pages = await asyncio.gather(*[
            fetch_chunk_pair(left_chunk, right_chunk)
            for left_chunk, right_chunk in chunk_pairs
        ])

        # A work can satisfy more than one chunk pair.  Parse it once against the
        # complete original sets, which both deduplicates API overlap and recovers
        # every valid cross-set author pair on that work.
        works_by_id: dict[str, dict] = {}
        for page, _ in pages:
            for work in page:
                raw_work_id = work.get("id")
                if raw_work_id:
                    works_by_id.setdefault(_short_id(raw_work_id), work)

        evidence: list[dict] = []
        seen_pairs: set[tuple[str, str, str]] = set()
        for work_id, work in works_by_id.items():
            work_author_ids = {
                identity["author_id"]
                for authorship in work.get("authorships", [])
                if (identity := _authorship_identity(authorship)) is not None
            }
            work_left_ids = [
                author_id for author_id in left_ids if author_id in work_author_ids
            ]
            work_right_ids = [
                author_id for author_id in right_ids if author_id in work_author_ids
            ]
            for left_id in work_left_ids:
                for right_id in work_right_ids:
                    if left_id == right_id:
                        continue
                    # Keep orientation: if the requested sets overlap, A→B and
                    # B→A represent different set-join memberships.
                    dedup_key = (work_id, left_id, right_id)
                    if dedup_key in seen_pairs:
                        continue
                    seen_pairs.add(dedup_key)
                    edge = _work_edge_evidence(work, left_id, right_id)
                    if edge is not None:
                        evidence.append(edge)

        complete = (
            all(page_complete for _, page_complete in pages)
            and not any(
                work.get("is_authors_truncated")
                for work in works_by_id.values()
            )
        )
        result = CoauthorLinks(evidence, complete=complete)
        if complete:
            self._coauthor_links_cache[cache_key] = result
            self._coauthor_links_cache.move_to_end(cache_key)
            while len(self._coauthor_links_cache) > self._coauthor_links_cache_max:
                self._coauthor_links_cache.popitem(last=False)
        return result

    async def get_work(self, work_id: str) -> dict:
        return await self._get(f"{API_BASE}/works/{work_id}", {})

    async def search_works(
        self, query: str, page: int = 1, per_page: int = 20
    ) -> tuple[list[WorkResult], int]:
        cache_key = ("works", query.casefold(), page, per_page)
        cached = self._get_search_cache(cache_key)
        if cached is not None:
            return cached
        data = await self._get(f"{API_BASE}/works", {
            "search": query,
            "page": page,
            "per_page": per_page,
            "select": "id,title,publication_year,cited_by_count,authorships,doi",
        }, max_attempts=1)
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
        value = (results, total)
        self._put_search_cache(cache_key, value)
        return value

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
                "select": "id,authorships,referenced_works,is_authors_truncated",
            })
            for chunk in chunk_list
        ])
        combined: list[dict] = []
        for r in results:
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
                "select": (
                    "id,display_name,display_name_alternatives,last_known_institutions,"
                    "affiliations,cited_by_count,works_count,topics,orcid"
                ),
            })
            for chunk in chunk_list
        ])
        for r in results:
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
        ])
        combined: list[dict] = []
        for r in results:
            combined.extend(r.get("results", []))
        return combined

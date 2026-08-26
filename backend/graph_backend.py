import asyncio
from abc import ABC, abstractmethod

from backend.affiliation_overrides import (
    get_preferred_affiliation_override,
    get_verified_work_ids,
)
from backend.models import Connection
from backend.neighbor_store import NeighborCache
from backend.openalex_client import OpenAlexClient, _short_id

# A multi-author OpenAlex works query is capped and sorted globally, so a
# prolific author can consume the whole page before a low-output author appears.
# Missing authors are recovered with individual queries, but keep that fallback
# bounded independently of the client's own HTTP semaphore.
_INDIVIDUAL_WORK_FETCH_CONCURRENCY = 8


class _NeighborBatch(dict[str, list[Connection]]):
    """Fresh rings plus the source IDs whose evidence is exhaustive/cacheable."""

    def __init__(
        self,
        values: dict[str, list[Connection]],
        *,
        complete_ids: set[str],
    ):
        super().__init__(values)
        self.complete_ids = complete_ids


class GraphBackend(ABC):
    @abstractmethod
    async def get_neighbors(self, author_id: str) -> list[Connection]:
        pass

    async def get_neighbors_batch(
        self, author_ids: list[str], cached_only: bool = False
    ) -> _NeighborBatch:
        """Expand all author_ids concurrently. Override for bulk-query backends.

        `cached_only` asks the backend to serve only already-cached rings and
        skip remote-API fetches (ids without a cached ring resolve to []).
        Backends whose bulk reads carry no remote-API cost may ignore it.
        """
        results = await asyncio.gather(
            *[self.get_neighbors(aid) for aid in author_ids],
            return_exceptions=True,
        )
        values = {
            aid: ([] if isinstance(r, Exception) else r)
            for aid, r in zip(author_ids, results)
        }
        complete_ids = {
            aid for aid, result in zip(author_ids, results)
            if not isinstance(result, Exception)
        }
        return _NeighborBatch(values, complete_ids=complete_ids)


ALL_EDGE_TYPES = {"coauthor", "citation", "institution"}
ALL_WORK_EDGE_TYPES = {"authorship", "citation"}


def _is_work_id(id_: str) -> bool:
    """OpenAlex IDs are prefix-typed: works start with 'W', authors with 'A'."""
    return id_.startswith("W")


class IdentityScopedBackend(GraphBackend):
    """Conservatively enforce reviewed work scopes around conflated authors.

    The wrapped graph backend may contain broad/truncated rings built from a
    conflated OpenAlex author record.  Scoped authors never reach that backend;
    their coauthor ring is derived only from reviewed works.  Edges *to* a scoped
    author are retained only when that reviewed ring confirms the relationship.
    This wrapper is used by every path/expansion flow, not just Institution
    Explorer, so adding a corrected researcher cannot reintroduce false edges.
    """

    def __init__(
        self,
        backend: GraphBackend,
        client: OpenAlexClient,
        *,
        edge_types: set[str],
    ):
        self._backend = backend
        self._client = client
        self._edge_types = edge_types

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        return (await self.get_neighbors_batch([author_id])).get(author_id, [])

    async def get_neighbors_batch(
        self,
        author_ids: list[str],
        cached_only: bool = False,
    ) -> _NeighborBatch:
        unique_ids = list(dict.fromkeys(author_ids))
        scoped_sources: dict[str, frozenset[str]] = {}
        for author_id in unique_ids:
            if _is_work_id(author_id):
                continue
            work_ids = get_verified_work_ids(author_id)
            if work_ids is not None:
                scoped_sources[author_id] = work_ids
        inner_ids = [author_id for author_id in unique_ids if author_id not in scoped_sources]
        inner = (
            await self._backend.get_neighbors_batch(inner_ids, cached_only=cached_only)
            if inner_ids
            else _NeighborBatch({}, complete_ids=set())
        )
        complete_ids = set(getattr(inner, "complete_ids", inner.keys()))

        # Discover reviewed targets in the broad rings.  They must be validated
        # from the target's scoped publications before an undirected edge is safe.
        scoped_targets: dict[str, frozenset[str]] = {}
        for connections in inner.values():
            for connection in connections:
                target_id = connection.target_author_id
                if _is_work_id(target_id) or target_id in scoped_targets:
                    continue
                work_ids = get_verified_work_ids(target_id)
                if work_ids is not None:
                    scoped_targets[target_id] = work_ids

        all_scoped = dict(scoped_targets)
        all_scoped.update(scoped_sources)
        summaries: dict[str, dict[str, dict]] = {}
        complete_summaries: set[str] = set()
        if all_scoped and not cached_only:
            scoped_ids = list(all_scoped)
            fetched = await asyncio.gather(*[
                self._client.get_coauthor_summary(
                    author_id,
                    verified_work_ids=all_scoped[author_id],
                )
                for author_id in scoped_ids
            ], return_exceptions=True)
            for author_id, summary in zip(scoped_ids, fetched):
                if not isinstance(summary, dict):
                    continue
                summaries[author_id] = summary
                if bool(getattr(summary, "complete", False)):
                    complete_summaries.add(author_id)

        result: dict[str, list[Connection]] = {}
        for source_id in unique_ids:
            if source_id in scoped_sources:
                # Never expose citations/institutions from a conflated author ID.
                # Coauthors are symmetric and can be rebuilt exactly from the
                # reviewed works.
                connections: list[Connection] = []
                source_complete = source_id in complete_summaries
                if "coauthor" in self._edge_types:
                    for target_id, edge in summaries.get(source_id, {}).items():
                        target_scope = get_verified_work_ids(target_id)
                        if target_scope is not None:
                            reciprocal = summaries.get(target_id, {})
                            if target_id not in complete_summaries:
                                source_complete = False
                            if source_id not in reciprocal:
                                continue
                        connections.append(Connection(
                            target_author_id=target_id,
                            target_name=edge.get("name") or target_id,
                            connection_type="coauthor",
                            label=edge.get("label") or "",
                        ))
                result[source_id] = connections
                if source_complete:
                    complete_ids.add(source_id)
                else:
                    complete_ids.discard(source_id)
                continue

            filtered: list[Connection] = []
            source_complete = source_id in complete_ids
            for connection in inner.get(source_id, []):
                target_id = connection.target_author_id
                target_scope = scoped_targets.get(target_id)
                if target_scope is None:
                    filtered.append(connection)
                    continue

                if (
                    _is_work_id(source_id)
                    and connection.connection_type == "authorship"
                    and source_id in target_scope
                ):
                    filtered.append(connection)
                    continue

                reviewed_edge = summaries.get(target_id, {}).get(source_id)
                if target_id not in complete_summaries:
                    source_complete = False
                if connection.connection_type == "coauthor" and reviewed_edge is not None:
                    override = get_preferred_affiliation_override(target_id)
                    filtered.append(Connection(
                        target_author_id=target_id,
                        target_name=(
                            override.display_name if override else connection.target_name
                        ),
                        connection_type="coauthor",
                        label=reviewed_edge.get("label") or connection.label,
                    ))
            result[source_id] = filtered
            if source_complete:
                complete_ids.add(source_id)
            else:
                complete_ids.discard(source_id)

        return _NeighborBatch(
            {author_id: result.get(author_id, []) for author_id in author_ids},
            complete_ids=complete_ids & set(author_ids),
        )


class OpenAlexBackend(GraphBackend):
    def __init__(
        self,
        client: OpenAlexClient,
        edge_types: set[str] | None = None,
        work_edge_types: set[str] | None = None,
        neighbor_cache: NeighborCache | None = None,
    ):
        self._client = client
        self._edge_types = edge_types if edge_types is not None else ALL_EDGE_TYPES
        self._work_edge_types = work_edge_types if work_edge_types is not None else ALL_WORK_EDGE_TYPES
        # Shared ring cache: bounded LRU in front of a durable store. Cache keys
        # include the active edge set so a cheap coauthor-only ring can never be
        # mistaken for an all-edge ring by another request.
        self._cache = neighbor_cache if neighbor_cache is not None else NeighborCache()
        # Deduplicate overlapping cache misses from parallel path searches: the
        # first batch to need an id owns its fetch (a Future in _inflight);
        # concurrent batches await that future for shared ids while fetching
        # their own un-shared ids immediately.
        self._inflight: dict[str, asyncio.Future] = {}

    def _cache_key(self, id_: str) -> str:
        """Namespace durable rings by node kind, active edges, and cache schema."""
        if _is_work_id(id_):
            node_kind = "work"
            active = self._work_edge_types
        else:
            node_kind = "author"
            active = self._edge_types
        edge_scope = ",".join(sorted(active)) or "none"
        return f"v3:{node_kind}:{edge_scope}:{id_}"

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        tasks = []
        if "coauthor" in self._edge_types:
            tasks.append(self._get_coauthors(author_id))
        if "citation" in self._edge_types:
            tasks.append(self._get_citation_neighbors(author_id))
        if "institution" in self._edge_types:
            tasks.append(self._get_institution_neighbors(author_id))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        connections: list[Connection] = []
        for r in results:
            if not isinstance(r, Exception):
                connections.extend(r)

        seen: set[str] = set()
        unique: list[Connection] = []
        for c in connections:
            if c.target_author_id not in seen:
                seen.add(c.target_author_id)
                unique.append(c)
        return unique

    async def _get_coauthors(self, author_id: str) -> list[Connection]:
        works = await self._client.get_author_works(author_id, limit=20)
        connections: list[Connection] = []
        for work in works:
            title = work.get("title") or "Untitled"
            for authorship in work.get("authorships", []):
                coauthor_id = _short_id(authorship["author"]["id"])
                if coauthor_id != author_id:
                    connections.append(Connection(
                        target_author_id=coauthor_id,
                        target_name=authorship["author"]["display_name"],
                        connection_type="coauthor",
                        label=title,
                    ))
        return connections

    async def _get_citation_neighbors(self, author_id: str) -> list[Connection]:
        works = await self._client.get_author_works(author_id, limit=5)
        if not works:
            return []

        titles = {_short_id(w["id"]): (w.get("title") or "Untitled") for w in works}
        work_ids = list(titles.keys())

        citing_results = await asyncio.gather(
            *[self._client.get_citing_works(wid, limit=10) for wid in work_ids],
            return_exceptions=True,
        )

        incoming: dict[str, Connection] = {}
        for work_id, citing_or_exc in zip(work_ids, citing_results):
            if isinstance(citing_or_exc, Exception):
                continue
            title = titles[work_id]
            for citing_work in citing_or_exc:
                for authorship in citing_work.get("authorships", []):
                    citer_id = _short_id(authorship["author"]["id"])
                    if citer_id != author_id:
                        incoming[citer_id] = Connection(
                            target_author_id=citer_id,
                            target_name=authorship["author"]["display_name"],
                            connection_type="citation",
                            direction="incoming",
                            label=title,
                        )

        referenced_ids = {
            _short_id(rid) for w in works for rid in w.get("referenced_works", [])
        }
        outgoing: dict[str, Connection] = {}
        if referenced_ids:
            referenced_details = await self._client.get_works_batch(list(referenced_ids))
            for rwork in referenced_details:
                rtitle = rwork.get("title") or "Untitled"
                for authorship in rwork.get("authorships", []):
                    author = authorship.get("author")
                    if not author or not author.get("id"):
                        continue
                    target_id = _short_id(author["id"])
                    if target_id == author_id:
                        continue
                    outgoing[target_id] = Connection(
                        target_author_id=target_id,
                        target_name=author.get("display_name", ""),
                        connection_type="citation",
                        direction="outgoing",
                        label=rtitle,
                    )

        connections: list[Connection] = []
        for target_id in set(incoming) | set(outgoing):
            inc, out = incoming.get(target_id), outgoing.get(target_id)
            if inc and out:
                connections.append(Connection(
                    target_author_id=target_id,
                    target_name=inc.target_name or out.target_name,
                    connection_type="citation",
                    direction="mutual",
                    label=inc.label,
                ))
            else:
                connections.append(inc or out)
        return connections

    async def get_neighbors_batch(
        self, ids: list[str], cached_only: bool = False
    ) -> _NeighborBatch:
        """
        Return neighbors for all ids (author or work). Reads walk the cache layers
        in order — in-memory LRU, then the durable store, then OpenAlex. Rings are
        fetched and namespaced under the active edge set, so coauthor-only searches
        do no citation/institution work and cannot poison a broader cache entry.
        Incomplete bounded samples can serve the current request but are not written
        to either cache layer. With cached_only=True (the stitch pass), OpenAlex is
        never consulted and uncached ids resolve to [].
        """
        unique = sorted(set(ids))
        resolved: dict[str, list[Connection]] = {}
        complete_ids: set[str] = set()

        # 1) In-memory hits (sync, LRU-touch).
        for i in unique:
            hit = self._cache.get_memory(self._cache_key(i))
            if hit is not None:
                resolved[i] = hit
                complete_ids.add(i)

        if cached_only:
            # Memory + durable store only; no OpenAlex, no in-flight claims.
            misses = [i for i in unique if i not in resolved]
            if misses:
                cache_to_source = {self._cache_key(i): i for i in misses}
                stored = await self._cache.fetch_from_store(list(cache_to_source))
                resolved.update({
                    cache_to_source[key]: connections
                    for key, connections in stored.items()
                })
                complete_ids.update(
                    cache_to_source[key] for key in stored
                )
        else:
            # 2) Claim ids nobody is fetching; collect futures for ids already
            #    in flight. A future resolving to None means its owner failed;
            #    waiters re-enter the claim loop and fetch those ids themselves.
            loop = asyncio.get_running_loop()
            pending = [i for i in unique if i not in resolved]
            while pending:
                owned: list[str] = []
                waiting: dict[str, asyncio.Future] = {}
                for i in pending:
                    hit = self._cache.get_memory(self._cache_key(i))
                    if hit is not None:
                        resolved[i] = hit
                        complete_ids.add(i)
                        continue
                    fut = self._inflight.get(i)
                    if fut is None:
                        self._inflight[i] = loop.create_future()
                        owned.append(i)
                    else:
                        waiting[i] = fut

                # 3) Fetch owned ids: durable store first, then OpenAlex.
                try:
                    if owned:
                        cache_to_source = {self._cache_key(i): i for i in owned}
                        stored = await self._cache.fetch_from_store(list(cache_to_source))
                        from_store = {
                            cache_to_source[key]: connections
                            for key, connections in stored.items()
                        }
                        complete_ids.update(from_store)
                        still_missing = [i for i in owned if i not in from_store]
                        fresh = _NeighborBatch({}, complete_ids=set())
                        if still_missing:
                            fresh = await self._fetch_neighbors_batch(still_missing)
                            cacheable = {
                                self._cache_key(i): fresh[i]
                                for i in fresh.complete_ids
                            }
                            if cacheable:
                                self._cache.put(cacheable)
                        for i in owned:
                            conns = from_store[i] if i in from_store else fresh.get(i, [])
                            is_complete = i in from_store or i in fresh.complete_ids
                            resolved[i] = conns
                            if is_complete:
                                complete_ids.add(i)
                            else:
                                complete_ids.discard(i)
                            fut = self._inflight.pop(i)
                            if not fut.done():
                                fut.set_result((conns, is_complete))
                except BaseException:
                    # Resolve our futures with the retry sentinel so waiters
                    # refetch, and drop the in-flight entries so later calls
                    # retry too.
                    for i in owned:
                        fut = self._inflight.pop(i, None)
                        if fut is not None and not fut.done():
                            fut.set_result(None)
                    raise

                # 4) Await fetches owned by concurrent batches; ids whose owner
                #    failed go back through the claim loop.
                pending = []
                for i, fut in waiting.items():
                    outcome = await fut
                    if outcome is None:
                        pending.append(i)
                    else:
                        conns, is_complete = outcome
                        resolved[i] = conns
                        if is_complete:
                            complete_ids.add(i)
                        else:
                            complete_ids.discard(i)

        result: dict[str, list[Connection]] = {}
        for i in ids:
            active = self._work_edge_types if _is_work_id(i) else self._edge_types
            result[i] = [c for c in resolved.get(i, []) if c.connection_type in active]
        return _NeighborBatch(
            result,
            complete_ids=complete_ids & set(ids),
        )

    async def _fetch_neighbors_batch(self, ids: list[str]) -> _NeighborBatch:
        """
        Fetch configured connection types for the given ids (no cache check),
        splitting work ids from author ids so each routes through its own neighbor
        logic. Returned completeness metadata controls which source rings may be
        persisted.
        """
        work_ids = [i for i in ids if _is_work_id(i)]
        author_ids = [i for i in ids if not _is_work_id(i)]

        tasks = []
        if author_ids:
            work_edge_types = self._edge_types & {"coauthor", "citation"}
            if work_edge_types:
                tasks.append(self._batch_works_connections(
                    author_ids,
                    edge_types=work_edge_types,
                ))
            if "institution" in self._edge_types:
                tasks.append(self._batch_institutions(author_ids))
        if work_ids and self._work_edge_types:
            tasks.append(self._batch_work_neighbors(
                work_ids,
                edge_types=self._work_edge_types,
            ))

        # A cached ring represents *all* enabled connection types.  Returning a
        # successful sibling when any subquery failed would turn a transient API
        # error into a durable partial (often empty) ring.  Fail the whole fetch
        # instead; get_neighbors_batch releases its in-flight claims and a later
        # request retries from OpenAlex.
        results = await asyncio.gather(*tasks)

        by_source: dict[str, list[Connection]] = {i: [] for i in ids}
        seen: dict[str, set[str]] = {i: set() for i in ids}
        complete_ids = set(ids)

        for batch_result in results:
            complete_ids.difference_update(
                set(batch_result) - batch_result.complete_ids
            )
            for src_id, connections in batch_result.items():
                for conn in connections:
                    if conn.target_author_id not in seen[src_id]:
                        seen[src_id].add(conn.target_author_id)
                        by_source[src_id].append(conn)

        return _NeighborBatch(by_source, complete_ids=complete_ids)

    async def _batch_works_connections(
        self, author_ids: list[str], *, edge_types: set[str] | None = None
    ) -> _NeighborBatch:
        """Fetch works for all frontier authors once; derive co-author and citation edges."""
        et = edge_types if edge_types is not None else self._edge_types
        author_set = set(author_ids)
        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}

        works = await self._client.get_works_by_authors(author_ids)
        self._require_untruncated_work_evidence(works, "shared author-works query")
        shared_complete = bool(getattr(works, "complete", True))
        complete_ids = set(author_ids) if shared_complete else set()

        # The shared endpoint returns the top 50 works across the *whole* filter,
        # not 50 per author. If that page is incomplete, recover every author
        # individually so low-output rings can still become complete/cacheable.
        # With a complete shared page, only absent authors need the conservative
        # fallback. A one-author query is already the fallback, and an incomplete
        # prolific-author sample may serve this request but is never persisted.
        represented = self._represented_authors(works) & author_set
        missing = [aid for aid in author_ids if aid not in represented]
        recovery_ids: list[str] = []
        if len(author_ids) > 1:
            recovery_ids = list(author_ids) if not shared_complete else missing
        if recovery_ids:
            gate = asyncio.Semaphore(_INDIVIDUAL_WORK_FETCH_CONCURRENCY)

            async def fetch_individual(author_id: str) -> tuple[str, list[dict]]:
                async with gate:
                    individual = await self._client.get_works_by_authors([author_id])
                self._require_untruncated_work_evidence(
                    individual,
                    f"individual author-works query for {author_id}",
                )
                if individual and author_id not in self._represented_authors(individual):
                    raise RuntimeError(
                        "Individual author-works query returned no authorship "
                        f"evidence for {author_id}"
                    )
                return author_id, individual

            recovered = await asyncio.gather(
                *(fetch_individual(author_id) for author_id in recovery_ids)
            )
            complete_ids.update(
                author_id
                for author_id, author_works in recovered
                if bool(getattr(author_works, "complete", True))
            )
            works = self._deduplicate_works([
                *works,
                *(work for _, author_works in recovered for work in author_works),
            ])

        work_to_sources: dict[str, tuple[str, list[str]]] = {}
        # referenced (cited) work id -> frontier authors whose own paper cites it
        referenced_to_sources: dict[str, list[str]] = {}
        for work in works:
            work_id = _short_id(work["id"])
            title = work.get("title") or "Untitled"
            work_author_map = {
                _short_id(a["author"]["id"]): a["author"].get("display_name", "")
                for a in work.get("authorships", [])
                if a.get("author") and a["author"].get("id")
            }
            frontier_in_work = [aid for aid in work_author_map if aid in author_set]

            if "coauthor" in et:
                for src_id in frontier_in_work:
                    for coauthor_id, coauthor_name in work_author_map.items():
                        if coauthor_id not in author_set:
                            by_source[src_id].append(Connection(
                                target_author_id=coauthor_id,
                                target_name=coauthor_name,
                                connection_type="coauthor",
                                label=title,
                            ))

            if "citation" in et and frontier_in_work:
                work_to_sources[work_id] = (title, frontier_in_work)
                for ref_id in {_short_id(rid) for rid in work.get("referenced_works", [])}:
                    referenced_to_sources.setdefault(ref_id, []).extend(frontier_in_work)

        # A coauthor-only traversal can recover beyond the top-work page cheaply
        # with OpenAlex's grouped coauthor endpoint. Positive groups augment the
        # current ring even when the 200-group bound is reached; the source becomes
        # cache-complete only when that summary explicitly says it was exhaustive.
        if et == {"coauthor"}:
            incomplete_ids = [
                author_id for author_id in author_ids
                if author_id not in complete_ids
            ]
            if incomplete_ids:
                gate = asyncio.Semaphore(_INDIVIDUAL_WORK_FETCH_CONCURRENCY)

                async def fetch_summary(author_id: str) -> tuple[str, dict[str, dict]]:
                    async with gate:
                        summary = await self._client.get_coauthor_summary(author_id)
                    if not isinstance(summary, dict):
                        raise TypeError(
                            f"Coauthor summary for {author_id} did not return a mapping"
                        )
                    return author_id, summary

                summaries = await asyncio.gather(*(
                    fetch_summary(author_id) for author_id in incomplete_ids
                ))
                for source_id, summary in summaries:
                    for target_id, edge in summary.items():
                        target_id = _short_id(target_id)
                        if target_id == source_id or target_id in author_set:
                            continue
                        by_source[source_id].append(Connection(
                            target_author_id=target_id,
                            target_name=edge.get("name") or target_id,
                            connection_type="coauthor",
                            label=edge.get("label") or "",
                        ))
                    if bool(getattr(summary, "complete", False)):
                        complete_ids.add(source_id)

        if "citation" not in et:
            return _NeighborBatch(by_source, complete_ids=complete_ids)
        if not work_to_sources and not referenced_to_sources:
            return _NeighborBatch(by_source, complete_ids=complete_ids)

        # incoming: papers that cite our frontier's own works (src is cited)
        incoming: dict[str, dict[str, Connection]] = {aid: {} for aid in author_ids}
        if work_to_sources:
            citing_papers = await self._client.get_citing_works_for_works(
                list(work_to_sources.keys())[:50]
            )
            self._require_untruncated_work_evidence(
                citing_papers,
                "incoming-citation query",
            )
            if not bool(getattr(citing_papers, "complete", False)):
                complete_ids.clear()
            for paper in citing_papers:
                referenced = {_short_id(wid) for wid in paper.get("referenced_works", [])}
                for authorship in paper.get("authorships", []):
                    if not authorship.get("author") or not authorship["author"].get("id"):
                        continue
                    citer_id = _short_id(authorship["author"]["id"])
                    if citer_id in author_set:
                        continue
                    citer_name = authorship["author"].get("display_name", "")
                    for work_id in referenced & work_to_sources.keys():
                        title, src_ids = work_to_sources[work_id]
                        for src_id in src_ids:
                            incoming[src_id][citer_id] = Connection(
                                target_author_id=citer_id,
                                target_name=citer_name,
                                connection_type="citation",
                                direction="incoming",
                                label=title,
                            )

        # outgoing: works our frontier's own papers reference (src is the citer)
        outgoing: dict[str, dict[str, Connection]] = {aid: {} for aid in author_ids}
        if referenced_to_sources:
            referenced_details = await self._client.get_works_batch(
                list(referenced_to_sources.keys())[:50]
            )
            self._require_untruncated_work_evidence(
                referenced_details,
                "referenced-work query",
            )
            if not bool(getattr(referenced_details, "complete", False)):
                complete_ids.clear()
            for rwork in referenced_details:
                rwork_id = _short_id(rwork["id"])
                src_ids = referenced_to_sources.get(rwork_id, [])
                if not src_ids:
                    continue
                rtitle = rwork.get("title") or "Untitled"
                for authorship in rwork.get("authorships", []):
                    author = authorship.get("author")
                    if not author or not author.get("id"):
                        continue
                    target_id = _short_id(author["id"])
                    if target_id in author_set:
                        continue
                    target_name = author.get("display_name", "")
                    for src_id in src_ids:
                        outgoing[src_id][target_id] = Connection(
                            target_author_id=target_id,
                            target_name=target_name,
                            connection_type="citation",
                            direction="outgoing",
                            label=rtitle,
                        )

        # Merge per (src, target): if both directions were found, collapse into a
        # single "mutual" Connection now, before the generic target-id dedup in
        # _fetch_neighbors_batch would otherwise silently drop one direction.
        for src_id in author_ids:
            for target_id in set(incoming[src_id]) | set(outgoing[src_id]):
                inc = incoming[src_id].get(target_id)
                out = outgoing[src_id].get(target_id)
                if inc and out:
                    by_source[src_id].append(Connection(
                        target_author_id=target_id,
                        target_name=inc.target_name or out.target_name,
                        connection_type="citation",
                        direction="mutual",
                        label=inc.label,
                    ))
                else:
                    by_source[src_id].append(inc or out)

        return _NeighborBatch(by_source, complete_ids=complete_ids)

    @staticmethod
    def _represented_authors(works: list[dict]) -> set[str]:
        """Return author IDs whose authorship evidence is present in ``works``."""
        return {
            _short_id(author["id"])
            for work in works
            for authorship in work.get("authorships", [])
            if (author := authorship.get("author")) and author.get("id")
        }

    @staticmethod
    def _deduplicate_works(works: list[dict]) -> list[dict]:
        """Merge shared and per-author results without reprocessing publications."""
        unique: dict[str, dict] = {}
        for work in works:
            work_id = work.get("id")
            if not work_id:
                raise RuntimeError("Publication evidence is missing an OpenAlex work ID")
            unique.setdefault(_short_id(work_id), work)
        return list(unique.values())

    @staticmethod
    def _require_untruncated_work_evidence(works: list[dict], context: str) -> None:
        """Reject authorship truncation, which cannot form a valid current ring."""
        if any(work.get("is_authors_truncated") for work in works):
            raise RuntimeError(f"Truncated authorship evidence from {context}")

    async def _batch_work_neighbors(
        self, work_ids: list[str], *, edge_types: set[str] | None = None
    ) -> _NeighborBatch:
        """
        Work-origin neighbors: only its direct authors ("authorship") and authors
        who directly cited it ("citation", always direction="incoming" since a
        work node never cites anything itself in this model).
        """
        et = edge_types if edge_types is not None else self._work_edge_types
        by_source: dict[str, list[Connection]] = {wid: [] for wid in work_ids}

        works = await self._client.get_works_batch(work_ids)
        self._require_untruncated_work_evidence(works, "work-neighbor query")
        complete_ids = (
            set(work_ids) if bool(getattr(works, "complete", True)) else set()
        )
        meta = {_short_id(w["id"]): w for w in works}

        if "authorship" in et:
            for wid in work_ids:
                w = meta.get(wid)
                if not w:
                    continue
                title = w.get("title") or "Untitled"
                for authorship in w.get("authorships", []):
                    author = authorship.get("author")
                    if not author or not author.get("id"):
                        continue
                    by_source[wid].append(Connection(
                        target_author_id=_short_id(author["id"]),
                        target_name=author.get("display_name", ""),
                        connection_type="authorship",
                        label=title,
                    ))

        if "citation" in et:
            citing_papers = await self._client.get_citing_works_for_works(work_ids)
            self._require_untruncated_work_evidence(
                citing_papers,
                "work-citation query",
            )
            if not bool(getattr(citing_papers, "complete", False)):
                complete_ids.clear()
            work_id_set = set(work_ids)
            for paper in citing_papers:
                referenced = {_short_id(r) for r in paper.get("referenced_works", [])} & work_id_set
                if not referenced:
                    continue
                for authorship in paper.get("authorships", []):
                    author = authorship.get("author")
                    if not author or not author.get("id"):
                        continue
                    citer_id = _short_id(author["id"])
                    citer_name = author.get("display_name", "")
                    for wid in referenced:
                        title = (meta.get(wid) or {}).get("title") or "Untitled"
                        by_source[wid].append(Connection(
                            target_author_id=citer_id,
                            target_name=citer_name,
                            connection_type="citation",
                            direction="incoming",
                            label=title,
                        ))

        return _NeighborBatch(by_source, complete_ids=complete_ids)

    async def _batch_institutions(self, author_ids: list[str]) -> _NeighborBatch:
        """Fetch all frontier authors' institutions then their colleagues — 2 API calls."""
        author_set = set(author_ids)
        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}

        authors = await self._client.get_authors_batch(author_ids)
        complete_ids = (
            set(author_ids) if bool(getattr(authors, "complete", False)) else set()
        )

        inst_to_sources: dict[str, tuple[str, list[str]]] = {}
        for author in authors:
            src_id = _short_id(author["id"])
            if src_id not in author_set:
                continue
            for inst in (author.get("last_known_institutions") or []):
                inst_id = _short_id(inst["id"])
                inst_name = inst.get("display_name", "Unknown institution")
                if inst_id not in inst_to_sources:
                    inst_to_sources[inst_id] = (inst_name, [])
                inst_to_sources[inst_id][1].append(src_id)

        if not inst_to_sources:
            return _NeighborBatch(by_source, complete_ids=complete_ids)

        colleagues = await self._client.get_institution_authors_batch(list(inst_to_sources.keys()))
        if not bool(getattr(colleagues, "complete", False)):
            complete_ids.clear()
        for colleague in colleagues:
            colleague_id = _short_id(colleague["id"])
            if colleague_id in author_set:
                continue
            colleague_name = colleague.get("display_name", "")
            for inst in (colleague.get("last_known_institutions") or []):
                inst_id = _short_id(inst["id"])
                if inst_id in inst_to_sources:
                    inst_name, src_ids = inst_to_sources[inst_id]
                    for src_id in src_ids:
                        by_source[src_id].append(Connection(
                            target_author_id=colleague_id,
                            target_name=colleague_name,
                            connection_type="institution",
                            label=inst_name,
                        ))

        return _NeighborBatch(by_source, complete_ids=complete_ids)

    async def _get_institution_neighbors(self, author_id: str) -> list[Connection]:
        author = await self._client.get_author(author_id)
        institutions = author.get("last_known_institutions", [])
        if not institutions:
            return []
        inst_id = _short_id(institutions[0]["id"])
        inst_name = institutions[0].get("display_name", "Unknown institution")
        colleagues = await self._client.get_institution_authors(inst_id, limit=50)
        return [
            Connection(
                target_author_id=_short_id(c["id"]),
                target_name=c["display_name"],
                connection_type="institution",
                label=inst_name,
            )
            for c in colleagues
            if _short_id(c["id"]) != author_id
        ]

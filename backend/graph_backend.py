import asyncio
import logging
from abc import ABC, abstractmethod

from backend.models import Connection
from backend.openalex_client import OpenAlexClient, _short_id

log = logging.getLogger(__name__)


class GraphBackend(ABC):
    @abstractmethod
    async def get_neighbors(self, author_id: str) -> list[Connection]:
        pass

    async def get_neighbors_batch(self, author_ids: list[str]) -> dict[str, list[Connection]]:
        """Expand all author_ids concurrently. Override for bulk-query backends."""
        results = await asyncio.gather(
            *[self.get_neighbors(aid) for aid in author_ids],
            return_exceptions=True,
        )
        return {
            aid: ([] if isinstance(r, Exception) else r)
            for aid, r in zip(author_ids, results)
        }


ALL_EDGE_TYPES = {"coauthor", "citation", "institution"}
ALL_WORK_EDGE_TYPES = {"authorship", "citation"}


def _is_work_id(id_: str) -> bool:
    """OpenAlex IDs are prefix-typed: works start with 'W', authors with 'A'."""
    return id_.startswith("W")


class OpenAlexBackend(GraphBackend):
    def __init__(
        self,
        client: OpenAlexClient,
        edge_types: set[str] | None = None,
        work_edge_types: set[str] | None = None,
        neighbor_cache: dict | None = None,
        on_cache_updated: "callable | None" = None,
    ):
        self._client = client
        self._edge_types = edge_types if edge_types is not None else ALL_EDGE_TYPES
        self._work_edge_types = work_edge_types if work_edge_types is not None else ALL_WORK_EDGE_TYPES
        # Shared ring cache: id (author or work) → list[Connection] (all edge types).
        # Populated with ALL_EDGE_TYPES/ALL_WORK_EDGE_TYPES so each ring is fetched
        # once and reused across requests regardless of which edge types are active.
        self._cache: dict[str, list[Connection]] = neighbor_cache if neighbor_cache is not None else {}
        # Called whenever new entries are written to the cache (e.g. to persist to disk).
        self._on_cache_updated = on_cache_updated

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

    async def get_neighbors_batch(self, ids: list[str]) -> dict[str, list[Connection]]:
        """
        Return neighbors for all ids (author or work), using the ring cache where
        available. Uncached ids are fetched with ALL_EDGE_TYPES/ALL_WORK_EDGE_TYPES
        and stored; results are then filtered to the type-appropriate active set
        before returning (self._work_edge_types for work ids, self._edge_types for
        author ids — dispatched by OpenAlex ID prefix).
        """
        uncached = [i for i in ids if i not in self._cache]

        if uncached:
            fresh = await self._fetch_neighbors_batch(uncached)
            self._cache.update(fresh)
            if self._on_cache_updated:
                self._on_cache_updated()

        result: dict[str, list[Connection]] = {}
        for i in ids:
            active = self._work_edge_types if _is_work_id(i) else self._edge_types
            result[i] = [c for c in self._cache.get(i, []) if c.connection_type in active]
        return result

    async def _fetch_neighbors_batch(self, ids: list[str]) -> dict[str, list[Connection]]:
        """
        Fetch ALL connection types for the given ids (no cache check), splitting
        work ids from author ids so each routes through its own neighbor logic.
        Always uses ALL_EDGE_TYPES/ALL_WORK_EDGE_TYPES so each stored ring is complete.
        """
        work_ids = [i for i in ids if _is_work_id(i)]
        author_ids = [i for i in ids if not _is_work_id(i)]

        tasks = []
        if author_ids:
            tasks.append(self._batch_works_connections(author_ids, edge_types=ALL_EDGE_TYPES))
            tasks.append(self._batch_institutions(author_ids))
        if work_ids:
            tasks.append(self._batch_work_neighbors(work_ids, edge_types=ALL_WORK_EDGE_TYPES))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        by_source: dict[str, list[Connection]] = {i: [] for i in ids}
        seen: dict[str, set[str]] = {i: set() for i in ids}

        for batch_result in results:
            if isinstance(batch_result, Exception):
                log.warning("Batch neighbor query failed: %s", batch_result, exc_info=batch_result)
                continue
            for src_id, connections in batch_result.items():
                for conn in connections:
                    if conn.target_author_id not in seen[src_id]:
                        seen[src_id].add(conn.target_author_id)
                        by_source[src_id].append(conn)

        return by_source

    async def _batch_works_connections(
        self, author_ids: list[str], *, edge_types: set[str] | None = None
    ) -> dict[str, list[Connection]]:
        """Fetch works for all frontier authors once; derive co-author and citation edges."""
        et = edge_types if edge_types is not None else self._edge_types
        author_set = set(author_ids)
        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}

        works = await self._client.get_works_by_authors(author_ids)

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
            frontier_in_work = [aid for aid in author_ids if aid in work_author_map]

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

        if "citation" not in et:
            return by_source
        if not work_to_sources and not referenced_to_sources:
            return by_source

        # incoming: papers that cite our frontier's own works (src is cited)
        incoming: dict[str, dict[str, Connection]] = {aid: {} for aid in author_ids}
        if work_to_sources:
            citing_papers = await self._client.get_citing_works_for_works(
                list(work_to_sources.keys())[:50]
            )
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

        return by_source

    async def _batch_work_neighbors(
        self, work_ids: list[str], *, edge_types: set[str] | None = None
    ) -> dict[str, list[Connection]]:
        """
        Work-origin neighbors: only its direct authors ("authorship") and authors
        who directly cited it ("citation", always direction="incoming" since a
        work node never cites anything itself in this model).
        """
        et = edge_types if edge_types is not None else self._work_edge_types
        by_source: dict[str, list[Connection]] = {wid: [] for wid in work_ids}

        works = await self._client.get_works_batch(work_ids)
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

        return by_source

    async def _batch_institutions(self, author_ids: list[str]) -> dict[str, list[Connection]]:
        """Fetch all frontier authors' institutions then their colleagues — 2 API calls."""
        author_set = set(author_ids)
        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}

        authors = await self._client.get_authors_batch(author_ids)

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
            return by_source

        colleagues = await self._client.get_institution_authors_batch(list(inst_to_sources.keys()))
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

        return by_source

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

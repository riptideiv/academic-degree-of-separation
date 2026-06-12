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


class OpenAlexBackend(GraphBackend):
    def __init__(self, client: OpenAlexClient, edge_types: set[str] | None = None):
        self._client = client
        self._edge_types = edge_types if edge_types is not None else ALL_EDGE_TYPES

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

        connections: list[Connection] = []
        for work_id, citing_or_exc in zip(work_ids, citing_results):
            if isinstance(citing_or_exc, Exception):
                continue
            title = titles[work_id]
            for citing_work in citing_or_exc:
                for authorship in citing_work.get("authorships", []):
                    citer_id = _short_id(authorship["author"]["id"])
                    if citer_id != author_id:
                        connections.append(Connection(
                            target_author_id=citer_id,
                            target_name=authorship["author"]["display_name"],
                            connection_type="citation",
                            label=title,
                        ))
        return connections

    async def get_neighbors_batch(self, author_ids: list[str]) -> dict[str, list[Connection]]:
        """
        Batch expansion: 2-4 API calls for the entire frontier regardless of size.
        Co-author + citation share one works fetch; institution is a parallel pair.
        """
        tasks = []
        if "coauthor" in self._edge_types or "citation" in self._edge_types:
            tasks.append(self._batch_works_connections(author_ids))
        if "institution" in self._edge_types:
            tasks.append(self._batch_institutions(author_ids))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}
        seen: dict[str, set[str]] = {aid: set() for aid in author_ids}

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

    async def _batch_works_connections(self, author_ids: list[str]) -> dict[str, list[Connection]]:
        """Fetch works for all frontier authors once; derive co-author and citation edges."""
        author_set = set(author_ids)
        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}

        works = await self._client.get_works_by_authors(author_ids)

        work_to_sources: dict[str, tuple[str, list[str]]] = {}
        for work in works:
            work_id = _short_id(work["id"])
            title = work.get("title") or "Untitled"
            work_author_map = {
                _short_id(a["author"]["id"]): a["author"].get("display_name", "")
                for a in work.get("authorships", [])
            }
            frontier_in_work = [aid for aid in author_ids if aid in work_author_map]

            if "coauthor" in self._edge_types:
                for src_id in frontier_in_work:
                    for coauthor_id, coauthor_name in work_author_map.items():
                        if coauthor_id not in author_set:
                            by_source[src_id].append(Connection(
                                target_author_id=coauthor_id,
                                target_name=coauthor_name,
                                connection_type="coauthor",
                                label=title,
                            ))

            if "citation" in self._edge_types and frontier_in_work:
                work_to_sources[work_id] = (title, frontier_in_work)

        if "citation" in self._edge_types and work_to_sources:
            citing_papers = await self._client.get_citing_works_for_works(
                list(work_to_sources.keys())[:50]
            )
            for paper in citing_papers:
                referenced = {_short_id(wid) for wid in paper.get("referenced_works", [])}
                for authorship in paper.get("authorships", []):
                    citer_id = _short_id(authorship["author"]["id"])
                    if citer_id in author_set:
                        continue
                    citer_name = authorship["author"].get("display_name", "")
                    for work_id in referenced & work_to_sources.keys():
                        title, src_ids = work_to_sources[work_id]
                        for src_id in src_ids:
                            by_source[src_id].append(Connection(
                                target_author_id=citer_id,
                                target_name=citer_name,
                                connection_type="citation",
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
            for inst in author.get("last_known_institutions", []):
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
            for inst in colleague.get("last_known_institutions", []):
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

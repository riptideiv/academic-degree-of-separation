import asyncio
from abc import ABC, abstractmethod

from backend.models import Connection
from backend.openalex_client import OpenAlexClient, _short_id


class GraphBackend(ABC):
    @abstractmethod
    async def get_neighbors(self, author_id: str) -> list[Connection]:
        pass


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

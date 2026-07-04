import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from google.cloud import bigquery

from backend.graph_backend import ALL_EDGE_TYPES, GraphBackend
from backend.models import Connection

log = logging.getLogger(__name__)

_DATASET = "openalex-bigquery.openalex"
_executor = ThreadPoolExecutor(max_workers=4)


class BigQueryBackend(GraphBackend):
    def __init__(self, project_id: str, edge_types: set[str] | None = None):
        self._bq = bigquery.Client(project=project_id)
        self._edge_types = edge_types if edge_types is not None else ALL_EDGE_TYPES

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        batch = await self.get_neighbors_batch([author_id])
        return batch.get(author_id, [])

    async def get_neighbors_batch(
        self, author_ids: list[str], cached_only: bool = False
    ) -> dict[str, list[Connection]]:
        full_ids = [f"https://openalex.org/{aid}" for aid in author_ids]

        tasks = []
        if "coauthor" in self._edge_types:
            tasks.append(self._query_coauthors(full_ids))
        if "citation" in self._edge_types:
            tasks.append(self._query_citations(full_ids))
        if "institution" in self._edge_types:
            tasks.append(self._query_institutions(full_ids))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        by_source: dict[str, list[Connection]] = {aid: [] for aid in author_ids}
        seen: dict[str, set[str]] = {aid: set() for aid in author_ids}

        for batch_rows in results:
            if isinstance(batch_rows, Exception):
                log.warning("BigQuery query failed: %s", batch_rows, exc_info=batch_rows)
                continue
            log.debug("BigQuery query returned %d rows", len(batch_rows))
            for row in batch_rows:
                source_id = row["source_id"]
                target_id = row["target_id"]
                if source_id not in by_source:
                    continue
                if target_id not in seen[source_id] and target_id not in {*author_ids}:
                    seen[source_id].add(target_id)
                    by_source[source_id].append(Connection(
                        target_author_id=target_id,
                        target_name=row["target_name"] or "",
                        connection_type=row["connection_type"],
                        label=row["label"] or "",
                    ))

        return by_source

    async def _run_query(self, sql: str, params: list) -> list:
        loop = asyncio.get_event_loop()
        job_config = bigquery.QueryJobConfig(query_parameters=params)

        def _execute():
            log.debug("Running BQ query:\n%s", sql.strip())
            rows = list(self._bq.query(sql, job_config=job_config).result())
            log.debug("Query returned %d rows", len(rows))
            if rows:
                log.debug("First row: %s", dict(rows[0]))
            return rows

        return await loop.run_in_executor(_executor, _execute)

    async def _query_coauthors(self, full_ids: list[str]) -> list:
        sql = f"""
        SELECT DISTINCT
            REGEXP_EXTRACT(src.author.id, r'/([^/]+)$') AS source_id,
            REGEXP_EXTRACT(co.author.id,  r'/([^/]+)$') AS target_id,
            co.author.display_name AS target_name,
            'coauthor' AS connection_type,
            COALESCE(w.title, 'Untitled') AS label
        FROM `{_DATASET}.works` w,
             UNNEST(w.authorships) AS src,
             UNNEST(w.authorships) AS co
        WHERE src.author.id IN UNNEST(@author_ids)
          AND co.author.id  != src.author.id
          AND w.publication_year >= 2000
        LIMIT 2000
        """
        return await self._run_query(sql, [
            bigquery.ArrayQueryParameter("author_ids", "STRING", full_ids),
        ])

    async def _query_citations(self, full_ids: list[str]) -> list:
        # Find authors whose papers are cited by our frontier authors' papers,
        # or who cite our frontier authors' papers — both via referenced_works.
        sql = f"""
        WITH author_works AS (
            SELECT DISTINCT
                REGEXP_EXTRACT(auth.author.id, r'/([^/]+)$') AS source_id,
                w.id AS work_id,
                COALESCE(w.title, 'Untitled') AS title
            FROM `{_DATASET}.works` w,
                 UNNEST(w.authorships) AS auth
            WHERE auth.author.id IN UNNEST(@author_ids)
              AND w.publication_year >= 2000
            LIMIT 500
        )
        SELECT DISTINCT
            aw.source_id,
            REGEXP_EXTRACT(auth.author.id, r'/([^/]+)$') AS target_id,
            auth.author.display_name AS target_name,
            'citation' AS connection_type,
            aw.title AS label
        FROM `{_DATASET}.works` citing,
             UNNEST(citing.referenced_works) AS ref_id,
             UNNEST(citing.authorships) AS auth
        JOIN author_works aw ON ref_id = aw.work_id
        WHERE auth.author.id NOT IN UNNEST(@author_ids)
          AND citing.publication_year >= 2010
        LIMIT 1000
        """
        return await self._run_query(sql, [
            bigquery.ArrayQueryParameter("author_ids", "STRING", full_ids),
        ])

    async def _query_institutions(self, full_ids: list[str]) -> list:
        sql = f"""
        WITH frontier_insts AS (
            SELECT DISTINCT
                REGEXP_EXTRACT(a.id, r'/([^/]+)$') AS source_id,
                inst.id AS inst_id,
                inst.display_name AS inst_name
            FROM `{_DATASET}.authors` a,
                 UNNEST(a.last_known_institutions) AS inst
            WHERE a.id IN UNNEST(@author_ids)
        )
        SELECT DISTINCT
            fi.source_id,
            REGEXP_EXTRACT(a.id, r'/([^/]+)$') AS target_id,
            a.display_name AS target_name,
            'institution' AS connection_type,
            fi.inst_name AS label
        FROM `{_DATASET}.authors` a,
             UNNEST(a.last_known_institutions) AS inst
        JOIN frontier_insts fi ON inst.id = fi.inst_id
        WHERE a.id NOT IN UNNEST(@author_ids)
          AND a.works_count > 5
        LIMIT 1000
        """
        return await self._run_query(sql, [
            bigquery.ArrayQueryParameter("author_ids", "STRING", full_ids),
        ])

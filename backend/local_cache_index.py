import json
from collections import Counter, defaultdict
from pathlib import Path

from backend.models import AuthorResult


KNOWN_INSTITUTION_IDS = {
    "duke kunshan university": "I4210159968",
    "duke university": "I170897317",
    "new york university": "I57206974",
}


class LocalCacheIndex:
    """Small searchable index derived from the persisted neighbor cache.

    This is not a replacement for OpenAlex. It is a local fallback for moments
    when OpenAlex refuses live search requests, so cached graph data remains
    selectable instead of the UI becoming a dead end.
    """

    def __init__(self, cache_path: Path):
        self._cache_path = cache_path
        self._loaded = False
        self._authors: dict[str, dict] = {}
        self._institutions: dict[str, dict] = {}
        self._institution_members: dict[str, set[str]] = defaultdict(set)
        self._name_hits: Counter[str] = Counter()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._cache_path.exists():
            return
        try:
            raw = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        author_insts: dict[str, list[str]] = defaultdict(list)
        for source_id, conns in raw.items():
            for conn in conns:
                target_id = conn.get("target_author_id")
                target_name = conn.get("target_name")
                if target_id and target_name:
                    self._authors.setdefault(target_id, {
                        "id": target_id,
                        "display_name": target_name,
                        "works_count": 0,
                        "cited_by_count": 0,
                    })
                    self._name_hits[target_id] += 1
                if conn.get("connection_type") == "institution" and conn.get("label"):
                    label = conn["label"]
                    key = label.casefold()
                    inst_id = KNOWN_INSTITUTION_IDS.get(key, key)
                    self._institutions[key] = {
                        "id": inst_id,
                        "display_name": label,
                        "country_code": None,
                        "works_count": 0,
                        "cited_by_count": 0,
                        "cached": True,
                    }
                    if target_id:
                        self._institution_members[key].add(target_id)
                        author_insts[target_id].append(label)
                    if source_id:
                        self._institution_members[key].add(source_id)
                        author_insts[source_id].append(label)

        for author_id, labels in author_insts.items():
            author = self._authors.get(author_id)
            if not author:
                continue
            primary = labels[0]
            inst_id = KNOWN_INSTITUTION_IDS.get(primary.casefold(), primary.casefold())
            author["last_known_institutions"] = [{
                "id": inst_id,
                "display_name": primary,
            }]
            author["affiliations"] = [{
                "institution": {
                    "id": inst_id,
                    "display_name": primary,
                },
                "years": [],
            }]

    def search_authors(self, query: str, page: int, per_page: int) -> tuple[list[AuthorResult], int]:
        self._ensure_loaded()
        terms = query.casefold().split()
        matches = [
            author for author in self._authors.values()
            if all(term in author["display_name"].casefold() for term in terms)
        ]
        matches.sort(key=lambda a: (
            not a["display_name"].casefold().startswith(query.casefold()),
            -self._name_hits[a["id"]],
            a["display_name"].casefold(),
        ))
        total = len(matches)
        start = (page - 1) * per_page
        page_items = matches[start:start + per_page]
        return [
            AuthorResult(
                id=a["id"],
                display_name=a["display_name"],
                institution=(a.get("last_known_institutions") or [{}])[0].get("display_name"),
                works_count=a.get("works_count", 0),
                cited_by_count=a.get("cited_by_count", 0),
            )
            for a in page_items
        ], total

    def search_institutions(self, query: str, page: int, per_page: int) -> tuple[list[dict], int]:
        self._ensure_loaded()
        terms = query.casefold().split()
        matches = [
            inst for key, inst in self._institutions.items()
            if all(term in key for term in terms)
        ]
        matches.sort(key=lambda i: (
            not i["display_name"].casefold().startswith(query.casefold()),
            i["display_name"].casefold(),
        ))
        total = len(matches)
        start = (page - 1) * per_page
        return matches[start:start + per_page], total

    def author_record(self, author_id: str, fallback_name: str | None = None) -> dict:
        self._ensure_loaded()
        author = dict(self._authors.get(author_id) or {})
        author.setdefault("id", author_id)
        author.setdefault("display_name", fallback_name or author_id)
        author.setdefault("works_count", 0)
        author.setdefault("cited_by_count", 0)
        author.setdefault("last_known_institutions", [])
        author.setdefault("affiliations", [])
        return author

    def institution_authors(
        self,
        institution_id: str,
        institution_name: str | None,
        limit: int,
    ) -> list[dict]:
        self._ensure_loaded()
        wanted_keys = []
        for key, inst in self._institutions.items():
            if inst["id"] == institution_id or (
                institution_name and key == institution_name.casefold()
            ):
                wanted_keys.append(key)
        seen: set[str] = set()
        authors: list[dict] = []
        for key in wanted_keys:
            for author_id in self._institution_members.get(key, set()):
                if author_id in seen or author_id not in self._authors:
                    continue
                seen.add(author_id)
                authors.append(self.author_record(author_id))
        authors.sort(key=lambda a: (-self._name_hits[a["id"]], a["display_name"].casefold()))
        return authors[:limit]

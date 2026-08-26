"""Deterministic selection and ranking helpers for Institution Explorer.

The helpers in this module deliberately do no I/O.  They keep candidate
discovery broad and balanced while leaving path lookup and evidence validation
to the caller.  Private ``_candidate_*`` fields added to returned author copies
are selection metadata; input mappings are never mutated.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


_SPACE_RE = re.compile(r"\s+")
_SOURCE_ORDER = ("reviewed", "topic", "citation")


def _normalized_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _SPACE_RE.sub(
        " ", unicodedata.normalize("NFKC", value).strip().casefold()
    )


def _normalized_id(value: object) -> str:
    """Normalize an OpenAlex URL/ID without fuzzy identity matching."""

    if not isinstance(value, str):
        return ""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def _author_id(author: Mapping[str, Any]) -> str:
    nested = author.get("author")
    if isinstance(nested, Mapping):
        return _normalized_id(nested.get("id"))
    return _normalized_id(author.get("id"))


def _bounded_score(value: object) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, score))


@dataclass(frozen=True)
class TopicMetadata:
    """Normalized OpenAlex topic hierarchy attached to an author profile."""

    topic_ids: frozenset[str] = frozenset()
    topic_names: frozenset[str] = frozenset()
    subfield_ids: frozenset[str] = frozenset()
    subfield_names: frozenset[str] = frozenset()
    field_ids: frozenset[str] = frozenset()
    field_names: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.topic_ids,
                self.topic_names,
                self.subfield_ids,
                self.subfield_names,
                self.field_ids,
                self.field_names,
            )
        )


def _add_entity(
    value: object,
    ids: set[str],
    names: set[str],
) -> None:
    if isinstance(value, str):
        name = _normalized_name(value)
        if name:
            names.add(name)
        return
    if not isinstance(value, Mapping):
        return
    entity_id = _normalized_id(value.get("id"))
    name = _normalized_name(value.get("display_name") or value.get("name"))
    if entity_id:
        ids.add(entity_id)
    if name:
        names.add(name)


def _iter_entities(value: object) -> Iterable[object]:
    if isinstance(value, (str, Mapping)):
        yield value
    elif isinstance(value, Sequence):
        yield from value


def extract_topic_metadata(author: Mapping[str, Any] | TopicMetadata | None) -> TopicMetadata:
    """Extract normalized topic, subfield, and field features from a profile.

    Both current OpenAlex ``topics`` and legacy ``x_concepts`` data are accepted.
    String-only topics (as used by the API's compact payloads) are also tolerated.
    Missing or malformed topic data produces an empty :class:`TopicMetadata`.
    """

    if isinstance(author, TopicMetadata):
        return author
    if not isinstance(author, Mapping):
        return TopicMetadata()

    topic_ids: set[str] = set()
    topic_names: set[str] = set()
    subfield_ids: set[str] = set()
    subfield_names: set[str] = set()
    field_ids: set[str] = set()
    field_names: set[str] = set()

    raw_topics = author.get("topics") or author.get("x_concepts") or ()
    for topic in _iter_entities(raw_topics):
        _add_entity(topic, topic_ids, topic_names)
        if not isinstance(topic, Mapping):
            continue
        subfield = topic.get("subfield")
        field = topic.get("field")
        _add_entity(subfield, subfield_ids, subfield_names)
        _add_entity(field, field_ids, field_names)
        # Tolerate expanded hierarchy objects from cached/derived OpenAlex data.
        if isinstance(subfield, Mapping) and not field:
            _add_entity(subfield.get("field"), field_ids, field_names)

    # Some derived profiles expose hierarchy collections separately.
    for subfield in _iter_entities(author.get("subfields") or ()):
        _add_entity(subfield, subfield_ids, subfield_names)
        if isinstance(subfield, Mapping):
            _add_entity(subfield.get("field"), field_ids, field_names)
    for field in _iter_entities(author.get("fields") or ()):
        _add_entity(field, field_ids, field_names)

    return TopicMetadata(
        topic_ids=frozenset(topic_ids),
        topic_names=frozenset(topic_names),
        subfield_ids=frozenset(subfield_ids),
        subfield_names=frozenset(subfield_names),
        field_ids=frozenset(field_ids),
        field_names=frozenset(field_names),
    )


# IDs carry more confidence than names, and a specific topic match carries more
# information than sharing only a broad field.  Weighted Jaccard keeps the score
# normalized even when profiles contain different numbers of topics.
_TOPIC_FEATURE_WEIGHTS = (
    ("topic_ids", 4.0),
    ("topic_names", 3.0),
    ("subfield_ids", 2.0),
    ("subfield_names", 1.5),
    ("field_ids", 1.0),
    ("field_names", 0.75),
)


def _pair_topic_similarity(left: TopicMetadata, right: TopicMetadata) -> float:
    intersection = 0.0
    union = 0.0
    for attribute, weight in _TOPIC_FEATURE_WEIGHTS:
        left_values = getattr(left, attribute)
        right_values = getattr(right, attribute)
        intersection += weight * len(left_values & right_values)
        union += weight * len(left_values | right_values)
    if union == 0:
        return 0.0
    return min(1.0, max(0.0, intersection / union))


def _metadata_many(
    origins: Iterable[Mapping[str, Any] | TopicMetadata]
    | Mapping[str, Any]
    | TopicMetadata
    | None,
) -> list[TopicMetadata]:
    if origins is None:
        return []
    if isinstance(origins, (Mapping, TopicMetadata)):
        values: Iterable[Mapping[str, Any] | TopicMetadata] = (origins,)
    else:
        values = origins
    return [
        metadata
        for metadata in (extract_topic_metadata(value) for value in values)
        if not metadata.is_empty
    ]


def topic_similarity(
    candidate: Mapping[str, Any] | TopicMetadata | None,
    origins: Iterable[Mapping[str, Any] | TopicMetadata]
    | Mapping[str, Any]
    | TopicMetadata
    | None,
) -> float:
    """Return the candidate's strongest weighted topic overlap with any origin.

    Institution Explorer represents a graph with potentially unrelated origins,
    so a researcher relevant to *one* origin should not be diluted by the others.
    The maximum pairwise weighted-Jaccard score has that property and is always in
    ``[0, 1]``.  Empty or malformed profiles score zero.
    """

    candidate_metadata = extract_topic_metadata(candidate)
    if candidate_metadata.is_empty:
        return 0.0
    origin_metadata = _metadata_many(origins)
    if not origin_metadata:
        return 0.0
    return max(
        _pair_topic_similarity(candidate_metadata, origin)
        for origin in origin_metadata
    )


def _record_richness(record: Mapping[str, Any]) -> int:
    return sum(value not in (None, "", [], {}, ()) for value in record.values())


def _stable_json(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, default=str, separators=(",", ":"))


def _canonical_record(records: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    return min(
        records,
        key=lambda record: (
            -_record_richness(record),
            _normalized_name(record.get("display_name")),
            _stable_json(record),
        ),
    )


def _is_missing(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == ()


def _explicit_topic_score(author: Mapping[str, Any]) -> float:
    for key in ("_topic_similarity", "topic_similarity"):
        if key in author:
            return _bounded_score(author.get(key))
    nested = author.get("author")
    if isinstance(nested, Mapping):
        return _explicit_topic_score(nested)
    return 0.0


def _citation_count(author: Mapping[str, Any]) -> int:
    nested = author.get("author")
    value = (
        nested.get("cited_by_count", 0)
        if isinstance(nested, Mapping)
        else author.get("cited_by_count", 0)
    )
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def merge_balanced_candidate_pool(
    reviewed_candidates: Iterable[Mapping[str, Any]],
    topic_candidates: Iterable[Mapping[str, Any]],
    citation_candidates: Iterable[Mapping[str, Any]],
    *,
    origins: Iterable[Mapping[str, Any] | TopicMetadata]
    | Mapping[str, Any]
    | TopicMetadata
    | None = None,
    origin_ids: Iterable[str] = (),
    pool_size: int = 80,
    topical_fraction: float = 0.4,
) -> list[dict[str, Any]]:
    """Merge discovery lanes into a stable, topically balanced candidate pool.

    Reviewed exact IDs are correctness records, so they are always retained even
    when they exceed ``pool_size``.  Of the remaining capacity, at least
    ``topical_fraction`` is reserved for the explicit topic-discovery lane before
    the citation lane fills unused slots.  Duplicate IDs are merged by fixed source
    priority (reviewed, topic, citation), never by input order.

    Returned copies contain ``_topic_similarity``, ``_candidate_sources``, and
    ``_reviewed_candidate`` metadata for later ranking/shortlisting.
    """

    if pool_size < 0:
        raise ValueError("pool_size must be non-negative")
    if not 0.0 <= topical_fraction <= 1.0:
        raise ValueError("topical_fraction must be between 0 and 1")

    source_values = {
        "reviewed": list(reviewed_candidates),
        "topic": list(topic_candidates),
        "citation": list(citation_candidates),
    }
    excluded_ids = {_normalized_id(author_id) for author_id in origin_ids}
    if origins is not None:
        if isinstance(origins, Mapping):
            origin_records: Iterable[Mapping[str, Any] | TopicMetadata] = (origins,)
        elif isinstance(origins, TopicMetadata):
            origin_records = (origins,)
        else:
            origin_records = origins
        # Materialize generators once because topic scoring also needs the profiles.
        origin_records = tuple(origin_records)
        origins_for_scoring: object = origin_records
        excluded_ids.update(
            _author_id(origin)
            for origin in origin_records
            if isinstance(origin, Mapping)
        )
    else:
        origins_for_scoring = ()
    excluded_ids.discard("")

    records: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for source in _SOURCE_ORDER:
        for author in source_values[source]:
            author_id = _author_id(author)
            if not author_id or author_id in excluded_ids:
                continue
            records.setdefault(author_id, {}).setdefault(source, []).append(author)

    merged_by_id: dict[str, dict[str, Any]] = {}
    for author_id in sorted(records):
        by_source = records[author_id]
        canonical_by_source = {
            source: _canonical_record(by_source[source])
            for source in _SOURCE_ORDER
            if source in by_source
        }
        first_source = next(
            source for source in _SOURCE_ORDER if source in canonical_by_source
        )
        merged = dict(canonical_by_source[first_source])
        for source in _SOURCE_ORDER:
            other = canonical_by_source.get(source)
            if other is None:
                continue
            for key, value in other.items():
                if key not in merged or _is_missing(merged[key]):
                    merged[key] = value
        computed_score = topic_similarity(merged, origins_for_scoring)  # type: ignore[arg-type]
        explicit_score = max(
            _explicit_topic_score(record)
            for values in by_source.values()
            for record in values
        )
        merged["_topic_similarity"] = round(
            max(computed_score, explicit_score), 8
        )
        merged["_candidate_sources"] = tuple(
            source for source in _SOURCE_ORDER if source in by_source
        )
        merged["_reviewed_candidate"] = "reviewed" in by_source
        merged_by_id[author_id] = merged

    reviewed_ids = sorted(
        (
            author_id
            for author_id, author in merged_by_id.items()
            if author["_reviewed_candidate"]
        ),
        key=lambda author_id: (
            -merged_by_id[author_id]["_topic_similarity"], author_id
        ),
    )
    if len(reviewed_ids) >= pool_size:
        return [merged_by_id[author_id] for author_id in reviewed_ids]

    remaining_capacity = pool_size - len(reviewed_ids)
    topic_ids = sorted(
        (
            author_id
            for author_id, author in merged_by_id.items()
            if not author["_reviewed_candidate"]
            and "topic" in author["_candidate_sources"]
        ),
        key=lambda author_id: (
            -merged_by_id[author_id]["_topic_similarity"], author_id
        ),
    )
    topical_slots = min(
        len(topic_ids), math.ceil(remaining_capacity * topical_fraction)
    )
    selected = reviewed_ids + topic_ids[:topical_slots]
    selected_ids = set(selected)

    citation_ids = sorted(
        (
            author_id
            for author_id, author in merged_by_id.items()
            if author_id not in selected_ids
            and "citation" in author["_candidate_sources"]
        ),
        key=lambda author_id: (
            -_citation_count(merged_by_id[author_id]), author_id
        ),
    )
    for author_id in citation_ids:
        if len(selected) >= pool_size:
            break
        selected.append(author_id)
        selected_ids.add(author_id)

    # Use any unfilled capacity for remaining topic-discovered candidates, then
    # for records supplied only through a reviewed-derived/custom lane.
    leftovers = sorted(
        (author_id for author_id in merged_by_id if author_id not in selected_ids),
        key=lambda author_id: (
            -merged_by_id[author_id]["_topic_similarity"], author_id
        ),
    )
    for author_id in leftovers:
        if len(selected) >= pool_size:
            break
        selected.append(author_id)
        selected_ids.add(author_id)

    return [merged_by_id[author_id] for author_id in selected]


def _result_author_id(result: Mapping[str, Any]) -> str:
    nested = result.get("author")
    if isinstance(nested, Mapping):
        return _normalized_id(nested.get("id"))
    return _normalized_id(result.get("author_id") or result.get("id"))


def _result_score(result: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        if key in result:
            return _bounded_score(result.get(key))
    nested = result.get("author")
    if isinstance(nested, Mapping):
        for key in keys:
            if key in nested:
                return _bounded_score(nested.get(key))
    return 0.0


def stable_result_sort_key(result: Mapping[str, Any]) -> tuple[object, ...]:
    """Sort verified Institution Explorer results without citation bias.

    ``verified_hops`` is preferred.  For compatibility, ``hops`` is treated as
    verified unless the caller explicitly sets ``path_verified=False``.  Reachable
    origin count, topic similarity, evidence quality, and the exact author ID are
    the only subsequent tie-breakers; citation counts are intentionally ignored.
    """

    if "verified_hops" in result:
        raw_hops = result.get("verified_hops")
    elif result.get("path_verified") is False:
        raw_hops = None
    else:
        raw_hops = result.get("hops")
    try:
        hops = int(raw_hops) if raw_hops is not None else None
    except (TypeError, ValueError):
        hops = None
    if hops is not None and hops < 0:
        hops = None

    try:
        reachable_origins = max(0, int(result.get("reachable_origin_count", 0)))
    except (TypeError, ValueError):
        reachable_origins = 0
    similarity = _result_score(result, "topic_similarity", "_topic_similarity")
    evidence_quality = _result_score(
        result, "evidence_quality", "path_evidence_quality"
    )
    return (
        hops is None,
        hops if hops is not None else math.inf,
        -reachable_origins,
        -similarity,
        -evidence_quality,
        _result_author_id(result),
    )


def choose_deeper_search_shortlist(
    candidates: Iterable[Mapping[str, Any]],
    *,
    origins: Iterable[Mapping[str, Any] | TopicMetadata]
    | Mapping[str, Any]
    | TopicMetadata
    | None = None,
    origin_ids: Iterable[str] = (),
    matched_ids: Iterable[str] = (),
    reviewed_ids: Iterable[str] = (),
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Choose an unmatched, topic-first shortlist for bounded deeper search.

    Reviewed candidates are retained even if their count exceeds ``limit``;
    normally the reviewed set is tiny, while this exception prevents correctness
    records from silently disappearing.  Every other candidate competes only on
    topic relevance and exact ID, never citations.
    """

    if limit < 0:
        raise ValueError("limit must be non-negative")
    excluded = {_normalized_id(value) for value in origin_ids}
    excluded.update(_normalized_id(value) for value in matched_ids)
    explicit_reviewed = {_normalized_id(value) for value in reviewed_ids}
    excluded.discard("")
    explicit_reviewed.discard("")

    if origins is None:
        origin_records: tuple[Mapping[str, Any] | TopicMetadata, ...] = ()
    elif isinstance(origins, (Mapping, TopicMetadata)):
        origin_records = (origins,)
    else:
        origin_records = tuple(origins)
    excluded.update(
        _author_id(origin)
        for origin in origin_records
        if isinstance(origin, Mapping)
    )
    excluded.discard("")

    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for author in candidates:
        author_id = _author_id(author)
        if author_id and author_id not in excluded:
            by_id.setdefault(author_id, []).append(author)

    normalized: dict[str, dict[str, Any]] = {}
    for author_id, records in by_id.items():
        author = dict(_canonical_record(records))
        score = max(
            topic_similarity(author, origin_records),
            max(_explicit_topic_score(record) for record in records),
        )
        author["_topic_similarity"] = round(score, 8)
        author["_reviewed_candidate"] = bool(
            author_id in explicit_reviewed
            or any(record.get("_reviewed_candidate") for record in records)
            or any("reviewed" in (record.get("_candidate_sources") or ()) for record in records)
        )
        normalized[author_id] = author

    ordering = sorted(
        normalized,
        key=lambda author_id: (
            -normalized[author_id]["_topic_similarity"], author_id
        ),
    )
    reviewed = [
        author_id
        for author_id in ordering
        if normalized[author_id]["_reviewed_candidate"]
    ]
    selected = list(reviewed)
    for author_id in ordering:
        if author_id in selected:
            continue
        if len(selected) >= max(limit, len(reviewed)):
            break
        selected.append(author_id)
    return [normalized[author_id] for author_id in selected]


__all__ = [
    "TopicMetadata",
    "choose_deeper_search_shortlist",
    "extract_topic_metadata",
    "merge_balanced_candidate_pool",
    "stable_result_sort_key",
    "topic_similarity",
]

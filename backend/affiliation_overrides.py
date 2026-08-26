"""Reviewed affiliation and identity corrections for known OpenAlex gaps.

The checked-in data is intentionally conservative: identifiers must be exact
OpenAlex IDs, and every affiliation carries human-reviewed evidence.  No names
are matched or normalized here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit


_DATA_PATH = Path(__file__).with_name("data") / "affiliation_overrides.json"
_AUTHOR_ID_RE = re.compile(r"A[0-9]+\Z")
_INSTITUTION_ID_RE = re.compile(r"I[0-9]+\Z")
_WORK_ID_RE = re.compile(r"W[0-9]+\Z")
_ROR_URL_RE = re.compile(r"https://ror\.org/[0-9a-z]{9}\Z")
_SUPPORTED_SOURCES = frozenset({"official_university"})
_ENTRY_FIELDS = frozenset({
    "institution_id",
    "institution_name",
    "institution_ror_url",
    "author_id",
    "display_name",
    "action",
    "evidence_url",
    "source",
    "reviewed_at",
    "verified_work_ids",
    "excluded_work_ids",
})


class AffiliationOverrideConfigError(ValueError):
    """Raised when the checked-in override data is malformed."""


AffiliationAction = Literal["include", "exclude"]


@dataclass(frozen=True, slots=True)
class AffiliationOverride:
    institution_id: str
    institution_name: str
    institution_ror_url: str
    author_id: str
    display_name: str
    action: AffiliationAction
    evidence_url: str
    source: str
    reviewed_at: str
    verified_work_ids: frozenset[str] | None
    excluded_work_ids: frozenset[str]

    @property
    def effective_verified_work_ids(self) -> frozenset[str] | None:
        """The reviewed allowlist, or ``None`` when identity is not scoped."""
        if self.verified_work_ids is None:
            return None
        return self.verified_work_ids - self.excluded_work_ids


@dataclass(frozen=True, slots=True)
class _OverrideIndex:
    by_institution: dict[str, tuple[AffiliationOverride, ...]]
    by_author: dict[str, tuple[AffiliationOverride, ...]]
    verified_work_ids_by_author: dict[str, frozenset[str]]


def _require_string(entry: dict[str, Any], field: str, index: int) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].{field} must be a non-empty string"
        )
    if value != value.strip():
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].{field} must not have surrounding whitespace"
        )
    return value


def _require_ids(
    entry: dict[str, Any],
    field: str,
    index: int,
) -> frozenset[str]:
    values = entry.get(field, [])
    if not isinstance(values, list):
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].{field} must be a list"
        )
    if any(not isinstance(value, str) or not _WORK_ID_RE.fullmatch(value) for value in values):
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].{field} must contain exact OpenAlex work IDs"
        )
    if len(values) != len(set(values)):
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].{field} contains duplicate work IDs"
        )
    return frozenset(values)


def _optional_ids(
    entry: dict[str, Any],
    field: str,
    index: int,
) -> frozenset[str] | None:
    if field not in entry or entry[field] is None:
        return None
    return _require_ids(entry, field, index)


def _validate_entry(raw: Any, index: int) -> AffiliationOverride:
    if not isinstance(raw, dict):
        raise AffiliationOverrideConfigError(f"affiliations[{index}] must be an object")
    unknown_fields = set(raw) - _ENTRY_FIELDS
    if unknown_fields:
        names = ", ".join(sorted(unknown_fields))
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}] has unknown fields: {names}"
        )

    institution_id = _require_string(raw, "institution_id", index)
    author_id = _require_string(raw, "author_id", index)
    if not _INSTITUTION_ID_RE.fullmatch(institution_id):
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].institution_id must be an exact OpenAlex institution ID"
        )
    if not _AUTHOR_ID_RE.fullmatch(author_id):
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].author_id must be an exact OpenAlex author ID"
        )

    institution_ror_url = _require_string(raw, "institution_ror_url", index)
    if not _ROR_URL_RE.fullmatch(institution_ror_url):
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].institution_ror_url must be an exact ROR URL"
        )

    evidence_url = _require_string(raw, "evidence_url", index)
    parsed_evidence_url = urlsplit(evidence_url)
    if parsed_evidence_url.scheme != "https" or not parsed_evidence_url.netloc:
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].evidence_url must be an absolute HTTPS URL"
        )

    source = _require_string(raw, "source", index)
    if source not in _SUPPORTED_SOURCES:
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].source is not supported: {source}"
        )

    action = _require_string(raw, "action", index)
    if action not in {"include", "exclude"}:
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].action must be include or exclude"
        )

    reviewed_at = _require_string(raw, "reviewed_at", index)
    try:
        date.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].reviewed_at must be an ISO date"
        ) from exc

    verified_work_ids = _optional_ids(raw, "verified_work_ids", index)
    excluded_work_ids = _require_ids(raw, "excluded_work_ids", index)
    if verified_work_ids is None and excluded_work_ids:
        raise AffiliationOverrideConfigError(
            f"affiliations[{index}].excluded_work_ids requires verified_work_ids"
        )

    return AffiliationOverride(
        institution_id=institution_id,
        institution_name=_require_string(raw, "institution_name", index),
        institution_ror_url=institution_ror_url,
        author_id=author_id,
        display_name=_require_string(raw, "display_name", index),
        action=action,
        evidence_url=evidence_url,
        source=source,
        reviewed_at=reviewed_at,
        verified_work_ids=verified_work_ids,
        excluded_work_ids=excluded_work_ids,
    )


def _build_index(raw: Any) -> _OverrideIndex:
    if not isinstance(raw, dict):
        raise AffiliationOverrideConfigError("override data must be an object")
    if set(raw) != {"version", "affiliations"}:
        raise AffiliationOverrideConfigError(
            "override data must contain only version and affiliations"
        )
    if raw["version"] != 1:
        raise AffiliationOverrideConfigError("unsupported affiliation override version")
    if not isinstance(raw["affiliations"], list):
        raise AffiliationOverrideConfigError("affiliations must be a list")

    by_institution_lists: dict[str, list[AffiliationOverride]] = {}
    by_author_lists: dict[str, list[AffiliationOverride]] = {}
    verified_by_author: dict[str, set[str]] = {}
    excluded_by_author: dict[str, set[str]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for index, raw_entry in enumerate(raw["affiliations"]):
        entry = _validate_entry(raw_entry, index)
        pair = (entry.institution_id, entry.author_id)
        if pair in seen_pairs:
            raise AffiliationOverrideConfigError(
                f"duplicate affiliation override for {entry.institution_id}/{entry.author_id}"
            )
        seen_pairs.add(pair)
        by_institution_lists.setdefault(entry.institution_id, []).append(entry)
        by_author_lists.setdefault(entry.author_id, []).append(entry)
        if entry.verified_work_ids is not None:
            verified_by_author.setdefault(entry.author_id, set()).update(
                entry.verified_work_ids
            )
            excluded_by_author.setdefault(entry.author_id, set()).update(
                entry.excluded_work_ids
            )

    by_institution = {
        institution_id: tuple(entries)
        for institution_id, entries in by_institution_lists.items()
    }
    effective_by_author = {
        author_id: frozenset(work_ids - excluded_by_author.get(author_id, set()))
        for author_id, work_ids in verified_by_author.items()
    }
    return _OverrideIndex(
        by_institution=by_institution,
        by_author={
            author_id: tuple(entries)
            for author_id, entries in by_author_lists.items()
        },
        verified_work_ids_by_author=effective_by_author,
    )


@lru_cache(maxsize=1)
def _load_index() -> _OverrideIndex:
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AffiliationOverrideConfigError(
            f"could not load affiliation overrides from {_DATA_PATH}"
        ) from exc
    return _build_index(raw)


def get_affiliation_overrides(institution_id: str) -> tuple[AffiliationOverride, ...]:
    """Return reviewed entries for one exact OpenAlex institution ID."""
    return _load_index().by_institution.get(institution_id, ())


def _resolve_affiliation_entries(
    entries: Iterable[AffiliationOverride],
) -> tuple[AffiliationOverride, ...]:
    """Resolve one effective entry per author, with exclusions winning."""
    by_author: dict[str, AffiliationOverride] = {}
    for entry in entries:
        current = by_author.get(entry.author_id)
        if current is None or (current.action == "include" and entry.action == "exclude"):
            by_author[entry.author_id] = entry
    return tuple(by_author.values())


def get_effective_affiliation_overrides(
    institution_ids: str | Iterable[str],
) -> tuple[AffiliationOverride, ...]:
    """Return effective overrides across exact institution IDs.

    This supports callers applying multiple applicable institution records (for
    example, an institution and a reviewed child unit). If an author is
    included by one entry and excluded by another, the exclusion wins.
    """
    if isinstance(institution_ids, str):
        institution_ids = (institution_ids,)
    entries = (
        entry
        for institution_id in institution_ids
        for entry in get_affiliation_overrides(institution_id)
    )
    return _resolve_affiliation_entries(entries)


def get_verified_work_ids(author_id: str) -> frozenset[str] | None:
    """Return an author's reviewed work allowlist, or ``None`` if unreviewed.

    An empty set is meaningful: the author has a reviewed identity scope, but
    every included work was explicitly excluded.  Across multiple affiliation
    entries for one author, includes are combined and exclusions always win.
    """
    return _load_index().verified_work_ids_by_author.get(author_id)


def get_preferred_affiliation_override(author_id: str) -> AffiliationOverride | None:
    """Return the newest reviewed inclusion for an exact author ID, if any."""
    includes = [
        entry
        for entry in _load_index().by_author.get(author_id, ())
        if entry.action == "include"
    ]
    return max(includes, key=lambda entry: entry.reviewed_at, default=None)


def apply_reviewed_identity(
    author: dict[str, Any],
    override: AffiliationOverride | None = None,
) -> dict[str, Any]:
    """Copy an OpenAlex record and attach safe reviewed identity metadata."""
    result = dict(author)
    raw_id = result.get("id")
    if not raw_id:
        return result
    author_id = str(raw_id).split("/")[-1]
    override = override or get_preferred_affiliation_override(author_id)
    if override is None or override.action != "include":
        return result

    result["_affiliation_override"] = override
    result["display_name"] = override.display_name
    verified_work_ids = get_verified_work_ids(author_id)
    if verified_work_ids is not None:
        result["_verified_identity_scope"] = True
        result["works_count"] = len(verified_work_ids)
        result["cited_by_count"] = 0
        # These belong to the conflated aggregate unless separately reviewed.
        result.pop("topics", None)
        result.pop("x_concepts", None)
        result.pop("orcid", None)
    return result


__all__ = [
    "AffiliationAction",
    "AffiliationOverride",
    "AffiliationOverrideConfigError",
    "get_affiliation_overrides",
    "get_effective_affiliation_overrides",
    "get_preferred_affiliation_override",
    "get_verified_work_ids",
    "apply_reviewed_identity",
]

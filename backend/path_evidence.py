"""Pure validation helpers for OpenAlex work-backed path edges.

OpenAlex author IDs are useful candidate identifiers, but they are not proof that
every work assigned to an ID belongs to the same human.  These helpers therefore
validate the raw authorship identity and require an independent continuity signal
before two publications are allowed to form a path through an intermediate ID.
They perform no I/O and return JSON-serializable verdicts for API diagnostics.
"""

from __future__ import annotations

import re
import unicodedata


_NAME_SUFFIXES = {"ii", "iii", "iv", "jr", "junior", "sr", "senior"}
_NAME_TITLES = {"dr", "mr", "mrs", "ms", "prof", "professor"}
_NICKNAME_GROUPS = (
    {"andrew", "andy", "drew"},
    {"benjamin", "ben"},
    {"elizabeth", "beth", "betsy", "liz"},
    {"james", "jim", "jimmy"},
    {"john", "jack"},
    {"katherine", "katharine", "kathryn", "katrina", "kate", "katie", "kathy"},
    {"margaret", "maggie", "meg", "peggy"},
    {"michael", "mike"},
    {"robert", "bob", "rob"},
    {"william", "bill", "will"},
)
_NICKNAME_INDEX = {
    name: group
    for group in _NICKNAME_GROUPS
    for name in group
}

# This deliberately narrow bridge allows genuine philosophy-of-mind /
# consciousness / perception / neuroscience collaborations without treating all
# Life Sciences or all Social Sciences as one identity-continuity signal.
_COGNITIVE_TOPIC_TERMS = {
    "attention",
    "awareness",
    "brain",
    "cognition",
    "cognitive",
    "consciousness",
    "mind",
    "neural",
    "neuron",
    "neuroscience",
    "perception",
    "psychology",
    "vision",
    "visual",
}


def _short_id(value) -> str:
    return value.rsplit("/", 1)[-1] if isinstance(value, str) else ""


def _normalise_text(value) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _normalise_orcid(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.casefold().strip().removeprefix("https://orcid.org/")
    value = value.removeprefix("http://orcid.org/")
    return re.sub(r"[^0-9x]", "", value)


def _name_parts(value: str) -> tuple[list[str], str]:
    if not isinstance(value, str):
        return [], ""
    if "," in value:
        surname, remainder = value.split(",", 1)
        value = f"{remainder} {surname}"
    tokens = [
        token
        for token in _normalise_text(value).split()
        if token not in _NAME_TITLES
    ]
    while tokens and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    if not tokens:
        return [], ""
    return tokens[:-1], tokens[-1]


def _given_names_compatible(left: str, right: str) -> bool:
    if left == right or left[:1] == right[:1] and (len(left) == 1 or len(right) == 1):
        return True
    return right in _NICKNAME_INDEX.get(left, set())


def _names_compatible(left: str, right: str) -> bool:
    left_given, left_surname = _name_parts(left)
    right_given, right_surname = _name_parts(right)
    if not left_surname or left_surname != right_surname:
        return False
    if not left_given or not right_given:
        return True
    if not _given_names_compatible(left_given[0], right_given[0]):
        return False

    # Conflicting spelled-out middle names are useful negative identity evidence;
    # initials or omitted middle names remain compatible.
    for left_middle, right_middle in zip(left_given[1:], right_given[1:]):
        if not _given_names_compatible(left_middle, right_middle):
            return False
    return True


def _edge_identity(edge: dict, author_id: str) -> dict | None:
    author_id = _short_id(author_id)
    for side in ("left", "right"):
        if _short_id(edge.get(f"{side}_id")) != author_id:
            continue
        identity = dict(edge.get(f"{side}_authorship") or {})
        identity.setdefault("author_id", author_id)
        identity.setdefault("display_name", edge.get(f"{side}_name") or "")
        identity.setdefault("raw_author_name", None)
        identity.setdefault("raw_affiliation_strings", [])
        identity.setdefault("institution_ids", [])
        identity.setdefault("institution_names", [])
        identity.setdefault("orcid", None)
        return identity
    return None


def _identity_names(identity: dict | None) -> list[str]:
    if not identity:
        return []
    return list(dict.fromkeys(
        value
        for value in (identity.get("raw_author_name"), identity.get("display_name"))
        if isinstance(value, str) and value.strip()
    ))


def _profile_names(profile: dict) -> list[str]:
    values = [profile.get("display_name"), profile.get("raw_author_name")]
    for key in ("display_name_alternatives", "alternate_names"):
        alternatives = profile.get(key) or []
        values.extend([alternatives] if isinstance(alternatives, str) else alternatives)
    return list(dict.fromkeys(
        value for value in values if isinstance(value, str) and value.strip()
    ))


def _profile_orcid(profile: dict) -> str:
    ids = profile.get("ids") or {}
    return _normalise_orcid(profile.get("orcid") or ids.get("orcid"))


def _identity_affiliations(identity: dict | None) -> tuple[set[str], set[str]]:
    if not identity:
        return set(), set()
    ids = {
        _short_id(value)
        for value in identity.get("institution_ids") or []
        if _short_id(value)
    }
    names = {
        normalised
        for value in (
            list(identity.get("institution_names") or [])
            + list(identity.get("raw_affiliation_strings") or [])
        )
        if (normalised := _normalise_text(value))
    }
    return ids, names


def _profile_affiliations(profile: dict) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    names: set[str] = set()
    institutions = list(profile.get("last_known_institutions") or [])
    for affiliation in profile.get("affiliations") or []:
        if not isinstance(affiliation, dict):
            continue
        institution = affiliation.get("institution")
        if isinstance(institution, dict):
            institutions.append(institution)
        raw = _normalise_text(affiliation.get("raw_affiliation_string"))
        if raw:
            names.add(raw)
    for institution in institutions:
        if not isinstance(institution, dict):
            continue
        institution_id = _short_id(institution.get("id"))
        if institution_id:
            ids.add(institution_id)
        name = _normalise_text(institution.get("display_name"))
        if name:
            names.add(name)
    ids.update(
        _short_id(value)
        for value in profile.get("institution_ids") or []
        if _short_id(value)
    )
    names.update(
        normalised
        for value in profile.get("institution_names") or []
        if (normalised := _normalise_text(value))
    )
    return ids, names


def _topic_dimensions(record: dict) -> dict[str, set[str]]:
    dimensions = {
        "topic_ids": set(),
        "topic_names": set(),
        "field_ids": set(),
        "field_names": set(),
    }
    topics = list(record.get("topics") or [])
    primary_topic = record.get("primary_topic")
    if isinstance(primary_topic, dict):
        topics.append(primary_topic)
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_id = _short_id(topic.get("id"))
        topic_name = _normalise_text(topic.get("name") or topic.get("display_name"))
        if topic_id:
            dimensions["topic_ids"].add(topic_id)
        if topic_name:
            dimensions["topic_names"].add(topic_name)
        for level in ("subfield", "field"):
            nested = topic.get(level) or {}
            nested_id = _short_id(
                topic.get(f"{level}_id")
                or (nested.get("id") if isinstance(nested, dict) else None)
            )
            nested_name = _normalise_text(
                topic.get(f"{level}_name")
                or (nested.get("display_name") if isinstance(nested, dict) else None)
            )
            if nested_id:
                dimensions["field_ids"].add(nested_id)
            if nested_name:
                dimensions["field_names"].add(nested_name)
    dimensions["topic_ids"].update(
        _short_id(value)
        for value in record.get("topic_ids") or []
        if _short_id(value)
    )
    dimensions["topic_names"].update(
        normalised
        for value in record.get("topic_names") or []
        if (normalised := _normalise_text(value))
    )
    return dimensions


def _related_cognitive_topics(left_names: set[str], right_names: set[str]) -> bool:
    def has_cluster_term(names: set[str]) -> bool:
        words = set(" ".join(names).split())
        return bool(words & _COGNITIVE_TOPIC_TERMS)

    return has_cluster_term(left_names) and has_cluster_term(right_names)


def _continuity_signals(
    first: dict,
    second: dict,
    *,
    first_identity: dict | None = None,
    second_identity: dict | None = None,
    author_id: str = "",
) -> dict:
    first_topics = _topic_dimensions(first)
    second_topics = _topic_dimensions(second)
    first_affiliation_ids, first_affiliation_names = _identity_affiliations(first_identity)
    second_affiliation_ids, second_affiliation_names = _identity_affiliations(second_identity)
    first_coauthors = {
        _short_id(value) for value in first.get("author_ids") or [] if _short_id(value)
    }
    second_coauthors = {
        _short_id(value) for value in second.get("author_ids") or [] if _short_id(value)
    }
    first_coauthors.discard(_short_id(author_id))
    second_coauthors.discard(_short_id(author_id))

    topic_overlap = (
        first_topics["topic_ids"] & second_topics["topic_ids"]
        or first_topics["topic_names"] & second_topics["topic_names"]
    )
    field_overlap = (
        first_topics["field_ids"] & second_topics["field_ids"]
        or first_topics["field_names"] & second_topics["field_names"]
    )
    return {
        "same_work": bool(
            _short_id(first.get("work_id"))
            and _short_id(first.get("work_id")) == _short_id(second.get("work_id"))
        ),
        "topic_overlap": sorted(topic_overlap),
        "field_overlap": sorted(field_overlap),
        "related_topic_cluster": _related_cognitive_topics(
            first_topics["topic_names"], second_topics["topic_names"]
        ),
        "affiliation_overlap": sorted(
            (first_affiliation_ids & second_affiliation_ids)
            or (first_affiliation_names & second_affiliation_names)
        ),
        "coauthor_overlap": sorted(first_coauthors & second_coauthors),
    }


def _verdict(compatible: bool, reason: str, signals: dict) -> dict:
    return {
        "compatible": compatible,
        "reason": reason,
        "signals": signals,
    }


def evaluate_edge_profile_compatibility(
    edge: dict,
    author_profile: dict,
    author_id: str | None = None,
) -> dict:
    """Evaluate whether one work endpoint coheres with an author profile.

    A shared OpenAlex ID is necessary but not sufficient. Names and ORCIDs must
    not conflict, and at least one independent work/profile continuity signal is
    required (topic/field, affiliation, reviewed work, or known coauthor).
    """
    profile_id = _short_id(author_profile.get("id"))
    author_id = _short_id(author_id or profile_id)
    base_signals = {"author_id": author_id, "profile_id": profile_id}
    if not author_id:
        return _verdict(False, "missing_author_id", base_signals)
    if profile_id and profile_id != author_id:
        return _verdict(False, "profile_id_mismatch", base_signals)

    identity = _edge_identity(edge, author_id)
    if identity is None:
        return _verdict(False, "author_not_on_work_edge", base_signals)

    edge_names = _identity_names(identity)
    profile_names = _profile_names(author_profile)
    names_match = bool(edge_names and profile_names and any(
        _names_compatible(edge_name, profile_name)
        for edge_name in edge_names
        for profile_name in profile_names
    ))
    base_signals.update({
        "edge_names": edge_names,
        "profile_names": profile_names,
        "name_compatible": names_match,
    })
    if not names_match:
        return _verdict(False, "name_mismatch", base_signals)

    edge_orcid = _normalise_orcid(identity.get("orcid"))
    profile_orcid = _profile_orcid(author_profile)
    base_signals["orcid_match"] = bool(
        edge_orcid and profile_orcid and edge_orcid == profile_orcid
    )
    if edge_orcid and profile_orcid and edge_orcid != profile_orcid:
        return _verdict(False, "orcid_mismatch", base_signals)

    edge_topics = _topic_dimensions(edge)
    profile_topics = _topic_dimensions(author_profile)
    edge_affiliation_ids, edge_affiliation_names = _identity_affiliations(identity)
    profile_affiliation_ids, profile_affiliation_names = _profile_affiliations(author_profile)
    topic_overlap = (
        edge_topics["topic_ids"] & profile_topics["topic_ids"]
        or edge_topics["topic_names"] & profile_topics["topic_names"]
    )
    field_overlap = (
        edge_topics["field_ids"] & profile_topics["field_ids"]
        or edge_topics["field_names"] & profile_topics["field_names"]
    )
    affiliation_overlap = (
        edge_affiliation_ids & profile_affiliation_ids
        or edge_affiliation_names & profile_affiliation_names
    )
    related_topics = _related_cognitive_topics(
        edge_topics["topic_names"], profile_topics["topic_names"]
    )
    profile_work_ids = {
        _short_id(item.get("id") if isinstance(item, dict) else item)
        for item in (
            list(author_profile.get("work_ids") or [])
            + list(author_profile.get("works") or [])
        )
    } - {""}
    profile_coauthor_ids = {
        _short_id(value) for value in author_profile.get("coauthor_ids") or []
    }
    edge_coauthor_ids = {
        _short_id(value) for value in edge.get("author_ids") or []
    } - {author_id}
    same_work = _short_id(edge.get("work_id")) in profile_work_ids
    coauthor_overlap = edge_coauthor_ids & profile_coauthor_ids
    base_signals.update({
        "same_work": same_work,
        "topic_overlap": sorted(topic_overlap),
        "field_overlap": sorted(field_overlap),
        "related_topic_cluster": related_topics,
        "affiliation_overlap": sorted(affiliation_overlap),
        "coauthor_overlap": sorted(coauthor_overlap),
    })

    positive_reasons = (
        (base_signals["orcid_match"], "orcid_match"),
        (same_work, "profile_contains_work"),
        (bool(topic_overlap), "topic_overlap"),
        (bool(field_overlap), "field_overlap"),
        (related_topics, "related_topic_cluster"),
        (bool(affiliation_overlap), "affiliation_overlap"),
        (bool(coauthor_overlap), "coauthor_overlap"),
    )
    for matched, reason in positive_reasons:
        if matched:
            return _verdict(True, reason, base_signals)
    return _verdict(False, "insufficient_profile_continuity", base_signals)


def evaluate_intermediate_coherence(
    first_edge: dict,
    second_edge: dict,
    intermediate_author_id: str,
    author_profile: dict | None = None,
) -> dict:
    """Evaluate whether two work edges plausibly refer to one intermediary."""
    author_id = _short_id(intermediate_author_id)
    first_identity = _edge_identity(first_edge, author_id)
    second_identity = _edge_identity(second_edge, author_id)
    signals = {"author_id": author_id}
    if not author_id:
        return _verdict(False, "missing_author_id", signals)
    if first_identity is None or second_identity is None:
        signals.update({
            "on_first_edge": first_identity is not None,
            "on_second_edge": second_identity is not None,
        })
        return _verdict(False, "intermediate_not_on_both_edges", signals)

    first_names = _identity_names(first_identity)
    second_names = _identity_names(second_identity)
    names_match = bool(first_names and second_names and any(
        _names_compatible(first_name, second_name)
        for first_name in first_names
        for second_name in second_names
    ))
    signals.update({
        "first_names": first_names,
        "second_names": second_names,
        "name_compatible": names_match,
    })
    if not names_match:
        return _verdict(False, "name_mismatch", signals)

    first_orcid = _normalise_orcid(first_identity.get("orcid"))
    second_orcid = _normalise_orcid(second_identity.get("orcid"))
    profile_orcid = _profile_orcid(author_profile or {})
    known_orcids = {value for value in (first_orcid, second_orcid, profile_orcid) if value}
    signals["orcid_match"] = len(known_orcids) == 1 and len(
        [value for value in (first_orcid, second_orcid, profile_orcid) if value]
    ) >= 2
    if len(known_orcids) > 1:
        return _verdict(False, "orcid_mismatch", signals)

    if author_profile:
        profile_names = _profile_names(author_profile)
        signals["profile_names"] = profile_names
        if profile_names and not all(
            any(
                _names_compatible(edge_name, profile_name)
                for edge_name in edge_names
                for profile_name in profile_names
            )
            for edge_names in (first_names, second_names)
        ):
            return _verdict(False, "profile_name_mismatch", signals)

    signals.update(_continuity_signals(
        first_edge,
        second_edge,
        first_identity=first_identity,
        second_identity=second_identity,
        author_id=author_id,
    ))
    reasons = (
        (signals["orcid_match"], "orcid_match"),
        (signals["same_work"], "same_work"),
        (bool(signals["topic_overlap"]), "topic_overlap"),
        (bool(signals["field_overlap"]), "field_overlap"),
        (signals["related_topic_cluster"], "related_topic_cluster"),
        (bool(signals["affiliation_overlap"]), "affiliation_overlap"),
        (bool(signals["coauthor_overlap"]), "coauthor_overlap"),
    )
    for matched, reason in reasons:
        if matched:
            return _verdict(True, reason, signals)
    return _verdict(False, "no_identity_continuity", signals)


# Short, discoverable aliases for callers that prefer "check" terminology.
check_edge_profile_compatibility = evaluate_edge_profile_compatibility
check_intermediate_coherence = evaluate_intermediate_coherence

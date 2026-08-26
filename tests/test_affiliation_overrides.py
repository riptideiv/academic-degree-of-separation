import pytest

from backend.affiliation_overrides import (
    AffiliationOverrideConfigError,
    _build_index,
    _load_index,
    apply_reviewed_identity,
    _resolve_affiliation_entries,
    get_affiliation_overrides,
    get_effective_affiliation_overrides,
    get_verified_work_ids,
)


KATRINA_WORK_IDS = frozenset({
    "W2891784578",
    "W2800467463",
    "W2345731203",
    "W3204177509",
    "W3139199066",
    "W3136126418",
})


def _entry(**changes):
    value = {
        "institution_id": "I1",
        "institution_name": "Example University",
        "institution_ror_url": "https://ror.org/05abbep66",
        "author_id": "A1",
        "display_name": "Example Author",
        "action": "include",
        "evidence_url": "https://example.edu/people/example-author",
        "source": "official_university",
        "reviewed_at": "2026-08-26",
        "verified_work_ids": ["W1", "W2"],
        "excluded_work_ids": [],
    }
    value.update(changes)
    return value


def test_checked_in_brandeis_override():
    entries = get_affiliation_overrides("I6902469")

    assert len(entries) == 3
    entry = next(item for item in entries if item.author_id == "A5072773992")
    assert entry.author_id == "A5072773992"
    assert entry.display_name == "Katrina Elliott"
    assert entry.action == "include"
    assert entry.institution_ror_url == "https://ror.org/05abbep66"
    assert entry.source == "official_university"
    assert entry.reviewed_at == "2026-08-26"
    assert entry.evidence_url == (
        "https://scholarworks.brandeis.edu/esploro/profile/katrina_elliott"
    )
    assert entry.effective_verified_work_ids == KATRINA_WORK_IDS
    assert get_verified_work_ids("A5072773992") == KATRINA_WORK_IDS
    effective = get_effective_affiliation_overrides("I6902469")
    assert {item.author_id for item in effective} == {
        "A5072773992", "A5071574342", "A5043078069",
    }


def test_checked_in_inactive_brandeis_faculty_are_exact_id_exclusions():
    entries = {
        item.author_id: item
        for item in get_affiliation_overrides("I6902469")
    }

    assert entries["A5071574342"].action == "exclude"
    assert entries["A5071574342"].evidence_url.endswith("john-lisman.html")
    assert entries["A5043078069"].action == "exclude"
    assert entries["A5043078069"].evidence_url.endswith("/physics/people/index.html")


def test_reviewed_identity_replaces_conflated_metadata():
    reviewed = apply_reviewed_identity({
        "id": "https://openalex.org/A5072773992",
        "display_name": "Wrong merged name",
        "works_count": 19,
        "cited_by_count": 87,
        "orcid": "https://orcid.org/unreviewed",
        "topics": [{"display_name": "Unrelated topic"}],
    })

    assert reviewed["display_name"] == "Katrina Elliott"
    assert reviewed["works_count"] == 6
    assert reviewed["cited_by_count"] == 0
    assert reviewed["_verified_identity_scope"] is True
    assert "orcid" not in reviewed
    assert "topics" not in reviewed


def test_lookup_uses_exact_ids_only():
    assert get_affiliation_overrides("https://openalex.org/I6902469") == ()
    assert get_affiliation_overrides("i6902469") == ()
    assert get_verified_work_ids("https://openalex.org/A5072773992") is None
    assert get_verified_work_ids("a5072773992") is None
    assert get_verified_work_ids("A999") is None


def test_loader_is_cached_once():
    assert _load_index() is _load_index()


def test_exclusions_win_across_affiliation_entries():
    raw = {
        "version": 1,
        "affiliations": [
            _entry(verified_work_ids=["W1", "W2"], excluded_work_ids=["W2"]),
            _entry(
                institution_id="I2",
                institution_name="Second University",
                verified_work_ids=["W2", "W3"],
                excluded_work_ids=["W3"],
            ),
        ],
    }

    index = _build_index(raw)

    assert index.verified_work_ids_by_author["A1"] == frozenset({"W1"})


def test_affiliation_only_override_does_not_scope_identity():
    entry = _entry()
    entry.pop("verified_work_ids")
    raw = {"version": 1, "affiliations": [entry]}

    index = _build_index(raw)

    override = index.by_institution["I1"][0]
    assert override.effective_verified_work_ids is None
    assert "A1" not in index.verified_work_ids_by_author


def test_affiliation_exclusion_wins_across_applicable_entries():
    index = _build_index({
        "version": 1,
        "affiliations": [
            _entry(action="include"),
            _entry(
                institution_id="I2",
                institution_name="Second University",
                action="exclude",
            ),
        ],
    })
    entries = (
        *index.by_institution["I1"],
        *index.by_institution["I2"],
    )

    resolved = _resolve_affiliation_entries(entries)

    assert len(resolved) == 1
    assert resolved[0].author_id == "A1"
    assert resolved[0].action == "exclude"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"institution_id": "https://openalex.org/I1"}, "exact OpenAlex institution ID"),
        ({"author_id": "a1"}, "exact OpenAlex author ID"),
        ({"verified_work_ids": ["https://openalex.org/W1"]}, "exact OpenAlex work IDs"),
        ({"reviewed_at": "August 26, 2026"}, "ISO date"),
        ({"evidence_url": "http://example.edu/profile"}, "absolute HTTPS URL"),
        ({"action": "review"}, "include or exclude"),
    ],
)
def test_rejects_malformed_entries(change, message):
    raw = {"version": 1, "affiliations": [_entry(**change)]}

    with pytest.raises(AffiliationOverrideConfigError, match=message):
        _build_index(raw)

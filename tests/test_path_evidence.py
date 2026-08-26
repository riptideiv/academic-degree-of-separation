from backend.path_evidence import (
    evaluate_edge_profile_compatibility,
    evaluate_intermediate_coherence,
)


def _edge(
    *,
    work_id: str,
    left_id: str,
    left_name: str,
    right_id: str,
    right_name: str,
    topic_id: str = "",
    topic_name: str = "",
    field_id: str = "",
    field_name: str = "",
    left_orcid: str | None = None,
    right_orcid: str | None = None,
    left_institution: str = "",
    right_institution: str = "",
    extra_authors: list[str] | None = None,
) -> dict:
    topic = {
        "id": topic_id,
        "name": topic_name,
        "field_id": field_id,
        "field_name": field_name,
    }
    return {
        "work_id": work_id,
        "topics": [topic] if topic_id or topic_name else [],
        "author_ids": [left_id, right_id, *(extra_authors or [])],
        "left_id": left_id,
        "left_name": left_name,
        "right_id": right_id,
        "right_name": right_name,
        "left_authorship": {
            "author_id": left_id,
            "display_name": left_name,
            "raw_author_name": left_name,
            "raw_affiliation_strings": [],
            "institution_ids": [left_institution] if left_institution else [],
            "institution_names": [],
            "orcid": left_orcid,
        },
        "right_authorship": {
            "author_id": right_id,
            "display_name": right_name,
            "raw_author_name": right_name,
            "raw_affiliation_strings": [],
            "institution_ids": [right_institution] if right_institution else [],
            "institution_names": [],
            "orcid": right_orcid,
        },
    }


def test_genuine_consciousness_neuroscience_intermediate_is_accepted():
    philosophy_edge = _edge(
        work_id="W1",
        left_id="Aorigin",
        left_name="David Chalmers",
        right_id="Abridge",
        right_name="Christof Koch",
        topic_id="T-consciousness",
        topic_name="Philosophy of mind and consciousness",
        field_id="F-philosophy",
        field_name="Philosophy",
    )
    neuroscience_edge = _edge(
        work_id="W2",
        left_id="Abridge",
        left_name="Christof Koch",
        right_id="Acandidate",
        right_name="Gina Turrigiano",
        topic_id="T-neural",
        topic_name="Neural correlates of visual perception",
        field_id="F-neuroscience",
        field_name="Neuroscience",
    )

    verdict = evaluate_intermediate_coherence(
        philosophy_edge, neuroscience_edge, "Abridge"
    )

    assert verdict["compatible"] is True
    assert verdict["reason"] == "related_topic_cluster"
    assert verdict["signals"]["related_topic_cluster"] is True


def test_andy_andrew_same_id_cross_domain_transition_is_rejected():
    philosopher_edge = _edge(
        work_id="W1",
        left_id="Aorigin",
        left_name="David Chalmers",
        right_id="Amerged",
        right_name="Andy Clark",
        topic_id="T-cognition",
        topic_name="Embodied cognition and philosophy of mind",
        field_id="F-philosophy",
        field_name="Philosophy",
    )
    geneticist_edge = _edge(
        work_id="W2",
        left_id="Amerged",
        left_name="Andrew G. Clark",
        right_id="Acandidate",
        right_name="Geneticist Collaborator",
        topic_id="T-genetics",
        topic_name="Population genetics in Drosophila",
        field_id="F-genetics",
        field_name="Genetics",
    )

    verdict = evaluate_intermediate_coherence(
        philosopher_edge, geneticist_edge, "Amerged"
    )

    # Andy/Andrew is treated as a plausible name variant, so the decisive
    # rejection is the absence of any independent identity continuity.
    assert verdict["signals"]["name_compatible"] is True
    assert verdict["compatible"] is False
    assert verdict["reason"] == "no_identity_continuity"


def test_exact_name_david_manley_style_cross_domain_transition_is_rejected():
    philosophy_edge = _edge(
        work_id="W1",
        left_id="Aorigin",
        left_name="Philosopher One",
        right_id="Amerged",
        right_name="David Manley",
        topic_id="T-modality",
        topic_name="Metaphysics and modality",
        field_id="F-philosophy",
        field_name="Philosophy",
    )
    genetics_edge = _edge(
        work_id="W2",
        left_id="Amerged",
        left_name="David Manley",
        right_id="Acandidate",
        right_name="Geneticist Two",
        topic_id="T-depression-genetics",
        topic_name="Genome wide association study of depression",
        field_id="F-genetics",
        field_name="Genetics",
    )

    verdict = evaluate_intermediate_coherence(philosophy_edge, genetics_edge, "Amerged")

    assert verdict["signals"]["name_compatible"] is True
    assert verdict["compatible"] is False
    assert verdict["reason"] == "no_identity_continuity"


def test_same_work_is_sufficient_continuity_after_identity_checks():
    first = _edge(
        work_id="Wsame",
        left_id="A1",
        left_name="Alice",
        right_id="Abridge",
        right_name="J. F. C. Wardle",
    )
    second = _edge(
        work_id="Wsame",
        left_id="Abridge",
        left_name="John F. C. Wardle",
        right_id="A2",
        right_name="Bob",
    )

    verdict = evaluate_intermediate_coherence(first, second, "Abridge")

    assert verdict["compatible"] is True
    assert verdict["reason"] == "same_work"


def test_endpoint_profile_name_mismatch_fails_closed():
    edge = _edge(
        work_id="W1",
        left_id="A1",
        left_name="Alice Smith",
        right_id="A2",
        right_name="Bob Jones",
        topic_id="T1",
        topic_name="Consciousness",
    )
    profile = {
        "id": "https://openalex.org/A1",
        "display_name": "Carol Williams",
        "topics": [{"id": "https://openalex.org/T1", "display_name": "Consciousness"}],
    }

    verdict = evaluate_edge_profile_compatibility(edge, profile)

    assert verdict["compatible"] is False
    assert verdict["reason"] == "name_mismatch"


def test_endpoint_profile_with_topic_and_name_continuity_is_accepted():
    edge = _edge(
        work_id="W1",
        left_id="A1",
        left_name="Alice Smith",
        right_id="A2",
        right_name="Bob Jones",
        topic_id="T1",
        topic_name="Consciousness",
    )
    profile = {
        "id": "https://openalex.org/A1",
        "display_name": "Alice B. Smith",
        "topics": [{"id": "https://openalex.org/T1", "display_name": "Consciousness"}],
    }

    verdict = evaluate_edge_profile_compatibility(edge, profile)

    assert verdict["compatible"] is True
    assert verdict["reason"] == "topic_overlap"


def test_orcid_conflict_overrides_other_intermediate_continuity():
    first = _edge(
        work_id="W1",
        left_id="A1",
        left_name="Alice",
        right_id="Abridge",
        right_name="Shared Name",
        topic_id="T1",
        topic_name="Consciousness",
        right_orcid="https://orcid.org/0000-0001-1111-1111",
    )
    second = _edge(
        work_id="W2",
        left_id="Abridge",
        left_name="Shared Name",
        right_id="A2",
        right_name="Bob",
        topic_id="T1",
        topic_name="Consciousness",
        left_orcid="https://orcid.org/0000-0002-2222-2222",
    )

    verdict = evaluate_intermediate_coherence(first, second, "Abridge")

    assert verdict["compatible"] is False
    assert verdict["reason"] == "orcid_mismatch"


def test_matching_orcid_is_sufficient_endpoint_continuity():
    edge = _edge(
        work_id="W1",
        left_id="A1",
        left_name="Alice Smith",
        right_id="A2",
        right_name="Bob Jones",
        left_orcid="https://orcid.org/0000-0001-1111-1111",
    )
    profile = {
        "id": "https://openalex.org/A1",
        "display_name": "Alice B. Smith",
        "orcid": "https://orcid.org/0000-0001-1111-1111",
    }

    verdict = evaluate_edge_profile_compatibility(edge, profile)

    assert verdict["compatible"] is True
    assert verdict["reason"] == "orcid_match"


def test_matching_orcid_is_sufficient_intermediate_continuity():
    first = _edge(
        work_id="W1",
        left_id="A1",
        left_name="Alice",
        right_id="Abridge",
        right_name="Shared Name",
        right_orcid="https://orcid.org/0000-0001-1111-1111",
    )
    second = _edge(
        work_id="W2",
        left_id="Abridge",
        left_name="Shared Name",
        right_id="A2",
        right_name="Bob",
        left_orcid="https://orcid.org/0000-0001-1111-1111",
    )

    verdict = evaluate_intermediate_coherence(first, second, "Abridge")

    assert verdict["compatible"] is True
    assert verdict["reason"] == "orcid_match"

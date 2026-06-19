import pytest
from pydantic import ValidationError

from backend.models import (
    AuthorResult, AuthorWork, Connection, PaginatedAuthors, PaginatedWorks, WorkResult,
)


def test_author_result_basic():
    a = AuthorResult(id="A123", display_name="Alice Smith", institution="MIT", works_count=42)
    assert a.id == "A123"
    assert a.display_name == "Alice Smith"
    assert a.institution == "MIT"
    assert a.works_count == 42
    assert a.cited_by_count == 0


def test_author_result_no_institution():
    a = AuthorResult(id="A999", display_name="Bob Jones", institution=None, works_count=0)
    assert a.institution is None


def test_author_result_with_citations():
    a = AuthorResult(id="A1", display_name="Alice", works_count=10, cited_by_count=500)
    assert a.cited_by_count == 500


def test_paginated_authors_basic():
    a = AuthorResult(id="A1", display_name="Alice", works_count=10)
    p = PaginatedAuthors(results=[a], page=2, per_page=20, total=45, total_pages=3)
    assert p.page == 2
    assert p.total_pages == 3
    assert p.results[0].id == "A1"


def test_author_work_basic():
    w = AuthorWork(id="W1", title="Paper One", cited_by_count=100, publication_year=2021, doi="https://doi.org/10.1/x")
    assert w.id == "W1"
    assert w.publication_year == 2021
    assert w.doi == "https://doi.org/10.1/x"


def test_author_work_defaults():
    w = AuthorWork(id="W2", title="Paper Two")
    assert w.cited_by_count == 0
    assert w.publication_year is None
    assert w.doi is None


def test_connection_coauthor():
    c = Connection(
        target_author_id="A456",
        target_name="Bob",
        connection_type="coauthor",
        label="A great paper",
    )
    assert c.connection_type == "coauthor"
    assert c.label == "A great paper"


def test_connection_invalid_type():
    with pytest.raises(ValidationError):
        Connection(
            target_author_id="A456",
            target_name="Bob",
            connection_type="invalid_type",
            label="Paper",
        )


@pytest.mark.parametrize("conn_type", ["citation", "institution", "authorship"])
def test_connection_valid_types(conn_type):
    c = Connection(
        target_author_id="A456",
        target_name="Bob",
        connection_type=conn_type,
        label="Some label",
    )
    assert c.connection_type == conn_type


def test_connection_direction_defaults_none():
    c = Connection(
        target_author_id="A456", target_name="Bob", connection_type="coauthor", label="Paper",
    )
    assert c.direction is None


@pytest.mark.parametrize("direction", ["incoming", "outgoing", "mutual"])
def test_connection_citation_direction(direction):
    c = Connection(
        target_author_id="A456", target_name="Bob", connection_type="citation",
        label="Paper", direction=direction,
    )
    assert c.direction == direction


def test_connection_invalid_direction():
    with pytest.raises(ValidationError):
        Connection(
            target_author_id="A456", target_name="Bob", connection_type="citation",
            label="Paper", direction="sideways",
        )


def test_work_result_basic():
    w = WorkResult(
        id="W1", title="Paper One", publication_year=2020, cited_by_count=50,
        author_names=["Alice", "Bob"], doi="https://doi.org/10.1/x",
    )
    assert w.id == "W1"
    assert w.author_names == ["Alice", "Bob"]


def test_work_result_defaults():
    w = WorkResult(id="W2", title="Paper Two")
    assert w.publication_year is None
    assert w.cited_by_count == 0
    assert w.author_names == []
    assert w.doi is None


def test_paginated_works_basic():
    w = WorkResult(id="W1", title="Paper One")
    p = PaginatedWorks(results=[w], page=1, per_page=20, total=1, total_pages=1)
    assert p.results[0].id == "W1"
    assert p.total_pages == 1

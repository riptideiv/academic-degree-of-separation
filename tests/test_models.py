import pytest
from pydantic import ValidationError

from backend.models import AuthorResult, Connection


def test_author_result_basic():
    a = AuthorResult(id="A123", display_name="Alice Smith", institution="MIT", works_count=42)
    assert a.id == "A123"
    assert a.display_name == "Alice Smith"
    assert a.institution == "MIT"
    assert a.works_count == 42


def test_author_result_no_institution():
    a = AuthorResult(id="A999", display_name="Bob Jones", institution=None, works_count=0)
    assert a.institution is None


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


@pytest.mark.parametrize("conn_type", ["citation", "institution"])
def test_connection_valid_types(conn_type):
    c = Connection(
        target_author_id="A456",
        target_name="Bob",
        connection_type=conn_type,
        label="Some label",
    )
    assert c.connection_type == conn_type

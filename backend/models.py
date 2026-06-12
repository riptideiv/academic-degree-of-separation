from typing import Literal
from pydantic import BaseModel


class AuthorResult(BaseModel):
    id: str
    display_name: str
    institution: str | None = None
    works_count: int


class Connection(BaseModel):
    target_author_id: str
    target_name: str
    connection_type: Literal["coauthor", "citation", "institution"]
    label: str



from typing import Literal
from pydantic import BaseModel


class AuthorResult(BaseModel):
    id: str
    display_name: str
    institution: str | None = None
    works_count: int
    cited_by_count: int = 0


class PaginatedAuthors(BaseModel):
    results: list[AuthorResult]
    page: int
    per_page: int
    total: int
    total_pages: int
    message: str | None = None


class AuthorWork(BaseModel):
    id: str
    title: str
    cited_by_count: int = 0
    publication_year: int | None = None
    doi: str | None = None


class WorkResult(BaseModel):
    id: str
    title: str
    publication_year: int | None = None
    cited_by_count: int = 0
    author_names: list[str] = []
    doi: str | None = None


class PaginatedWorks(BaseModel):
    results: list[WorkResult]
    page: int
    per_page: int
    total: int
    total_pages: int
    message: str | None = None


class Connection(BaseModel):
    target_author_id: str
    target_name: str
    connection_type: Literal["coauthor", "citation", "institution", "authorship"]
    label: str
    # Only meaningful for connection_type == "citation": "outgoing" means the
    # dict-key author cites target_author_id, "incoming" means the reverse,
    # "mutual" means both directions hold between the pair.
    direction: Literal["incoming", "outgoing", "mutual"] | None = None


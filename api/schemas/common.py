"""Shared Pydantic v2 building blocks.

Every domain-specific schema module in this package inherits from
:class:`APIModel`, and every list endpoint returns a :class:`Page` envelope.
Keeping the generic envelopes / pagination params here avoids both duplication
and inconsistent shapes across the API surface.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ItemT = TypeVar("ItemT")


class APIModel(BaseModel):
    """Project-wide base model.

    - ``from_attributes=True`` lets us pass ORM instances straight into
      ``ResponseModel.model_validate(orm_obj)`` without an intermediate dict.
    - ``populate_by_name=True`` allows callers to use either field names or
      aliases on input.
    - ``str_strip_whitespace=True`` trims accidental leading/trailing whitespace
      on every string field — handy for free-text inputs from the frontend.
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class PageParams(BaseModel):
    """Standard pagination query parameters.

    Used as a FastAPI dependency on every list endpoint::

        async def list_things(
            page_params: Annotated[PageParams, Depends()],
            ...
        ) -> Page[Thing]: ...

    Not a subclass of :class:`APIModel` because we don't want
    ``from_attributes=True`` on URL query params.
    """

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=100, description="Items per page (max 100)."
    )

    @property
    def offset(self) -> int:
        """SQL ``OFFSET`` value derived from ``page`` and ``page_size``."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """SQL ``LIMIT`` value — same as ``page_size`` but named for clarity."""
        return self.page_size


class PageMeta(APIModel):
    """Pagination metadata returned alongside the items in a :class:`Page`."""

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)

    @classmethod
    def build(cls, *, page: int, page_size: int, total: int) -> PageMeta:
        """Compute ``total_pages`` consistently across services."""
        if page_size <= 0:
            total_pages = 0
        else:
            total_pages = (total + page_size - 1) // page_size
        return cls(
            page=page, page_size=page_size, total=total, total_pages=total_pages
        )


class Page(APIModel, Generic[ItemT]):
    """Generic paginated envelope. Used as the response model for every list endpoint."""

    items: list[ItemT]
    meta: PageMeta


class MessageResponse(APIModel):
    """Trivial ``{"message": ...}`` body for endpoints that have no resource to return
    (e.g. logout, reject, ban). Keeps the response shape uniform."""

    message: str = Field(min_length=1, max_length=500)


# A few reusable constrained types so domain modules don't redefine them.
NonEmptyStr = Annotated[str, Field(min_length=1)]

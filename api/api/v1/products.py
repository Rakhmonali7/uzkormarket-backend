"""``/api/v1/products`` — public catalogue + seller-side CRUD.

GETs are public (no role gate); writes require an APPROVED seller via
``ApprovedSeller``. The dependency layer enforces both that the caller
is a SELLER and that their approval is in the APPROVED state — the
service layer adds belt-and-braces.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from dependencies.auth import ApprovedSeller
from dependencies.db import DBSession
from schemas.common import Page, PageMeta, PageParams
from schemas.product import (
    ProductCreate,
    ProductPublic,
    ProductSellerView,
    ProductUpdate,
)
from services import product_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=Page[ProductPublic],
    summary="Browse the public product catalogue",
)
async def list_products(
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
    category: Annotated[
        str | None,
        Query(min_length=1, max_length=80, description="Filter by category"),
    ] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=120, description="Substring match on title/description"),
    ] = None,
    seller_id: Annotated[
        UUID | None,
        Query(description="Restrict to a specific seller's listings"),
    ] = None,
) -> Page[ProductPublic]:
    """Public catalogue — only ``is_approved=True`` and ``is_active=True``
    rows are returned regardless of who calls."""
    items, total = await product_service.list_public_products(
        db,
        page=page_params.page,
        page_size=page_params.page_size,
        category=category,
        search=search,
        seller_id=seller_id,
    )
    return Page[ProductPublic](
        items=[ProductPublic.model_validate(p) for p in items],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


@router.get(
    "/{product_id}",
    response_model=ProductPublic,
    summary="Get a public product by id",
)
async def read_product(
    product_id: UUID, db: DBSession
) -> ProductPublic:
    """Single product detail. 404 on missing **or** unapproved/inactive
    products to avoid disclosing that pending listings exist."""
    product = await product_service.get_public_product(db, product_id)
    return ProductPublic.model_validate(product)


# ---------------------------------------------------------------------------
# Seller-side CRUD
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ProductSellerView,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product listing",
)
async def create_product(
    payload: ProductCreate,
    seller: ApprovedSeller,
    db: DBSession,
) -> ProductSellerView:
    """Create a new product. Lands at ``is_approved=False`` (pending
    review); admins surface it to buyers via ``POST /admin/products/{id}/approve``."""
    product = await product_service.create_product(db, seller, payload)
    return ProductSellerView.model_validate(product)


@router.put(
    "/{product_id}",
    response_model=ProductSellerView,
    summary="Update a product listing",
)
async def update_product(
    product_id: UUID,
    payload: ProductUpdate,
    seller: ApprovedSeller,
    db: DBSession,
) -> ProductSellerView:
    """Patch-style update — only fields explicitly present are touched.
    Any edit re-enters the moderation queue (``is_approved`` cleared)."""
    product = await product_service.update_product(
        db, seller, product_id, payload
    )
    return ProductSellerView.model_validate(product)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Soft-delete a product listing",
)
async def delete_product(
    product_id: UUID,
    seller: ApprovedSeller,
    db: DBSession,
) -> Response:
    """Soft delete: flips ``is_active=False`` so order_items still
    resolve the FK. Idempotent on already-inactive products."""
    await product_service.delete_product(db, seller, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""``/api/v1/sellers`` — seller registration, profile, document handshake.

Two role gates in play:

* ``POST /sellers/register`` is **public** — anyone can apply.
* Everything under ``/sellers/me/*`` and the document endpoints requires
  a SELLER role and an existing Seller row (``CurrentSeller`` dependency).
  Approval is *not* required for any of these — sellers in PENDING /
  REJECTED states still need to see their own dashboard.

Product creation, which **does** require APPROVED, lives under
``/api/v1/products`` and uses the ``ApprovedSeller`` guard there.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from typing import Annotated

from dependencies.auth import CurrentSeller
from dependencies.db import DBSession
from schemas.common import MessageResponse, Page, PageMeta, PageParams
from schemas.order import OrderListItem
from schemas.product import ProductSellerView
from schemas.seller import (
    SellerDocumentCreateRequest,
    SellerDocumentCreateResponse,
    SellerDocumentPublic,
    SellerProfile,
    SellerRegisterRequest,
)
from services import seller_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Public registration
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=SellerProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Register a Korean business as a seller",
)
async def register_seller(
    payload: SellerRegisterRequest,
    db: DBSession,
) -> SellerProfile:
    """Register a Seller in PENDING status. The applicant receives a
    profile back immediately; an admin team is notified asynchronously
    via Celery."""
    seller = await seller_service.register_seller(db, payload)
    return SellerProfile.model_validate(seller)


# ---------------------------------------------------------------------------
# /me — profile + dashboard
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    response_model=SellerProfile,
    summary="Get the current seller's profile and approval status",
)
async def read_my_profile(
    seller: CurrentSeller,
    db: DBSession,
) -> SellerProfile:
    """Returns the full seller profile including approval status, used
    by the seller dashboard to show "pending review" / "approved" /
    "rejected (reason: ...)" banners."""
    refreshed = await seller_service.get_seller_profile(db, seller)
    return SellerProfile.model_validate(refreshed)


@router.get(
    "/me/products",
    response_model=Page[ProductSellerView],
    summary="List the current seller's product listings",
)
async def list_my_products(
    seller: CurrentSeller,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
) -> Page[ProductSellerView]:
    """All products owned by the caller — including pending / rejected
    listings that aren't visible in the public catalogue."""
    items, total = await seller_service.list_seller_products(
        db, seller, page=page_params.page, page_size=page_params.page_size
    )
    return Page[ProductSellerView](
        items=[ProductSellerView.model_validate(p) for p in items],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


@router.get(
    "/me/orders",
    response_model=Page[OrderListItem],
    summary="List orders placed against the current seller's products",
)
async def list_my_orders(
    seller: CurrentSeller,
    db: DBSession,
    page_params: Annotated[PageParams, Depends()],
) -> Page[OrderListItem]:
    """Seller-side order feed — every order whose items reference one of
    this seller's products."""
    items, total = await seller_service.list_seller_orders(
        db, seller, page=page_params.page, page_size=page_params.page_size
    )
    return Page[OrderListItem](
        items=[
            OrderListItem(
                id=o.id,
                seller_id=o.seller_id,
                status=o.status,
                total_amount_uzs=o.total_amount_uzs,
                total_amount_krw=o.total_amount_krw,
                payment_method=o.payment_method,
                item_count=len(o.items),
                created_at=o.created_at,
            )
            for o in items
        ],
        meta=PageMeta.build(
            page=page_params.page, page_size=page_params.page_size, total=total
        ),
    )


# ---------------------------------------------------------------------------
# Document upload handshake (3-step: create → upload-direct-to-MinIO → confirm)
# ---------------------------------------------------------------------------


@router.post(
    "/documents",
    response_model=SellerDocumentCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 1 of the document upload handshake",
)
async def create_document_upload(
    payload: SellerDocumentCreateRequest,
    seller: CurrentSeller,
    db: DBSession,
) -> SellerDocumentCreateResponse:
    """Record metadata, return a 15-minute presigned PUT URL.

    The frontend then PUTs the file bytes directly to MinIO and calls
    ``POST /sellers/documents/{id}/confirm`` to finalise the handshake.
    The API never touches the file bytes."""
    document, presigned = await seller_service.create_document_upload(
        db, seller, payload
    )
    return SellerDocumentCreateResponse(
        id=document.id,
        document_type=document.document_type,
        status=document.status,
        upload_url=presigned.url,
        upload_method="PUT",
        upload_headers=presigned.required_headers,
        expires_in=presigned.expires_in,
    )


@router.post(
    "/documents/{document_id}/confirm",
    response_model=SellerDocumentPublic,
    summary="Step 3 of the document upload handshake",
)
async def confirm_document_upload(
    document_id: UUID,
    seller: CurrentSeller,
    db: DBSession,
) -> SellerDocumentPublic:
    """Mark the document as UPLOADED and notify admins.

    Idempotent — re-confirming an already-UPLOADED document returns the
    same row with no extra notification, so a flaky network can safely
    retry the call."""
    document = await seller_service.confirm_document_upload(
        db, seller, document_id
    )
    return SellerDocumentPublic.model_validate(document)

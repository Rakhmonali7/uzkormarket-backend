"""Seller-side service: registration, document upload handshake, dashboard data.

The document upload handshake is the part that's worth reading carefully.
It implements the project rule's exact sequence:

  1. Seller calls POST /sellers/documents → :func:`create_document_upload`
     records metadata at status PENDING_UPLOAD and returns a presigned PUT URL.
  2. Frontend uploads bytes directly to MinIO using the URL.
  3. Seller calls POST /sellers/documents/{id}/confirm → :func:`confirm_document_upload`
     flips the status to UPLOADED, stamps ``uploaded_at``, and notifies admins.
  4. Admin reads the file via :func:`services.admin_service.create_document_view_url`,
     which mints a fresh 5-minute presigned GET.

The API never handles document bytes — by design.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core import storage
from core.security import hash_password
from models.document import SellerDocument, SellerDocumentStatus
from models.order import Order
from models.product import Product
from models.seller import Seller, SellerApprovalStatus
from models.user import User, UserRole
from schemas.seller import (
    SellerDocumentCreateRequest,
    SellerRegisterRequest,
)
from services._dispatch import dispatch_task
from services._pagination import paginate

log = logging.getLogger(__name__)

# Allowlist of MIME types we accept for seller documents. PDFs cover
# scanned tax/business filings; PNG/JPEG cover phone-camera captures.
_ALLOWED_DOCUMENT_MIMES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
    }
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def register_seller(
    db: AsyncSession, payload: SellerRegisterRequest
) -> Seller:
    """Create a Korean-business seller account in PENDING approval state.

    Two rows land in one transaction: a User with role=SELLER and a Seller
    with the business metadata. The unique indexes on ``users.email`` and
    ``sellers.business_registration_number`` are what make the operation
    race-safe — we don't pre-check, we just attempt the INSERT and
    translate a 409 from either constraint.

    A best-effort Celery dispatch notifies the admin team of the new
    pending application; if Celery isn't ready yet (step 12) the
    registration still succeeds and the dispatch is logged.
    """
    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        phone_number=payload.phone_number,
        role=UserRole.SELLER,
        is_active=True,
        is_verified=False,
    )
    seller = Seller(
        user=user,
        business_name=payload.business_name,
        business_registration_number=payload.business_registration_number,
        business_address=payload.business_address,
        bank_account_number=payload.bank_account_number,
        bank_name=payload.bank_name,
        approval_status=SellerApprovalStatus.PENDING,
    )
    db.add(seller)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Generic 409 — both unique constraints (email, BRN) collapse to
        # the same client-facing message to avoid disclosing which one
        # collided (a registration probe wouldn't gain useful info this way).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email or business registration number already exists.",
        ) from exc

    await db.refresh(seller, attribute_names=["user"])
    dispatch_task(
        "workers.notification_tasks.notify_admins_new_seller", str(seller.id)
    )
    return seller


# ---------------------------------------------------------------------------
# Profile / dashboard
# ---------------------------------------------------------------------------


async def get_seller_profile(db: AsyncSession, seller: Seller) -> Seller:
    """Re-load a seller with its user relationship eager-loaded.

    The ``ApprovedSeller`` / ``CurrentSeller`` dependency may have loaded
    the row without the user joined; this helper guarantees the response
    schema (which embeds ``UserMe``) has everything it needs.
    """
    stmt = (
        select(Seller)
        .where(Seller.id == seller.id)
        .options(selectinload(Seller.user))
    )
    result = (await db.execute(stmt)).scalar_one_or_none()
    if result is None:
        # Should never happen — the dependency loaded the seller — but
        # surface a clean 404 if the row was deleted between requests.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seller profile not found.",
        )
    return result


async def list_seller_products(
    db: AsyncSession,
    seller: Seller,
    *,
    page: int,
    page_size: int,
) -> tuple[Sequence[Product], int]:
    """Paginated list of *every* product belonging to this seller.

    Unlike the public catalogue (:func:`services.product_service.list_public_products`),
    the seller dashboard sees pending and rejected listings too.
    """
    stmt = (
        select(Product)
        .where(Product.seller_id == seller.id)
        .order_by(Product.created_at.desc())
    )
    return await paginate(db, stmt, page=page, page_size=page_size)


async def list_seller_orders(
    db: AsyncSession,
    seller: Seller,
    *,
    page: int,
    page_size: int,
) -> tuple[Sequence[Order], int]:
    """Paginated list of orders against this seller's products.

    ``items`` is eager-loaded so :class:`schemas.order.OrderListItem`'s
    ``item_count`` resolves via ``len(order.items)`` in the handler.
    """
    stmt = (
        select(Order)
        .where(Order.seller_id == seller.id)
        .order_by(Order.created_at.desc())
        .options(selectinload(Order.items))
    )
    return await paginate(db, stmt, page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# Document upload handshake
# ---------------------------------------------------------------------------


async def create_document_upload(
    db: AsyncSession,
    seller: Seller,
    payload: SellerDocumentCreateRequest,
) -> tuple[SellerDocument, storage.PresignedUpload]:
    """Step 1 of the upload handshake — record metadata, mint presigned PUT URL.

    The MinIO object key follows the canonical
    ``{seller_id}/{uuid}-{filename}`` layout produced by
    :func:`core.storage.build_seller_document_key`. We commit the row at
    status PENDING_UPLOAD *before* handing back the URL so a hung client
    doesn't leave us in a state where the bytes exist in MinIO with no
    matching DB record.
    """
    if payload.mime_type not in _ALLOWED_DOCUMENT_MIMES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Unsupported MIME type. Allowed: "
                f"{', '.join(sorted(_ALLOWED_DOCUMENT_MIMES))}."
            ),
        )

    object_key = storage.build_seller_document_key(seller.id, payload.original_filename)
    presigned = storage.create_presigned_put_url(
        "seller-documents",
        object_key,
        content_type=payload.mime_type,
    )

    document = SellerDocument(
        seller_id=seller.id,
        document_type=payload.document_type,
        status=SellerDocumentStatus.PENDING_UPLOAD,
        file_key=presigned.object_key,
        original_filename=payload.original_filename,
        mime_type=payload.mime_type,
        file_size_bytes=payload.file_size_bytes,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document, presigned


async def confirm_document_upload(
    db: AsyncSession,
    seller: Seller,
    document_id: UUID,
) -> SellerDocument:
    """Step 3 of the handshake — mark a document as UPLOADED and notify admins.

    Idempotent: re-confirming an already-UPLOADED document is a no-op
    (no second admin notification, no exception). That matters because
    the frontend's upload-then-confirm pair isn't atomic and may be
    retried under flaky network conditions.

    Hard 404 if the document doesn't belong to this seller — refuses to
    leak the existence of other sellers' documents.
    """
    stmt = select(SellerDocument).where(
        SellerDocument.id == document_id,
        SellerDocument.seller_id == seller.id,
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    if document.status == SellerDocumentStatus.UPLOADED:
        # Idempotent re-confirm — nothing to do, no extra notification.
        return document

    from datetime import datetime, timezone

    document.status = SellerDocumentStatus.UPLOADED
    document.uploaded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(document)

    dispatch_task(
        "workers.notification_tasks.notify_admins_document_uploaded",
        str(document.id),
    )
    return document

"""Product catalogue service — public listing + seller-side CRUD.

Two audiences with very different views of the same table:

* **Buyers** see only ``is_approved=True AND is_active=True`` rows
  through :func:`list_public_products` and :func:`get_public_product`.
* **Sellers** manage every row they own through :func:`create_product`,
  :func:`update_product`, :func:`delete_product`. Newly-created listings
  start at ``is_approved=False, is_active=True`` (pending review); a
  delete is always soft (``is_active=False``) so dependent order_items
  remain queryable.

Admin moderation lives in :mod:`services.admin_service` — kept separate
because the moderation paths have different auth requirements.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.product import Product
from models.seller import Seller, SellerApprovalStatus
from schemas.product import ProductCreate, ProductUpdate
from services._pagination import paginate

# ---------------------------------------------------------------------------
# Public (buyer-facing) reads
# ---------------------------------------------------------------------------


async def list_public_products(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    category: str | None = None,
    search: str | None = None,
    seller_id: UUID | None = None,
) -> tuple[Sequence[Product], int]:
    """Buyer-facing catalogue query.

    Always restricts to approved + active rows. ``search`` does a
    case-insensitive substring match on title and description; for
    anything more sophisticated (full-text, fuzzy) we'd switch to a
    Postgres ``tsvector`` column. The composite ``ix_products_listing``
    index makes the predicate cheap on the hot path.
    """
    stmt = (
        select(Product)
        .where(
            Product.is_approved.is_(True),
            Product.is_active.is_(True),
        )
        .order_by(Product.created_at.desc())
    )
    if category is not None:
        stmt = stmt.where(Product.category == category)
    if seller_id is not None:
        stmt = stmt.where(Product.seller_id == seller_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(Product.title.ilike(pattern), Product.description.ilike(pattern))
        )
    return await paginate(db, stmt, page=page, page_size=page_size)


async def get_public_product(db: AsyncSession, product_id: UUID) -> Product:
    """Single buyer-facing product by id.

    404s on missing **or** unapproved/inactive products — refuses to
    disclose that pending-or-rejected listings exist at all.
    """
    stmt = select(Product).where(
        Product.id == product_id,
        Product.is_approved.is_(True),
        Product.is_active.is_(True),
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )
    return product


# ---------------------------------------------------------------------------
# Seller-facing CRUD
# ---------------------------------------------------------------------------


async def create_product(
    db: AsyncSession,
    seller: Seller,
    payload: ProductCreate,
) -> Product:
    """Create a new product on behalf of an APPROVED seller.

    The ``ApprovedSeller`` dependency in the route handler already
    enforces ``approval_status == APPROVED``; the assert here is a
    belt-and-braces guard against a future refactor that bypasses it.
    Newly-created products start at ``is_approved=False, is_active=True``
    so they're invisible to buyers until admin moderation.
    """
    if seller.approval_status != SellerApprovalStatus.APPROVED:
        # Defence in depth — the dependency layer already screens this.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Seller must be approved before listing products.",
        )

    product = Product(
        seller_id=seller.id,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        attributes=dict(payload.attributes),
        price_krw=payload.price_krw,
        price_uzs=payload.price_uzs,
        stock_quantity=payload.stock_quantity,
        thumbnail_key=payload.thumbnail_key,
        image_keys=list(payload.image_keys),
        is_active=True,
        is_approved=False,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


async def update_product(
    db: AsyncSession,
    seller: Seller,
    product_id: UUID,
    payload: ProductUpdate,
) -> Product:
    """Patch-style update — only fields explicitly present are touched.

    Re-approval policy: any seller-side edit clears the approval flag
    back to ``is_approved=False`` so a previously-live product re-enters
    the moderation queue. This is a deliberate choice — sellers could
    otherwise sneak through banned content by editing an approved
    listing post-hoc. Admin-side edits go through a different path.

    404 if the product doesn't exist OR doesn't belong to this seller —
    both collapse to the same status code so we don't disclose other
    sellers' product ids by probing.
    """
    product = await _get_owned_product(db, seller, product_id)

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return product

    # ``attributes`` and ``image_keys`` come in as plain dict / list — copy
    # to avoid the caller mutating our persisted state via the response.
    if "attributes" in updates and updates["attributes"] is not None:
        updates["attributes"] = dict(updates["attributes"])
    if "image_keys" in updates and updates["image_keys"] is not None:
        updates["image_keys"] = list(updates["image_keys"])

    for field, value in updates.items():
        setattr(product, field, value)

    # Re-enter moderation queue regardless of which fields changed. Cheap
    # to over-approve again; very expensive to under-screen.
    product.is_approved = False
    product.approved_at = None
    product.approved_by = None
    product.rejection_reason = None

    await db.commit()
    await db.refresh(product)
    return product


async def delete_product(
    db: AsyncSession,
    seller: Seller,
    product_id: UUID,
) -> None:
    """Soft-delete: flips ``is_active=False`` so the product disappears from
    the public catalogue but order_items and analytics still resolve the
    FK. Hard deletes are reserved for the admin tooling.

    Idempotent — already-inactive products return 204 without raising.
    """
    product = await _get_owned_product(db, seller, product_id)
    if product.is_active:
        product.is_active = False
        await db.commit()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _get_owned_product(
    db: AsyncSession, seller: Seller, product_id: UUID
) -> Product:
    """Fetch a product or 404 — used by every seller-side mutation."""
    stmt = select(Product).where(
        Product.id == product_id,
        Product.seller_id == seller.id,
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )
    return product

"""Seller verification documents stored as MinIO object keys.

Per the project rules, document bytes never touch the API:

  1. Seller calls POST /sellers/documents -> we record metadata at status
     PENDING_UPLOAD and return a presigned PUT URL (15 min TTL).
  2. Frontend uploads directly to MinIO using that URL.
  3. Seller calls POST /sellers/documents/{id}/confirm -> status flips to
     UPLOADED, ``uploaded_at`` is stamped, admin team is notified.
  4. Admin reads the file via a fresh presigned GET URL (5 min TTL).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.seller import Seller


class SellerDocumentType(str, enum.Enum):
    BUSINESS_REGISTRATION = "BUSINESS_REGISTRATION"
    BANK_VERIFICATION = "BANK_VERIFICATION"
    EXPORT_LICENSE = "EXPORT_LICENSE"


class SellerDocumentStatus(str, enum.Enum):
    """Tracks the upload handshake described above."""

    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"


class SellerDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "seller_documents"

    seller_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sellers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    document_type: Mapped[SellerDocumentType] = mapped_column(
        Enum(SellerDocumentType, name="seller_document_type", native_enum=True),
        nullable=False,
    )
    status: Mapped[SellerDocumentStatus] = mapped_column(
        Enum(SellerDocumentStatus, name="seller_document_status", native_enum=True),
        nullable=False,
        default=SellerDocumentStatus.PENDING_UPLOAD,
        index=True,
    )

    # MinIO object key in the documents bucket. NOT a public URL — admin reads
    # always go through a freshly generated presigned GET URL.
    file_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Project-rule field — set when the seller calls /confirm after the
    # presigned PUT to MinIO has succeeded.
    uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    seller: Mapped["Seller"] = relationship("Seller", back_populates="documents")

    def __repr__(self) -> str:
        return (
            f"SellerDocument(id={self.id!r}, seller_id={self.seller_id!r}, "
            f"type={self.document_type!r}, status={self.status!r})"
        )

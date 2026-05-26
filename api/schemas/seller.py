"""Seller-facing schemas: registration, profile, document upload handshake.

The document-upload flow is the most subtle piece. It implements the exact
sequence laid out in the project rules:

1. Frontend submits :class:`SellerDocumentCreateRequest` → API returns
   :class:`SellerDocumentCreateResponse` carrying a 15-minute presigned PUT URL.
2. Frontend uploads bytes directly to MinIO (the API never touches the file).
3. Frontend submits :class:`SellerDocumentConfirmRequest` (empty body) →
   API stamps ``uploaded_at`` and notifies admins.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import EmailStr, Field

from models.document import SellerDocumentStatus, SellerDocumentType
from models.seller import SellerApprovalStatus
from schemas.common import APIModel
from schemas.user import UserMe


class SellerRegisterRequest(APIModel):
    """Korean business self-registration. Creates a User (role=SELLER) plus a
    Seller row at status PENDING in a single atomic operation server-side."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(
        min_length=1,
        max_length=120,
        description="Legal representative's name on the business registration.",
    )
    phone_number: str | None = Field(default=None, max_length=32)

    business_name: str = Field(min_length=1, max_length=200)
    business_registration_number: str = Field(
        min_length=1,
        max_length=64,
        description="Korean BRN (사업자등록번호). Globally unique in our system.",
    )
    business_address: str = Field(min_length=1, max_length=500)
    bank_account_number: str = Field(min_length=1, max_length=64)
    bank_name: str = Field(min_length=1, max_length=120)


class SellerProfile(APIModel):
    """Returned by ``GET /sellers/me``. Bundles approval state with the
    nested user identity so the frontend has everything it needs in one call."""

    id: UUID
    user: UserMe

    business_name: str
    business_registration_number: str
    business_address: str
    bank_account_number: str
    bank_name: str

    approval_status: SellerApprovalStatus
    rejection_reason: str | None
    approved_at: datetime | None

    created_at: datetime


# ---------------------------------------------------------------------------
# Document upload handshake
# ---------------------------------------------------------------------------


class SellerDocumentCreateRequest(APIModel):
    """Step 1 of the upload handshake. Frontend declares what it's about to
    upload; we record metadata and hand back a presigned PUT URL.

    ``file_size_bytes`` is optional — we use it (when present) to tighten the
    presigned PUT policy and pre-validate the size client-side, but don't
    require it because the value isn't always known at request time.
    """

    document_type: SellerDocumentType
    original_filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(
        min_length=1,
        max_length=120,
        description="The browser-reported Content-Type. Validated server-side against an allowlist.",
    )
    file_size_bytes: int | None = Field(
        default=None,
        ge=0,
        le=50 * 1024 * 1024,
        description="Optional. Hard ceiling at 50 MB; service layer may apply tighter limits per type.",
    )


class SellerDocumentCreateResponse(APIModel):
    """Step 1 response. The frontend should ``PUT`` the file bytes to
    ``upload_url`` with the supplied ``upload_headers``, then call the
    confirm endpoint.

    ``expires_in`` is the lifetime of the presigned URL in seconds — keep
    matching to ``settings.MINIO_PRESIGN_PUT_TTL_SECONDS`` (15 min default)."""

    id: UUID
    document_type: SellerDocumentType
    status: SellerDocumentStatus
    upload_url: str
    upload_method: str = "PUT"
    upload_headers: dict[str, str] = Field(default_factory=dict)
    expires_in: int = Field(gt=0, description="Presigned URL lifetime, in seconds.")


class SellerDocumentConfirmRequest(APIModel):
    """Step 3 body. Currently empty — the endpoint is identified by the
    document id in the URL path. Kept as an explicit (empty) schema so we
    have a stable place to add fields like client-side checksums later
    without breaking the contract."""

    pass


class SellerDocumentPublic(APIModel):
    """Document metadata as the seller sees it. Never includes the MinIO
    object key — that's a server-side implementation detail."""

    id: UUID
    document_type: SellerDocumentType
    status: SellerDocumentStatus
    original_filename: str
    mime_type: str
    file_size_bytes: int | None
    uploaded_at: datetime | None
    created_at: datetime

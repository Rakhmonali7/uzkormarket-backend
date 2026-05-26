"""MinIO / S3-compatible object storage wrapper.

Public surface:

* :class:`PresignedUpload` — return value of :func:`create_presigned_put_url`.
* :func:`build_seller_document_key` — canonical object-key builder for
  seller documents (kept identical to the step-10 stub so service code
  doesn't change).
* :func:`create_presigned_put_url` — 15-minute presigned PUT URL the
  browser uses to upload directly to MinIO.
* :func:`create_presigned_get_url` — short-lived presigned GET URL the
  admin UI uses to view documents.
* :func:`ensure_buckets` — idempotent bucket bootstrap, called from
  FastAPI's startup hook (and any test that needs the buckets to exist).

Design notes
------------

The official ``minio`` Python SDK is sync — but every operation we use
(presigned URL generation in particular) is local HMAC computation with
no network I/O, so calling it from async request handlers is safe.
``ensure_buckets`` does perform two real round-trips (``bucket_exists``
+ optional ``make_bucket``) but it runs once at app startup, off the
request hot path.

Two endpoints, not one
~~~~~~~~~~~~~~~~~~~~~~

The MinIO SDK takes a single ``endpoint`` (host:port) — it then bakes
that into every signed URL. That works fine in production where the
browser and the API can both reach MinIO at the same hostname (e.g.
``minio.example.com``), but breaks in dev/Docker where the API
container talks to ``minio:9000`` while the browser sees ``localhost:9000``.

We resolve this by maintaining two clients:

* an **internal** client used for ``ensure_buckets`` and any future
  server-side I/O, pointed at ``settings.MINIO_ENDPOINT``;
* a **public** client used only for presigning, pointed at
  ``settings.MINIO_PUBLIC_ENDPOINT`` (falls back to the internal
  endpoint when unset). Signed URLs from this client work in the
  browser regardless of where the API process happens to live.

Both clients are lazy singletons — created on first access, not at
import time — so unit tests that don't touch storage don't need MinIO
running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from core.config import settings

log = logging.getLogger(__name__)

BucketName = Literal["seller-documents", "product-images"]

# Buckets we manage. Order matters only for log readability.
_MANAGED_BUCKETS: tuple[tuple[BucketName, str], ...] = (
    # (bucket-name, "private" | "public-read")
    # Seller documents are NEVER public — every read goes through a
    # short-lived presigned GET URL minted from an admin route.
    ("seller-documents", "private"),
    # Product images are public-read (configured via the docker-compose
    # ``minio-init`` job today). We keep the SDK calls consistent with
    # whatever that job does so the code-path and the compose-path
    # converge.
    ("product-images", "public-read"),
)


@dataclass(frozen=True)
class PresignedUpload:
    """Return value of :func:`create_presigned_put_url`.

    * ``url`` — the URL the browser will ``PUT`` the file bytes to.
    * ``object_key`` — what we persist on the document row so we can
      later mint GET URLs for admin viewing.
    * ``expires_in`` — TTL in seconds, supplied verbatim to the
      frontend so it can refresh the upload before the URL expires.
    * ``required_headers`` — headers the client should include with
      its PUT. AWS SigV4 doesn't require Content-Type to be in the
      signed-headers set by default, but if the frontend sends it
      MinIO will persist it as object metadata, which we want — so we
      surface it here for the frontend to forward verbatim.
    """

    url: str
    object_key: str
    expires_in: int
    required_headers: dict[str, str]


# ---------------------------------------------------------------------------
# Object-key helpers
# ---------------------------------------------------------------------------


def build_seller_document_key(
    seller_id: str | object, original_filename: str
) -> str:
    """Compose the canonical MinIO object key for a seller document.

    Layout: ``{seller_id}/{uuid}-{sanitised_filename}``. The seller id
    prefix lets us cheaply scope IAM policies and lifecycle rules to a
    single seller. The UUID guarantees uniqueness even if the seller
    re-uploads the same filename.
    """
    safe_name = quote(original_filename, safe="._-")
    return f"{seller_id}/{uuid4().hex}-{safe_name}"


# ---------------------------------------------------------------------------
# Client factories — lazy singletons
# ---------------------------------------------------------------------------


_internal_client: Minio | None = None
_public_client: Minio | None = None


def _build_client(endpoint: str) -> Minio:
    """Construct a MinIO SDK client against ``endpoint``.

    ``endpoint`` is ``host:port`` without a scheme — the SDK injects
    https/http based on ``secure``. We strip any accidental scheme that
    snuck in via env vars so misconfiguration produces a useful error
    early instead of an opaque signing failure later.
    """
    cleaned = endpoint
    for prefix in ("http://", "https://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    cleaned = cleaned.rstrip("/")
    return Minio(
        cleaned,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_SSL,
        region=settings.MINIO_REGION,
    )


def get_internal_client() -> Minio:
    """Client pointed at the in-cluster MinIO endpoint.

    Used for server-side operations (bucket bootstrap, future direct
    uploads from background workers, etc.). Lazy: we only build it on
    first call so an API process that never talks to storage doesn't
    pay the cost.
    """
    global _internal_client
    if _internal_client is None:
        _internal_client = _build_client(settings.MINIO_ENDPOINT)
    return _internal_client


def get_public_client() -> Minio:
    """Client used solely for minting presigned URLs the browser hits.

    Falls back to :func:`get_internal_client` when
    ``MINIO_PUBLIC_ENDPOINT`` is unset — that's the right behaviour for
    production where the same hostname is reachable from both the API
    and the browser.
    """
    global _public_client
    if _public_client is None:
        if settings.MINIO_PUBLIC_ENDPOINT:
            _public_client = _build_client(settings.MINIO_PUBLIC_ENDPOINT)
        else:
            _public_client = get_internal_client()
    return _public_client


def reset_clients() -> None:
    """Drop the cached clients. Used by tests that swap MinIO endpoints
    mid-process; never called in normal operation."""
    global _internal_client, _public_client
    _internal_client = None
    _public_client = None


# ---------------------------------------------------------------------------
# Bucket bootstrap
# ---------------------------------------------------------------------------


def ensure_buckets() -> list[str]:
    """Create any missing managed buckets. Returns the list of names
    that were *created* (not the list of names that already existed),
    which is useful for logging.

    Idempotent and safe to call on every startup. Errors propagate
    upward so the caller can decide whether to fail-fast or warn.
    """
    client = get_internal_client()
    created: list[str] = []
    for bucket, _policy in _MANAGED_BUCKETS:
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket, location=settings.MINIO_REGION)
                created.append(bucket)
        except S3Error as exc:
            # ``BucketAlreadyOwnedByYou`` is the natural race when two
            # API replicas boot at the same moment; treat as success.
            if exc.code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                continue
            raise
    return created


# ---------------------------------------------------------------------------
# Presigned URLs
# ---------------------------------------------------------------------------


def create_presigned_put_url(
    bucket: BucketName,
    object_key: str,
    *,
    content_type: str | None = None,
    expires_in: int | None = None,
) -> PresignedUpload:
    """Mint a presigned URL the browser uses to PUT object bytes.

    The URL is signed against the **public** endpoint so the browser
    can hit it directly without going through the API. The signature
    covers the bucket, key, and expiry — Content-Type isn't part of
    the signed-headers set by default, but we still surface it via
    ``required_headers`` so the frontend can forward it (MinIO stores
    it as object metadata on a successful upload).
    """
    ttl = expires_in or settings.MINIO_PRESIGNED_PUT_TTL_SECONDS
    url = get_public_client().presigned_put_object(
        bucket_name=bucket,
        object_name=object_key,
        expires=timedelta(seconds=ttl),
    )
    return PresignedUpload(
        url=url,
        object_key=object_key,
        expires_in=ttl,
        required_headers=(
            {"Content-Type": content_type} if content_type else {}
        ),
    )


def create_presigned_get_url(
    bucket: BucketName,
    object_key: str,
    *,
    expires_in: int | None = None,
    response_filename: str | None = None,
) -> str:
    """Mint a short-lived presigned GET URL for admin viewing.

    ``response_filename`` controls the download filename when the
    admin browser saves the document — passed through MinIO's
    ``response-content-disposition`` query parameter so the original
    filename is preserved without us having to rename the underlying
    object key (which we keep UUID-prefixed for uniqueness).
    """
    ttl = expires_in or settings.MINIO_PRESIGNED_GET_TTL_SECONDS
    response_headers: dict[str, str] | None = None
    if response_filename:
        # Use ``attachment; filename=...`` so the browser triggers a
        # download dialog rather than rendering the PDF/image inline
        # — admins should explicitly save before opening untrusted
        # files. ``quote`` keeps spaces and unicode safe for the URL.
        safe_name = quote(response_filename)
        response_headers = {
            "response-content-disposition": (
                f'attachment; filename="{safe_name}"'
            )
        }
    return get_public_client().presigned_get_object(
        bucket_name=bucket,
        object_name=object_key,
        expires=timedelta(seconds=ttl),
        response_headers=response_headers,
    )


__all__ = [
    "BucketName",
    "PresignedUpload",
    "build_seller_document_key",
    "create_presigned_get_url",
    "create_presigned_put_url",
    "ensure_buckets",
    "get_internal_client",
    "get_public_client",
    "reset_clients",
]

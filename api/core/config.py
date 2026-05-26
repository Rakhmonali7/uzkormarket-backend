"""Application settings loaded from environment / .env via pydantic-settings.

Single source of truth for every configurable knob in the app. Import the
module-level ``settings`` object anywhere you need a config value:

    from core.config import settings
    print(settings.assembled_database_url)

The class refuses to instantiate when a required value (e.g. ``JWT_SECRET_KEY``)
is missing, which is exactly what we want — the process should fail fast at
boot rather than handing out unauthenticated tokens at runtime.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

from pydantic import (
    AnyHttpUrl,
    EmailStr,
    Field,
    PostgresDsn,
    RedisDsn,
    computed_field,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]


class Settings(BaseSettings):
    """Project configuration. All values are sourced from env vars / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    ENVIRONMENT: Environment = "development"
    PROJECT_NAME: str = "KorUZ Mall API"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    FRONTEND_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:3000")
    # Comma-separated list or JSON array. Empty -> falls back to FRONTEND_URL.
    # NoDecode prevents pydantic-settings from JSON-parsing the env value
    # before our @field_validator below splits commas.
    BACKEND_CORS_ORIGINS: Annotated[list[AnyHttpUrl], NoDecode] = []

    # ------------------------------------------------------------------
    # Database — supply DATABASE_URL directly OR rely on POSTGRES_* parts.
    # ------------------------------------------------------------------
    DATABASE_URL: PostgresDsn | None = None
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "koruz"
    POSTGRES_PASSWORD: str = "koruz"
    POSTGRES_DB: str = "koruz"

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE_SECONDS: int = 1800
    DB_POOL_PRE_PING: bool = True
    DB_ECHO: bool = False

    # ------------------------------------------------------------------
    # Redis / Celery — Celery URLs default to REDIS_URL when not provided.
    # ------------------------------------------------------------------
    REDIS_URL: RedisDsn = RedisDsn("redis://redis:6379/0")
    CELERY_BROKER_URL: RedisDsn | None = None
    CELERY_RESULT_BACKEND: RedisDsn | None = None

    # Auth / refresh-token storage uses a dedicated DB index so flushing it
    # never wipes Celery state.
    REDIS_AUTH_DB: int = 1

    # ------------------------------------------------------------------
    # MinIO — object storage for seller documents and product images.
    # ------------------------------------------------------------------
    MINIO_ENDPOINT: str = "minio:9000"
    # Public-facing endpoint for presigned URLs the browser will hit. Defaults
    # to MINIO_ENDPOINT for in-cluster usage; in dev set this to "localhost:9000".
    MINIO_PUBLIC_ENDPOINT: str | None = None
    MINIO_REGION: str = "us-east-1"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin123"
    MINIO_USE_SSL: bool = False
    MINIO_BUCKET_DOCUMENTS: str = "seller-documents"
    MINIO_BUCKET_IMAGES: str = "product-images"
    MINIO_PRESIGNED_PUT_TTL_SECONDS: int = 15 * 60  # seller upload window
    MINIO_PRESIGNED_GET_TTL_SECONDS: int = 5 * 60   # admin doc viewing window

    # ------------------------------------------------------------------
    # JWT — JWT_SECRET_KEY is REQUIRED (no default). Refusing to start
    # without it is the correct security posture.
    # ------------------------------------------------------------------
    JWT_SECRET_KEY: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    BCRYPT_ROUNDS: int = Field(default=12, ge=12, le=15)

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_USE_TLS: bool = True
    EMAILS_FROM_ADDRESS: EmailStr | None = None
    EMAILS_FROM_NAME: str = "KorUZ Mall"
    ADMIN_NOTIFY_EMAILS: Annotated[list[EmailStr], NoDecode] = []

    # ------------------------------------------------------------------
    # Payment gateways — all optional; absent values disable that gateway.
    # ------------------------------------------------------------------
    TOSS_CLIENT_KEY: str | None = None
    TOSS_SECRET_KEY: str | None = None
    KG_INICIS_MERCHANT_ID: str | None = None
    KG_INICIS_SIGN_KEY: str | None = None
    PAYME_MERCHANT_ID: str | None = None
    PAYME_SECRET_KEY: str | None = None
    CLICK_MERCHANT_ID: str | None = None
    CLICK_SECRET_KEY: str | None = None
    UZCARD_MERCHANT_ID: str | None = None
    UZCARD_SECRET_KEY: str | None = None

    # ------------------------------------------------------------------
    # FX / exchange-rate provider used by the Celery beat job.
    # ------------------------------------------------------------------
    EXCHANGE_RATE_PROVIDER_URL: AnyHttpUrl | None = None
    EXCHANGE_RATE_PROVIDER_KEY: str | None = None
    EXCHANGE_RATE_REFRESH_HOURS: int = 6
    # Used by ``services.payment_service`` whenever Redis doesn't carry a
    # fresh provider quote — happens at boot before the first beat tick and
    # in unit tests. Decimal default reflects roughly 1 KRW ≈ 9.5 UZS as of
    # April 2026; tune via env when a tighter fallback is needed.
    EXCHANGE_RATE_FALLBACK_KRW_TO_UZS: Decimal = Field(
        default=Decimal("9.5"), gt=0
    )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    SENTRY_DSN: str | None = None

    # ------------------------------------------------------------------
    # Validators — accept comma-separated or JSON-array values for list[]
    # fields, which is friendlier than pydantic's default JSON-only parsing.
    # ------------------------------------------------------------------
    @field_validator(
        "BACKEND_CORS_ORIGINS",
        "ADMIN_NOTIFY_EMAILS",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Accept either a JSON array or a comma-separated string from env."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                # NoDecode disables pydantic-settings' built-in JSON decoder,
                # so we have to parse the array form ourselves.
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def assembled_database_url(self) -> str:
        """Async DSN for SQLAlchemy. Forces the asyncpg driver."""
        if self.DATABASE_URL is not None:
            url = str(self.DATABASE_URL)
            scheme = url.split("://", 1)[0]
            if "+" not in scheme:
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url
        return (
            "postgresql+asyncpg://"
            f"{quote_plus(self.POSTGRES_USER)}:{quote_plus(self.POSTGRES_PASSWORD)}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Sync DSN — used by Alembic env.py for migration runs."""
        async_url = self.assembled_database_url
        return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_broker(self) -> str:
        return str(self.CELERY_BROKER_URL or self.REDIS_URL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_backend(self) -> str:
        return str(self.CELERY_RESULT_BACKEND or self.REDIS_URL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        if self.BACKEND_CORS_ORIGINS:
            return [str(o).rstrip("/") for o in self.BACKEND_CORS_ORIGINS]
        return [str(self.FRONTEND_URL).rstrip("/")]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docs_url(self) -> str | None:
        """OpenAPI Swagger UI path — disabled in production."""
        return None if self.is_production else "/docs"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redoc_url(self) -> str | None:
        return None if self.is_production else "/redoc"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def openapi_url(self) -> str | None:
        return None if self.is_production else "/openapi.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — instantiates Settings exactly once per process."""
    return Settings()  # type: ignore[call-arg]


# Module-level convenience export.
settings: Settings = get_settings()

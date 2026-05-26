"""FastAPI application factory.

The factory pattern (``create_app()``) is preferred over a module-level
``app = FastAPI(...)`` because it keeps test isolation easy: pytest can
build a fresh app per session with a different settings instance and
overridden dependencies, without monkey-patching globals.

Wiring done here, in order:

  1. Logging configuration (level from ``settings.LOG_LEVEL``).
  2. FastAPI instance — ``debug`` only in non-production, OpenAPI URL
     namespaced under ``/api/v1`` so the public docs URL matches the
     route prefix.
  3. CORS middleware — origins come from ``settings.cors_origins`` which
     falls back to ``FRONTEND_URL`` when the explicit list is empty.
  4. v1 router mount.
  5. Lifespan handler — closes the SQLAlchemy engine and Redis client on
     shutdown so an orderly ``docker compose down`` doesn't leave dangling
     connections.
  6. Global exception handler for unanticipated errors (project rule).

  7. Prometheus exposition — HTTP request histograms plus process metrics
     on GET ``/metrics`` (wired via ``Instrumentator``, excluded from noisy
     paths like probes and scraping itself).

The ASGI app is ``main:app`` for uvicorn / gunicorn invocations; tests
build their own via ``create_app()``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from api.v1.router import api_router
from core.config import settings
from core.database import engine
from core.redis import dispose_redis
from core.storage import ensure_buckets

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """App startup / shutdown hook.

    Startup:

    * Log so operators can correlate "app booted" in container logs.
    * Best-effort MinIO bucket bootstrap — creates ``seller-documents``
      and ``product-images`` if they don't already exist. Failure is
      non-fatal: presigned URL generation is local-only HMAC so the
      API can still serve metadata-only routes, and the docker-compose
      ``minio-init`` job is a parallel safety net.

    Shutdown: dispose the SQLAlchemy engine pool and the Redis client so
    a graceful ``docker stop`` doesn't leak sockets in CLOSE_WAIT.
    """
    log.info(
        "Starting %s (env=%s)", settings.PROJECT_NAME, settings.ENVIRONMENT
    )
    try:
        # Run the (sync) MinIO calls off the event loop so a sluggish
        # bucket-exists round-trip doesn't block readiness for other
        # async startup tasks we may add later.
        import asyncio

        try:
            created = await asyncio.to_thread(ensure_buckets)
        except Exception:  # noqa: BLE001
            log.warning(
                "MinIO bucket bootstrap skipped (storage unreachable at "
                "startup); presign-only paths will still work, but uploads "
                "will fail until the buckets exist.",
                exc_info=True,
            )
        else:
            if created:
                log.info("Created MinIO buckets: %s", ", ".join(created))
            else:
                log.info("MinIO buckets already present.")
        yield
    finally:
        await engine.dispose()
        await dispose_redis()
        log.info("Shutdown complete.")


def create_app() -> FastAPI:
    """Build and return the FastAPI app.

    Kept side-effect-free so tests can build a fresh instance per
    fixture without process-wide state bleed. ``main:app`` below
    instantiates one for the production ASGI worker.
    """
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        debug=settings.ENVIRONMENT != "production",
        docs_url=f"{settings.API_V1_PREFIX}/docs",
        redoc_url=f"{settings.API_V1_PREFIX}/redoc",
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    # ---------------- Prometheus (/metrics) ----------------
    Instrumentator(
        should_group_status_codes=True,
        excluded_handlers=["/metrics", "/healthz"],
    ).instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        tags=["meta"],
    )

    # ---------------- CORS ----------------
    # The project rule explicitly says "only allow frontend origin", so we
    # don't use the wildcard list. ``allow_credentials`` is on because the
    # frontend sends Authorization headers (and may add cookies later).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=600,
    )

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    # ---------------- Global exception handler ----------------
    # FastAPI already converts HTTPException to a JSON 4xx for us; this
    # handler is the safety net for *unanticipated* errors so a stack
    # trace never leaks to the client. Logged with traceback for the
    # operator. Project rule: "add a global exception handler for
    # unexpected errors".
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        log.exception(
            "Unhandled exception on %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error."},
        )

    @app.get("/healthz", tags=["meta"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness probe used by Docker, Kubernetes, and load balancers.

        Intentionally outside the ``/api/v1`` prefix so probes don't
        depend on the API version contract.
        """
        return {"status": "ok"}

    return app


app = create_app()

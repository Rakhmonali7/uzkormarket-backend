# syntax=docker/dockerfile:1.7
# KorUZ Mall API — multi-stage Dockerfile
#
# Targets:
#   - development : full toolchain, source mounted from host, uvicorn --reload
#   - production  : slim runtime, non-root user, gunicorn + UvicornWorker
#
# Build:
#   docker build --target development -t koruz-mall/api:dev  ./api
#   docker build --target production  -t koruz-mall/api:prod ./api

ARG PYTHON_VERSION=3.11

############################
# 1. Base — system packages shared by build/runtime stages
############################
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libpq5 \
        tini \
    && rm -rf /var/lib/apt/lists/*

############################
# 2. Deps — install Python packages into an isolated prefix
#    so we can copy them cleanly into the runtime image.
############################
FROM base AS deps

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --prefix=/install -r requirements.txt

############################
# 3. Development — source is volume-mounted by docker compose.
#    Includes build toolchain so editable installs / ad-hoc deps work.
############################
FROM deps AS development

ENV ENVIRONMENT=development

# Bring deps into the standard prefix so `python` finds them without any
# extra PYTHONPATH gymnastics.
RUN cp -r /install/. /usr/local/

COPY . .

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

############################
# 4. Production — slim, non-root, no build toolchain.
############################
FROM base AS production

ENV ENVIRONMENT=production

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY --from=deps /install /usr/local
COPY --chown=app:app . .

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", \
     "--workers", "4", \
     "--timeout", "60", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]

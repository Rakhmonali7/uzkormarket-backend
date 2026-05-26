"""Business-logic layer.

One module per domain, each exposing ``async def`` functions that take an
``AsyncSession`` first and raise :class:`fastapi.HTTPException` for any
caller-facing failure mode. Route handlers in :mod:`api.v1` are kept thin
on purpose — every non-trivial decision (uniqueness checks, role gates
beyond the dependency layer, stock locking, FX conversion, transaction
commits) lives here.

Modules are intentionally not re-exported at package level: importing
``services.auth_service`` (rather than ``services``) keeps grep-ability
high and avoids accidental cross-module coupling at import time.
"""

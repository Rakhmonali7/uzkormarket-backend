"""v1 API surface.

Each module under this package exports an ``APIRouter`` named ``router``
and is aggregated by :mod:`api.v1.router`. Route handlers stay thin —
input validation is done by Pydantic schemas, business logic lives in
:mod:`services`.
"""

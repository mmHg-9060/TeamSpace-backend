import re

from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.domain import router as domain_router
from app.api.inhouse_router import router as inhouse_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(inhouse_router)

# Retain legacy routes not superseded by the frontend's in-house API contract.
inhouse_keys = {
    (method, re.sub(r"\{[^}]+\}", "{}", route.path))
    for route in inhouse_router.routes
    for method in getattr(route, "methods", set())
}
legacy_router = APIRouter()
legacy_router.routes.extend(
    route
    for route in domain_router.routes
    if not any(
        (method, re.sub(r"\{[^}]+\}", "{}", route.path)) in inhouse_keys
        for method in getattr(route, "methods", set())
    )
)
api_router.include_router(legacy_router)

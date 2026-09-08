"""Inflector's local FastAPI entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from inflector_api.routers.companies import router as companies_router
from inflector_api.routers.health import router as health_router
from inflector_core.settings import get_settings

settings = get_settings()
app = FastAPI(
    title="Inflector API",
    version="0.1.0",
    description="Read-only Phase 1 canonical company identity API.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(companies_router, prefix="/api/v1")

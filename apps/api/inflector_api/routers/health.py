"""Health endpoint that verifies database connectivity."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from inflector_api.schemas import HealthResponse
from inflector_database.session import get_db_session

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(session: Session = Depends(get_db_session)) -> HealthResponse:
    """Report whether the API can connect to its source-of-truth database."""

    try:
        session.execute(select(1))
    except SQLAlchemyError:
        return HealthResponse(status="unavailable", database="unavailable")
    return HealthResponse(status="ok", database="ok")

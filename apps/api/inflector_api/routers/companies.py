"""Versioned company identity endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from inflector_api.schemas import CompanyListResponse, CompanyRead
from inflector_api.services import CompanyNotFoundError, CompanyService
from inflector_database.session import get_db_session

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=CompanyListResponse, summary="List canonical companies")
def list_companies(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> CompanyListResponse:
    """List a paginated company universe with security and listing identities."""

    companies, total = CompanyService(session).list_companies(limit=limit, offset=offset)
    return CompanyListResponse(
        items=[CompanyRead.model_validate(company) for company in companies],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{company_id}", response_model=CompanyRead, summary="Get a canonical company")
def get_company(company_id: UUID, session: Session = Depends(get_db_session)) -> CompanyRead:
    """Return a company and all current/historical security listing identities."""

    try:
        return CompanyRead.model_validate(CompanyService(session).get_company(company_id))
    except CompanyNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        ) from error

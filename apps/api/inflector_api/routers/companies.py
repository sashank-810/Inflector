"""Versioned company identity endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from inflector_api.company_research_schemas import (
    CompanyResearchContextListResponse,
    OpportunityScoreHistoryResponse,
)
from inflector_api.opportunity_score_service import (
    OpportunityResearchContextNotFoundError,
    OpportunityScoreService,
)
from inflector_api.schemas import CompanyListResponse, CompanyRead
from inflector_api.services import CompanyNotFoundError, CompanyService
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreReadRepository,
)
from inflector_database.score_repository import ScoreSnapshotIntegrityError
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


@router.get(
    "/{company_id}/research-contexts",
    response_model=CompanyResearchContextListResponse,
    summary="List company research contexts",
)
def list_company_research_contexts(
    company_id: UUID,
    model_family: str = Query(min_length=1, pattern=r".*\S.*"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> CompanyResearchContextListResponse:
    """Discover every explicit V5 security/configuration context for a company."""

    service = OpportunityScoreService(OpportunityScoreReadRepository(session))
    try:
        return service.list_company_contexts(
            company_id=company_id,
            model_family=model_family,
            limit=limit,
            offset=offset,
        )
    except CompanyNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        ) from error
    except ScoreSnapshotIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Opportunity score snapshot integrity check failed",
        ) from error


@router.get(
    "/{company_id}/opportunity-score-history",
    response_model=OpportunityScoreHistoryResponse,
    summary="List exact-context Opportunity Score history",
)
def list_company_opportunity_score_history(
    company_id: UUID,
    model_family: str = Query(min_length=1, pattern=r".*\S.*"),
    security_id: UUID = Query(),
    scoring_configuration_id: UUID = Query(),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> OpportunityScoreHistoryResponse:
    """Read persisted V5 history for one explicit research context."""

    service = OpportunityScoreService(OpportunityScoreReadRepository(session))
    try:
        return service.list_context_history(
            company_id=company_id,
            model_family=model_family,
            security_id=security_id,
            scoring_configuration_id=scoring_configuration_id,
            limit=limit,
            offset=offset,
        )
    except OpportunityResearchContextNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Opportunity score research context not found",
        ) from error
    except ScoreSnapshotIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Opportunity score snapshot integrity check failed",
        ) from error


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

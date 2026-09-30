"""Read-only Phase 5A Opportunity Score resources."""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from inflector_api.opportunity_score_schemas import (
    ConfigurationChecksum,
    OpportunityScoreDetailRead,
    OpportunityScoreListResponse,
    OpportunityScoreSort,
    OpportunityScoreStatus,
)
from inflector_api.opportunity_score_service import (
    OpportunityScoreNotFoundError,
    OpportunityScoreService,
)
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreReadRepository,
)
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreSort as RepositorySort,
)
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreStatus as RepositoryStatus,
)
from inflector_database.score_repository import ScoreSnapshotIntegrityError
from inflector_database.session import get_db_session

router = APIRouter(prefix="/opportunity-scores", tags=["opportunity-scores"])


@router.get("", response_model=OpportunityScoreListResponse, summary="List research queue")
def list_opportunity_scores(
    model_family: Annotated[
        str,
        Query(min_length=1, pattern=r".*\S.*"),
    ],
    configuration_checksum_sha256: Annotated[
        ConfigurationChecksum | None,
        Query(),
    ] = None,
    status_filter: Annotated[
        OpportunityScoreStatus | None,
        Query(alias="status"),
    ] = None,
    sort: Annotated[OpportunityScoreSort, Query()] = (
        OpportunityScoreSort.KNOWLEDGE_CUTOFF_DESC
    ),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_db_session),
) -> OpportunityScoreListResponse:
    """Return one latest integrity-checked V5 snapshot per explicit context."""

    service = OpportunityScoreService(OpportunityScoreReadRepository(session))
    try:
        return service.list_latest(
            model_family=model_family,
            configuration_checksum_sha256=configuration_checksum_sha256,
            status=(
                cast(RepositoryStatus, status_filter.value)
                if status_filter is not None
                else None
            ),
            sort=cast(RepositorySort, sort.value),
            limit=limit,
            offset=offset,
        )
    except ScoreSnapshotIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Opportunity score snapshot integrity check failed",
        ) from error


@router.get(
    "/{snapshot_id}",
    response_model=OpportunityScoreDetailRead,
    summary="Get Opportunity Score detail",
)
def get_opportunity_score(
    snapshot_id: UUID,
    session: Session = Depends(get_db_session),
) -> OpportunityScoreDetailRead:
    """Return one immutable V5 snapshot with persisted component explanations."""

    service = OpportunityScoreService(OpportunityScoreReadRepository(session))
    try:
        return service.get(snapshot_id)
    except OpportunityScoreNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Opportunity score snapshot not found",
        ) from error
    except ScoreSnapshotIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Opportunity score snapshot integrity check failed",
        ) from error

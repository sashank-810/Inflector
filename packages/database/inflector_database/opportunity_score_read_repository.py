"""Read-only persistence boundary for V5 Opportunity Score resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, raiseload, selectinload
from sqlalchemy.sql.elements import ColumnElement

from inflector_database.models import (
    Company,
    ExchangeListing,
    ModelVersion,
    ScoreComponent,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
)
from inflector_database.score_repository import ScoreSnapshotRepository

OPPORTUNITY_SCORE_SNAPSHOT_VERSION = "score_snapshot_v5"
OpportunityScoreStatus = Literal[
    "ineligible",
    "implemented_components_unavailable",
    "partial_component_set",
    "final_score_available",
]
OpportunityScoreSort = Literal[
    "knowledge_cutoff_desc",
    "opportunity_score_desc",
    "confidence_desc",
    "coverage_desc",
    "company_name_asc",
]


@dataclass(frozen=True, slots=True)
class OpportunityScoreReadRecord:
    snapshot: ScoreSnapshot
    company: Company
    security: Security
    listings: tuple[ExchangeListing, ...]
    model_version: ModelVersion
    scoring_configuration: ScoringConfiguration


class OpportunityScoreReadRepository:
    """Select and validate persisted V5 snapshots without recalculation or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._integrity = ScoreSnapshotRepository(session)

    def list_latest(
        self,
        *,
        model_family: str,
        configuration_checksum_sha256: str | None,
        status: OpportunityScoreStatus | None,
        sort: OpportunityScoreSort,
        limit: int,
        offset: int,
    ) -> tuple[tuple[OpportunityScoreReadRecord, ...], int]:
        ranked = self._ranked_snapshot_ids(
            model_family=model_family,
            configuration_checksum_sha256=configuration_checksum_sha256,
        )
        latest_ids = select(ranked.c.snapshot_id).where(ranked.c.latest_rank == 1)
        filters: list[ColumnElement[bool]] = [ScoreSnapshot.id.in_(latest_ids)]
        if status is not None:
            filters.append(ScoreSnapshot.snapshot_status == status)

        total = self._session.scalar(
            select(func.count()).select_from(ScoreSnapshot).where(*filters)
        )
        statement = (
            self._base_statement(include_explanations=False)
            .where(*filters)
            .order_by(*self._sort_expressions(sort))
            .limit(limit)
            .offset(offset)
        )
        records = tuple(self._record(tuple(row)) for row in self._session.execute(statement))
        return records, total or 0

    def get(self, snapshot_id: UUID) -> OpportunityScoreReadRecord | None:
        statement = self._base_statement(include_explanations=True).where(
            ScoreSnapshot.id == snapshot_id,
            ScoreSnapshot.algorithm_version == OPPORTUNITY_SCORE_SNAPSHOT_VERSION,
        )
        row = self._session.execute(statement).one_or_none()
        return None if row is None else self._record(tuple(row))

    def company_exists(self, company_id: UUID) -> bool:
        return bool(
            self._session.scalar(
                select(func.count()).select_from(Company).where(Company.id == company_id)
            )
        )

    def list_company_contexts(
        self,
        *,
        company_id: UUID,
        model_family: str,
        limit: int,
        offset: int,
    ) -> tuple[tuple[OpportunityScoreReadRecord, ...], int]:
        ranked = self._ranked_snapshot_ids(
            model_family=model_family,
            configuration_checksum_sha256=None,
            company_id=company_id,
        )
        latest_ids = select(ranked.c.snapshot_id).where(ranked.c.latest_rank == 1)
        filters = [ScoreSnapshot.id.in_(latest_ids)]
        total = self._session.scalar(
            select(func.count()).select_from(ScoreSnapshot).where(*filters)
        )
        statement = (
            self._base_statement(include_explanations=False)
            .where(*filters)
            .order_by(
                Security.isin.asc(),
                ScoringConfiguration.checksum_sha256.asc(),
                ScoringConfiguration.id.asc(),
                ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        records = tuple(self._record(tuple(row)) for row in self._session.execute(statement))
        return records, total or 0

    def list_context_history(
        self,
        *,
        company_id: UUID,
        model_family: str,
        security_id: UUID,
        scoring_configuration_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[
        tuple[OpportunityScoreReadRecord, ...],
        int,
        tuple[OpportunityScoreReadRecord, ...],
    ]:
        filters = (
            ScoreSnapshot.algorithm_version == OPPORTUNITY_SCORE_SNAPSHOT_VERSION,
            ScoreSnapshot.company_id == company_id,
            ScoreSnapshot.selected_security_id == security_id,
            ScoreSnapshot.scoring_configuration_id == scoring_configuration_id,
            Security.company_id == company_id,
            ModelVersion.model_family == model_family,
            ScoringConfiguration.model_version_id == ModelVersion.id,
        )
        total = self._session.scalar(
            select(func.count())
            .select_from(ScoreSnapshot)
            .join(Security, ScoreSnapshot.selected_security_id == Security.id)
            .join(ModelVersion, ScoreSnapshot.model_version_id == ModelVersion.id)
            .join(
                ScoringConfiguration,
                ScoreSnapshot.scoring_configuration_id == ScoringConfiguration.id,
            )
            .where(*filters)
        )
        ordering = self._history_ordering()
        page_statement = (
            self._base_statement(include_explanations=False)
            .where(*filters)
            .order_by(*ordering)
            .limit(limit)
            .offset(offset)
        )
        comparison_statement = (
            self._base_statement(include_explanations=False)
            .where(*filters)
            .order_by(*ordering)
            .limit(2)
        )
        page = tuple(self._record(tuple(row)) for row in self._session.execute(page_statement))
        latest_two = tuple(
            self._record(tuple(row)) for row in self._session.execute(comparison_statement)
        )
        return page, total or 0, latest_two

    def get_v5_audit_snapshot(self, snapshot_id: UUID) -> ScoreSnapshot | None:
        component_loader = selectinload(ScoreSnapshot.components).options(
            raiseload(ScoreComponent.explanations)
        )
        snapshot = self._session.scalar(
            select(ScoreSnapshot)
            .where(
                ScoreSnapshot.id == snapshot_id,
                ScoreSnapshot.algorithm_version == OPPORTUNITY_SCORE_SNAPSHOT_VERSION,
            )
            .options(component_loader)
        )
        if snapshot is None:
            return None
        return self._integrity.validate_persisted_snapshot(snapshot)

    @staticmethod
    def _ranked_snapshot_ids(
        *,
        model_family: str,
        configuration_checksum_sha256: str | None,
        company_id: UUID | None = None,
    ):
        filters = [
            ScoreSnapshot.algorithm_version == OPPORTUNITY_SCORE_SNAPSHOT_VERSION,
            ModelVersion.model_family == model_family,
        ]
        if configuration_checksum_sha256 is not None:
            filters.append(
                ScoreSnapshot.configuration_checksum_sha256 == configuration_checksum_sha256.lower()
            )
        if company_id is not None:
            filters.append(ScoreSnapshot.company_id == company_id)
        return (
            select(
                ScoreSnapshot.id.label("snapshot_id"),
                func.row_number()
                .over(
                    partition_by=(
                        ScoreSnapshot.company_id,
                        ScoreSnapshot.selected_security_id,
                        ScoreSnapshot.scoring_configuration_id,
                    ),
                    order_by=(
                        ScoreSnapshot.knowledge_cutoff.desc(),
                        ScoreSnapshot.ending_fiscal_year.desc(),
                        ScoreSnapshot.ending_fiscal_quarter.desc(),
                        ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
                    ),
                )
                .label("latest_rank"),
            )
            .join(ModelVersion, ScoreSnapshot.model_version_id == ModelVersion.id)
            .where(*filters)
            .subquery()
        )

    @staticmethod
    def _history_ordering() -> tuple[ColumnElement[Any], ...]:
        return (
            ScoreSnapshot.knowledge_cutoff.desc(),
            ScoreSnapshot.ending_fiscal_year.desc(),
            ScoreSnapshot.ending_fiscal_quarter.desc(),
            ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
        )

    @staticmethod
    def _base_statement(*, include_explanations: bool) -> Select:
        component_loader = selectinload(ScoreSnapshot.components)
        if include_explanations:
            component_loader = component_loader.selectinload(ScoreComponent.explanations)
        else:
            component_loader = component_loader.options(raiseload(ScoreComponent.explanations))
        return (
            select(
                ScoreSnapshot,
                Company,
                Security,
                ModelVersion,
                ScoringConfiguration,
            )
            .join(Company, ScoreSnapshot.company_id == Company.id)
            .join(Security, ScoreSnapshot.selected_security_id == Security.id)
            .join(ModelVersion, ScoreSnapshot.model_version_id == ModelVersion.id)
            .join(
                ScoringConfiguration,
                ScoreSnapshot.scoring_configuration_id == ScoringConfiguration.id,
            )
            .options(
                component_loader,
                selectinload(Security.listings),
            )
        )

    @staticmethod
    def _sort_expressions(
        sort: OpportunityScoreSort,
    ) -> tuple[ColumnElement[Any], ...]:
        final_score_desc = ScoreSnapshot.final_score.desc().nulls_last()
        common_identity = (
            Company.display_name.asc(),
            Security.isin.asc(),
            ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
        )
        if sort == "knowledge_cutoff_desc":
            return (
                ScoreSnapshot.knowledge_cutoff.desc(),
                Company.display_name.asc(),
                Security.isin.asc(),
                ScoreSnapshot.configuration_checksum_sha256.asc(),
                ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
            )
        if sort == "opportunity_score_desc":
            return (
                final_score_desc,
                ScoreSnapshot.top_level_component_weight_coverage.desc(),
                ScoreSnapshot.confidence.desc(),
                *common_identity,
            )
        if sort == "confidence_desc":
            return (
                ScoreSnapshot.confidence.desc(),
                final_score_desc,
                *common_identity,
            )
        if sort == "coverage_desc":
            return (
                ScoreSnapshot.top_level_component_weight_coverage.desc(),
                final_score_desc,
                *common_identity,
            )
        return (
            Company.display_name.asc(),
            Security.isin.asc(),
            ScoreSnapshot.knowledge_cutoff.desc(),
            ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
        )

    def _record(
        self,
        row: tuple[
            ScoreSnapshot,
            Company,
            Security,
            ModelVersion,
            ScoringConfiguration,
        ],
    ) -> OpportunityScoreReadRecord:
        snapshot, company, security, model_version, scoring_configuration = row
        validated = self._integrity.validate_persisted_snapshot(snapshot)
        listings = tuple(
            sorted(
                security.listings,
                key=lambda item: (
                    item.exchange,
                    item.valid_from,
                    item.symbol,
                    str(item.id),
                ),
            )
        )
        return OpportunityScoreReadRecord(
            snapshot=validated,
            company=company,
            security=security,
            listings=listings,
            model_version=model_version,
            scoring_configuration=scoring_configuration,
        )

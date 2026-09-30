"""Read-only application service for persisted V5 Opportunity Scores."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from inflector_api.opportunity_score_schemas import (
    OpportunityCompanyRead,
    OpportunityComponentDetailRead,
    OpportunityComponentSummaryRead,
    OpportunityEligibilityRead,
    OpportunityExplanationRead,
    OpportunityListingRead,
    OpportunityModelVersionRead,
    OpportunityScoreDetailRead,
    OpportunityScoreListResponse,
    OpportunityScoreQueueItemRead,
    OpportunityScoreStatus,
    OpportunityScoreSummaryRead,
    OpportunityScoringConfigurationRead,
    OpportunitySecurityRead,
)
from inflector_database.models import ScoreComponent, ScoreExplanation
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreReadRecord,
    OpportunityScoreReadRepository,
)
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreSort as RepositorySort,
)
from inflector_database.opportunity_score_read_repository import (
    OpportunityScoreStatus as RepositoryStatus,
)
from inflector_database.score_repository import V5_COMPONENT_ORDER


class OpportunityScoreNotFoundError(Exception):
    """Raised when a UUID is absent or does not identify a V5 snapshot."""


class OpportunityScoreService:
    """Convert integrity-checked persistence rows to stable product read models."""

    def __init__(self, repository: OpportunityScoreReadRepository) -> None:
        self._repository = repository

    def list_latest(
        self,
        *,
        model_family: str,
        configuration_checksum_sha256: str | None,
        status: RepositoryStatus | None,
        sort: RepositorySort,
        limit: int,
        offset: int,
    ) -> OpportunityScoreListResponse:
        records, total = self._repository.list_latest(
            model_family=model_family,
            configuration_checksum_sha256=configuration_checksum_sha256,
            status=status,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return OpportunityScoreListResponse(
            items=[self._queue_item(record) for record in records],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get(self, snapshot_id: UUID) -> OpportunityScoreDetailRead:
        record = self._repository.get(snapshot_id)
        if record is None:
            raise OpportunityScoreNotFoundError(str(snapshot_id))
        summary = self._summary(record)
        snapshot = record.snapshot
        return OpportunityScoreDetailRead(
            **summary.model_dump(),
            eligibility=OpportunityEligibilityRead(
                eligible=snapshot.eligibility_eligible,
                reasons=self._strings(snapshot.eligibility_reasons_json),
                warnings=self._strings(snapshot.eligibility_warnings_json),
            ),
            confidence_details=snapshot.confidence_details_json,
            context_resolution=snapshot.context_resolution_json,
            model_version=OpportunityModelVersionRead(
                id=record.model_version.id,
                model_family=record.model_version.model_family,
                semantic_version=record.model_version.semantic_version,
                git_sha=record.model_version.git_sha,
                status=record.model_version.status,
            ),
            scoring_configuration=OpportunityScoringConfigurationRead(
                id=record.scoring_configuration.id,
                configuration_name=record.scoring_configuration.configuration_name,
                configuration_version=record.scoring_configuration.configuration_version,
                status=record.scoring_configuration.status,
                checksum_sha256=record.scoring_configuration.checksum_sha256,
                effective_from=self._optional_utc(
                    record.scoring_configuration.effective_from
                ),
                effective_to=self._optional_utc(record.scoring_configuration.effective_to),
            ),
            components=[
                self._component_detail(component)
                for component in self._ordered_components(record)
            ],
        )

    def _queue_item(self, record: OpportunityScoreReadRecord) -> OpportunityScoreQueueItemRead:
        return OpportunityScoreQueueItemRead(
            **self._summary(record).model_dump(),
            components=[
                self._component_summary(component)
                for component in self._ordered_components(record)
            ],
        )

    def _summary(self, record: OpportunityScoreReadRecord) -> OpportunityScoreSummaryRead:
        snapshot = record.snapshot
        company = record.company
        security = record.security
        return OpportunityScoreSummaryRead(
            company=OpportunityCompanyRead(
                company_id=company.id,
                display_name=company.display_name,
                legal_name=company.legal_name,
                sector=company.sector,
                industry=company.industry,
            ),
            security=OpportunitySecurityRead(
                security_id=security.id,
                isin=security.isin,
                security_type=security.security_type,
                security_status=security.status,
                listings=[
                    OpportunityListingRead(
                        id=listing.id,
                        exchange=listing.exchange,
                        symbol=listing.symbol,
                        valid_from=listing.valid_from,
                        valid_to=listing.valid_to,
                        status=listing.status,
                    )
                    for listing in record.listings
                ],
            ),
            snapshot_id=snapshot.id,
            algorithm_version=snapshot.algorithm_version,
            snapshot_status=OpportunityScoreStatus(snapshot.snapshot_status),
            as_of_date=snapshot.as_of_date,
            knowledge_cutoff=self._utc(snapshot.knowledge_cutoff),
            ending_fiscal_year=snapshot.ending_fiscal_year,
            ending_fiscal_quarter=snapshot.ending_fiscal_quarter,
            model_version_id=snapshot.model_version_id,
            scoring_configuration_id=snapshot.scoring_configuration_id,
            configuration_checksum_sha256=snapshot.configuration_checksum_sha256,
            selected_provider_dataset_id=snapshot.selected_provider_dataset_id,
            selected_filing_scope=snapshot.selected_filing_scope,
            final_score=snapshot.final_score,
            confidence=snapshot.confidence,
            financial_core_coverage=snapshot.financial_core_coverage,
            top_level_component_weight_coverage=(
                snapshot.top_level_component_weight_coverage
            ),
            available_component_codes=self._strings(
                snapshot.available_component_codes_json
            ),
            missing_component_codes=self._strings(snapshot.missing_component_codes_json),
            snapshot_fingerprint_sha256=snapshot.snapshot_fingerprint_sha256,
            created_at=self._utc(snapshot.created_at),
        )

    def _ordered_components(
        self, record: OpportunityScoreReadRecord
    ) -> tuple[ScoreComponent, ...]:
        rank = {code: index for index, code in enumerate(V5_COMPONENT_ORDER)}
        return tuple(
            sorted(
                record.snapshot.components,
                key=lambda component: rank[component.component_code],
            )
        )

    def _component_summary(
        self, component: ScoreComponent
    ) -> OpportunityComponentSummaryRead:
        if component.score is None:
            raise AssertionError("integrity-checked V5 component has no score")
        return OpportunityComponentSummaryRead(
            component_code=component.component_code,
            score=component.score,
            unit=component.unit,
            configured_top_level_weight=component.configured_top_level_weight,
            subfactor_weight_coverage=component.subfactor_weight_coverage,
            final_contribution=component.final_contribution,
            available_at=self._optional_utc(component.available_at),
            algorithm_version=component.algorithm_version,
            missing_subfactors=self._strings(component.missing_subfactors_json),
            warnings=self._strings(component.warnings_json),
        )

    def _component_detail(self, component: ScoreComponent) -> OpportunityComponentDetailRead:
        summary = self._component_summary(component)
        explanations = sorted(
            component.explanations,
            key=lambda item: (item.rank, item.factor_code, str(item.id)),
        )
        return OpportunityComponentDetailRead(
            **summary.model_dump(),
            detail=component.detail_json,
            explanations=[self._explanation(item) for item in explanations],
        )

    def _explanation(self, value: ScoreExplanation) -> OpportunityExplanationRead:
        return OpportunityExplanationRead(
            id=value.id,
            factor_code=value.factor_code,
            rank=value.rank,
            raw_value=value.raw_value,
            raw_unit=value.raw_unit,
            normalized_score=value.normalized_score,
            configured_weight=value.configured_weight,
            effective_weight=value.effective_weight,
            component_contribution=value.component_contribution,
            input_available_at=self._utc(value.input_available_at),
            evidence_type=value.evidence_type,
            template_code=value.template_code,
            direction=value.direction,
            evidence_manifest=value.evidence_manifest_json,
        )

    @staticmethod
    def _strings(values: list[object]) -> list[str]:
        return [str(value) for value in values]

    @staticmethod
    def _optional_utc(value: datetime | None) -> datetime | None:
        return None if value is None else OpportunityScoreService._utc(value)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

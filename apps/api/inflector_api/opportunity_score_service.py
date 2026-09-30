"""Read-only application service for persisted V5 Opportunity Scores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from inflector_api.company_research_schemas import (
    CompanyResearchContextListResponse,
    CompanyResearchContextRead,
    OpportunityAuditCategory,
    OpportunityAuditCategoryCountRead,
    OpportunityComponentChangeRead,
    OpportunityScoreAuditCategoryResponse,
    OpportunityScoreAuditSummaryRead,
    OpportunityScoreChangeRead,
    OpportunityScoreHistoryResponse,
)
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
from inflector_api.services import CompanyNotFoundError
from inflector_database.models import ScoreComponent, ScoreExplanation, ScoreSnapshot
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
from inflector_database.score_repository import V5_COMPONENT_ORDER, ScoreSnapshotIntegrityError


class OpportunityScoreNotFoundError(Exception):
    """Raised when a UUID is absent or does not identify a V5 snapshot."""


class OpportunityResearchContextNotFoundError(Exception):
    """Raised when an exact company/security/configuration V5 context is absent."""


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
                effective_from=self._optional_utc(record.scoring_configuration.effective_from),
                effective_to=self._optional_utc(record.scoring_configuration.effective_to),
            ),
            components=[
                self._component_detail(component) for component in self._ordered_components(record)
            ],
        )

    def list_company_contexts(
        self,
        *,
        company_id: UUID,
        model_family: str,
        limit: int,
        offset: int,
    ) -> CompanyResearchContextListResponse:
        if not self._repository.company_exists(company_id):
            raise CompanyNotFoundError(str(company_id))
        records, total = self._repository.list_company_contexts(
            company_id=company_id,
            model_family=model_family,
            limit=limit,
            offset=offset,
        )
        return CompanyResearchContextListResponse(
            items=[
                CompanyResearchContextRead(
                    company_id=company_id,
                    security=self._security(record),
                    model_version=self._model_version(record),
                    scoring_configuration=self._scoring_configuration(record),
                    latest_snapshot=self._queue_item(record),
                )
                for record in records
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    def list_context_history(
        self,
        *,
        company_id: UUID,
        model_family: str,
        security_id: UUID,
        scoring_configuration_id: UUID,
        limit: int,
        offset: int,
    ) -> OpportunityScoreHistoryResponse:
        records, total, latest_two = self._repository.list_context_history(
            company_id=company_id,
            model_family=model_family,
            security_id=security_id,
            scoring_configuration_id=scoring_configuration_id,
            limit=limit,
            offset=offset,
        )
        if total == 0:
            raise OpportunityResearchContextNotFoundError(
                f"{company_id}/{security_id}/{scoring_configuration_id}"
            )
        return OpportunityScoreHistoryResponse(
            items=[self._queue_item(record) for record in records],
            total=total,
            limit=limit,
            offset=offset,
            latest_change=(
                self._change(latest_two[0], latest_two[1]) if len(latest_two) == 2 else None
            ),
        )

    def audit_summary(self, snapshot_id: UUID) -> OpportunityScoreAuditSummaryRead:
        snapshot = self._audit_snapshot(snapshot_id)
        union = self._audit_union(snapshot)
        return OpportunityScoreAuditSummaryRead(
            snapshot_id=snapshot.id,
            snapshot_fingerprint_sha256=snapshot.snapshot_fingerprint_sha256,
            algorithm_version=snapshot.algorithm_version,
            curve_algorithm_version=cast(str, union["curve_algorithm_version"]),
            opportunity_score_aggregation_version=cast(
                str, union["opportunity_score_aggregation_version"]
            ),
            component_algorithm_versions=cast(
                list[dict[str, object]], union["component_algorithm_versions"]
            ),
            phase3_algorithm_versions=cast(
                list[dict[str, object]], union["phase3_algorithm_versions"]
            ),
            categories=[
                OpportunityAuditCategoryCountRead(
                    category=category,
                    count=len(cast(list[dict[str, object]], union[category.value])),
                )
                for category in OpportunityAuditCategory
            ],
        )

    def audit_category(
        self,
        *,
        snapshot_id: UUID,
        category: OpportunityAuditCategory,
        limit: int,
        offset: int,
    ) -> OpportunityScoreAuditCategoryResponse:
        snapshot = self._audit_snapshot(snapshot_id)
        union = self._audit_union(snapshot)
        values = cast(list[dict[str, object]], union[category.value])
        return OpportunityScoreAuditCategoryResponse(
            snapshot_id=snapshot.id,
            snapshot_fingerprint_sha256=snapshot.snapshot_fingerprint_sha256,
            category=category,
            items=values[offset : offset + limit],
            total=len(values),
            limit=limit,
            offset=offset,
        )

    def _queue_item(self, record: OpportunityScoreReadRecord) -> OpportunityScoreQueueItemRead:
        return OpportunityScoreQueueItemRead(
            **self._summary(record).model_dump(),
            components=[
                self._component_summary(component) for component in self._ordered_components(record)
            ],
        )

    def _security(self, record: OpportunityScoreReadRecord) -> OpportunitySecurityRead:
        security = record.security
        return OpportunitySecurityRead(
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
        )

    def _model_version(self, record: OpportunityScoreReadRecord) -> OpportunityModelVersionRead:
        value = record.model_version
        return OpportunityModelVersionRead(
            id=value.id,
            model_family=value.model_family,
            semantic_version=value.semantic_version,
            git_sha=value.git_sha,
            status=value.status,
        )

    def _scoring_configuration(
        self, record: OpportunityScoreReadRecord
    ) -> OpportunityScoringConfigurationRead:
        value = record.scoring_configuration
        return OpportunityScoringConfigurationRead(
            id=value.id,
            configuration_name=value.configuration_name,
            configuration_version=value.configuration_version,
            status=value.status,
            checksum_sha256=value.checksum_sha256,
            effective_from=self._optional_utc(value.effective_from),
            effective_to=self._optional_utc(value.effective_to),
        )

    def _summary(self, record: OpportunityScoreReadRecord) -> OpportunityScoreSummaryRead:
        snapshot = record.snapshot
        company = record.company
        return OpportunityScoreSummaryRead(
            company=OpportunityCompanyRead(
                company_id=company.id,
                display_name=company.display_name,
                legal_name=company.legal_name,
                sector=company.sector,
                industry=company.industry,
            ),
            security=self._security(record),
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
            top_level_component_weight_coverage=(snapshot.top_level_component_weight_coverage),
            available_component_codes=self._strings(snapshot.available_component_codes_json),
            missing_component_codes=self._strings(snapshot.missing_component_codes_json),
            snapshot_fingerprint_sha256=snapshot.snapshot_fingerprint_sha256,
            created_at=self._utc(snapshot.created_at),
        )

    def _ordered_components(self, record: OpportunityScoreReadRecord) -> tuple[ScoreComponent, ...]:
        rank = {code: index for index, code in enumerate(V5_COMPONENT_ORDER)}
        return tuple(
            sorted(
                record.snapshot.components,
                key=lambda component: rank[component.component_code],
            )
        )

    def _component_summary(self, component: ScoreComponent) -> OpportunityComponentSummaryRead:
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

    def _change(
        self,
        current: OpportunityScoreReadRecord,
        comparison: OpportunityScoreReadRecord,
    ) -> OpportunityScoreChangeRead:
        current_snapshot = current.snapshot
        comparison_snapshot = comparison.snapshot
        current_components = {
            component.component_code: component for component in current_snapshot.components
        }
        comparison_components = {
            component.component_code: component for component in comparison_snapshot.components
        }
        component_changes: list[OpportunityComponentChangeRead] = []
        for code in V5_COMPONENT_ORDER:
            current_component = current_components.get(code)
            comparison_component = comparison_components.get(code)
            current_score = None if current_component is None else current_component.score
            comparison_score = None if comparison_component is None else comparison_component.score
            if current_component is None and comparison_component is not None:
                availability_change = "removed"
            elif current_component is not None and comparison_component is None:
                availability_change = "added"
            else:
                availability_change = "unchanged"
            component_changes.append(
                OpportunityComponentChangeRead(
                    component_code=code,
                    current_score=current_score,
                    comparison_score=comparison_score,
                    score_delta=(
                        current_score - comparison_score
                        if current_score is not None and comparison_score is not None
                        else None
                    ),
                    current_available=current_component is not None,
                    comparison_available=comparison_component is not None,
                    availability_change=availability_change,
                )
            )
        return OpportunityScoreChangeRead(
            current_snapshot_id=current_snapshot.id,
            comparison_snapshot_id=comparison_snapshot.id,
            current_status=OpportunityScoreStatus(current_snapshot.snapshot_status),
            comparison_status=OpportunityScoreStatus(comparison_snapshot.snapshot_status),
            status_changed=(
                current_snapshot.snapshot_status != comparison_snapshot.snapshot_status
            ),
            final_score_delta=(
                current_snapshot.final_score - comparison_snapshot.final_score
                if current_snapshot.final_score is not None
                and comparison_snapshot.final_score is not None
                else None
            ),
            confidence_delta=current_snapshot.confidence - comparison_snapshot.confidence,
            coverage_delta=(
                current_snapshot.top_level_component_weight_coverage
                - comparison_snapshot.top_level_component_weight_coverage
            ),
            newly_available_component_codes=[
                code
                for code in V5_COMPONENT_ORDER
                if code in current_components and code not in comparison_components
            ],
            newly_missing_component_codes=[
                code
                for code in V5_COMPONENT_ORDER
                if code not in current_components and code in comparison_components
            ],
            component_changes=component_changes,
        )

    def _audit_snapshot(self, snapshot_id: UUID) -> ScoreSnapshot:
        snapshot = self._repository.get_v5_audit_snapshot(snapshot_id)
        if snapshot is None:
            raise OpportunityScoreNotFoundError(str(snapshot_id))
        return snapshot

    @staticmethod
    def _audit_union(snapshot: ScoreSnapshot) -> dict[str, object]:
        manifest = snapshot.input_manifest_json
        if not isinstance(manifest, dict):
            raise ScoreSnapshotIntegrityError("v5 audit manifest is not an object")
        union = manifest.get("union")
        if not isinstance(union, dict):
            raise ScoreSnapshotIntegrityError("v5 audit union is not an object")
        for category in OpportunityAuditCategory:
            values = union.get(category.value)
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise ScoreSnapshotIntegrityError(
                    f"v5 audit category {category.value} is malformed"
                )
        for key in ("component_algorithm_versions", "phase3_algorithm_versions"):
            values = union.get(key)
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise ScoreSnapshotIntegrityError(f"v5 audit {key} is malformed")
        curve_version = union.get("curve_algorithm_version")
        aggregation_version = union.get("opportunity_score_aggregation_version")
        payload_aggregation = snapshot.fingerprint_payload_json.get(
            "opportunity_score_aggregation_version"
        )
        if not isinstance(curve_version, str) or not curve_version:
            raise ScoreSnapshotIntegrityError("v5 audit curve version is malformed")
        if (
            not isinstance(aggregation_version, str)
            or not aggregation_version
            or aggregation_version != payload_aggregation
        ):
            raise ScoreSnapshotIntegrityError("v5 audit aggregation version is malformed")
        return cast(dict[str, object], union)

    @staticmethod
    def _strings(values: list[object]) -> list[str]:
        return [str(value) for value in values]

    @staticmethod
    def _optional_utc(value: datetime | None) -> datetime | None:
        return None if value is None else OpportunityScoreService._utc(value)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

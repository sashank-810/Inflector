"""Phase 5B company research history and bounded audit response contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from inflector_api.opportunity_score_schemas import (
    ExactDecimalString,
    OpportunityModelVersionRead,
    OpportunityScoreQueueItemRead,
    OpportunityScoreStatus,
    OpportunityScoringConfigurationRead,
    OpportunitySecurityRead,
)


class OpportunityAuditCategory(StrEnum):
    FINANCIAL_FACTS = "financial_facts"
    MARKET_BARS = "market_bars"
    BENCHMARK_BARS = "benchmark_bars"
    CORPORATE_ACTIONS = "corporate_actions"
    ANNOUNCEMENTS = "announcements"
    DOCUMENTS = "documents"
    DOCUMENT_ASSETS = "document_assets"
    TEXT_EXTRACTIONS = "text_extractions"
    BUSINESS_EVENTS = "business_events"
    BUSINESS_EVENT_EVIDENCE = "business_event_evidence"
    QUANTITATIVE_DERIVATIONS = "quantitative_derivations"
    QUANTITATIVE_FACTS = "quantitative_facts"
    ATTENTION_OBSERVATIONS = "attention_observations"
    ATTENTION_SOURCE_RECORDS = "attention_source_records"


class CompanyResearchContextRead(BaseModel):
    company_id: UUID
    security: OpportunitySecurityRead
    model_version: OpportunityModelVersionRead
    scoring_configuration: OpportunityScoringConfigurationRead
    latest_snapshot: OpportunityScoreQueueItemRead


class CompanyResearchContextListResponse(BaseModel):
    items: list[CompanyResearchContextRead]
    total: int
    limit: int
    offset: int


class OpportunityComponentChangeRead(BaseModel):
    component_code: str
    current_score: ExactDecimalString | None
    comparison_score: ExactDecimalString | None
    score_delta: ExactDecimalString | None
    current_available: bool
    comparison_available: bool
    availability_change: Literal["unchanged", "added", "removed"]


class OpportunityScoreChangeRead(BaseModel):
    current_snapshot_id: UUID
    comparison_snapshot_id: UUID
    current_status: OpportunityScoreStatus
    comparison_status: OpportunityScoreStatus
    status_changed: bool
    final_score_delta: ExactDecimalString | None
    confidence_delta: ExactDecimalString
    coverage_delta: ExactDecimalString
    newly_available_component_codes: list[str]
    newly_missing_component_codes: list[str]
    component_changes: list[OpportunityComponentChangeRead]


class OpportunityScoreHistoryResponse(BaseModel):
    items: list[OpportunityScoreQueueItemRead]
    total: int
    limit: int
    offset: int
    latest_change: OpportunityScoreChangeRead | None


class OpportunityAuditCategoryCountRead(BaseModel):
    category: OpportunityAuditCategory
    count: int


class OpportunityScoreAuditSummaryRead(BaseModel):
    snapshot_id: UUID
    snapshot_fingerprint_sha256: str
    algorithm_version: str
    curve_algorithm_version: str
    opportunity_score_aggregation_version: str
    component_algorithm_versions: list[dict[str, object]]
    phase3_algorithm_versions: list[dict[str, object]]
    categories: list[OpportunityAuditCategoryCountRead]


class OpportunityScoreAuditCategoryResponse(BaseModel):
    snapshot_id: UUID
    snapshot_fingerprint_sha256: str
    category: OpportunityAuditCategory
    items: list[dict[str, object]]
    total: int
    limit: int
    offset: int

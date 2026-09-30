"""Stable Phase 5A response contracts for persisted V5 Opportunity Scores."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, PlainSerializer, StringConstraints


def canonical_decimal_string(value: Decimal) -> str:
    """Serialize an exact Decimal without floats, exponent notation, or spare zeros."""

    if value == 0:
        return "0"
    return format(value.normalize(), "f")


ExactDecimalString = Annotated[
    Decimal,
    PlainSerializer(canonical_decimal_string, return_type=str, when_used="json"),
]
ConfigurationChecksum = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-fA-F]{64}$"),
    AfterValidator(str.lower),
]


class OpportunityScoreStatus(StrEnum):
    INELIGIBLE = "ineligible"
    IMPLEMENTED_COMPONENTS_UNAVAILABLE = "implemented_components_unavailable"
    PARTIAL_COMPONENT_SET = "partial_component_set"
    FINAL_SCORE_AVAILABLE = "final_score_available"


class OpportunityScoreSort(StrEnum):
    KNOWLEDGE_CUTOFF_DESC = "knowledge_cutoff_desc"
    OPPORTUNITY_SCORE_DESC = "opportunity_score_desc"
    CONFIDENCE_DESC = "confidence_desc"
    COVERAGE_DESC = "coverage_desc"
    COMPANY_NAME_ASC = "company_name_asc"


class OpportunityCompanyRead(BaseModel):
    company_id: UUID
    display_name: str
    legal_name: str
    sector: str
    industry: str


class OpportunityListingRead(BaseModel):
    id: UUID
    exchange: str
    symbol: str
    valid_from: date
    valid_to: date | None
    status: str


class OpportunitySecurityRead(BaseModel):
    security_id: UUID
    isin: str
    security_type: str
    security_status: str
    listings: list[OpportunityListingRead]


class OpportunityComponentSummaryRead(BaseModel):
    component_code: str
    score: ExactDecimalString
    unit: str
    configured_top_level_weight: ExactDecimalString
    subfactor_weight_coverage: ExactDecimalString
    final_contribution: ExactDecimalString | None
    available_at: datetime | None
    algorithm_version: str
    missing_subfactors: list[str]
    warnings: list[str]


class OpportunityScoreSummaryRead(BaseModel):
    company: OpportunityCompanyRead
    security: OpportunitySecurityRead
    snapshot_id: UUID
    algorithm_version: str
    snapshot_status: OpportunityScoreStatus
    as_of_date: date
    knowledge_cutoff: datetime
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    model_version_id: UUID
    scoring_configuration_id: UUID
    configuration_checksum_sha256: str
    selected_provider_dataset_id: UUID | None
    selected_filing_scope: str | None
    final_score: ExactDecimalString | None
    confidence: ExactDecimalString
    financial_core_coverage: ExactDecimalString
    top_level_component_weight_coverage: ExactDecimalString
    available_component_codes: list[str]
    missing_component_codes: list[str]
    snapshot_fingerprint_sha256: str
    created_at: datetime


class OpportunityScoreQueueItemRead(OpportunityScoreSummaryRead):
    components: list[OpportunityComponentSummaryRead]


class OpportunityScoreListResponse(BaseModel):
    items: list[OpportunityScoreQueueItemRead]
    total: int
    limit: int
    offset: int


class OpportunityModelVersionRead(BaseModel):
    id: UUID
    model_family: str
    semantic_version: str
    git_sha: str
    status: str


class OpportunityScoringConfigurationRead(BaseModel):
    id: UUID
    configuration_name: str
    configuration_version: str
    status: str
    checksum_sha256: str
    effective_from: datetime | None
    effective_to: datetime | None


class OpportunityExplanationRead(BaseModel):
    id: UUID
    factor_code: str
    rank: int
    raw_value: ExactDecimalString
    raw_unit: str
    normalized_score: ExactDecimalString
    configured_weight: ExactDecimalString
    effective_weight: ExactDecimalString
    component_contribution: ExactDecimalString
    input_available_at: datetime
    evidence_type: str
    template_code: str
    direction: str | None
    evidence_manifest: dict[str, object]


class OpportunityComponentDetailRead(OpportunityComponentSummaryRead):
    detail: dict[str, object]
    explanations: list[OpportunityExplanationRead]


class OpportunityEligibilityRead(BaseModel):
    eligible: bool
    reasons: list[str]
    warnings: list[str]


class OpportunityScoreDetailRead(OpportunityScoreSummaryRead):
    eligibility: OpportunityEligibilityRead
    confidence_details: dict[str, object]
    context_resolution: dict[str, object]
    model_version: OpportunityModelVersionRead
    scoring_configuration: OpportunityScoringConfigurationRead
    components: list[OpportunityComponentDetailRead]

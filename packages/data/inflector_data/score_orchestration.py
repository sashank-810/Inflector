"""Versioned orchestration for immutable partial scoring snapshots."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from inflector_core.balance_sheet_scoring import (
    BALANCE_SHEET_SUBFACTOR_ORDER,
    BalanceSheetComponentScore,
    BalanceSheetComponentScorer,
    BalanceSheetEvidence,
    BalanceSheetSubfactorScore,
)
from inflector_core.business_catalyst_scoring import (
    BUSINESS_CATALYST_AGGREGATION_VERSION,
    BUSINESS_CATALYST_EVENT_SCORE_VERSION,
    BusinessCatalystComponentScore,
    BusinessCatalystComponentScorer,
)
from inflector_core.business_event_rules import BUSINESS_EVENT_TYPE_ORDER
from inflector_core.business_quality_scoring import (
    BUSINESS_QUALITY_SUBFACTOR_ORDER,
    BusinessQualityComponentScore,
    BusinessQualityComponentScorer,
    BusinessQualityEvidence,
    BusinessQualitySubfactorScore,
)
from inflector_core.cash_flow_quality_scoring import (
    CASH_FLOW_QUALITY_SUBFACTOR_ORDER,
    CashFlowQualityComponentScore,
    CashFlowQualityComponentScorer,
    CashFlowQualityEvidence,
    CashFlowQualitySubfactorScore,
)
from inflector_core.component_scoring import (
    PIECEWISE_LINEAR_CURVE_VERSION,
    SUBFACTOR_ORDER,
    FinancialInflectionComponentScore,
    FinancialInflectionComponentScorer,
    FinancialInflectionEvidence,
    FinancialInflectionSubfactorScore,
)
from inflector_core.market_structure_scoring import (
    MARKET_STRUCTURE_SUBFACTOR_ORDER,
    MarketStructureComponentScore,
    MarketStructureComponentScorer,
    MarketStructureSubfactorScore,
)
from inflector_core.score_audit import canonical_audit_value
from inflector_core.scoring_policy import (
    ConfidenceEvaluator,
    ConfidenceInputs,
    EligibilityEvaluator,
    EligibilityInputs,
    FinancialContextPolicyResolver,
    FinancialContextSelection,
    InflectionScoringPolicy,
)
from inflector_core.valuation_scoring import (
    VALUATION_SUBFACTOR_ORDER,
    ValuationComponentScore,
    ValuationComponentScorer,
    ValuationSubfactorScore,
)
from inflector_data.announcement_pit import PointInTimeAnnouncement, PointInTimeDocument
from inflector_data.business_event_features import BusinessEventFeatureBundle
from inflector_data.business_event_pit import (
    BusinessEventEvidenceView,
    PointInTimeBusinessEvent,
)
from inflector_data.business_event_quantitative_pit import (
    BusinessEventQuantitativeFactView,
    PointInTimeBusinessEventQuantitativeDerivation,
)
from inflector_data.corporate_action_pit import PointInTimeCorporateAction
from inflector_data.document_text import DocumentAssetView, DocumentTextExtractionView
from inflector_data.market_pit import PointInTimeBenchmarkBar, PointInTimeMarketBar
from inflector_data.market_structure_features import MarketStructureFeatureBundle
from inflector_data.pit import PointInTimeFinancialFact
from inflector_data.valuation_features import ValuationFeatureBundle
from inflector_database.score_repository import (
    PersistedScoreSnapshotResult,
    ScoreComponentWrite,
    ScoreExplanationWrite,
    ScoreSnapshotRepository,
    ScoreSnapshotWrite,
)
from inflector_database.scoring_repository import ScoringPolicyRepository

SCORE_SNAPSHOT_VERSION = "score_snapshot_v1"
SCORE_SNAPSHOT_FINANCIAL_V2_VERSION = "score_snapshot_v2"
SCORE_SNAPSHOT_CROSS_DOMAIN_V3_VERSION = "score_snapshot_v3"
SCORE_SNAPSHOT_BUSINESS_CATALYST_V4_VERSION = "score_snapshot_v4"
EXPLANATION_TEMPLATE_CODE = "financial_inflection_subfactor_v1"
IMPLEMENTED_FINANCIAL_COMPONENT_ORDER = (
    "financial_inflection",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
)
CROSS_DOMAIN_COMPONENT_ORDER = (
    "financial_inflection",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
    "valuation",
    "market_structure",
)
CROSS_DOMAIN_FINANCIAL_CONTEXT_ORDER = CROSS_DOMAIN_COMPONENT_ORDER[:-1]
BUSINESS_CATALYST_CROSS_DOMAIN_COMPONENT_ORDER = (
    "financial_inflection",
    "business_catalyst",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
    "valuation",
    "market_structure",
)
TOP_LEVEL_COMPONENT_ORDER = (
    "financial_inflection",
    "business_catalyst",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
    "valuation",
    "market_structure",
    "low_market_attention",
)

COMPONENT_TEMPLATE_CODES = {
    "financial_inflection": "financial_inflection_subfactor_v1",
    "business_quality": "business_quality_subfactor_v1",
    "cash_flow_quality": "cash_flow_quality_subfactor_v1",
    "balance_sheet": "balance_sheet_subfactor_v1",
    "valuation": "valuation_subfactor_v1",
    "market_structure": "market_structure_subfactor_v1",
}
COMPONENT_SUBFACTOR_ORDERS = {
    "financial_inflection": SUBFACTOR_ORDER,
    "business_quality": BUSINESS_QUALITY_SUBFACTOR_ORDER,
    "cash_flow_quality": CASH_FLOW_QUALITY_SUBFACTOR_ORDER,
    "balance_sheet": BALANCE_SHEET_SUBFACTOR_ORDER,
    "valuation": VALUATION_SUBFACTOR_ORDER,
    "market_structure": MARKET_STRUCTURE_SUBFACTOR_ORDER,
}


@dataclass(frozen=True, slots=True)
class FinancialComponentContextCandidate:
    provider_dataset_id: UUID
    filing_scope: str
    financial_inflection: FinancialInflectionEvidence | None = None
    business_quality: BusinessQualityEvidence | None = None
    cash_flow_quality: CashFlowQualityEvidence | None = None
    balance_sheet: BalanceSheetEvidence | None = None


@dataclass(frozen=True, slots=True)
class CrossDomainContextCandidate:
    provider_dataset_id: UUID
    filing_scope: str
    financial_inflection: FinancialInflectionEvidence | None = None
    business_quality: BusinessQualityEvidence | None = None
    cash_flow_quality: CashFlowQualityEvidence | None = None
    balance_sheet: BalanceSheetEvidence | None = None
    valuation: ValuationFeatureBundle | None = None


@dataclass(frozen=True, slots=True)
class BusinessCatalystContextCandidate:
    financial_provider_dataset_id: UUID
    filing_scope: str
    event_provider_dataset_id: UUID
    event_bundles: tuple[BusinessEventFeatureBundle, ...]


class NoActiveScoringConfigurationError(RuntimeError):
    """Raised when orchestration has no active persisted policy at the cutoff."""


class ScoreSnapshotOrchestrator:
    """Resolve policy and persist auditable, non-final versioned snapshots."""

    def __init__(
        self,
        policy_repository: ScoringPolicyRepository,
        snapshot_repository: ScoreSnapshotRepository,
        context_resolver: FinancialContextPolicyResolver,
        eligibility_evaluator: EligibilityEvaluator,
        confidence_evaluator: ConfidenceEvaluator,
        component_scorer: FinancialInflectionComponentScorer,
        business_quality_scorer: BusinessQualityComponentScorer | None = None,
        cash_flow_quality_scorer: CashFlowQualityComponentScorer | None = None,
        balance_sheet_scorer: BalanceSheetComponentScorer | None = None,
        valuation_scorer: ValuationComponentScorer | None = None,
        market_structure_scorer: MarketStructureComponentScorer | None = None,
        business_catalyst_scorer: BusinessCatalystComponentScorer | None = None,
    ) -> None:
        self._policies = policy_repository
        self._snapshots = snapshot_repository
        self._contexts = context_resolver
        self._eligibility = eligibility_evaluator
        self._confidence = confidence_evaluator
        self._component_scorer = component_scorer
        self._business_quality_scorer = business_quality_scorer or BusinessQualityComponentScorer()
        self._cash_flow_quality_scorer = (
            cash_flow_quality_scorer or CashFlowQualityComponentScorer()
        )
        self._balance_sheet_scorer = balance_sheet_scorer or BalanceSheetComponentScorer()
        self._valuation_scorer = valuation_scorer or ValuationComponentScorer()
        self._market_structure_scorer = market_structure_scorer or MarketStructureComponentScorer()
        self._business_catalyst_scorer = (
            business_catalyst_scorer or BusinessCatalystComponentScorer()
        )

    def orchestrate_and_persist(
        self,
        *,
        model_family: str,
        company_id: UUID,
        ending_fiscal_year: int,
        ending_fiscal_quarter: int,
        knowledge_cutoff: datetime,
        eligibility_inputs: EligibilityInputs,
        confidence_inputs: ConfidenceInputs,
        financial_inflection_candidates: tuple[FinancialInflectionEvidence, ...],
    ) -> PersistedScoreSnapshotResult:
        cutoff = self._aware_utc(knowledge_cutoff, "knowledge_cutoff")
        resolved = self._policies.resolve_active_configuration(
            model_family=model_family,
            at=cutoff,
        )
        if resolved is None:
            raise NoActiveScoringConfigurationError(
                f"no active scoring configuration for {model_family!r}"
            )
        if (
            self._aware_utc(confidence_inputs.knowledge_cutoff, "confidence knowledge_cutoff")
            != cutoff
        ):
            raise ValueError("confidence knowledge cutoff must match orchestration cutoff")

        policy = resolved.policy
        eligibility = self._eligibility.evaluate(eligibility_inputs, policy.eligibility)
        confidence = self._confidence.evaluate(confidence_inputs, policy.confidence)
        selected: FinancialContextSelection | None = None
        selected_component: FinancialInflectionComponentScore | None = None
        attempts: list[dict[str, object]] = []

        if eligibility.eligible:
            selected, selected_component, attempts = self._score_candidates(
                candidates=financial_inflection_candidates,
                policy=policy,
                company_id=company_id,
                fiscal_year=ending_fiscal_year,
                fiscal_quarter=ending_fiscal_quarter,
                cutoff=cutoff,
            )

        if not eligibility.eligible:
            status = "ineligible"
            context_resolution = {
                "attempts": [],
                "outcome": "ineligible_not_attempted",
                "selected": None,
            }
        elif selected_component is None or selected is None:
            status = "financial_inflection_unavailable"
            context_resolution = {
                "attempts": attempts,
                "outcome": "no_scoreable_context",
                "selected": None,
            }
        else:
            status = "partial_component_set"
            context_resolution = {
                "attempts": attempts,
                "outcome": "selected",
                "selected": self._selection_mapping(selected),
            }

        component_write, input_manifest = self._component_write(
            selected_component,
            policy.component_weights.financial_inflection,
        )
        components = (component_write,) if component_write is not None else ()
        available_codes = ["financial_inflection"] if component_write is not None else []
        missing_codes = [
            code
            for code in TOP_LEVEL_COMPONENT_ORDER
            if code not in available_codes and getattr(policy.component_weights, code) > 0
        ]
        top_level_coverage = (
            policy.component_weights.financial_inflection
            if component_write is not None
            else Decimal("0")
        )

        eligibility_inputs_json = self._canonical_mapping(eligibility_inputs)
        confidence_inputs_json = self._canonical_mapping(confidence_inputs)
        eligibility_result_json = self._canonical_mapping(eligibility)
        confidence_details_json = self._canonical_mapping(confidence)
        context_json = self._canonical_dict(context_resolution)
        input_manifest_json = self._canonical_dict(input_manifest)
        component_summary = component_write.detail_json if component_write is not None else None
        model = resolved.record.model_version
        fingerprint_payload = self._canonical_dict(
            {
                "algorithm_version": SCORE_SNAPSHOT_VERSION,
                "company_id": company_id,
                "model_version_id": model.id,
                "model_semantic_identity": {
                    "model_family": model.model_family,
                    "semantic_version": model.semantic_version,
                    "git_sha": model.git_sha,
                },
                "scoring_configuration_id": resolved.record.id,
                "configuration_checksum_sha256": resolved.record.checksum_sha256,
                "as_of_date": cutoff.date(),
                "knowledge_cutoff": cutoff,
                "ending_fiscal_year": ending_fiscal_year,
                "ending_fiscal_quarter": ending_fiscal_quarter,
                "eligibility_inputs": eligibility_inputs_json,
                "eligibility_result": eligibility_result_json,
                "confidence_inputs": confidence_inputs_json,
                "confidence_result": confidence_details_json,
                "selected_context": (
                    self._selection_mapping(selected) if selected is not None else None
                ),
                "context_resolution": context_json,
                "top_level_component_weight_coverage": top_level_coverage,
                "available_component_codes": available_codes,
                "missing_component_codes": missing_codes,
                "financial_inflection_component": component_summary,
                "input_manifest": input_manifest_json,
            }
        )
        return self._snapshots.persist_snapshot(
            ScoreSnapshotWrite(
                company_id=company_id,
                model_version_id=model.id,
                scoring_configuration_id=resolved.record.id,
                configuration_checksum_sha256=resolved.record.checksum_sha256,
                as_of_date=cutoff.date(),
                knowledge_cutoff=cutoff,
                ending_fiscal_year=ending_fiscal_year,
                ending_fiscal_quarter=ending_fiscal_quarter,
                selected_provider_dataset_id=(
                    selected.provider_dataset_id if selected is not None else None
                ),
                selected_filing_scope=selected.filing_scope if selected is not None else None,
                selected_security_id=None,
                snapshot_status=status,
                eligibility_eligible=eligibility.eligible,
                eligibility_inputs_json=eligibility_inputs_json,
                eligibility_reasons_json=list(eligibility.reasons),
                eligibility_warnings_json=list(eligibility.warnings),
                financial_core_coverage=eligibility.financial_core_coverage,
                confidence=confidence.confidence,
                confidence_inputs_json=confidence_inputs_json,
                confidence_details_json=confidence_details_json,
                top_level_component_weight_coverage=top_level_coverage,
                available_component_codes_json=available_codes,
                missing_component_codes_json=missing_codes,
                context_resolution_json=context_json,
                input_manifest_json=input_manifest_json,
                fingerprint_payload_json=fingerprint_payload,
                final_score=None,
                algorithm_version=SCORE_SNAPSHOT_VERSION,
                components=components,
            )
        )

    def orchestrate_financial_components_and_persist(
        self,
        *,
        model_family: str,
        company_id: UUID,
        ending_fiscal_year: int,
        ending_fiscal_quarter: int,
        knowledge_cutoff: datetime,
        eligibility_inputs: EligibilityInputs,
        confidence_inputs: ConfidenceInputs,
        financial_context_candidates: tuple[FinancialComponentContextCandidate, ...],
    ) -> PersistedScoreSnapshotResult:
        """Persist a v2 partial snapshot from one coherent financial context."""

        cutoff = self._aware_utc(knowledge_cutoff, "knowledge_cutoff")
        resolved = self._policies.resolve_active_configuration(
            model_family=model_family,
            at=cutoff,
        )
        if resolved is None:
            raise NoActiveScoringConfigurationError(
                f"no active scoring configuration for {model_family!r}"
            )
        if (
            self._aware_utc(confidence_inputs.knowledge_cutoff, "confidence knowledge_cutoff")
            != cutoff
        ):
            raise ValueError("confidence knowledge cutoff must match orchestration cutoff")

        policy = resolved.policy
        eligibility = self._eligibility.evaluate(eligibility_inputs, policy.eligibility)
        confidence = self._confidence.evaluate(confidence_inputs, policy.confidence)
        selected: FinancialContextSelection | None = None
        selected_components: dict[
            str,
            FinancialInflectionComponentScore
            | BusinessQualityComponentScore
            | CashFlowQualityComponentScore
            | BalanceSheetComponentScore,
        ] = {}
        attempts: list[dict[str, object]] = []

        if eligibility.eligible:
            selected, selected_components, attempts = self._score_financial_contexts(
                candidates=financial_context_candidates,
                policy=policy,
                company_id=company_id,
                fiscal_year=ending_fiscal_year,
                fiscal_quarter=ending_fiscal_quarter,
                cutoff=cutoff,
            )

        if not eligibility.eligible:
            status = "ineligible"
            context_resolution = {
                "attempts": [],
                "outcome": "ineligible_not_attempted",
                "selected": None,
            }
        elif selected is None:
            status = "financial_components_unavailable"
            context_resolution = {
                "attempts": attempts,
                "outcome": "no_scoreable_context",
                "selected": None,
            }
        else:
            status = "partial_component_set"
            context_resolution = {
                "attempts": attempts,
                "outcome": "selected",
                "selected": self._selection_mapping(selected),
            }

        component_writes: list[ScoreComponentWrite] = []
        component_manifests: list[dict[str, object]] = []
        for code in IMPLEMENTED_FINANCIAL_COMPONENT_ORDER:
            component = selected_components.get(code)
            if component is None:
                continue
            component_write, manifest = self._financial_component_write(
                code,
                component,
                getattr(policy.component_weights, code),
            )
            component_writes.append(component_write)
            component_manifests.append(manifest)

        available_codes = [
            code for code in TOP_LEVEL_COMPONENT_ORDER if code in selected_components
        ]
        missing_codes = [
            code
            for code in TOP_LEVEL_COMPONENT_ORDER
            if code not in available_codes and getattr(policy.component_weights, code) > 0
        ]
        top_level_coverage = sum(
            (getattr(policy.component_weights, code) for code in available_codes),
            Decimal("0"),
        )
        input_manifest = {"components": component_manifests}

        eligibility_inputs_json = self._canonical_mapping(eligibility_inputs)
        confidence_inputs_json = self._canonical_mapping(confidence_inputs)
        eligibility_result_json = self._canonical_mapping(eligibility)
        confidence_details_json = self._canonical_mapping(confidence)
        context_json = self._canonical_dict(context_resolution)
        input_manifest_json = self._canonical_dict(input_manifest)
        component_summaries = [component.detail_json for component in component_writes]
        model = resolved.record.model_version
        fingerprint_payload = self._canonical_dict(
            {
                "algorithm_version": SCORE_SNAPSHOT_FINANCIAL_V2_VERSION,
                "company_id": company_id,
                "model_version_id": model.id,
                "model_semantic_identity": {
                    "model_family": model.model_family,
                    "semantic_version": model.semantic_version,
                    "git_sha": model.git_sha,
                },
                "scoring_configuration_id": resolved.record.id,
                "configuration_checksum_sha256": resolved.record.checksum_sha256,
                "as_of_date": cutoff.date(),
                "knowledge_cutoff": cutoff,
                "ending_fiscal_year": ending_fiscal_year,
                "ending_fiscal_quarter": ending_fiscal_quarter,
                "eligibility_inputs": eligibility_inputs_json,
                "eligibility_result": eligibility_result_json,
                "confidence_inputs": confidence_inputs_json,
                "confidence_result": confidence_details_json,
                "selected_context": (
                    self._selection_mapping(selected) if selected is not None else None
                ),
                "context_resolution": context_json,
                "top_level_component_weight_coverage": top_level_coverage,
                "available_component_codes": available_codes,
                "missing_component_codes": missing_codes,
                "financial_components": component_summaries,
                "input_manifest": input_manifest_json,
            }
        )
        return self._snapshots.persist_snapshot(
            ScoreSnapshotWrite(
                company_id=company_id,
                model_version_id=model.id,
                scoring_configuration_id=resolved.record.id,
                configuration_checksum_sha256=resolved.record.checksum_sha256,
                as_of_date=cutoff.date(),
                knowledge_cutoff=cutoff,
                ending_fiscal_year=ending_fiscal_year,
                ending_fiscal_quarter=ending_fiscal_quarter,
                selected_provider_dataset_id=(
                    selected.provider_dataset_id if selected is not None else None
                ),
                selected_filing_scope=selected.filing_scope if selected is not None else None,
                selected_security_id=None,
                snapshot_status=status,
                eligibility_eligible=eligibility.eligible,
                eligibility_inputs_json=eligibility_inputs_json,
                eligibility_reasons_json=list(eligibility.reasons),
                eligibility_warnings_json=list(eligibility.warnings),
                financial_core_coverage=eligibility.financial_core_coverage,
                confidence=confidence.confidence,
                confidence_inputs_json=confidence_inputs_json,
                confidence_details_json=confidence_details_json,
                top_level_component_weight_coverage=top_level_coverage,
                available_component_codes_json=available_codes,
                missing_component_codes_json=missing_codes,
                context_resolution_json=context_json,
                input_manifest_json=input_manifest_json,
                fingerprint_payload_json=fingerprint_payload,
                final_score=None,
                algorithm_version=SCORE_SNAPSHOT_FINANCIAL_V2_VERSION,
                components=tuple(component_writes),
            )
        )

    def orchestrate_cross_domain_components_and_persist(
        self,
        *,
        model_family: str,
        company_id: UUID,
        security_id: UUID,
        ending_fiscal_year: int,
        ending_fiscal_quarter: int,
        knowledge_cutoff: datetime,
        eligibility_inputs: EligibilityInputs,
        confidence_inputs: ConfidenceInputs,
        cross_domain_context_candidates: tuple[CrossDomainContextCandidate, ...],
        market_structure_evidence: MarketStructureFeatureBundle | None,
    ) -> PersistedScoreSnapshotResult:
        """Persist a v3 partial snapshot across one financial and one market context."""

        cutoff = self._aware_utc(knowledge_cutoff, "knowledge_cutoff")
        self._snapshots.validate_security_company(security_id, company_id)
        resolved = self._policies.resolve_active_configuration(
            model_family=model_family,
            at=cutoff,
        )
        if resolved is None:
            raise NoActiveScoringConfigurationError(
                f"no active scoring configuration for {model_family!r}"
            )
        if (
            self._aware_utc(confidence_inputs.knowledge_cutoff, "confidence knowledge_cutoff")
            != cutoff
        ):
            raise ValueError("confidence knowledge cutoff must match orchestration cutoff")
        if market_structure_evidence is not None:
            self._validate_market_structure_identity(
                market_structure_evidence,
                security_id=security_id,
                cutoff=cutoff,
            )

        policy = resolved.policy
        eligibility = self._eligibility.evaluate(eligibility_inputs, policy.eligibility)
        confidence = self._confidence.evaluate(confidence_inputs, policy.confidence)
        selected: FinancialContextSelection | None = None
        selected_components: dict[
            str,
            FinancialInflectionComponentScore
            | BusinessQualityComponentScore
            | CashFlowQualityComponentScore
            | BalanceSheetComponentScore
            | ValuationComponentScore
            | MarketStructureComponentScore,
        ] = {}
        attempts: list[dict[str, object]] = []

        if eligibility.eligible:
            selected, selected_components, attempts = self._score_cross_domain_contexts(
                candidates=cross_domain_context_candidates,
                policy=policy,
                company_id=company_id,
                security_id=security_id,
                fiscal_year=ending_fiscal_year,
                fiscal_quarter=ending_fiscal_quarter,
                cutoff=cutoff,
            )

        market_result, market_attempt = self._attempt_market_structure(
            evidence=market_structure_evidence,
            policy=policy,
            selected_context_exists=eligibility.eligible and selected is not None,
        )
        if market_result is not None:
            selected_components["market_structure"] = market_result

        selected_candidate = (
            self._cross_domain_candidate_for_selection(
                cross_domain_context_candidates,
                selected,
            )
            if selected is not None
            else None
        )
        valuation_result = selected_components.get("valuation")
        if isinstance(valuation_result, ValuationComponentScore) and market_result is not None:
            assert selected_candidate is not None and selected_candidate.valuation is not None
            assert market_structure_evidence is not None
            self._validate_cross_market_coherence(
                selected_candidate.valuation,
                market_structure_evidence,
                security_id=security_id,
                cutoff=cutoff,
            )

        if not eligibility.eligible:
            status = "ineligible"
            financial_outcome = "ineligible_not_attempted"
        elif selected is None:
            status = "implemented_components_unavailable"
            financial_outcome = "no_scoreable_context"
        else:
            status = "partial_component_set"
            financial_outcome = "selected"

        valuation_evidence = (
            selected_candidate.valuation
            if isinstance(valuation_result, ValuationComponentScore)
            and selected_candidate is not None
            else None
        )
        market_context = self._market_context_mapping(
            security_id,
            market_structure_evidence,
            valuation_evidence,
        )
        context_resolution = {
            "financial_context": {
                "attempts": attempts,
                "outcome": financial_outcome,
                "selected": (self._selection_mapping(selected) if selected is not None else None),
            },
            "market_context": market_context,
            "market_structure_attempt": market_attempt,
        }

        component_writes: list[ScoreComponentWrite] = []
        component_manifests: list[dict[str, object]] = []
        for code in CROSS_DOMAIN_COMPONENT_ORDER:
            component = selected_components.get(code)
            if component is None:
                continue
            if code in IMPLEMENTED_FINANCIAL_COMPONENT_ORDER:
                assert isinstance(
                    component,
                    (
                        FinancialInflectionComponentScore,
                        BusinessQualityComponentScore,
                        CashFlowQualityComponentScore,
                        BalanceSheetComponentScore,
                    ),
                )
                component_write, manifest = self._v3_financial_component_write(
                    code,
                    component,
                    getattr(policy.component_weights, code),
                )
            else:
                assert isinstance(
                    component, (ValuationComponentScore, MarketStructureComponentScore)
                )
                component_write, manifest = self._cross_domain_component_write(
                    code,
                    component,
                    getattr(policy.component_weights, code),
                    security_id=security_id,
                )
            component_writes.append(component_write)
            component_manifests.append(manifest)

        available_codes = [
            code for code in CROSS_DOMAIN_COMPONENT_ORDER if code in selected_components
        ]
        missing_codes = [
            code
            for code in TOP_LEVEL_COMPONENT_ORDER
            if code not in available_codes and getattr(policy.component_weights, code) > 0
        ]
        top_level_coverage = sum(
            (getattr(policy.component_weights, code) for code in available_codes),
            Decimal("0"),
        )
        input_manifest = {
            "components": component_manifests,
            "union": self._cross_domain_snapshot_union(tuple(component_manifests)),
        }

        eligibility_inputs_json = self._canonical_mapping(eligibility_inputs)
        confidence_inputs_json = self._canonical_mapping(confidence_inputs)
        eligibility_result_json = self._canonical_mapping(eligibility)
        confidence_details_json = self._canonical_mapping(confidence)
        context_json = self._canonical_dict(context_resolution)
        input_manifest_json = self._canonical_dict(input_manifest)
        component_summaries = [component.detail_json for component in component_writes]
        model = resolved.record.model_version
        fingerprint_payload = self._canonical_dict(
            {
                "algorithm_version": SCORE_SNAPSHOT_CROSS_DOMAIN_V3_VERSION,
                "company_id": company_id,
                "security_id": security_id,
                "model_version_id": model.id,
                "model_semantic_identity": {
                    "model_family": model.model_family,
                    "semantic_version": model.semantic_version,
                    "git_sha": model.git_sha,
                },
                "scoring_configuration_id": resolved.record.id,
                "configuration_checksum_sha256": resolved.record.checksum_sha256,
                "as_of_date": cutoff.date(),
                "knowledge_cutoff": cutoff,
                "ending_fiscal_year": ending_fiscal_year,
                "ending_fiscal_quarter": ending_fiscal_quarter,
                "eligibility_inputs": eligibility_inputs_json,
                "eligibility_result": eligibility_result_json,
                "confidence_inputs": confidence_inputs_json,
                "confidence_result": confidence_details_json,
                "selected_financial_context": (
                    self._selection_mapping(selected) if selected is not None else None
                ),
                "market_context": market_context,
                "context_resolution": context_json,
                "top_level_component_weight_coverage": top_level_coverage,
                "available_component_codes": available_codes,
                "missing_component_codes": missing_codes,
                "components": component_summaries,
                "input_manifest": input_manifest_json,
            }
        )
        return self._snapshots.persist_snapshot(
            ScoreSnapshotWrite(
                company_id=company_id,
                model_version_id=model.id,
                scoring_configuration_id=resolved.record.id,
                configuration_checksum_sha256=resolved.record.checksum_sha256,
                as_of_date=cutoff.date(),
                knowledge_cutoff=cutoff,
                ending_fiscal_year=ending_fiscal_year,
                ending_fiscal_quarter=ending_fiscal_quarter,
                selected_provider_dataset_id=(
                    selected.provider_dataset_id if selected is not None else None
                ),
                selected_filing_scope=selected.filing_scope if selected is not None else None,
                selected_security_id=security_id,
                snapshot_status=status,
                eligibility_eligible=eligibility.eligible,
                eligibility_inputs_json=eligibility_inputs_json,
                eligibility_reasons_json=list(eligibility.reasons),
                eligibility_warnings_json=list(eligibility.warnings),
                financial_core_coverage=eligibility.financial_core_coverage,
                confidence=confidence.confidence,
                confidence_inputs_json=confidence_inputs_json,
                confidence_details_json=confidence_details_json,
                top_level_component_weight_coverage=top_level_coverage,
                available_component_codes_json=available_codes,
                missing_component_codes_json=missing_codes,
                context_resolution_json=context_json,
                input_manifest_json=input_manifest_json,
                fingerprint_payload_json=fingerprint_payload,
                final_score=None,
                algorithm_version=SCORE_SNAPSHOT_CROSS_DOMAIN_V3_VERSION,
                components=tuple(component_writes),
            )
        )

    def orchestrate_business_catalyst_cross_domain_and_persist(
        self,
        *,
        model_family: str,
        company_id: UUID,
        security_id: UUID,
        ending_fiscal_year: int,
        ending_fiscal_quarter: int,
        knowledge_cutoff: datetime,
        eligibility_inputs: EligibilityInputs,
        confidence_inputs: ConfidenceInputs,
        cross_domain_context_candidates: tuple[CrossDomainContextCandidate, ...],
        business_catalyst_context_candidates: tuple[
            BusinessCatalystContextCandidate, ...
        ],
        market_structure_evidence: MarketStructureFeatureBundle | None,
    ) -> PersistedScoreSnapshotResult:
        """Persist a v4 partial snapshot without changing V3 context selection."""

        cutoff = self._aware_utc(knowledge_cutoff, "knowledge_cutoff")
        self._snapshots.validate_security_company(security_id, company_id)
        resolved = self._policies.resolve_active_configuration(
            model_family=model_family,
            at=cutoff,
        )
        if resolved is None:
            raise NoActiveScoringConfigurationError(
                f"no active scoring configuration for {model_family!r}"
            )
        if (
            self._aware_utc(confidence_inputs.knowledge_cutoff, "confidence knowledge_cutoff")
            != cutoff
        ):
            raise ValueError("confidence knowledge cutoff must match orchestration cutoff")
        if market_structure_evidence is not None:
            self._validate_market_structure_identity(
                market_structure_evidence,
                security_id=security_id,
                cutoff=cutoff,
            )
        policy = resolved.policy
        business_candidates = self._business_catalyst_candidates_by_context(
            business_catalyst_context_candidates,
            company_id=company_id,
            security_id=security_id,
            cutoff=cutoff,
            policy=policy,
        )

        eligibility = self._eligibility.evaluate(eligibility_inputs, policy.eligibility)
        confidence = self._confidence.evaluate(confidence_inputs, policy.confidence)
        selected: FinancialContextSelection | None = None
        selected_components: dict[
            str,
            FinancialInflectionComponentScore
            | BusinessCatalystComponentScore
            | BusinessQualityComponentScore
            | CashFlowQualityComponentScore
            | BalanceSheetComponentScore
            | ValuationComponentScore
            | MarketStructureComponentScore,
        ] = {}
        attempts: list[dict[str, object]] = []

        if eligibility.eligible:
            selected, v3_components, attempts = self._score_cross_domain_contexts(
                candidates=cross_domain_context_candidates,
                policy=policy,
                company_id=company_id,
                security_id=security_id,
                fiscal_year=ending_fiscal_year,
                fiscal_quarter=ending_fiscal_quarter,
                cutoff=cutoff,
            )
            selected_components.update(v3_components)

        selected_context_exists = eligibility.eligible and selected is not None
        market_result, market_attempt = self._attempt_market_structure(
            evidence=market_structure_evidence,
            policy=policy,
            selected_context_exists=selected_context_exists,
        )
        if market_result is not None:
            selected_components["market_structure"] = market_result

        selected_candidate = (
            self._cross_domain_candidate_for_selection(
                cross_domain_context_candidates,
                selected,
            )
            if selected is not None
            else None
        )
        valuation_result = selected_components.get("valuation")
        if isinstance(valuation_result, ValuationComponentScore) and market_result is not None:
            assert selected_candidate is not None and selected_candidate.valuation is not None
            assert market_structure_evidence is not None
            self._validate_cross_market_coherence(
                selected_candidate.valuation,
                market_structure_evidence,
                security_id=security_id,
                cutoff=cutoff,
            )

        selected_business_candidate = (
            business_candidates.get((selected.provider_dataset_id, selected.filing_scope))
            if selected is not None
            else None
        )
        business_result, business_attempt = self._attempt_business_catalyst(
            candidate=selected_business_candidate,
            policy=policy,
            selected_context_exists=selected_context_exists,
        )
        if business_result is not None:
            selected_components["business_catalyst"] = business_result

        if not eligibility.eligible:
            status = "ineligible"
            financial_outcome = "ineligible_not_attempted"
        elif selected is None:
            status = "implemented_components_unavailable"
            financial_outcome = "no_scoreable_context"
        else:
            status = "partial_component_set"
            financial_outcome = "selected"

        valuation_evidence = (
            selected_candidate.valuation
            if isinstance(valuation_result, ValuationComponentScore)
            and selected_candidate is not None
            else None
        )
        market_context = self._market_context_mapping(
            security_id,
            market_structure_evidence,
            valuation_evidence,
        )
        business_context = self._business_catalyst_context_mapping(
            tuple(business_candidates.values()),
            selected,
            selected_business_candidate,
        )
        context_resolution = {
            "financial_context": {
                "attempts": attempts,
                "outcome": financial_outcome,
                "selected": (self._selection_mapping(selected) if selected is not None else None),
            },
            "market_context": market_context,
            "market_structure_attempt": market_attempt,
            "business_catalyst_context": business_context,
            "business_catalyst_attempt": business_attempt,
        }

        component_writes: list[ScoreComponentWrite] = []
        component_manifests: list[dict[str, object]] = []
        for code in BUSINESS_CATALYST_CROSS_DOMAIN_COMPONENT_ORDER:
            component = selected_components.get(code)
            if component is None:
                continue
            if code in IMPLEMENTED_FINANCIAL_COMPONENT_ORDER:
                assert isinstance(
                    component,
                    (
                        FinancialInflectionComponentScore,
                        BusinessQualityComponentScore,
                        CashFlowQualityComponentScore,
                        BalanceSheetComponentScore,
                    ),
                )
                component_write, manifest = self._v3_financial_component_write(
                    code,
                    component,
                    getattr(policy.component_weights, code),
                )
            elif code == "business_catalyst":
                assert isinstance(component, BusinessCatalystComponentScore)
                component_write, manifest = self._business_catalyst_component_write(
                    component,
                    policy.component_weights.business_catalyst,
                )
            else:
                assert isinstance(
                    component, (ValuationComponentScore, MarketStructureComponentScore)
                )
                component_write, manifest = self._cross_domain_component_write(
                    code,
                    component,
                    getattr(policy.component_weights, code),
                    security_id=security_id,
                )
            component_writes.append(component_write)
            component_manifests.append(manifest)

        available_codes = [
            code
            for code in BUSINESS_CATALYST_CROSS_DOMAIN_COMPONENT_ORDER
            if code in selected_components
        ]
        missing_codes = [
            code
            for code in TOP_LEVEL_COMPONENT_ORDER
            if code not in available_codes and getattr(policy.component_weights, code) > 0
        ]
        top_level_coverage = sum(
            (getattr(policy.component_weights, code) for code in available_codes),
            Decimal("0"),
        )
        input_manifest = {
            "components": component_manifests,
            "union": self._v4_snapshot_union(tuple(component_manifests)),
        }

        eligibility_inputs_json = self._canonical_mapping(eligibility_inputs)
        confidence_inputs_json = self._canonical_mapping(confidence_inputs)
        eligibility_result_json = self._canonical_mapping(eligibility)
        confidence_details_json = self._canonical_mapping(confidence)
        context_json = self._canonical_dict(context_resolution)
        input_manifest_json = self._canonical_dict(input_manifest)
        component_summaries = [component.detail_json for component in component_writes]
        model = resolved.record.model_version
        fingerprint_payload = self._canonical_dict(
            {
                "algorithm_version": SCORE_SNAPSHOT_BUSINESS_CATALYST_V4_VERSION,
                "company_id": company_id,
                "security_id": security_id,
                "model_version_id": model.id,
                "model_semantic_identity": {
                    "model_family": model.model_family,
                    "semantic_version": model.semantic_version,
                    "git_sha": model.git_sha,
                },
                "scoring_configuration_id": resolved.record.id,
                "configuration_checksum_sha256": resolved.record.checksum_sha256,
                "as_of_date": cutoff.date(),
                "knowledge_cutoff": cutoff,
                "ending_fiscal_year": ending_fiscal_year,
                "ending_fiscal_quarter": ending_fiscal_quarter,
                "eligibility_inputs": eligibility_inputs_json,
                "eligibility_result": eligibility_result_json,
                "confidence_inputs": confidence_inputs_json,
                "confidence_result": confidence_details_json,
                "selected_financial_context": (
                    self._selection_mapping(selected) if selected is not None else None
                ),
                "market_context": market_context,
                "business_catalyst_context": business_context,
                "business_catalyst_attempt": business_attempt,
                "context_resolution": context_json,
                "top_level_component_weight_coverage": top_level_coverage,
                "available_component_codes": available_codes,
                "missing_component_codes": missing_codes,
                "components": component_summaries,
                "input_manifest": input_manifest_json,
            }
        )
        return self._snapshots.persist_snapshot(
            ScoreSnapshotWrite(
                company_id=company_id,
                model_version_id=model.id,
                scoring_configuration_id=resolved.record.id,
                configuration_checksum_sha256=resolved.record.checksum_sha256,
                as_of_date=cutoff.date(),
                knowledge_cutoff=cutoff,
                ending_fiscal_year=ending_fiscal_year,
                ending_fiscal_quarter=ending_fiscal_quarter,
                selected_provider_dataset_id=(
                    selected.provider_dataset_id if selected is not None else None
                ),
                selected_filing_scope=selected.filing_scope if selected is not None else None,
                selected_security_id=security_id,
                snapshot_status=status,
                eligibility_eligible=eligibility.eligible,
                eligibility_inputs_json=eligibility_inputs_json,
                eligibility_reasons_json=list(eligibility.reasons),
                eligibility_warnings_json=list(eligibility.warnings),
                financial_core_coverage=eligibility.financial_core_coverage,
                confidence=confidence.confidence,
                confidence_inputs_json=confidence_inputs_json,
                confidence_details_json=confidence_details_json,
                top_level_component_weight_coverage=top_level_coverage,
                available_component_codes_json=available_codes,
                missing_component_codes_json=missing_codes,
                context_resolution_json=context_json,
                input_manifest_json=input_manifest_json,
                fingerprint_payload_json=fingerprint_payload,
                final_score=None,
                algorithm_version=SCORE_SNAPSHOT_BUSINESS_CATALYST_V4_VERSION,
                components=tuple(component_writes),
            )
        )

    def _score_financial_contexts(
        self,
        *,
        candidates: tuple[FinancialComponentContextCandidate, ...],
        policy: InflectionScoringPolicy,
        company_id: UUID,
        fiscal_year: int,
        fiscal_quarter: int,
        cutoff: datetime,
    ) -> tuple[
        FinancialContextSelection | None,
        dict[
            str,
            FinancialInflectionComponentScore
            | BusinessQualityComponentScore
            | CashFlowQualityComponentScore
            | BalanceSheetComponentScore,
        ],
        list[dict[str, object]],
    ]:
        by_context: dict[tuple[UUID, str], FinancialComponentContextCandidate] = {}
        for candidate in candidates:
            if candidate.filing_scope not in {"standalone", "consolidated"}:
                raise ValueError("candidate filing_scope must be standalone or consolidated")
            key = (candidate.provider_dataset_id, candidate.filing_scope)
            if key in by_context:
                raise ValueError("duplicate financial-component candidate context")
            self._validate_financial_candidate(
                candidate,
                company_id=company_id,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                cutoff=cutoff,
            )
            by_context[key] = candidate

        configured_keys = [
            (provider_id, scope)
            for provider_id in policy.financial_context.provider_dataset_priority
            for scope in policy.financial_context.filing_scope_priority
            if (provider_id, scope) in by_context
        ]
        results_by_context: dict[
            tuple[UUID, str],
            dict[
                str,
                FinancialInflectionComponentScore
                | BusinessQualityComponentScore
                | CashFlowQualityComponentScore
                | BalanceSheetComponentScore,
            ],
        ] = {}
        attempts: list[dict[str, object]] = []
        for key in configured_keys:
            selection = self._contexts.resolve(policy.financial_context, (key,))
            assert selection is not None
            candidate = by_context[key]
            results: dict[
                str,
                FinancialInflectionComponentScore
                | BusinessQualityComponentScore
                | CashFlowQualityComponentScore
                | BalanceSheetComponentScore,
            ] = {}
            component_attempts: list[dict[str, object]] = []
            for code in IMPLEMENTED_FINANCIAL_COMPONENT_ORDER:
                result, attempt = self._attempt_financial_component(
                    code=code,
                    candidate=candidate,
                    selection=selection,
                    policy=policy,
                )
                component_attempts.append(attempt)
                if result is not None:
                    results[code] = result
            if results:
                results_by_context[key] = results
            attempts.append(
                {
                    "provider_dataset_id": key[0],
                    "filing_scope": key[1],
                    "components": component_attempts,
                    "context_scoreable": bool(results),
                    "available_top_level_weight": sum(
                        (getattr(policy.component_weights, code) for code in results),
                        Decimal("0"),
                    ),
                }
            )

        selected = self._contexts.resolve(policy.financial_context, results_by_context.keys())
        selected_results = (
            results_by_context[(selected.provider_dataset_id, selected.filing_scope)]
            if selected is not None
            else {}
        )
        return selected, selected_results, attempts

    def _score_cross_domain_contexts(
        self,
        *,
        candidates: tuple[CrossDomainContextCandidate, ...],
        policy: InflectionScoringPolicy,
        company_id: UUID,
        security_id: UUID,
        fiscal_year: int,
        fiscal_quarter: int,
        cutoff: datetime,
    ) -> tuple[
        FinancialContextSelection | None,
        dict[
            str,
            FinancialInflectionComponentScore
            | BusinessQualityComponentScore
            | CashFlowQualityComponentScore
            | BalanceSheetComponentScore
            | ValuationComponentScore
            | MarketStructureComponentScore,
        ],
        list[dict[str, object]],
    ]:
        by_context: dict[tuple[UUID, str], CrossDomainContextCandidate] = {}
        for candidate in candidates:
            if candidate.filing_scope not in {"standalone", "consolidated"}:
                raise ValueError("candidate filing_scope must be standalone or consolidated")
            key = (candidate.provider_dataset_id, candidate.filing_scope)
            if key in by_context:
                raise ValueError("duplicate cross-domain candidate context")
            self._validate_cross_domain_candidate(
                candidate,
                company_id=company_id,
                security_id=security_id,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                cutoff=cutoff,
            )
            by_context[key] = candidate

        configured_keys = [
            (provider_id, scope)
            for provider_id in policy.financial_context.provider_dataset_priority
            for scope in policy.financial_context.filing_scope_priority
            if (provider_id, scope) in by_context
        ]
        results_by_context: dict[
            tuple[UUID, str],
            dict[
                str,
                FinancialInflectionComponentScore
                | BusinessQualityComponentScore
                | CashFlowQualityComponentScore
                | BalanceSheetComponentScore
                | ValuationComponentScore
                | MarketStructureComponentScore,
            ],
        ] = {}
        attempts: list[dict[str, object]] = []
        for key in configured_keys:
            selection = self._contexts.resolve(policy.financial_context, (key,))
            assert selection is not None
            candidate = by_context[key]
            financial_candidate = FinancialComponentContextCandidate(
                provider_dataset_id=candidate.provider_dataset_id,
                filing_scope=candidate.filing_scope,
                financial_inflection=candidate.financial_inflection,
                business_quality=candidate.business_quality,
                cash_flow_quality=candidate.cash_flow_quality,
                balance_sheet=candidate.balance_sheet,
            )
            results: dict[
                str,
                FinancialInflectionComponentScore
                | BusinessQualityComponentScore
                | CashFlowQualityComponentScore
                | BalanceSheetComponentScore
                | ValuationComponentScore
                | MarketStructureComponentScore,
            ] = {}
            component_attempts: list[dict[str, object]] = []
            for code in CROSS_DOMAIN_FINANCIAL_CONTEXT_ORDER:
                if code == "valuation":
                    result, attempt = self._attempt_valuation_component(
                        candidate=candidate,
                        policy=policy,
                    )
                else:
                    result, attempt = self._attempt_financial_component(
                        code=code,
                        candidate=financial_candidate,
                        selection=selection,
                        policy=policy,
                    )
                attempt["available_top_level_weight"] = (
                    getattr(policy.component_weights, code) if result is not None else Decimal("0")
                )
                component_attempts.append(attempt)
                if result is not None:
                    results[code] = result
            if results:
                results_by_context[key] = results
            attempts.append(
                {
                    "provider_dataset_id": key[0],
                    "filing_scope": key[1],
                    "components": component_attempts,
                    "context_scoreable": bool(results),
                    "available_top_level_weight": sum(
                        (getattr(policy.component_weights, code) for code in results),
                        Decimal("0"),
                    ),
                }
            )

        selected = self._contexts.resolve(policy.financial_context, results_by_context.keys())
        selected_results = (
            results_by_context[(selected.provider_dataset_id, selected.filing_scope)]
            if selected is not None
            else {}
        )
        return selected, selected_results, attempts

    def _business_catalyst_candidates_by_context(
        self,
        candidates: tuple[BusinessCatalystContextCandidate, ...],
        *,
        company_id: UUID,
        security_id: UUID,
        cutoff: datetime,
        policy: InflectionScoringPolicy,
    ) -> dict[tuple[UUID, str], BusinessCatalystContextCandidate]:
        by_context: dict[tuple[UUID, str], BusinessCatalystContextCandidate] = {}
        event_provider_dataset_id: UUID | None = None
        expected_event_set: tuple[tuple[object, ...], ...] | None = None
        event_rank = {code: rank for rank, code in enumerate(BUSINESS_EVENT_TYPE_ORDER)}
        for candidate in candidates:
            if candidate.filing_scope not in {"standalone", "consolidated"}:
                raise ValueError(
                    "business catalyst candidate filing_scope must be standalone or consolidated"
                )
            key = (candidate.financial_provider_dataset_id, candidate.filing_scope)
            if key in by_context:
                raise ValueError("duplicate business catalyst candidate context")
            if not candidate.event_bundles:
                raise ValueError("business catalyst candidate event_bundles must not be empty")
            if event_provider_dataset_id is None:
                event_provider_dataset_id = candidate.event_provider_dataset_id
            elif candidate.event_provider_dataset_id != event_provider_dataset_id:
                raise ValueError("business catalyst candidates use different event providers")

            identities: list[tuple[object, ...]] = []
            seen_event_ids: set[UUID] = set()
            for bundle in candidate.event_bundles:
                if bundle.business_event_id in seen_event_ids:
                    raise ValueError("duplicate business event in catalyst candidate")
                seen_event_ids.add(bundle.business_event_id)
                if bundle.financial_provider_dataset_id != candidate.financial_provider_dataset_id:
                    raise ValueError(
                        "business catalyst financial provider does not match candidate"
                    )
                if bundle.filing_scope != candidate.filing_scope:
                    raise ValueError("business catalyst filing scope does not match candidate")
                if bundle.provider_dataset_id != candidate.event_provider_dataset_id:
                    raise ValueError("business catalyst event provider does not match candidate")
                if bundle.company_id != company_id:
                    raise ValueError(
                        "business catalyst company does not match orchestration company"
                    )
                if self._aware_utc(bundle.as_of, "business catalyst bundle as_of") != cutoff:
                    raise ValueError("business catalyst cutoff does not match orchestration cutoff")
                if bundle.security_id not in {None, security_id}:
                    raise ValueError(
                        "business catalyst security does not match orchestration security"
                    )
                identities.append(self._business_event_identity(bundle))
                derivation = bundle.quantitative_derivation
                if derivation is not None:
                    if (
                        derivation.event.id != bundle.business_event_id
                        or derivation.ruleset_code != bundle.quantitative_ruleset_code
                        or derivation.ruleset_semantic_version
                        != bundle.quantitative_ruleset_semantic_version
                        or self._aware_utc(
                            derivation.source_available_at,
                            "quantitative derivation source_available_at",
                        )
                        != self._aware_utc(
                            bundle.source_available_at,
                            "business catalyst source_available_at",
                        )
                    ):
                        raise ValueError(
                            "business catalyst quantitative derivation does not match bundle"
                        )
                elif bundle.quantitative_ruleset_code is not None:
                    raise ValueError(
                        "business catalyst bundle omits quantitative derivation lineage"
                    )

            canonical_event_set = tuple(sorted(identities, key=lambda item: tuple(map(str, item))))
            if expected_event_set is None:
                expected_event_set = canonical_event_set
            elif canonical_event_set != expected_event_set:
                raise ValueError("business catalyst candidates use different event identity sets")
            unknown_event_types = {
                bundle.event_type
                for bundle in candidate.event_bundles
                if bundle.event_type not in event_rank
            }
            if unknown_event_types:
                raise ValueError("business catalyst candidate contains unsupported event type")
            canonical_bundles = tuple(
                sorted(
                    candidate.event_bundles,
                    key=lambda bundle: (
                        event_rank[bundle.event_type],
                        self._aware_utc(
                            bundle.source_available_at,
                            "business catalyst source_available_at",
                        ),
                        str(bundle.business_event_id),
                    ),
                )
            )
            if policy.business_catalyst is not None:
                self._business_catalyst_scorer.validate_evidence(
                    evidence=canonical_bundles,
                    policy=policy,
                )
            by_context[key] = replace(candidate, event_bundles=canonical_bundles)
        return by_context

    def _attempt_business_catalyst(
        self,
        *,
        candidate: BusinessCatalystContextCandidate | None,
        policy: InflectionScoringPolicy,
        selected_context_exists: bool,
    ) -> tuple[BusinessCatalystComponentScore | None, dict[str, object]]:
        policy_configured = policy.business_catalyst is not None
        top_level_weight = policy.component_weights.business_catalyst
        result: BusinessCatalystComponentScore | None = None
        if not selected_context_exists:
            warnings = ("financial_context_not_selected",)
        elif top_level_weight == 0:
            warnings = ()
        elif not policy_configured:
            warnings = ("business_catalyst_scoring_not_configured",)
        elif candidate is None:
            warnings = ("business_catalyst_evidence_missing",)
        else:
            result = self._business_catalyst_scorer.score(
                evidence=candidate.event_bundles,
                policy=policy,
            )
            warnings = result.warnings
        scoreable = result is not None and result.score is not None and top_level_weight > 0
        return (
            result if scoreable else None,
            {
                "policy_configured": policy_configured,
                "top_level_weight": top_level_weight,
                "selected_financial_context_exists": selected_context_exists,
                "evidence_supplied": candidate is not None,
                "event_provider_dataset_id": (
                    candidate.event_provider_dataset_id if candidate is not None else None
                ),
                "event_count": len(candidate.event_bundles) if candidate is not None else 0,
                "scoreable": scoreable,
                "warnings": warnings,
            },
        )

    def _business_catalyst_context_mapping(
        self,
        candidates: tuple[BusinessCatalystContextCandidate, ...],
        selected: FinancialContextSelection | None,
        selected_candidate: BusinessCatalystContextCandidate | None,
    ) -> dict[str, object]:
        candidate_mappings = []
        for candidate in sorted(
            candidates,
            key=lambda item: (str(item.financial_provider_dataset_id), item.filing_scope),
        ):
            candidate_mappings.append(
                {
                    "financial_provider_dataset_id": candidate.financial_provider_dataset_id,
                    "filing_scope": candidate.filing_scope,
                    "event_provider_dataset_id": candidate.event_provider_dataset_id,
                    "event_identities": [
                        {
                            "business_event_id": identity[0],
                            "announcement_id": identity[1],
                            "event_type": identity[2],
                            "provider_dataset_id": identity[3],
                            "source_available_at": identity[4],
                        }
                        for identity in sorted(
                            (
                                self._business_event_identity(bundle)
                                for bundle in candidate.event_bundles
                            ),
                            key=lambda item: tuple(map(str, item)),
                        )
                    ],
                }
            )
        return {
            "candidates": candidate_mappings,
            "selected_financial_context": (
                self._selection_mapping(selected) if selected is not None else None
            ),
            "selected": (
                {
                    "financial_provider_dataset_id": (
                        selected_candidate.financial_provider_dataset_id
                    ),
                    "filing_scope": selected_candidate.filing_scope,
                    "event_provider_dataset_id": selected_candidate.event_provider_dataset_id,
                    "event_count": len(selected_candidate.event_bundles),
                }
                if selected_candidate is not None
                else None
            ),
        }

    def _business_event_identity(
        self,
        bundle: BusinessEventFeatureBundle,
    ) -> tuple[object, ...]:
        return (
            bundle.business_event_id,
            bundle.announcement_id,
            bundle.event_type,
            bundle.provider_dataset_id,
            self._aware_utc(
                bundle.source_available_at,
                "business catalyst source_available_at",
            ),
        )

    def _validate_cross_domain_candidate(
        self,
        candidate: CrossDomainContextCandidate,
        *,
        company_id: UUID,
        security_id: UUID,
        fiscal_year: int,
        fiscal_quarter: int,
        cutoff: datetime,
    ) -> None:
        self._validate_financial_candidate(
            FinancialComponentContextCandidate(
                provider_dataset_id=candidate.provider_dataset_id,
                filing_scope=candidate.filing_scope,
                financial_inflection=candidate.financial_inflection,
                business_quality=candidate.business_quality,
                cash_flow_quality=candidate.cash_flow_quality,
                balance_sheet=candidate.balance_sheet,
            ),
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            cutoff=cutoff,
        )
        valuation = candidate.valuation
        if valuation is None:
            return
        if valuation.financial_provider_dataset_id != candidate.provider_dataset_id:
            raise ValueError("valuation provider does not match candidate key")
        if valuation.filing_scope != candidate.filing_scope:
            raise ValueError("valuation scope does not match candidate key")
        if valuation.company_id != company_id:
            raise ValueError("valuation company does not match orchestration company")
        if valuation.security_id != security_id:
            raise ValueError("valuation security does not match orchestration security")
        if (valuation.ending_fiscal_year, valuation.ending_fiscal_quarter) != (
            fiscal_year,
            fiscal_quarter,
        ):
            raise ValueError("valuation endpoint does not match orchestration endpoint")
        if self._aware_utc(valuation.as_of, "valuation as_of") != cutoff:
            raise ValueError("valuation as_of does not match orchestration cutoff")

    def _attempt_valuation_component(
        self,
        *,
        candidate: CrossDomainContextCandidate,
        policy: InflectionScoringPolicy,
    ) -> tuple[ValuationComponentScore | None, dict[str, object]]:
        code = "valuation"
        evidence = candidate.valuation
        policy_configured = policy.valuation is not None
        top_level_weight = policy.component_weights.valuation
        result: ValuationComponentScore | None = None
        if top_level_weight == 0:
            warnings = ()
        elif not policy_configured:
            warnings = ("valuation_scoring_not_configured",)
        elif evidence is None:
            warnings = ("valuation_evidence_missing",)
        else:
            result = self._valuation_scorer.score(evidence=evidence, policy=policy)
            warnings = result.warnings
        scoreable = result is not None and result.score is not None and top_level_weight > 0
        attempt = {
            "component_code": code,
            "policy_configured": policy_configured,
            "evidence_supplied": evidence is not None,
            "scoreable": scoreable,
            "subfactor_weight_coverage": (
                result.weight_coverage if result is not None else Decimal("0")
            ),
            "warnings": warnings,
        }
        return (result if scoreable else None), attempt

    def _attempt_market_structure(
        self,
        *,
        evidence: MarketStructureFeatureBundle | None,
        policy: InflectionScoringPolicy,
        selected_context_exists: bool,
    ) -> tuple[MarketStructureComponentScore | None, dict[str, object]]:
        policy_configured = policy.market_structure is not None
        top_level_weight = policy.component_weights.market_structure
        result: MarketStructureComponentScore | None = None
        if not selected_context_exists:
            warnings = ("financial_context_not_selected",)
        elif top_level_weight == 0:
            warnings = ()
        elif not policy_configured:
            warnings = ("market_structure_scoring_not_configured",)
        elif evidence is None:
            warnings = ("market_structure_evidence_missing",)
        else:
            result = self._market_structure_scorer.score(evidence=evidence, policy=policy)
            warnings = result.warnings
        scoreable = result is not None and result.score is not None and top_level_weight > 0
        return (
            result if scoreable else None,
            {
                "policy_configured": policy_configured,
                "evidence_supplied": evidence is not None,
                "scoreable": scoreable,
                "subfactor_weight_coverage": (
                    result.weight_coverage if result is not None else Decimal("0")
                ),
                "warnings": warnings,
            },
        )

    @staticmethod
    def _cross_domain_candidate_for_selection(
        candidates: tuple[CrossDomainContextCandidate, ...],
        selection: FinancialContextSelection,
    ) -> CrossDomainContextCandidate:
        matches = tuple(
            candidate
            for candidate in candidates
            if candidate.provider_dataset_id == selection.provider_dataset_id
            and candidate.filing_scope == selection.filing_scope
        )
        if len(matches) != 1:
            raise AssertionError("selected financial context has no unique candidate")
        return matches[0]

    def _validate_market_structure_identity(
        self,
        evidence: MarketStructureFeatureBundle,
        *,
        security_id: UUID,
        cutoff: datetime,
    ) -> None:
        if evidence.security_id != security_id:
            raise ValueError("market structure security does not match orchestration security")
        if self._aware_utc(evidence.as_of, "market structure as_of") != cutoff:
            raise ValueError("market structure as_of does not match orchestration cutoff")

    def _validate_cross_market_coherence(
        self,
        valuation: ValuationFeatureBundle,
        market_structure: MarketStructureFeatureBundle,
        *,
        security_id: UUID,
        cutoff: datetime,
    ) -> None:
        if valuation.market_provider_dataset_id != market_structure.market_provider_dataset_id:
            raise ValueError("valuation and market structure use different market providers")
        if valuation.security_id != security_id or market_structure.security_id != security_id:
            raise ValueError("cross-domain market evidence uses a different security")
        if (
            self._aware_utc(valuation.as_of, "valuation as_of") != cutoff
            or self._aware_utc(market_structure.as_of, "market structure as_of") != cutoff
        ):
            raise ValueError("cross-domain market evidence uses a different cutoff")
        if valuation.market_on_or_before != market_structure.market_on_or_before:
            raise ValueError("cross-domain market date bounds do not match")
        if valuation.market_bar is not None:
            if valuation.market_bar.interval != market_structure.interval:
                raise ValueError("cross-domain market intervals do not match")
            if (
                market_structure.basis_date is not None
                and valuation.market_bar.trading_date != market_structure.basis_date
            ):
                raise ValueError("cross-domain market endpoints do not match")

    @staticmethod
    def _market_context_mapping(
        security_id: UUID,
        market_structure: MarketStructureFeatureBundle | None,
        valuation: ValuationFeatureBundle | None,
    ) -> dict[str, object]:
        valuation_bar = valuation.market_bar if valuation is not None else None
        return {
            "security_id": security_id,
            "market_provider_dataset_id": (
                market_structure.market_provider_dataset_id
                if market_structure is not None
                else (valuation.market_provider_dataset_id if valuation is not None else None)
            ),
            "corporate_action_provider_dataset_id": (
                market_structure.corporate_action_provider_dataset_id
                if market_structure is not None
                else None
            ),
            "benchmark_provider_dataset_id": (
                market_structure.benchmark_provider_dataset_id
                if market_structure is not None
                else None
            ),
            "benchmark_code": (
                market_structure.benchmark_code if market_structure is not None else None
            ),
            "interval": (
                market_structure.interval
                if market_structure is not None
                else (valuation_bar.interval if valuation_bar is not None else None)
            ),
            "market_on_or_before": (
                market_structure.market_on_or_before
                if market_structure is not None
                else (valuation.market_on_or_before if valuation is not None else None)
            ),
            "basis_date": (
                market_structure.basis_date
                if market_structure is not None
                else (valuation_bar.trading_date if valuation_bar is not None else None)
            ),
        }

    def _validate_financial_candidate(
        self,
        candidate: FinancialComponentContextCandidate,
        *,
        company_id: UUID,
        fiscal_year: int,
        fiscal_quarter: int,
        cutoff: datetime,
    ) -> None:
        for evidence in (
            candidate.financial_inflection,
            candidate.business_quality,
            candidate.cash_flow_quality,
            candidate.balance_sheet,
        ):
            if evidence is None:
                continue
            if evidence.context.provider_dataset_id != candidate.provider_dataset_id:
                raise ValueError("candidate evidence provider does not match candidate key")
            if evidence.context.filing_scope != candidate.filing_scope:
                raise ValueError("candidate evidence scope does not match candidate key")
            if evidence.company_id != company_id:
                raise ValueError("candidate company does not match orchestration company")
            if (evidence.fiscal_year, evidence.fiscal_quarter) != (
                fiscal_year,
                fiscal_quarter,
            ):
                raise ValueError("candidate endpoint does not match orchestration endpoint")
            if self._aware_utc(evidence.as_of, "candidate as_of") != cutoff:
                raise ValueError("candidate as_of does not match orchestration cutoff")

    def _attempt_financial_component(
        self,
        *,
        code: str,
        candidate: FinancialComponentContextCandidate,
        selection: FinancialContextSelection,
        policy: InflectionScoringPolicy,
    ) -> tuple[
        FinancialInflectionComponentScore
        | BusinessQualityComponentScore
        | CashFlowQualityComponentScore
        | BalanceSheetComponentScore
        | None,
        dict[str, object],
    ]:
        evidence = getattr(candidate, code)
        policy_configured = self._component_policy_configured(code, policy)
        top_level_weight = getattr(policy.component_weights, code)
        result: (
            FinancialInflectionComponentScore
            | BusinessQualityComponentScore
            | CashFlowQualityComponentScore
            | BalanceSheetComponentScore
            | None
        ) = None
        if top_level_weight == 0:
            warnings = ()
        elif not policy_configured:
            warnings = (f"{code}_scoring_not_configured",)
        elif evidence is None:
            warnings = (f"{code}_evidence_missing",)
        else:
            coherent = replace(evidence, context=selection)
            if code == "financial_inflection":
                result = self._component_scorer.score(evidence=coherent, policy=policy)
            elif code == "business_quality":
                result = self._business_quality_scorer.score(evidence=coherent, policy=policy)
            elif code == "cash_flow_quality":
                result = self._cash_flow_quality_scorer.score(evidence=coherent, policy=policy)
            elif code == "balance_sheet":
                result = self._balance_sheet_scorer.score(evidence=coherent, policy=policy)
            else:
                raise AssertionError("unknown implemented component")
            warnings = result.warnings

        scoreable = result is not None and result.score is not None and top_level_weight > 0
        attempt = {
            "component_code": code,
            "policy_configured": policy_configured,
            "evidence_supplied": evidence is not None,
            "scoreable": scoreable,
            "subfactor_weight_coverage": (
                result.weight_coverage if result is not None else Decimal("0")
            ),
            "warnings": warnings,
        }
        return (result if scoreable else None), attempt

    @staticmethod
    def _component_policy_configured(code: str, policy: InflectionScoringPolicy) -> bool:
        if code == "financial_inflection":
            return policy.financial_inflection.scoring is not None
        return getattr(policy, code) is not None

    def _score_candidates(
        self,
        *,
        candidates: tuple[FinancialInflectionEvidence, ...],
        policy: InflectionScoringPolicy,
        company_id: UUID,
        fiscal_year: int,
        fiscal_quarter: int,
        cutoff: datetime,
    ) -> tuple[
        FinancialContextSelection | None,
        FinancialInflectionComponentScore | None,
        list[dict[str, object]],
    ]:
        by_context: dict[tuple[UUID, str], FinancialInflectionEvidence] = {}
        for candidate in candidates:
            key = (candidate.context.provider_dataset_id, candidate.context.filing_scope)
            if key in by_context:
                raise ValueError("duplicate financial-inflection candidate context")
            by_context[key] = candidate

        configured_keys = [
            (provider_id, scope)
            for provider_id in policy.financial_context.provider_dataset_priority
            for scope in policy.financial_context.filing_scope_priority
            if (provider_id, scope) in by_context
        ]
        scoreable: dict[tuple[UUID, str], FinancialInflectionComponentScore] = {}
        attempts: list[dict[str, object]] = []
        for key in configured_keys:
            canonical_selection = self._contexts.resolve(policy.financial_context, (key,))
            assert canonical_selection is not None
            candidate = replace(by_context[key], context=canonical_selection)
            if candidate.company_id != company_id:
                raise ValueError("candidate company does not match orchestration company")
            if (candidate.fiscal_year, candidate.fiscal_quarter) != (
                fiscal_year,
                fiscal_quarter,
            ):
                raise ValueError("candidate endpoint does not match orchestration endpoint")
            if self._aware_utc(candidate.as_of, "candidate as_of") != cutoff:
                raise ValueError("candidate as_of does not match orchestration cutoff")
            if policy.financial_inflection.scoring is None:
                result = None
                coverage = Decimal("0")
                warnings = ("financial_inflection_scoring_not_configured",)
            else:
                result = self._component_scorer.score(evidence=candidate, policy=policy)
                coverage = result.weight_coverage
                warnings = result.warnings
                if result.score is not None:
                    scoreable[key] = result
            attempts.append(
                {
                    "provider_dataset_id": key[0],
                    "filing_scope": key[1],
                    "scoreable": result is not None and result.score is not None,
                    "subfactor_weight_coverage": coverage,
                    "component_score_available": result is not None and result.score is not None,
                    "warnings": warnings,
                }
            )

        selected = self._contexts.resolve(policy.financial_context, scoreable.keys())
        selected_component = (
            scoreable[(selected.provider_dataset_id, selected.filing_scope)]
            if selected is not None
            else None
        )
        return selected, selected_component, attempts

    def _component_write(
        self,
        component: FinancialInflectionComponentScore | None,
        configured_top_level_weight: Decimal,
    ) -> tuple[ScoreComponentWrite | None, dict[str, object]]:
        if component is None or component.score is None:
            return None, {"components": []}
        subfactor_details = [self._subfactor_detail(item) for item in component.subfactors]
        manifests = {
            item.code: self._lineage_manifest(item.evidence, component.algorithm_version)
            for item in component.subfactors
        }
        explanations = self._explanation_writes(component, manifests)
        detail = self._canonical_dict(
            {
                "component_code": "financial_inflection",
                "score": component.score,
                "unit": component.unit,
                "weight_coverage": component.weight_coverage,
                "missing_subfactors": component.missing_subfactors,
                "warnings": component.warnings,
                "algorithm_version": component.algorithm_version,
                "subfactors": subfactor_details,
            }
        )
        union_manifest = self._union_manifests(tuple(manifests.values()), component)
        return (
            ScoreComponentWrite(
                component_code="financial_inflection",
                score=component.score,
                unit=component.unit,
                configured_top_level_weight=configured_top_level_weight,
                subfactor_weight_coverage=component.weight_coverage,
                final_contribution=None,
                available_at=component.available_at,
                algorithm_version=component.algorithm_version,
                missing_subfactors_json=list(component.missing_subfactors),
                warnings_json=list(component.warnings),
                detail_json=detail,
                explanations=explanations,
            ),
            {"components": [union_manifest]},
        )

    def _financial_component_write(
        self,
        component_code: str,
        component: FinancialInflectionComponentScore
        | BusinessQualityComponentScore
        | CashFlowQualityComponentScore
        | BalanceSheetComponentScore,
        configured_top_level_weight: Decimal,
    ) -> tuple[ScoreComponentWrite, dict[str, object]]:
        if component.score is None:
            raise AssertionError("only scoreable components may be persisted")
        subfactors = cast(
            tuple[
                FinancialInflectionSubfactorScore
                | BusinessQualitySubfactorScore
                | CashFlowQualitySubfactorScore
                | BalanceSheetSubfactorScore,
                ...,
            ],
            component.subfactors,
        )
        subfactor_details = [self._financial_subfactor_detail(item) for item in subfactors]
        manifests = {
            item.code: self._lineage_manifest(item.evidence, component.algorithm_version)
            for item in subfactors
        }
        explanations = self._financial_explanation_writes(
            component_code,
            subfactors,
            manifests,
        )
        detail = self._canonical_dict(
            {
                "component_code": component_code,
                "score": component.score,
                "unit": component.unit,
                "weight_coverage": component.weight_coverage,
                "missing_subfactors": component.missing_subfactors,
                "warnings": component.warnings,
                "algorithm_version": component.algorithm_version,
                "subfactors": subfactor_details,
            }
        )
        union_manifest = self._generic_union_manifests(
            tuple(manifests.values()),
            component_code=component_code,
            component_algorithm_version=component.algorithm_version,
        )
        return (
            ScoreComponentWrite(
                component_code=component_code,
                score=component.score,
                unit=component.unit,
                configured_top_level_weight=configured_top_level_weight,
                subfactor_weight_coverage=component.weight_coverage,
                final_contribution=None,
                available_at=component.available_at,
                algorithm_version=component.algorithm_version,
                missing_subfactors_json=list(component.missing_subfactors),
                warnings_json=list(component.warnings),
                detail_json=detail,
                explanations=explanations,
            ),
            union_manifest,
        )

    def _cross_domain_component_write(
        self,
        component_code: str,
        component: ValuationComponentScore | MarketStructureComponentScore,
        configured_top_level_weight: Decimal,
        *,
        security_id: UUID,
    ) -> tuple[ScoreComponentWrite, dict[str, object]]:
        if component.score is None:
            raise AssertionError("only scoreable cross-domain components may be persisted")
        subfactors = cast(
            tuple[ValuationSubfactorScore | MarketStructureSubfactorScore, ...],
            component.subfactors,
        )
        details = [self._cross_domain_subfactor_detail(item) for item in subfactors]
        manifests = {
            item.code: self._cross_domain_lineage_manifest(
                item.evidence,
                component_algorithm_version=component.algorithm_version,
            )
            for item in subfactors
        }
        explanations = self._cross_domain_explanation_writes(
            component_code,
            subfactors,
            manifests,
        )
        detail_value: dict[str, object] = {
            "component_code": component_code,
            "security_id": security_id,
            "score": component.score,
            "unit": component.unit,
            "weight_coverage": component.weight_coverage,
            "missing_subfactors": component.missing_subfactors,
            "warnings": component.warnings,
            "algorithm_version": component.algorithm_version,
            "subfactors": details,
        }
        if isinstance(component, MarketStructureComponentScore):
            detail_value["market_context"] = {
                "market_provider_dataset_id": component.market_provider_dataset_id,
                "corporate_action_provider_dataset_id": (
                    component.corporate_action_provider_dataset_id
                ),
                "benchmark_provider_dataset_id": component.benchmark_provider_dataset_id,
                "benchmark_code": component.benchmark_code,
                "interval": component.interval,
                "market_on_or_before": component.market_on_or_before,
                "basis_date": component.basis_date,
            }
        else:
            detail_value["market_provider_dataset_id"] = component.market_provider_dataset_id
            detail_value["financial_provider_dataset_id"] = component.financial_provider_dataset_id
            detail_value["filing_scope"] = component.filing_scope
        detail = self._canonical_dict(detail_value)
        union_manifest = self._cross_domain_component_union(
            tuple(manifests.values()),
            component_code=component_code,
            component_algorithm_version=component.algorithm_version,
        )
        return (
            ScoreComponentWrite(
                component_code=component_code,
                score=component.score,
                unit=component.unit,
                configured_top_level_weight=configured_top_level_weight,
                subfactor_weight_coverage=component.weight_coverage,
                final_contribution=None,
                available_at=component.available_at,
                algorithm_version=component.algorithm_version,
                missing_subfactors_json=list(component.missing_subfactors),
                warnings_json=list(component.warnings),
                detail_json=detail,
                explanations=explanations,
            ),
            union_manifest,
        )

    def _business_catalyst_component_write(
        self,
        component: BusinessCatalystComponentScore,
        configured_top_level_weight: Decimal,
    ) -> tuple[ScoreComponentWrite, dict[str, object]]:
        if component.score is None or component.selected_event_id is None:
            raise AssertionError("only scoreable Business Catalyst components may persist")
        selected = next(
            (
                item
                for item in component.event_scores
                if item.business_event_id == component.selected_event_id
            ),
            None,
        )
        if selected is None or selected.score is None:
            raise AssertionError("selected Business Catalyst event is unavailable")
        manifest = self._business_catalyst_lineage_manifest(
            tuple(item.evidence for item in component.event_scores),
            component_algorithm_version=component.algorithm_version,
        )
        selected_manifest = self._business_catalyst_lineage_manifest(
            (selected.evidence,),
            component_algorithm_version=component.algorithm_version,
        )
        explanation_manifest = self._canonical_dict(
            {
                **selected_manifest,
                "selected_event_score": self._business_catalyst_event_detail(selected),
            }
        )
        explanation = ScoreExplanationWrite(
            factor_code="selected_business_catalyst_event",
            rank=1,
            raw_value=selected.score,
            raw_unit="score_0_100",
            normalized_score=selected.score,
            configured_weight=Decimal("1"),
            effective_weight=Decimal("1"),
            component_contribution=selected.score,
            input_available_at=selected.source_available_at,
            evidence_type="BusinessEventFeatureBundle",
            template_code="business_catalyst_selected_event_v1",
            direction=None,
            evidence_manifest_json=explanation_manifest,
        )
        detail = self._canonical_dict(
            {
                "component_code": "business_catalyst",
                "score": component.score,
                "unit": component.unit,
                "event_provider_dataset_id": component.event_provider_dataset_id,
                "financial_provider_dataset_id": component.financial_provider_dataset_id,
                "filing_scope": component.filing_scope,
                "security_id": component.security_id,
                "selected_event_id": component.selected_event_id,
                "selected_event_type": component.selected_event_type,
                "aggregation_method": component.aggregation_method,
                "available_at": component.available_at,
                "algorithm_version": component.algorithm_version,
                "event_scores": [
                    self._business_catalyst_event_detail(item)
                    for item in component.event_scores
                ],
            }
        )
        return (
            ScoreComponentWrite(
                component_code="business_catalyst",
                score=component.score,
                unit=component.unit,
                configured_top_level_weight=configured_top_level_weight,
                subfactor_weight_coverage=Decimal("1"),
                final_contribution=None,
                available_at=component.available_at,
                algorithm_version=component.algorithm_version,
                missing_subfactors_json=[],
                warnings_json=list(component.warnings),
                detail_json=detail,
                explanations=(explanation,),
            ),
            manifest,
        )

    @staticmethod
    def _business_catalyst_event_detail(event: object) -> dict[str, object]:
        value = canonical_audit_value(
            {
                "business_event_id": getattr(event, "business_event_id"),
                "announcement_id": getattr(event, "announcement_id"),
                "event_type": getattr(event, "event_type"),
                "source_available_at": getattr(event, "source_available_at"),
                "score": getattr(event, "score"),
                "strength_score": getattr(event, "strength_score"),
                "strength_code": getattr(event, "strength_code"),
                "strength_raw_value": getattr(event, "strength_raw_value"),
                "strength_raw_unit": getattr(event, "strength_raw_unit"),
                "strength_transform_code": getattr(event, "strength_transform_code"),
                "strength_curve_algorithm_version": getattr(
                    event, "strength_curve_algorithm_version"
                ),
                "event_age_days": getattr(event, "event_age_days"),
                "recency_scoring_value": getattr(event, "recency_scoring_value"),
                "recency_transform_code": getattr(event, "recency_transform_code"),
                "recency_score": getattr(event, "recency_score"),
                "recency_curve_algorithm_version": getattr(
                    event, "recency_curve_algorithm_version"
                ),
                "warnings": getattr(event, "warnings"),
                "algorithm_version": getattr(event, "algorithm_version"),
            }
        )
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def _v3_financial_component_write(
        self,
        component_code: str,
        component: FinancialInflectionComponentScore
        | BusinessQualityComponentScore
        | CashFlowQualityComponentScore
        | BalanceSheetComponentScore,
        configured_top_level_weight: Decimal,
    ) -> tuple[ScoreComponentWrite, dict[str, object]]:
        if component.score is None:
            raise AssertionError("only scoreable financial components may be persisted")
        subfactors = cast(
            tuple[
                FinancialInflectionSubfactorScore
                | BusinessQualitySubfactorScore
                | CashFlowQualitySubfactorScore
                | BalanceSheetSubfactorScore,
                ...,
            ],
            component.subfactors,
        )
        details = [self._financial_subfactor_detail(item) for item in subfactors]
        manifests = {
            item.code: self._cross_domain_lineage_manifest(
                item.evidence,
                component_algorithm_version=component.algorithm_version,
            )
            for item in subfactors
        }
        explanations = self._financial_explanation_writes(
            component_code,
            subfactors,
            manifests,
        )
        detail = self._canonical_dict(
            {
                "component_code": component_code,
                "score": component.score,
                "unit": component.unit,
                "weight_coverage": component.weight_coverage,
                "missing_subfactors": component.missing_subfactors,
                "warnings": component.warnings,
                "algorithm_version": component.algorithm_version,
                "subfactors": details,
            }
        )
        union_manifest = self._cross_domain_component_union(
            tuple(manifests.values()),
            component_code=component_code,
            component_algorithm_version=component.algorithm_version,
        )
        return (
            ScoreComponentWrite(
                component_code=component_code,
                score=component.score,
                unit=component.unit,
                configured_top_level_weight=configured_top_level_weight,
                subfactor_weight_coverage=component.weight_coverage,
                final_contribution=None,
                available_at=component.available_at,
                algorithm_version=component.algorithm_version,
                missing_subfactors_json=list(component.missing_subfactors),
                warnings_json=list(component.warnings),
                detail_json=detail,
                explanations=explanations,
            ),
            union_manifest,
        )

    def _cross_domain_explanation_writes(
        self,
        component_code: str,
        subfactors: tuple[ValuationSubfactorScore | MarketStructureSubfactorScore, ...],
        manifests: dict[str, dict[str, object]],
    ) -> tuple[ScoreExplanationWrite, ...]:
        order = {
            code: index for index, code in enumerate(COMPONENT_SUBFACTOR_ORDERS[component_code])
        }
        ranked = sorted(subfactors, key=lambda item: (-item.contribution, order[item.code]))
        writes: list[ScoreExplanationWrite] = []
        for rank, item in enumerate(ranked, start=1):
            manifest = self._canonical_dict(
                {
                    **manifests[item.code],
                    "scoring_transform": {
                        "raw_value": item.raw_value,
                        "raw_unit": item.raw_unit,
                        "normalized_raw_value": item.normalized_raw_value,
                        "normalized_raw_unit": item.normalized_raw_unit,
                        "scoring_value": item.scoring_value,
                        "scoring_unit": item.scoring_unit,
                        "transform_code": item.transform_code,
                        "normalized_score": item.normalized_score,
                        "configured_weight": item.configured_weight,
                        "effective_weight": item.effective_weight,
                        "contribution": item.contribution,
                    },
                }
            )
            writes.append(
                ScoreExplanationWrite(
                    factor_code=item.code,
                    rank=rank,
                    raw_value=item.raw_value,
                    raw_unit=item.raw_unit,
                    normalized_score=item.normalized_score,
                    configured_weight=item.configured_weight,
                    effective_weight=item.effective_weight,
                    component_contribution=item.contribution,
                    input_available_at=item.input_available_at,
                    evidence_type=item.evidence_type,
                    template_code=COMPONENT_TEMPLATE_CODES[component_code],
                    direction=None,
                    evidence_manifest_json=manifest,
                )
            )
        return tuple(writes)

    @staticmethod
    def _cross_domain_subfactor_detail(
        item: ValuationSubfactorScore | MarketStructureSubfactorScore,
    ) -> dict[str, object]:
        value = canonical_audit_value(
            {
                "code": item.code,
                "raw_value": item.raw_value,
                "raw_unit": item.raw_unit,
                "normalized_raw_value": item.normalized_raw_value,
                "normalized_raw_unit": item.normalized_raw_unit,
                "scoring_value": item.scoring_value,
                "scoring_unit": item.scoring_unit,
                "transform_code": item.transform_code,
                "normalized_score": item.normalized_score,
                "configured_weight": item.configured_weight,
                "effective_weight": item.effective_weight,
                "component_contribution": item.contribution,
                "input_available_at": item.input_available_at,
                "evidence_type": item.evidence_type,
                "curve_algorithm_version": item.curve_algorithm_version,
            }
        )
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def _financial_explanation_writes(
        self,
        component_code: str,
        subfactors: tuple[
            FinancialInflectionSubfactorScore
            | BusinessQualitySubfactorScore
            | CashFlowQualitySubfactorScore
            | BalanceSheetSubfactorScore,
            ...,
        ],
        manifests: dict[str, dict[str, object]],
    ) -> tuple[ScoreExplanationWrite, ...]:
        order = {
            code: index for index, code in enumerate(COMPONENT_SUBFACTOR_ORDERS[component_code])
        }
        ranked = sorted(
            subfactors,
            key=lambda item: (-item.contribution, order[item.code]),
        )
        writes: list[ScoreExplanationWrite] = []
        for rank, item in enumerate(ranked, start=1):
            manifest = manifests[item.code]
            if isinstance(item, (CashFlowQualitySubfactorScore, BalanceSheetSubfactorScore)):
                manifest = self._canonical_dict(
                    {
                        **manifest,
                        "scoring_transform": {
                            "normalized_raw_value": item.normalized_raw_value,
                            "normalized_raw_unit": item.normalized_raw_unit,
                            "scoring_value": item.scoring_value,
                            "scoring_unit": item.scoring_unit,
                            "transform_code": item.transform_code,
                        },
                    }
                )
            writes.append(
                ScoreExplanationWrite(
                    factor_code=item.code,
                    rank=rank,
                    raw_value=item.raw_value,
                    raw_unit=item.raw_unit,
                    normalized_score=item.normalized_score,
                    configured_weight=item.configured_weight,
                    effective_weight=item.effective_weight,
                    component_contribution=item.contribution,
                    input_available_at=item.input_available_at,
                    evidence_type=item.evidence_type,
                    template_code=COMPONENT_TEMPLATE_CODES[component_code],
                    direction=None,
                    evidence_manifest_json=manifest,
                )
            )
        return tuple(writes)

    @staticmethod
    def _financial_subfactor_detail(
        item: FinancialInflectionSubfactorScore
        | BusinessQualitySubfactorScore
        | CashFlowQualitySubfactorScore
        | BalanceSheetSubfactorScore,
    ) -> dict[str, object]:
        detail: dict[str, object] = {
            "code": item.code,
            "raw_value": item.raw_value,
            "raw_unit": item.raw_unit,
            "normalized_score": item.normalized_score,
            "configured_weight": item.configured_weight,
            "effective_weight": item.effective_weight,
            "component_contribution": item.contribution,
            "input_available_at": item.input_available_at,
            "evidence_type": item.evidence_type,
            "curve_algorithm_version": item.curve_algorithm_version,
        }
        if isinstance(item, (CashFlowQualitySubfactorScore, BalanceSheetSubfactorScore)):
            detail.update(
                {
                    "normalized_raw_value": item.normalized_raw_value,
                    "normalized_raw_unit": item.normalized_raw_unit,
                    "scoring_value": item.scoring_value,
                    "scoring_unit": item.scoring_unit,
                    "transform_code": item.transform_code,
                }
            )
        value = canonical_audit_value(detail)
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def _explanation_writes(
        self,
        component: FinancialInflectionComponentScore,
        manifests: dict[str, dict[str, object]],
    ) -> tuple[ScoreExplanationWrite, ...]:
        order = {code: index for index, code in enumerate(SUBFACTOR_ORDER)}
        ranked = sorted(
            component.subfactors,
            key=lambda item: (-item.contribution, order[item.code]),
        )
        return tuple(
            ScoreExplanationWrite(
                factor_code=item.code,
                rank=rank,
                raw_value=item.raw_value,
                raw_unit=item.raw_unit,
                normalized_score=item.normalized_score,
                configured_weight=item.configured_weight,
                effective_weight=item.effective_weight,
                component_contribution=item.contribution,
                input_available_at=item.input_available_at,
                evidence_type=item.evidence_type,
                template_code=EXPLANATION_TEMPLATE_CODE,
                direction=None,
                evidence_manifest_json=manifests[item.code],
            )
            for rank, item in enumerate(ranked, start=1)
        )

    @staticmethod
    def _subfactor_detail(item: FinancialInflectionSubfactorScore) -> dict[str, object]:
        value = canonical_audit_value(
            {
                "code": item.code,
                "raw_value": item.raw_value,
                "raw_unit": item.raw_unit,
                "normalized_score": item.normalized_score,
                "configured_weight": item.configured_weight,
                "effective_weight": item.effective_weight,
                "component_contribution": item.contribution,
                "input_available_at": item.input_available_at,
                "evidence_type": item.evidence_type,
                "curve_algorithm_version": item.curve_algorithm_version,
            }
        )
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def _lineage_manifest(
        self,
        evidence: object,
        component_algorithm_version: str,
    ) -> dict[str, object]:
        facts: dict[str, dict[str, object]] = {}
        algorithms: set[tuple[str, str]] = set()
        visited: set[int] = set()

        def visit(value: object) -> None:
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            if isinstance(value, PointInTimeFinancialFact):
                facts[str(value.id)] = self._canonical_dict(
                    {
                        "financial_fact_id": value.id,
                        "source_record_id": value.source_record.id,
                        "external_record_id": value.source_record.external_record_id,
                        "raw_object_key": value.source_record.raw_object_key,
                        "raw_payload_reference": value.source_record.raw_payload_reference,
                        "metric_code": value.metric_code,
                        "available_at": value.available_at,
                    }
                )
                return
            algorithm = getattr(value, "algorithm_version", None)
            if isinstance(algorithm, str):
                algorithms.add((type(value).__name__, algorithm))
            if is_dataclass(value) and not isinstance(value, type):
                for field in fields(value):
                    visit(getattr(value, field.name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)

        visit(evidence)
        return self._canonical_dict(
            {
                "component_algorithm_version": component_algorithm_version,
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [
                    {"evidence_type": kind, "algorithm_version": version}
                    for kind, version in sorted(algorithms)
                ],
                "facts": [facts[key] for key in sorted(facts)],
            }
        )

    def _cross_domain_lineage_manifest(
        self,
        evidence: object,
        *,
        component_algorithm_version: str,
    ) -> dict[str, object]:
        financial_facts: dict[str, dict[str, object]] = {}
        market_bars: dict[str, dict[str, object]] = {}
        benchmark_bars: dict[str, dict[str, object]] = {}
        corporate_actions: dict[str, dict[str, object]] = {}
        algorithms: set[tuple[str, str]] = set()
        visited: set[int] = set()

        def source_mapping(value: object) -> dict[str, object]:
            source = getattr(value, "source_record")
            return {
                "source_record_id": source.id,
                "external_record_id": source.external_record_id,
                "raw_object_key": source.raw_object_key,
                "raw_payload_reference": source.raw_payload_reference,
                "content_sha256": source.content_sha256,
            }

        def visit(value: object) -> None:
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            algorithm = getattr(value, "algorithm_version", None)
            if isinstance(algorithm, str):
                algorithms.add((type(value).__name__, algorithm))
            if isinstance(value, PointInTimeFinancialFact):
                financial_facts[str(value.id)] = self._canonical_dict(
                    {
                        "financial_fact_id": value.id,
                        **source_mapping(value),
                        "metric_code": value.metric_code,
                        "available_at": value.available_at,
                    }
                )
                return
            if isinstance(value, PointInTimeMarketBar):
                market_bars[str(value.id)] = self._canonical_dict(
                    {
                        "price_bar_id": value.id,
                        "provider_dataset_id": value.provider_dataset_id,
                        "security_id": value.security_id,
                        "trading_date": value.trading_date,
                        "interval": value.interval,
                        "available_at": value.available_at,
                        **source_mapping(value),
                    }
                )
                return
            if isinstance(value, PointInTimeBenchmarkBar):
                benchmark_bars[str(value.id)] = self._canonical_dict(
                    {
                        "benchmark_bar_id": value.id,
                        "benchmark_series_id": value.benchmark_series.id,
                        "provider_dataset_id": value.benchmark_series.provider_dataset_id,
                        "benchmark_code": value.benchmark_series.code,
                        "trading_date": value.trading_date,
                        "interval": value.interval,
                        "available_at": value.available_at,
                        **source_mapping(value),
                    }
                )
                return
            if isinstance(value, PointInTimeCorporateAction):
                corporate_actions[str(value.id)] = self._canonical_dict(
                    {
                        "corporate_action_id": value.id,
                        "provider_dataset_id": value.provider_dataset_id,
                        "security_id": value.security_id,
                        "action_type": value.action_type,
                        "event_date": value.event_anchor,
                        "effective_date": value.effective_date,
                        "available_at": value.available_at,
                        **source_mapping(value),
                    }
                )
                return
            if is_dataclass(value) and not isinstance(value, type):
                for field in fields(value):
                    visit(getattr(value, field.name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)

        visit(evidence)
        return self._canonical_dict(
            {
                "component_algorithm_version": component_algorithm_version,
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [
                    {"evidence_type": kind, "algorithm_version": version}
                    for kind, version in sorted(algorithms)
                ],
                "financial_facts": [financial_facts[key] for key in sorted(financial_facts)],
                "market_bars": [market_bars[key] for key in sorted(market_bars)],
                "benchmark_bars": [benchmark_bars[key] for key in sorted(benchmark_bars)],
                "corporate_actions": [corporate_actions[key] for key in sorted(corporate_actions)],
            }
        )

    def _business_catalyst_lineage_manifest(
        self,
        bundles: tuple[BusinessEventFeatureBundle, ...],
        *,
        component_algorithm_version: str,
    ) -> dict[str, object]:
        financial_facts: dict[str, dict[str, object]] = {}
        announcements: dict[str, dict[str, object]] = {}
        documents: dict[str, dict[str, object]] = {}
        document_assets: dict[str, dict[str, object]] = {}
        text_extractions: dict[str, dict[str, object]] = {}
        business_events: dict[str, dict[str, object]] = {}
        business_event_evidence: dict[str, dict[str, object]] = {}
        quantitative_derivations: dict[str, dict[str, object]] = {}
        quantitative_facts: dict[str, dict[str, object]] = {}
        algorithms: set[tuple[str, str]] = {
            ("BusinessCatalystEventScore", BUSINESS_CATALYST_EVENT_SCORE_VERSION),
            ("BusinessCatalystAggregation", BUSINESS_CATALYST_AGGREGATION_VERSION),
            ("PiecewiseLinearScoringCurve", PIECEWISE_LINEAR_CURVE_VERSION),
        }
        visited: set[int] = set()

        def source_mapping(value: object) -> dict[str, object]:
            source = getattr(value, "source_record")
            return {
                "source_record_id": source.id,
                "external_record_id": source.external_record_id,
                "source_content_sha256": source.content_sha256,
                "raw_object_key": source.raw_object_key,
                "raw_payload_reference": source.raw_payload_reference,
            }

        def visit(value: object, business_event_id: UUID | None = None) -> None:
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            algorithm = getattr(value, "algorithm_version", None)
            if isinstance(algorithm, str):
                algorithms.add((type(value).__name__, algorithm))
            if isinstance(value, PointInTimeFinancialFact):
                financial_facts[str(value.id)] = self._canonical_dict(
                    {
                        "financial_fact_id": value.id,
                        **source_mapping(value),
                        "metric_code": value.metric_code,
                        "available_at": value.available_at,
                    }
                )
                return
            if isinstance(value, PointInTimeAnnouncement):
                announcements[str(value.id)] = self._canonical_dict(
                    {
                        "announcement_id": value.id,
                        "provider_dataset_id": value.provider_dataset_id,
                        "external_record_id": value.external_record_id,
                        "available_at": value.available_at,
                        **source_mapping(value),
                    }
                )
                for document in value.documents:
                    visit(document)
                return
            if isinstance(value, PointInTimeDocument):
                documents[str(value.id)] = self._canonical_dict(
                    {
                        "document_id": value.id,
                        "provider_dataset_id": value.provider_dataset_id,
                        "document_type": value.document_type,
                        "document_content_sha256": value.document_content_sha256,
                        "available_at": value.available_at,
                        **source_mapping(value),
                    }
                )
                return
            if isinstance(value, DocumentAssetView):
                document_assets[str(value.id)] = self._canonical_dict(
                    {
                        "document_asset_id": value.id,
                        "document_id": value.document_id,
                        "content_sha256": value.content_sha256,
                        "object_key": value.object_key,
                        "size_bytes": value.size_bytes,
                        "status": value.status,
                    }
                )
                return
            if isinstance(value, DocumentTextExtractionView):
                text_extractions[str(value.id)] = self._canonical_dict(
                    {
                        "text_extraction_id": value.id,
                        "document_asset_id": value.document_asset_id,
                        "extractor_code": value.extractor_code,
                        "extractor_semantic_version": value.extractor_semantic_version,
                        "extractor_runtime_version": value.extractor_runtime_version,
                        "text_sha256": value.text_sha256,
                        "text_object_key": value.text_object_key,
                        "status": value.status,
                    }
                )
                algorithms.add(
                    (
                        f"DocumentTextExtractor:{value.extractor_code}",
                        f"{value.extractor_semantic_version}@{value.extractor_runtime_version}",
                    )
                )
                return
            if isinstance(value, PointInTimeBusinessEvent):
                business_events[str(value.id)] = self._canonical_dict(
                    {
                        "business_event_id": value.id,
                        "announcement_id": value.announcement.id,
                        "event_type": value.event_type,
                        "source_available_at": value.source_available_at,
                        "ruleset_code": value.ruleset_code,
                        "ruleset_semantic_version": value.ruleset_semantic_version,
                        "detection_fingerprint_sha256": (
                            value.detection_fingerprint_sha256
                        ),
                        "matched_rule_codes": value.matched_rule_codes,
                    }
                )
                algorithms.add((value.ruleset_code, value.ruleset_semantic_version))
                visit(value.announcement)
                for evidence in value.evidence:
                    visit(evidence, value.id)
                return
            if isinstance(value, BusinessEventEvidenceView):
                business_event_evidence[str(value.id)] = self._canonical_dict(
                    {
                        "business_event_evidence_id": value.id,
                        "business_event_id": business_event_id,
                        "evidence_kind": value.evidence_kind,
                        "document_id": value.document.id if value.document is not None else None,
                        "document_asset_id": (
                            value.document_asset.id
                            if value.document_asset is not None
                            else None
                        ),
                        "text_extraction_id": (
                            value.text_extraction.id
                            if value.text_extraction is not None
                            else None
                        ),
                        "rule_code": value.rule_code,
                        "rule_semantic_version": value.rule_semantic_version,
                        "start_offset": value.start_offset,
                        "end_offset": value.end_offset,
                        "page_numbers": value.page_numbers,
                        "page_text_sha256s": value.page_text_sha256s,
                        "excerpt_sha256": value.excerpt_sha256,
                        "evidence_fingerprint_sha256": value.evidence_fingerprint_sha256,
                        "source_available_at": value.source_available_at,
                    }
                )
                algorithms.add((value.rule_code, value.rule_semantic_version))
                if value.document is not None:
                    visit(value.document)
                if value.document_asset is not None:
                    visit(value.document_asset)
                if value.text_extraction is not None:
                    visit(value.text_extraction)
                return
            if isinstance(value, PointInTimeBusinessEventQuantitativeDerivation):
                quantitative_derivations[str(value.id)] = self._canonical_dict(
                    {
                        "quantitative_derivation_id": value.id,
                        "business_event_id": value.event.id,
                        "ruleset_code": value.ruleset_code,
                        "ruleset_semantic_version": value.ruleset_semantic_version,
                        "source_available_at": value.source_available_at,
                        "available_fact_codes": value.available_fact_codes,
                        "derivation_fingerprint_sha256": (
                            value.derivation_fingerprint_sha256
                        ),
                    }
                )
                algorithms.add((value.ruleset_code, value.ruleset_semantic_version))
                visit(value.event)
                for fact in value.facts:
                    visit(fact, value.event.id)
                return
            if isinstance(value, BusinessEventQuantitativeFactView):
                quantitative_facts[str(value.id)] = self._canonical_dict(
                    {
                        "quantitative_fact_id": value.id,
                        "business_event_id": business_event_id,
                        "business_event_evidence_id": value.business_event_evidence_id,
                        "fact_code": value.fact_code,
                        "fact_kind": value.fact_kind,
                        "rule_code": value.rule_code,
                        "rule_semantic_version": value.rule_semantic_version,
                        "start_offset": value.start_offset,
                        "end_offset": value.end_offset,
                        "raw_text_sha256": value.raw_text_sha256,
                        "reported_value": value.reported_value,
                        "reported_scale": value.reported_scale,
                        "reported_unit": value.reported_unit,
                        "reported_currency": value.reported_currency,
                        "normalized_value": value.normalized_value,
                        "normalized_unit": value.normalized_unit,
                        "date_value": value.date_value,
                        "source_available_at": value.source_available_at,
                        "fact_fingerprint_sha256": value.fact_fingerprint_sha256,
                    }
                )
                algorithms.add((value.rule_code, value.rule_semantic_version))
                return
            if is_dataclass(value) and not isinstance(value, type):
                for field in fields(value):
                    visit(getattr(value, field.name), business_event_id)
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item, business_event_id)

        for bundle in bundles:
            visit(bundle)
        return self._canonical_dict(
            {
                "component_code": "business_catalyst",
                "component_algorithm_version": component_algorithm_version,
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [
                    {"evidence_type": kind, "algorithm_version": version}
                    for kind, version in sorted(algorithms)
                ],
                "financial_facts": [
                    financial_facts[key] for key in sorted(financial_facts)
                ],
                "market_bars": [],
                "benchmark_bars": [],
                "corporate_actions": [],
                "announcements": [announcements[key] for key in sorted(announcements)],
                "documents": [documents[key] for key in sorted(documents)],
                "document_assets": [
                    document_assets[key] for key in sorted(document_assets)
                ],
                "text_extractions": [
                    text_extractions[key] for key in sorted(text_extractions)
                ],
                "business_events": [
                    business_events[key] for key in sorted(business_events)
                ],
                "business_event_evidence": [
                    business_event_evidence[key]
                    for key in sorted(business_event_evidence)
                ],
                "quantitative_derivations": [
                    quantitative_derivations[key]
                    for key in sorted(quantitative_derivations)
                ],
                "quantitative_facts": [
                    quantitative_facts[key] for key in sorted(quantitative_facts)
                ],
            }
        )

    def _cross_domain_component_union(
        self,
        manifests: tuple[dict[str, object], ...],
        *,
        component_code: str,
        component_algorithm_version: str,
    ) -> dict[str, object]:
        union = self._merge_cross_domain_manifests(manifests)
        return self._canonical_dict(
            {
                "component_code": component_code,
                "component_algorithm_version": component_algorithm_version,
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                **union,
            }
        )

    def _cross_domain_snapshot_union(
        self,
        manifests: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        return self._canonical_dict(self._merge_cross_domain_manifests(manifests))

    def _v4_snapshot_union(
        self,
        manifests: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        identity_fields = {
            "financial_facts": "financial_fact_id",
            "market_bars": "price_bar_id",
            "benchmark_bars": "benchmark_bar_id",
            "corporate_actions": "corporate_action_id",
            "announcements": "announcement_id",
            "documents": "document_id",
            "document_assets": "document_asset_id",
            "text_extractions": "text_extraction_id",
            "business_events": "business_event_id",
            "business_event_evidence": "business_event_evidence_id",
            "quantitative_derivations": "quantitative_derivation_id",
            "quantitative_facts": "quantitative_fact_id",
        }
        merged: dict[str, dict[str, dict[str, object]]] = {
            category: {} for category in identity_fields
        }
        algorithms: dict[tuple[str, str], dict[str, object]] = {}
        component_algorithms: dict[str, dict[str, object]] = {}
        for manifest in manifests:
            component_code = manifest.get("component_code")
            component_version = manifest.get("component_algorithm_version")
            if isinstance(component_code, str) and isinstance(component_version, str):
                component_algorithms[component_code] = {
                    "component_code": component_code,
                    "algorithm_version": component_version,
                }
            for category, identity_field in identity_fields.items():
                for item in cast(list[dict[str, object]], manifest.get(category, [])):
                    merged[category][str(item[identity_field])] = item
            for item in cast(
                list[dict[str, object]], manifest.get("phase3_algorithm_versions", [])
            ):
                key = (str(item["evidence_type"]), str(item["algorithm_version"]))
                algorithms[key] = item
        result: dict[str, object] = {
            category: [merged[category][key] for key in sorted(merged[category])]
            for category in identity_fields
        }
        result.update(
            {
                "component_algorithm_versions": [
                    component_algorithms[key] for key in sorted(component_algorithms)
                ],
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [algorithms[key] for key in sorted(algorithms)],
            }
        )
        return self._canonical_dict(result)

    @staticmethod
    def _merge_cross_domain_manifests(
        manifests: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        identity_fields = {
            "financial_facts": "financial_fact_id",
            "market_bars": "price_bar_id",
            "benchmark_bars": "benchmark_bar_id",
            "corporate_actions": "corporate_action_id",
        }
        merged: dict[str, dict[str, dict[str, object]]] = {
            category: {} for category in identity_fields
        }
        algorithms: dict[tuple[str, str], dict[str, object]] = {}
        component_algorithms: dict[str, dict[str, object]] = {}
        for manifest in manifests:
            component_code = manifest.get("component_code")
            component_version = manifest.get("component_algorithm_version")
            if isinstance(component_code, str) and isinstance(component_version, str):
                component_algorithms[component_code] = {
                    "component_code": component_code,
                    "algorithm_version": component_version,
                }
            for category, identity_field in identity_fields.items():
                for item in cast(list[dict[str, object]], manifest.get(category, [])):
                    merged[category][cast(str, item[identity_field])] = item
            for item in cast(
                list[dict[str, object]], manifest.get("phase3_algorithm_versions", [])
            ):
                key = (
                    cast(str, item["evidence_type"]),
                    cast(str, item["algorithm_version"]),
                )
                algorithms[key] = item
        result: dict[str, object] = {
            category: [merged[category][key] for key in sorted(merged[category])]
            for category in identity_fields
        }
        result.update(
            {
                "component_algorithm_versions": [
                    component_algorithms[key] for key in sorted(component_algorithms)
                ],
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [algorithms[key] for key in sorted(algorithms)],
            }
        )
        return result

    def _union_manifests(
        self,
        manifests: tuple[dict[str, object], ...],
        component: FinancialInflectionComponentScore,
    ) -> dict[str, object]:
        facts: dict[str, object] = {}
        algorithms: dict[tuple[str, str], dict[str, object]] = {}
        for manifest in manifests:
            for fact_value in cast(list[dict[str, object]], manifest["facts"]):
                facts[cast(str, fact_value["financial_fact_id"])] = fact_value
            for algorithm in cast(list[dict[str, object]], manifest["phase3_algorithm_versions"]):
                key = (
                    cast(str, algorithm["evidence_type"]),
                    cast(str, algorithm["algorithm_version"]),
                )
                algorithms[key] = algorithm
        return self._canonical_dict(
            {
                "component_code": "financial_inflection",
                "component_algorithm_version": component.algorithm_version,
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [algorithms[key] for key in sorted(algorithms)],
                "facts": [facts[key] for key in sorted(facts)],
            }
        )

    def _generic_union_manifests(
        self,
        manifests: tuple[dict[str, object], ...],
        *,
        component_code: str,
        component_algorithm_version: str,
    ) -> dict[str, object]:
        facts: dict[str, object] = {}
        algorithms: dict[tuple[str, str], dict[str, object]] = {}
        for manifest in manifests:
            for fact_value in cast(list[dict[str, object]], manifest["facts"]):
                facts[cast(str, fact_value["financial_fact_id"])] = fact_value
            for algorithm in cast(list[dict[str, object]], manifest["phase3_algorithm_versions"]):
                key = (
                    cast(str, algorithm["evidence_type"]),
                    cast(str, algorithm["algorithm_version"]),
                )
                algorithms[key] = algorithm
        return self._canonical_dict(
            {
                "component_code": component_code,
                "component_algorithm_version": component_algorithm_version,
                "curve_algorithm_version": PIECEWISE_LINEAR_CURVE_VERSION,
                "phase3_algorithm_versions": [algorithms[key] for key in sorted(algorithms)],
                "facts": [facts[key] for key in sorted(facts)],
            }
        )

    @staticmethod
    def _selection_mapping(selection: FinancialContextSelection) -> dict[str, object]:
        return {
            "provider_dataset_id": selection.provider_dataset_id,
            "filing_scope": selection.filing_scope,
            "provider_priority_index": selection.provider_priority_index,
            "scope_priority_index": selection.scope_priority_index,
            "fallback_used": selection.fallback_used,
            "selection_reason": selection.selection_reason,
        }

    @staticmethod
    def _canonical_mapping(value: object) -> dict[str, object]:
        assert is_dataclass(value) and not isinstance(value, type)
        raw = {field.name: getattr(value, field.name) for field in fields(value)}
        return ScoreSnapshotOrchestrator._canonical_dict(raw)

    @staticmethod
    def _canonical_dict(value: object) -> dict[str, object]:
        canonical = canonical_audit_value(value)
        assert isinstance(canonical, dict)
        return cast(dict[str, object], canonical)

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)

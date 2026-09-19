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
from inflector_data.pit import PointInTimeFinancialFact
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
EXPLANATION_TEMPLATE_CODE = "financial_inflection_subfactor_v1"
IMPLEMENTED_FINANCIAL_COMPONENT_ORDER = (
    "financial_inflection",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
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
}
COMPONENT_SUBFACTOR_ORDERS = {
    "financial_inflection": SUBFACTOR_ORDER,
    "business_quality": BUSINESS_QUALITY_SUBFACTOR_ORDER,
    "cash_flow_quality": CASH_FLOW_QUALITY_SUBFACTOR_ORDER,
    "balance_sheet": BALANCE_SHEET_SUBFACTOR_ORDER,
}


@dataclass(frozen=True, slots=True)
class FinancialComponentContextCandidate:
    provider_dataset_id: UUID
    filing_scope: str
    financial_inflection: FinancialInflectionEvidence | None = None
    business_quality: BusinessQualityEvidence | None = None
    cash_flow_quality: CashFlowQualityEvidence | None = None
    balance_sheet: BalanceSheetEvidence | None = None


class NoActiveScoringConfigurationError(RuntimeError):
    """Raised when orchestration has no active persisted policy at the cutoff."""


class ScoreSnapshotOrchestrator:
    """Resolve policy and persist auditable, non-final v1 or v2 snapshots."""

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

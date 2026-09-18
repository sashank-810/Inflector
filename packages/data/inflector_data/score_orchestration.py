"""Phase 4C orchestration for immutable partial scoring snapshots."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

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
EXPLANATION_TEMPLATE_CODE = "financial_inflection_subfactor_v1"
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


class NoActiveScoringConfigurationError(RuntimeError):
    """Raised when orchestration has no active persisted policy at the cutoff."""


class ScoreSnapshotOrchestrator:
    """Resolve policy and persist an auditable, non-final Phase 4C snapshot."""

    def __init__(
        self,
        policy_repository: ScoringPolicyRepository,
        snapshot_repository: ScoreSnapshotRepository,
        context_resolver: FinancialContextPolicyResolver,
        eligibility_evaluator: EligibilityEvaluator,
        confidence_evaluator: ConfidenceEvaluator,
        component_scorer: FinancialInflectionComponentScorer,
    ) -> None:
        self._policies = policy_repository
        self._snapshots = snapshot_repository
        self._contexts = context_resolver
        self._eligibility = eligibility_evaluator
        self._confidence = confidence_evaluator
        self._component_scorer = component_scorer

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
        if self._aware_utc(
            confidence_inputs.knowledge_cutoff, "confidence knowledge_cutoff"
        ) != cutoff:
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
                    "component_score_available": result is not None
                    and result.score is not None,
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
            for algorithm in cast(
                list[dict[str, object]], manifest["phase3_algorithm_versions"]
            ):
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

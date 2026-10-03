"""Freeze and rank current accepted V5 snapshots without recomputing research."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.scoring_policy import scoring_policy_checksum
from inflector_data.opportunity_policy import (
    OpportunityDiscoveryPolicy,
    canonical_json_sha256,
)
from inflector_data.production_policy import bind_production_policy
from inflector_data.research_profile import ProductionResearchProfile
from inflector_database.models import (
    Company,
    ExchangeListing,
    OpportunityDiscoveryRun,
    ScoreSnapshot,
    Security,
)
from inflector_database.opportunity_repository import (
    OpportunityDiscoveryItemWrite,
    OpportunityDiscoveryRepository,
)
from inflector_database.score_repository import (
    ScoreSnapshotIntegrityError,
    ScoreSnapshotRepository,
)
from inflector_database.scoring_repository import ScoringPolicyRepository


@dataclass(frozen=True, slots=True)
class DiscoveryDecision:
    symbol: str
    company_id: UUID | None
    security_id: UUID | None
    snapshot: ScoreSnapshot | None
    rankable: bool
    unranked_reason: str | None
    snapshot_age_days: int | None
    freshness_state: str
    score_rank: int | None = None
    display_order: int | None = None
    detail: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class OpportunityDiscoveryBuildResult:
    run_id: UUID
    run_key_sha256: str
    created_run: bool
    already_completed: bool
    requested_securities: int
    rankable_securities: int
    unranked_securities: int
    selected_snapshot_set_checksum_sha256: str


def normalize_discovery_symbols(
    values: list[str] | tuple[str, ...], *, maximum: int
) -> tuple[str, ...]:
    if maximum < 1 or maximum > 5000:
        raise ValueError("discovery maximum symbols must be between 1 and 5000")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        symbol = raw.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    if not result:
        raise ValueError("at least one discovery symbol is required")
    if len(result) > maximum:
        raise ValueError("discovery symbol input exceeds policy maximum")
    return tuple(result)


def symbol_set_checksum(symbols: tuple[str, ...]) -> str:
    return canonical_json_sha256(list(symbols))


def selected_snapshot_set_checksum(decisions: tuple[DiscoveryDecision, ...]) -> str:
    return canonical_json_sha256([_decision_identity(item) for item in decisions])


def select_current_snapshots(
    session: Session,
    *,
    symbols: tuple[str, ...],
    discovery_cutoff: datetime,
    scoring_configuration_id: UUID,
    scoring_configuration_checksum_sha256: str,
    maximum_snapshot_age_days: int,
    unranked_reason_priority: tuple[str, ...],
) -> tuple[DiscoveryDecision, ...]:
    """Select one bound latest snapshot per exact current symbol and classify it."""

    cutoff = _utc(discovery_cutoff, "discovery_cutoff")
    validator = ScoreSnapshotRepository(session)
    decisions: list[DiscoveryDecision] = []
    for symbol in symbols:
        identities = list(
            session.execute(
                select(Company, Security, ExchangeListing)
                .join(Security, Security.company_id == Company.id)
                .join(ExchangeListing, ExchangeListing.security_id == Security.id)
                .where(
                    ExchangeListing.exchange == "NSE",
                    ExchangeListing.symbol == symbol,
                    ExchangeListing.valid_to.is_(None),
                )
            )
        )
        if len(identities) != 1:
            decisions.append(
                DiscoveryDecision(
                    symbol=symbol,
                    company_id=None,
                    security_id=None,
                    snapshot=None,
                    rankable=False,
                    unranked_reason="identity_unavailable",
                    snapshot_age_days=None,
                    freshness_state="unavailable",
                    detail={"identity_match_count": len(identities)},
                )
            )
            continue
        company, security, _ = identities[0]
        visible = list(
            session.scalars(
                select(ScoreSnapshot)
                .where(
                    ScoreSnapshot.selected_security_id == security.id,
                    ScoreSnapshot.knowledge_cutoff <= cutoff,
                )
                .order_by(ScoreSnapshot.knowledge_cutoff.desc())
            )
        )
        if not visible:
            decisions.append(
                _unranked(symbol, company.id, security.id, "no_visible_snapshot")
            )
            continue
        bound = [
            snapshot
            for snapshot in visible
            if snapshot.scoring_configuration_id == scoring_configuration_id
            and snapshot.configuration_checksum_sha256
            == scoring_configuration_checksum_sha256
            and snapshot.algorithm_version == "score_snapshot_v5"
        ]
        if not bound:
            decisions.append(
                _unranked(
                    symbol,
                    company.id,
                    security.id,
                    "configuration_mismatch",
                    detail={"visible_snapshot_count": len(visible)},
                )
            )
            continue
        latest_cutoff = max(_stored_utc(item.knowledge_cutoff) for item in bound)
        latest = [item for item in bound if _stored_utc(item.knowledge_cutoff) == latest_cutoff]
        fingerprints = {item.snapshot_fingerprint_sha256 for item in latest}
        if len(fingerprints) != 1:
            decisions.append(
                _unranked(
                    symbol,
                    company.id,
                    security.id,
                    "ambiguous_latest_snapshot",
                    detail={
                        "candidate_snapshot_ids": sorted(str(item.id) for item in latest),
                        "candidate_snapshot_fingerprints": sorted(fingerprints),
                        "latest_knowledge_cutoff": latest_cutoff.isoformat(),
                    },
                )
            )
            continue
        snapshot = sorted(latest, key=lambda item: str(item.id))[0]
        age_days = (cutoff.date() - latest_cutoff.date()).days
        try:
            validator.validate_persisted_snapshot(snapshot)
        except ScoreSnapshotIntegrityError as error:
            decisions.append(
                DiscoveryDecision(
                    symbol=symbol,
                    company_id=company.id,
                    security_id=security.id,
                    snapshot=snapshot,
                    rankable=False,
                    unranked_reason="invalid_snapshot_state",
                    snapshot_age_days=age_days,
                    freshness_state=(
                        "fresh" if age_days <= maximum_snapshot_age_days else "stale"
                    ),
                    detail={"integrity_error": str(error)},
                )
            )
            continue
        freshness = "fresh" if age_days <= maximum_snapshot_age_days else "stale"
        reason_candidates = {
            "stale_snapshot": freshness == "stale",
            "v5_ineligible": not snapshot.eligibility_eligible,
            "partial_score": snapshot.final_score is None,
            "invalid_snapshot_state": (
                snapshot.final_score is not None
                and snapshot.snapshot_status != "final_score_available"
            ),
        }
        reason = next(
            (
                candidate
                for candidate in unranked_reason_priority
                if reason_candidates.get(candidate, False)
            ),
            None,
        )
        decisions.append(
            DiscoveryDecision(
                symbol=symbol,
                company_id=company.id,
                security_id=security.id,
                snapshot=snapshot,
                rankable=reason is None,
                unranked_reason=reason,
                snapshot_age_days=age_days,
                freshness_state=freshness,
                detail={},
            )
        )
    return assign_dense_score_ranks(tuple(decisions))


def assign_dense_score_ranks(
    decisions: tuple[DiscoveryDecision, ...],
) -> tuple[DiscoveryDecision, ...]:
    """Assign dense merit rank by final_score only; ties use display ordering only."""

    ranked = sorted(
        (item for item in decisions if item.rankable),
        key=lambda item: (
            -_required_score(item),
            item.symbol,
            str(item.security_id),
        ),
    )
    assigned: dict[str, DiscoveryDecision] = {}
    previous_score: Decimal | None = None
    dense_rank = 0
    for display_order, item in enumerate(ranked, start=1):
        score = _required_score(item)
        if previous_score is None or score != previous_score:
            dense_rank += 1
            previous_score = score
        assigned[item.symbol] = replace(
            item,
            score_rank=dense_rank,
            display_order=display_order,
        )
    return tuple(assigned.get(item.symbol, item) for item in decisions)


def build_opportunity_discovery(
    session: Session,
    *,
    policy: OpportunityDiscoveryPolicy,
    research_profile: ProductionResearchProfile,
    model_family: str,
    symbols: tuple[str, ...],
    discovery_cutoff: datetime,
    repository_root: Path,
) -> OpportunityDiscoveryBuildResult:
    """Freeze current snapshot choices, rank complete V5 states, and persist once."""

    cutoff = _utc(discovery_cutoff, "discovery_cutoff")
    _validate_profile(policy, research_profile)
    resolved = ScoringPolicyRepository(session).resolve_active_configuration(
        model_family=model_family,
        at=cutoff,
    )
    if resolved is None:
        raise ValueError("active scoring configuration is unavailable at discovery cutoff")
    expected = bind_production_policy(session, research_profile, repository_root=repository_root)
    if (
        resolved.record.configuration_name != research_profile.research_profile_code
        or resolved.record.configuration_version != research_profile.profile_version
        or resolved.record.checksum_sha256 != scoring_policy_checksum(expected.policy)
    ):
        raise ValueError("active scoring configuration does not match bound Research V4 policy")
    decisions = select_current_snapshots(
        session,
        symbols=symbols,
        discovery_cutoff=cutoff,
        scoring_configuration_id=resolved.record.id,
        scoring_configuration_checksum_sha256=resolved.record.checksum_sha256,
        maximum_snapshot_age_days=policy.maximum_snapshot_age_days,
        unranked_reason_priority=policy.unranked_reason_priority,
    )
    snapshot_set_checksum = selected_snapshot_set_checksum(decisions)
    inputs: dict[str, object] = {
        "discovery_policy_checksum": policy.checksum_sha256,
        "scoring_configuration_id": str(resolved.record.id),
        "scoring_configuration_checksum": resolved.record.checksum_sha256,
        "research_profile_checksum": research_profile.checksum_sha256,
        "financial_primitive_policy_checksum": (
            policy.financial_primitive_policy_checksum_sha256
        ),
        "financial_endpoint_policy_checksum": (
            policy.financial_endpoint_policy_checksum_sha256
        ),
        "model_family": model_family,
        "discovery_cutoff": cutoff.isoformat(),
        "universe_mode": policy.universe_mode,
        "ordered_symbols": list(symbols),
        "symbol_set_checksum": symbol_set_checksum(symbols),
        "selected_snapshot_set_checksum": snapshot_set_checksum,
        "snapshot_selection_version": policy.snapshot_selection_version,
        "ranking_version": policy.ranking_version,
    }
    run_key = canonical_json_sha256(inputs)
    repository = OpportunityDiscoveryRepository(session)
    run, created = repository.create_run(
        run_key_sha256=run_key,
        discovery_policy_code=policy.code,
        discovery_policy_checksum_sha256=policy.checksum_sha256,
        scoring_configuration_id=resolved.record.id,
        scoring_configuration_checksum_sha256=resolved.record.checksum_sha256,
        research_profile_code=research_profile.research_profile_code,
        research_profile_checksum_sha256=research_profile.checksum_sha256,
        financial_primitive_policy_checksum_sha256=(
            policy.financial_primitive_policy_checksum_sha256
        ),
        financial_endpoint_policy_checksum_sha256=(
            policy.financial_endpoint_policy_checksum_sha256
        ),
        model_family=model_family,
        discovery_cutoff=cutoff,
        universe_mode=policy.universe_mode,
        ordered_symbols=symbols,
        symbol_set_checksum_sha256=symbol_set_checksum(symbols),
        selected_snapshot_set_checksum_sha256=snapshot_set_checksum,
        snapshot_selection_version=policy.snapshot_selection_version,
        ranking_version=policy.ranking_version,
        inputs_json=inputs,
    )
    if not created and run.status == "completed":
        return _build_result(run, created=False, already_completed=True)
    for decision in decisions:
        repository.add_item(
            run,
            OpportunityDiscoveryItemWrite(
                company_id=decision.company_id,
                security_id=decision.security_id,
                score_snapshot_id=(decision.snapshot.id if decision.snapshot is not None else None),
                symbol=decision.symbol,
                rankable=decision.rankable,
                unranked_reason=decision.unranked_reason,
                score_rank=decision.score_rank,
                display_order=decision.display_order,
                snapshot_age_days=decision.snapshot_age_days,
                freshness_state=decision.freshness_state,
                selected_snapshot_fingerprint_sha256=(
                    decision.snapshot.snapshot_fingerprint_sha256
                    if decision.snapshot is not None
                    else None
                ),
                detail_json=decision.detail or {},
            ),
        )
    summary = discovery_summary(decisions)
    repository.complete(run, summary=summary)
    session.commit()
    return _build_result(run, created=created, already_completed=False)


def discovery_summary(decisions: tuple[DiscoveryDecision, ...]) -> dict[str, object]:
    ranked = [item for item in decisions if item.rankable]
    scores = [_required_score(item) for item in ranked]
    reasons: dict[str, int] = {}
    components: dict[str, int] = {}
    for item in decisions:
        if item.unranked_reason is not None:
            reasons[item.unranked_reason] = reasons.get(item.unranked_reason, 0) + 1
        if item.snapshot is not None:
            for code in item.snapshot.available_component_codes_json:
                text = str(code)
                components[text] = components.get(text, 0) + 1
    score_distribution: dict[str, str] | None = None
    if scores:
        score_distribution = {
            "minimum": str(min(scores)),
            "median": str(median(scores)),
            "mean": str(sum(scores, Decimal("0")) / Decimal(len(scores))),
            "maximum": str(max(scores)),
        }
    return {
        "requested_securities": len(decisions),
        "securities_with_snapshots": sum(item.snapshot is not None for item in decisions),
        "fresh_snapshots": sum(item.freshness_state == "fresh" for item in decisions),
        "stale_snapshots": sum(item.freshness_state == "stale" for item in decisions),
        "rankable_securities": len(ranked),
        "unranked_securities": len(decisions) - len(ranked),
        "unranked_reason_counts": dict(sorted(reasons.items())),
        "component_availability_counts": dict(sorted(components.items())),
        "score_distribution": score_distribution,
        "ranking_metric": "persisted_v5_final_score_only",
    }


def _validate_profile(
    policy: OpportunityDiscoveryPolicy, profile: ProductionResearchProfile
) -> None:
    if profile.research_profile_code != "nse_current_research_v4":
        raise ValueError("Production K requires accepted Research V4")
    if profile.checksum_sha256 != policy.research_profile_checksum_sha256:
        raise ValueError("research profile checksum does not match discovery policy")
    if (
        profile.financial_primitive_policy_checksum_sha256
        != policy.financial_primitive_policy_checksum_sha256
        or profile.financial_endpoint_policy_checksum_sha256
        != policy.financial_endpoint_policy_checksum_sha256
    ):
        raise ValueError("research semantic policy checksums do not match discovery policy")
    if profile.scoring_policy_asset != policy.scoring_policy_asset:
        raise ValueError("research scoring asset does not match discovery policy")


def _unranked(
    symbol: str,
    company_id: UUID,
    security_id: UUID,
    reason: str,
    *,
    detail: dict[str, object] | None = None,
) -> DiscoveryDecision:
    return DiscoveryDecision(
        symbol=symbol,
        company_id=company_id,
        security_id=security_id,
        snapshot=None,
        rankable=False,
        unranked_reason=reason,
        snapshot_age_days=None,
        freshness_state="unavailable",
        detail=detail or {},
    )


def _decision_identity(item: DiscoveryDecision) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "company_id": str(item.company_id) if item.company_id is not None else None,
        "security_id": str(item.security_id) if item.security_id is not None else None,
        "snapshot_id": str(item.snapshot.id) if item.snapshot is not None else None,
        "snapshot_fingerprint": (
            item.snapshot.snapshot_fingerprint_sha256 if item.snapshot is not None else None
        ),
        "snapshot_knowledge_cutoff": (
            _stored_utc(item.snapshot.knowledge_cutoff).isoformat()
            if item.snapshot is not None
            else None
        ),
        "rankable": item.rankable,
        "unranked_reason": item.unranked_reason,
        "score_rank": item.score_rank,
        "display_order": item.display_order,
        "detail": item.detail or {},
    }


def _required_score(item: DiscoveryDecision) -> Decimal:
    if item.snapshot is None or item.snapshot.final_score is None:
        raise ValueError("rankable discovery item requires persisted final_score")
    return item.snapshot.final_score


def _build_result(
    run: OpportunityDiscoveryRun, *, created: bool, already_completed: bool
) -> OpportunityDiscoveryBuildResult:
    return OpportunityDiscoveryBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        created_run=created,
        already_completed=already_completed,
        requested_securities=_summary_count(run, "requested_securities"),
        rankable_securities=_summary_count(run, "rankable_securities"),
        unranked_securities=_summary_count(run, "unranked_securities"),
        selected_snapshot_set_checksum_sha256=run.selected_snapshot_set_checksum_sha256,
    )


def _summary_count(run: OpportunityDiscoveryRun, field: str) -> int:
    value = run.summary_json.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"opportunity discovery summary {field} is invalid")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

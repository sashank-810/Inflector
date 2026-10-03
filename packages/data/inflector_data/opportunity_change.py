"""Deterministically compare two frozen Production K discovery runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.opportunity_change_policy import (
    OpportunityChangePolicy,
    canonical_json_sha256,
)
from inflector_database.models import (
    OpportunityChangeRun,
    OpportunityDiscoveryItem,
    OpportunityDiscoveryRun,
    ScoreComponent,
)
from inflector_database.opportunity_change_repository import (
    OpportunityChangeItemWrite,
    OpportunityChangeRepository,
)
from inflector_database.opportunity_repository import OpportunityDiscoveryRepository


class IncompatibleDiscoveryRunsError(ValueError):
    """Raised before comparison when the two K runs have different semantics."""

    status = "incompatible_runs"


@dataclass(frozen=True, slots=True)
class OpportunityChangeBuildResult:
    run_id: UUID
    run_key_sha256: str
    created_run: bool
    already_completed: bool
    compared_identities: int
    changed_identities: int


def build_opportunity_change(
    session: Session,
    *,
    policy: OpportunityChangePolicy,
    baseline_run_id: UUID,
    current_run_id: UUID,
) -> OpportunityChangeBuildResult:
    discovery_repository = OpportunityDiscoveryRepository(session)
    baseline = discovery_repository.get_run(baseline_run_id)
    current = discovery_repository.get_run(current_run_id)
    if baseline is None or current is None:
        raise ValueError("baseline and current discovery runs must exist")
    _validate_runs(policy, baseline, current)
    decisions = compare_discovery_items(policy, baseline.items, current.items)
    inputs: dict[str, object] = {
        "change_policy_checksum": policy.checksum_sha256,
        "baseline_discovery_run_id": str(baseline.id),
        "current_discovery_run_id": str(current.id),
        "baseline_run_key": baseline.run_key_sha256,
        "current_run_key": current.run_key_sha256,
        "baseline_snapshot_set_checksum": (
            baseline.selected_snapshot_set_checksum_sha256
        ),
        "current_snapshot_set_checksum": current.selected_snapshot_set_checksum_sha256,
        "comparison_version": policy.comparison_version,
    }
    run_key = canonical_json_sha256(inputs)
    repository = OpportunityChangeRepository(session)
    run, created = repository.create_run(
        run_key_sha256=run_key,
        change_policy_code=policy.code,
        change_policy_checksum_sha256=policy.checksum_sha256,
        baseline_discovery_run_id=baseline.id,
        current_discovery_run_id=current.id,
        baseline_run_key_sha256=baseline.run_key_sha256,
        current_run_key_sha256=current.run_key_sha256,
        baseline_snapshot_set_checksum_sha256=(
            baseline.selected_snapshot_set_checksum_sha256
        ),
        current_snapshot_set_checksum_sha256=(
            current.selected_snapshot_set_checksum_sha256
        ),
        comparison_version=policy.comparison_version,
        inputs_json=inputs,
    )
    if not created and run.status == "completed":
        return _build_result(run, created=False, already_completed=True)
    for decision in decisions:
        repository.add_item(run, decision)
    repository.complete(run, summary=_summary(baseline, current, decisions))
    session.commit()
    return _build_result(run, created=created, already_completed=False)


def compare_discovery_items(
    policy: OpportunityChangePolicy,
    baseline_items: list[OpportunityDiscoveryItem],
    current_items: list[OpportunityDiscoveryItem],
) -> tuple[OpportunityChangeItemWrite, ...]:
    pairs = _match_items(baseline_items, current_items)
    return tuple(
        _compare_item(policy, identity_key, before, after)
        for identity_key, before, after in sorted(pairs, key=lambda value: value[0])
    )


def _compare_item(
    policy: OpportunityChangePolicy,
    identity_key: str,
    baseline: OpportunityDiscoveryItem | None,
    current: OpportunityDiscoveryItem | None,
) -> OpportunityChangeItemWrite:
    if baseline is None or current is None:
        item = current if current is not None else baseline
        if item is None:  # pragma: no cover - guarded by set union
            raise AssertionError("comparison identity has no item")
        code = "entered_compared_universe" if baseline is None else "left_compared_universe"
        ordered_codes = _ordered_codes(policy, {code})
        return _write(
            identity_key,
            baseline,
            current,
            ordered_codes,
            components_gained=[],
            components_lost=[],
            component_detail={},
            detail={"universe_membership": code},
            policy=policy,
        )

    codes: set[str] = set()
    if baseline.symbol != current.symbol:
        codes.add("symbol_changed")
    snapshot_unchanged = (
        baseline.score_snapshot_id == current.score_snapshot_id
        and baseline.selected_snapshot_fingerprint_sha256
        == current.selected_snapshot_fingerprint_sha256
    )
    codes.add("snapshot_unchanged" if snapshot_unchanged else "snapshot_changed")

    if baseline.rankable and current.rankable:
        codes.add("remained_rankable")
    elif not baseline.rankable and current.rankable:
        codes.add("became_rankable")
    elif baseline.rankable and not current.rankable:
        codes.add("lost_rankability")
    else:
        codes.add("remained_unranked")
        if baseline.unranked_reason != current.unranked_reason:
            codes.add("unranked_reason_changed")

    before_score = _score(baseline)
    after_score = _score(current)
    score_delta: Decimal | None = None
    rank_delta: int | None = None
    if baseline.rankable and current.rankable:
        if before_score is None or after_score is None:
            raise ValueError("rankable K items must reference persisted final scores")
        score_delta = after_score - before_score
        codes.add(
            "score_increased"
            if score_delta > 0
            else "score_decreased"
            if score_delta < 0
            else "score_unchanged"
        )
        if baseline.score_rank is None or current.score_rank is None:
            raise ValueError("rankable K items must persist dense score ranks")
        rank_delta = baseline.score_rank - current.score_rank
        codes.add(
            "rank_moved_up"
            if rank_delta > 0
            else "rank_moved_down"
            if rank_delta < 0
            else "rank_unchanged"
        )

    if _is_partial(baseline) and current.rankable and not _is_partial(current):
        codes.add("coverage_completed")
    if baseline.rankable and not _is_partial(baseline) and _is_partial(current):
        codes.add("coverage_regressed")
    if baseline.freshness_state != "stale" and current.freshness_state == "stale":
        codes.add("became_stale")
    if baseline.freshness_state == "stale" and current.freshness_state != "stale":
        codes.add("recovered_from_stale")

    before_snapshot = baseline.score_snapshot
    after_snapshot = current.score_snapshot
    before_eligible = before_snapshot.eligibility_eligible if before_snapshot else None
    after_eligible = after_snapshot.eligibility_eligible if after_snapshot else None
    if before_eligible is not None and after_eligible is not None:
        if before_eligible and not after_eligible:
            codes.add("became_ineligible")
        elif not before_eligible and after_eligible:
            codes.add("became_eligible")
        else:
            codes.add("eligibility_unchanged")

    before_confidence = before_snapshot.confidence if before_snapshot else None
    after_confidence = after_snapshot.confidence if after_snapshot else None
    confidence_delta = _decimal_delta(before_confidence, after_confidence)
    if confidence_delta is not None and confidence_delta != 0:
        codes.add("confidence_changed")

    before_coverage = (
        before_snapshot.top_level_component_weight_coverage if before_snapshot else None
    )
    after_coverage = (
        after_snapshot.top_level_component_weight_coverage if after_snapshot else None
    )
    coverage_delta = _decimal_delta(before_coverage, after_coverage)
    if coverage_delta is not None and coverage_delta != 0:
        codes.add("coverage_changed")

    before_components = _component_map(baseline)
    after_components = _component_map(current)
    before_available = _available_component_codes(baseline)
    after_available = _available_component_codes(current)
    gained = sorted(after_available - before_available)
    lost = sorted(before_available - after_available)
    if gained:
        codes.add("components_gained")
    if lost:
        codes.add("components_lost")
    component_detail: dict[str, object] = {}
    score_changed = False
    contribution_changed = False
    for component_code in sorted(set(before_components) & set(after_components)):
        before_component = before_components[component_code]
        after_component = after_components[component_code]
        component_score_delta = _decimal_delta(
            before_component.score, after_component.score
        )
        contribution_delta = (
            _decimal_delta(
                before_component.final_contribution,
                after_component.final_contribution,
            )
            if baseline.rankable and current.rankable
            else None
        )
        if component_score_delta not in (None, Decimal("0")):
            score_changed = True
        if contribution_delta not in (None, Decimal("0")):
            contribution_changed = True
        if component_score_delta not in (None, Decimal("0")) or contribution_delta not in (
            None,
            Decimal("0"),
        ):
            component_detail[component_code] = {
                "baseline_score": _decimal_text(before_component.score),
                "current_score": _decimal_text(after_component.score),
                "component_score_delta": _decimal_text(component_score_delta),
                "baseline_final_contribution": _decimal_text(
                    before_component.final_contribution
                ),
                "current_final_contribution": _decimal_text(
                    after_component.final_contribution
                ),
                "component_contribution_delta": _decimal_text(contribution_delta),
            }
    if score_changed:
        codes.add("component_scores_changed")
    if contribution_changed:
        codes.add("component_contributions_changed")

    ordered = _ordered_codes(policy, codes)
    return _write(
        identity_key,
        baseline,
        current,
        ordered,
        components_gained=gained,
        components_lost=lost,
        component_detail=component_detail,
        detail={
            "baseline_snapshot_fingerprint": (
                baseline.selected_snapshot_fingerprint_sha256
            ),
            "current_snapshot_fingerprint": current.selected_snapshot_fingerprint_sha256,
            "baseline_eligible": before_eligible,
            "current_eligible": after_eligible,
            "baseline_freshness_state": baseline.freshness_state,
            "current_freshness_state": current.freshness_state,
            "baseline_component_weight_coverage": _decimal_text(before_coverage),
            "current_component_weight_coverage": _decimal_text(after_coverage),
            "component_weight_coverage_delta": _decimal_text(coverage_delta),
        },
        policy=policy,
        score_delta=score_delta,
        rank_delta=rank_delta,
        confidence_delta=confidence_delta,
    )


def _write(
    identity_key: str,
    baseline: OpportunityDiscoveryItem | None,
    current: OpportunityDiscoveryItem | None,
    codes: tuple[str, ...],
    *,
    components_gained: list[str],
    components_lost: list[str],
    component_detail: dict[str, object],
    detail: dict[str, object],
    policy: OpportunityChangePolicy,
    score_delta: Decimal | None = None,
    rank_delta: int | None = None,
    confidence_delta: Decimal | None = None,
) -> OpportunityChangeItemWrite:
    item = current if current is not None else baseline
    if item is None:  # pragma: no cover - caller invariant
        raise AssertionError("change item requires at least one K item")
    before_snapshot = baseline.score_snapshot if baseline else None
    after_snapshot = current.score_snapshot if current else None
    return OpportunityChangeItemWrite(
        identity_key=identity_key,
        identity_basis="security_id" if identity_key.startswith("security:") else "symbol",
        company_id=item.company_id,
        security_id=item.security_id,
        baseline_symbol=baseline.symbol if baseline else None,
        current_symbol=current.symbol if current else None,
        baseline_discovery_item_id=baseline.id if baseline else None,
        current_discovery_item_id=current.id if current else None,
        baseline_score_snapshot_id=baseline.score_snapshot_id if baseline else None,
        current_score_snapshot_id=current.score_snapshot_id if current else None,
        change_codes_json=list(codes),
        changed=any(code not in policy.neutral_change_codes for code in codes),
        baseline_rankable=baseline.rankable if baseline else None,
        current_rankable=current.rankable if current else None,
        baseline_unranked_reason=baseline.unranked_reason if baseline else None,
        current_unranked_reason=current.unranked_reason if current else None,
        baseline_final_score=before_snapshot.final_score if before_snapshot else None,
        current_final_score=after_snapshot.final_score if after_snapshot else None,
        score_delta=score_delta,
        baseline_score_rank=baseline.score_rank if baseline else None,
        current_score_rank=current.score_rank if current else None,
        rank_delta=rank_delta,
        baseline_confidence=before_snapshot.confidence if before_snapshot else None,
        current_confidence=after_snapshot.confidence if after_snapshot else None,
        confidence_delta=confidence_delta,
        components_gained_json=list(components_gained),
        components_lost_json=list(components_lost),
        component_change_detail_json=component_detail,
        detail_json=detail,
    )


def _validate_runs(
    policy: OpportunityChangePolicy,
    baseline: OpportunityDiscoveryRun,
    current: OpportunityDiscoveryRun,
) -> None:
    if baseline.status != "completed" or current.status != "completed":
        raise ValueError("baseline and current discovery runs must be completed")
    if _utc(baseline.discovery_cutoff) >= _utc(current.discovery_cutoff):
        raise ValueError("baseline discovery cutoff must precede current cutoff")
    if (
        baseline.discovery_policy_code != policy.discovery_policy_code
        or current.discovery_policy_code != policy.discovery_policy_code
        or baseline.discovery_policy_checksum_sha256
        != policy.discovery_policy_checksum_sha256
        or current.discovery_policy_checksum_sha256
        != policy.discovery_policy_checksum_sha256
    ):
        raise IncompatibleDiscoveryRunsError(
            "discovery runs do not match the change policy's bound K policy"
        )
    fields = (
        "discovery_policy_checksum_sha256",
        "scoring_configuration_checksum_sha256",
        "research_profile_checksum_sha256",
        "financial_primitive_policy_checksum_sha256",
        "financial_endpoint_policy_checksum_sha256",
        "ranking_version",
        "snapshot_selection_version",
        "model_family",
    )
    mismatches = [field for field in fields if getattr(baseline, field) != getattr(current, field)]
    if mismatches:
        raise IncompatibleDiscoveryRunsError(
            "incompatible discovery run semantics: " + ", ".join(mismatches)
        )


def _match_items(
    baseline_items: list[OpportunityDiscoveryItem],
    current_items: list[OpportunityDiscoveryItem],
) -> list[
    tuple[str, OpportunityDiscoveryItem | None, OpportunityDiscoveryItem | None]
]:
    baseline_security = _security_map(baseline_items)
    current_security = _security_map(current_items)
    pairs: list[
        tuple[str, OpportunityDiscoveryItem | None, OpportunityDiscoveryItem | None]
    ] = []
    matched_baseline: set[UUID] = set()
    matched_current: set[UUID] = set()
    for security_id in sorted(set(baseline_security) & set(current_security), key=str):
        before = baseline_security[security_id]
        after = current_security[security_id]
        pairs.append((f"security:{security_id}", before, after))
        matched_baseline.add(before.id)
        matched_current.add(after.id)

    baseline_remaining = [item for item in baseline_items if item.id not in matched_baseline]
    current_remaining = [item for item in current_items if item.id not in matched_current]
    baseline_symbols = _symbol_map(baseline_remaining)
    current_symbols = _symbol_map(current_remaining)
    for symbol in sorted(set(baseline_symbols) & set(current_symbols)):
        before = baseline_symbols[symbol]
        after = current_symbols[symbol]
        if before.security_id is not None and after.security_id is not None:
            continue
        pairs.append((f"symbol:{symbol}", before, after))
        matched_baseline.add(before.id)
        matched_current.add(after.id)
    for item in baseline_items:
        if item.id not in matched_baseline:
            pairs.append((_single_identity(item), item, None))
    for item in current_items:
        if item.id not in matched_current:
            pairs.append((_single_identity(item), None, item))
    keys = [key for key, _, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("discovery runs contain ambiguous comparison identities")
    return pairs


def _security_map(items: list[OpportunityDiscoveryItem]) -> dict[UUID, OpportunityDiscoveryItem]:
    result: dict[UUID, OpportunityDiscoveryItem] = {}
    for item in items:
        if item.security_id is None:
            continue
        if item.security_id in result:
            raise ValueError("discovery run contains duplicate security identity")
        result[item.security_id] = item
    return result


def _symbol_map(items: list[OpportunityDiscoveryItem]) -> dict[str, OpportunityDiscoveryItem]:
    result: dict[str, OpportunityDiscoveryItem] = {}
    for item in items:
        if item.symbol in result:
            raise ValueError("discovery run contains ambiguous symbol identity")
        result[item.symbol] = item
    return result


def _single_identity(item: OpportunityDiscoveryItem) -> str:
    return (
        f"security:{item.security_id}"
        if item.security_id is not None
        else f"symbol:{item.symbol}"
    )


def _component_map(item: OpportunityDiscoveryItem) -> dict[str, ScoreComponent]:
    snapshot = item.score_snapshot
    if snapshot is None:
        return {}
    return {component.component_code: component for component in snapshot.components}


def _available_component_codes(item: OpportunityDiscoveryItem) -> set[str]:
    snapshot = item.score_snapshot
    if snapshot is None:
        return set()
    return {str(code) for code in snapshot.available_component_codes_json}


def _score(item: OpportunityDiscoveryItem) -> Decimal | None:
    return item.score_snapshot.final_score if item.score_snapshot is not None else None


def _is_partial(item: OpportunityDiscoveryItem) -> bool:
    snapshot = item.score_snapshot
    return snapshot is not None and snapshot.snapshot_status == "partial_component_set"


def _decimal_delta(before: Decimal | None, after: Decimal | None) -> Decimal | None:
    return after - before if before is not None and after is not None else None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _ordered_codes(
    policy: OpportunityChangePolicy, codes: set[str]
) -> tuple[str, ...]:
    unknown = codes - set(policy.change_code_order)
    if unknown:
        raise ValueError(f"unconfigured opportunity change codes: {sorted(unknown)}")
    return tuple(code for code in policy.change_code_order if code in codes)


def _summary(
    baseline: OpportunityDiscoveryRun,
    current: OpportunityDiscoveryRun,
    items: tuple[OpportunityChangeItemWrite, ...],
) -> dict[str, object]:
    counts: dict[str, int] = {}
    for item in items:
        for code in item.change_codes_json:
            text = str(code)
            counts[text] = counts.get(text, 0) + 1
    return {
        "baseline_cutoff": _utc(baseline.discovery_cutoff).isoformat(),
        "current_cutoff": _utc(current.discovery_cutoff).isoformat(),
        "baseline_universe_size": len(baseline.items),
        "current_universe_size": len(current.items),
        "compared_identities": len(items),
        "changed_identities": sum(item.changed for item in items),
        "unchanged_identities": sum(not item.changed for item in items),
        "change_code_counts": dict(sorted(counts.items())),
    }


def _build_result(
    run: OpportunityChangeRun, *, created: bool, already_completed: bool
) -> OpportunityChangeBuildResult:
    compared = run.summary_json.get("compared_identities", 0)
    changed = run.summary_json.get("changed_identities", 0)
    if not isinstance(compared, int) or not isinstance(changed, int):
        raise ValueError("opportunity change summary counts are invalid")
    return OpportunityChangeBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        created_run=created,
        already_completed=already_completed,
        compared_identities=compared,
        changed_identities=changed,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

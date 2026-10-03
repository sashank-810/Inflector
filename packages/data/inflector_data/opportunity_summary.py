"""Mechanical summaries and derivative CSV export for frozen discovery runs."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.opportunity_policy import OpportunityDiscoveryPolicy
from inflector_database.models import (
    Company,
    OpportunityDiscoveryItem,
    OpportunityDiscoveryRun,
    Security,
)
from inflector_database.opportunity_repository import OpportunityDiscoveryRepository


@dataclass(frozen=True, slots=True)
class OpportunitySummaryResult:
    run_id: UUID
    summary: dict[str, object]
    items: tuple[dict[str, object], ...]
    export_path: str | None


def summarize_opportunity_discovery(
    session: Session,
    *,
    run_id: UUID,
    policy: OpportunityDiscoveryPolicy,
    limit: int | None = None,
    export_csv: Path | None = None,
) -> OpportunitySummaryResult:
    if limit is not None and (limit < 1 or limit > policy.maximum_symbols):
        raise ValueError(f"limit must be between 1 and {policy.maximum_symbols}")
    run = OpportunityDiscoveryRepository(session).get_run(run_id)
    if run is None:
        raise ValueError("opportunity discovery run is unavailable")
    if run.status != "completed":
        raise ValueError("opportunity discovery run is not completed")
    if (
        run.discovery_policy_code != policy.code
        or run.discovery_policy_checksum_sha256 != policy.checksum_sha256
    ):
        raise ValueError("opportunity discovery run policy binding conflicts")
    ordered = sorted(
        run.items,
        key=lambda item: (
            0 if item.rankable else 1,
            item.display_order if item.display_order is not None else 0,
            item.symbol,
            str(item.security_id) if item.security_id is not None else "",
        ),
    )
    rows = tuple(_item_row(session, run, item) for item in ordered)
    export_path = None
    if export_csv is not None:
        _export(export_csv, rows)
        export_path = str(export_csv.resolve())
    displayed = rows if limit is None else rows[:limit]
    return OpportunitySummaryResult(
        run_id=run.id,
        summary=dict(run.summary_json),
        items=displayed,
        export_path=export_path,
    )


def _item_row(
    session: Session, run: OpportunityDiscoveryRun, item: OpportunityDiscoveryItem
) -> dict[str, object]:
    snapshot = item.score_snapshot
    company = session.get(Company, item.company_id) if item.company_id is not None else None
    security = session.get(Security, item.security_id) if item.security_id is not None else None
    contributions: list[dict[str, object]] = []
    if snapshot is not None:
        components = sorted(
            snapshot.components,
            key=lambda component: (
                -component.final_contribution
                if component.final_contribution is not None
                else 0,
                component.component_code,
            ),
        )
        contributions = [
            {
                "component_code": component.component_code,
                "score": str(component.score) if component.score is not None else None,
                "final_contribution": (
                    str(component.final_contribution)
                    if component.final_contribution is not None
                    else None
                ),
                "subfactor_weight_coverage": str(component.subfactor_weight_coverage),
            }
            for component in components
            if component.final_contribution is not None
            and component.final_contribution > 0
        ]
    return {
        "run_id": str(run.id),
        "run_key": run.run_key_sha256,
        "discovery_cutoff": run.discovery_cutoff.isoformat(),
        "company_id": str(item.company_id) if item.company_id is not None else None,
        "company": company.legal_name if company is not None else None,
        "security_id": str(item.security_id) if item.security_id is not None else None,
        "isin": security.isin if security is not None else None,
        "symbol": item.symbol,
        "score_snapshot_id": str(item.score_snapshot_id) if item.score_snapshot_id else None,
        "snapshot_fingerprint": item.selected_snapshot_fingerprint_sha256,
        "snapshot_cutoff": snapshot.knowledge_cutoff.isoformat() if snapshot else None,
        "snapshot_age_days": item.snapshot_age_days,
        "freshness_state": item.freshness_state,
        "rankable": item.rankable,
        "unranked_reason": item.unranked_reason,
        "final_score": (
            str(snapshot.final_score)
            if snapshot is not None and snapshot.final_score is not None
            else None
        ),
        "score_rank": item.score_rank,
        "display_order": item.display_order,
        "eligibility_eligible": snapshot.eligibility_eligible if snapshot else None,
        "eligibility_reasons": snapshot.eligibility_reasons_json if snapshot else [],
        "confidence": str(snapshot.confidence) if snapshot else None,
        "component_coverage": (
            str(snapshot.top_level_component_weight_coverage) if snapshot else None
        ),
        "available_component_codes": snapshot.available_component_codes_json if snapshot else [],
        "missing_component_codes": snapshot.missing_component_codes_json if snapshot else [],
        "component_contributions": contributions,
        "discovery_policy_checksum": run.discovery_policy_checksum_sha256,
        "scoring_configuration_id": str(run.scoring_configuration_id),
        "scoring_configuration_checksum": run.scoring_configuration_checksum_sha256,
        "research_profile_checksum": run.research_profile_checksum_sha256,
        "financial_primitive_policy_checksum": (
            run.financial_primitive_policy_checksum_sha256
        ),
        "financial_endpoint_policy_checksum": run.financial_endpoint_policy_checksum_sha256,
        "snapshot_selection_version": run.snapshot_selection_version,
        "ranking_version": run.ranking_version,
    }


def _export(path: Path, rows: tuple[dict[str, object], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["run_id"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, sort_keys=True, separators=(",", ":"))
                        if isinstance(value, (list, dict))
                        else value
                    )
                    for key, value in row.items()
                }
            )

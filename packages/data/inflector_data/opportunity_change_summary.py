"""Mechanical summaries and derivative CSV for immutable opportunity changes."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.opportunity_change_policy import OpportunityChangePolicy
from inflector_database.models import (
    Company,
    OpportunityChangeItem,
    OpportunityDiscoveryRun,
    Security,
)
from inflector_database.opportunity_change_repository import OpportunityChangeRepository


@dataclass(frozen=True, slots=True)
class OpportunityChangeSummaryResult:
    run_id: UUID
    summary: dict[str, object]
    items: tuple[dict[str, object], ...]
    export_path: str | None


def summarize_opportunity_change(
    session: Session,
    *,
    run_id: UUID,
    policy: OpportunityChangePolicy,
    changed_only: bool = False,
    change_code: str | None = None,
    limit: int | None = None,
    export_csv: Path | None = None,
) -> OpportunityChangeSummaryResult:
    if limit is not None and (limit < 1 or limit > policy.maximum_summary_limit):
        raise ValueError(f"limit must be between 1 and {policy.maximum_summary_limit}")
    if change_code is not None and change_code not in policy.change_code_order:
        raise ValueError("change-code is not configured by the change policy")
    run = OpportunityChangeRepository(session).get_run(run_id)
    if run is None or run.status != "completed":
        raise ValueError("completed opportunity change run is unavailable")
    if (
        run.change_policy_code != policy.code
        or run.change_policy_checksum_sha256 != policy.checksum_sha256
    ):
        raise ValueError("opportunity change run policy binding conflicts")
    baseline = session.get(OpportunityDiscoveryRun, run.baseline_discovery_run_id)
    current = session.get(OpportunityDiscoveryRun, run.current_discovery_run_id)
    if baseline is None or current is None:
        raise ValueError("bound discovery run is unavailable")
    items = sorted(
        run.items,
        key=lambda item: (
            item.identity_key,
            item.current_symbol or "",
            item.baseline_symbol or "",
        ),
    )
    if changed_only:
        items = [item for item in items if item.changed]
    if change_code is not None:
        items = [item for item in items if change_code in item.change_codes_json]
    rows = tuple(_row(session, run, baseline, current, item) for item in items)
    export_path = None
    if export_csv is not None:
        _export(export_csv, rows)
        export_path = str(export_csv.resolve())
    displayed = rows if limit is None else rows[:limit]
    return OpportunityChangeSummaryResult(
        run_id=run.id,
        summary=dict(run.summary_json),
        items=displayed,
        export_path=export_path,
    )


def _row(
    session: Session,
    run: object,
    baseline: OpportunityDiscoveryRun,
    current: OpportunityDiscoveryRun,
    item: OpportunityChangeItem,
) -> dict[str, object]:
    from inflector_database.models import OpportunityChangeRun

    if not isinstance(run, OpportunityChangeRun):
        raise TypeError("opportunity change export requires persisted run")
    company = session.get(Company, item.company_id) if item.company_id else None
    security = session.get(Security, item.security_id) if item.security_id else None
    return {
        "change_run_id": str(run.id),
        "change_run_key": run.run_key_sha256,
        "baseline_discovery_run_id": str(baseline.id),
        "current_discovery_run_id": str(current.id),
        "baseline_discovery_cutoff": baseline.discovery_cutoff.isoformat(),
        "current_discovery_cutoff": current.discovery_cutoff.isoformat(),
        "identity_key": item.identity_key,
        "identity_basis": item.identity_basis,
        "company_id": str(item.company_id) if item.company_id else None,
        "company": company.legal_name if company else None,
        "security_id": str(item.security_id) if item.security_id else None,
        "isin": security.isin if security else None,
        "baseline_symbol": item.baseline_symbol,
        "current_symbol": item.current_symbol,
        "baseline_snapshot_id": (
            str(item.baseline_score_snapshot_id) if item.baseline_score_snapshot_id else None
        ),
        "current_snapshot_id": (
            str(item.current_score_snapshot_id) if item.current_score_snapshot_id else None
        ),
        "changed": item.changed,
        "change_codes": item.change_codes_json,
        "baseline_rankable": item.baseline_rankable,
        "current_rankable": item.current_rankable,
        "baseline_unranked_reason": item.baseline_unranked_reason,
        "current_unranked_reason": item.current_unranked_reason,
        "baseline_final_score": _text(item.baseline_final_score),
        "current_final_score": _text(item.current_final_score),
        "score_delta": _text(item.score_delta),
        "baseline_score_rank": item.baseline_score_rank,
        "current_score_rank": item.current_score_rank,
        "rank_delta": item.rank_delta,
        "baseline_confidence": _text(item.baseline_confidence),
        "current_confidence": _text(item.current_confidence),
        "confidence_delta": _text(item.confidence_delta),
        "components_gained": item.components_gained_json,
        "components_lost": item.components_lost_json,
        "component_changes": item.component_change_detail_json,
        "detail": item.detail_json,
        "change_policy_checksum": run.change_policy_checksum_sha256,
        "discovery_policy_checksum": baseline.discovery_policy_checksum_sha256,
        "scoring_configuration_checksum": baseline.scoring_configuration_checksum_sha256,
        "research_profile_checksum": baseline.research_profile_checksum_sha256,
        "financial_primitive_policy_checksum": (
            baseline.financial_primitive_policy_checksum_sha256
        ),
        "financial_endpoint_policy_checksum": (
            baseline.financial_endpoint_policy_checksum_sha256
        ),
        "comparison_version": run.comparison_version,
    }


def _export(path: Path, rows: tuple[dict[str, object], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["change_run_id"]
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


def _text(value: object | None) -> str | None:
    return str(value) if value is not None else None

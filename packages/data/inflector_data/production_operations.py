"""Recoverable execution of the accepted Production D current-research workflow."""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from inflector_data import nse_cli, research_cli
from inflector_data.nse_cli import production_preflight
from inflector_data.operations_profile import ProductionOperationsProfile
from inflector_data.production_policy import resolve_profile_datasets
from inflector_data.research_profile import ProductionResearchProfile
from inflector_database.models import ModelVersion, OperationalRun, ScoreSnapshot
from inflector_database.operations_repository import (
    OperationsRepository,
    OperationsStateError,
)

LOGGER = logging.getLogger("inflector.operations")
OPERATIONAL_STAGE_DEFINITIONS = (
    ("preflight", True),
    ("universe", True),
    ("market_history", True),
    ("financials", True),
    ("corporate_actions", True),
    ("catalyst_evidence", True),
    ("news_attention", False),
    ("model_initialization", True),
    ("current_research", True),
)
RESEARCH_STATE_PROJECTION_VERSION = "research_state_projection_v1"


def operational_stage_definitions(
    profile: ProductionOperationsProfile,
) -> tuple[tuple[str, bool], ...]:
    if profile.delivery_history_calendar_lookback_days is None:
        return OPERATIONAL_STAGE_DEFINITIONS
    return (
        *OPERATIONAL_STAGE_DEFINITIONS[:3],
        ("delivery_history", False),
        *OPERATIONAL_STAGE_DEFINITIONS[3:],
    )


@dataclass(frozen=True, slots=True)
class CycleInputs:
    operations_profile_path: Path
    research_profile_path: Path
    model_family: str
    symbols_file: Path
    symbols: tuple[str, ...]
    fiscal_year: int | None
    fiscal_quarter: int | None
    cycle_at: datetime
    raw_root: Path
    nse_license_class: str
    gdelt_raw_root: Path | None
    gdelt_license_class: str | None
    model_semantic_version: str
    git_sha: str
    model_effective_from: datetime

    @property
    def knowledge_cutoff(self) -> datetime:
        return aware_utc(self.cycle_at, "cycle_at")


@dataclass(frozen=True, slots=True)
class CyclePlan:
    inputs: CycleInputs
    operations_profile: ProductionOperationsProfile
    research_profile: ProductionResearchProfile
    symbol_set_checksum_sha256: str
    run_key_sha256: str
    persisted_inputs: dict[str, object]


class StageExecutor(Protocol):
    def execute(
        self,
        stage: str,
        *,
        session: Session,
        plan: CyclePlan,
    ) -> tuple[dict[str, object], bool]: ...


def normalize_symbols(path: Path, *, maximum: int) -> tuple[str, ...]:
    raw = path.read_text(encoding="utf-8-sig").splitlines()
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        symbol = value.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    if not result:
        raise ValueError("symbols file must contain at least one symbol")
    if len(result) > maximum or len(result) > 100:
        raise ValueError("symbols file exceeds the configured or hard maximum")
    return tuple(result)


def symbol_set_checksum(symbols: tuple[str, ...]) -> str:
    return sha256(json.dumps(symbols, separators=(",", ":")).encode("utf-8")).hexdigest()


def plan_cycle(
    inputs: CycleInputs,
    operations_profile: ProductionOperationsProfile,
    research_profile: ProductionResearchProfile,
) -> CyclePlan:
    cutoff = inputs.knowledge_cutoff
    automatic_endpoint = inputs.fiscal_year is None and inputs.fiscal_quarter is None
    if (inputs.fiscal_year is None) != (inputs.fiscal_quarter is None):
        raise ValueError("fiscal-year and fiscal-quarter must be supplied together")
    if not automatic_endpoint and not 1 <= cast(int, inputs.fiscal_quarter) <= 4:
        raise ValueError("fiscal-quarter must be between 1 and 4")
    if automatic_endpoint != operations_profile.automatic_financial_endpoint:
        raise ValueError("cycle endpoint mode must match the operations profile")
    if automatic_endpoint and research_profile.financial_endpoint_policy_checksum_sha256 is None:
        raise ValueError("automatic cycles require a bound financial endpoint policy")
    if inputs.symbols != normalize_symbols(
        inputs.symbols_file, maximum=operations_profile.maximum_symbols_per_cycle
    ):
        raise ValueError("cycle symbol inputs do not match the normalized symbols file")
    if operations_profile.gdelt_enabled:
        if inputs.gdelt_raw_root is None or not (inputs.gdelt_license_class or "").strip():
            raise ValueError("enabled GDELT operations require raw root and license class")
    symbols_checksum = symbol_set_checksum(inputs.symbols)
    persisted: dict[str, object] = {
        "operations_profile_path": str(inputs.operations_profile_path.resolve()),
        "research_profile_path": str(inputs.research_profile_path.resolve()),
        "model_family": _trimmed(inputs.model_family, "model_family"),
        "symbols_file": str(inputs.symbols_file.resolve()),
        "symbols": list(inputs.symbols),
        "fiscal_year": inputs.fiscal_year,
        "fiscal_quarter": inputs.fiscal_quarter,
        "financial_endpoint_mode": "automatic" if automatic_endpoint else "explicit",
        "financial_endpoint_policy_code": research_profile.financial_endpoint_policy_code,
        "financial_endpoint_policy_checksum_sha256": (
            research_profile.financial_endpoint_policy_checksum_sha256
        ),
        "financial_primitive_policy_checksum_sha256": (
            research_profile.financial_primitive_policy_checksum_sha256
        ),
        "cycle_at": cutoff.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "raw_root": str(inputs.raw_root.resolve()),
        "nse_license_class": _trimmed(inputs.nse_license_class, "nse_license_class"),
        "gdelt_enabled": operations_profile.gdelt_enabled,
        "gdelt_raw_root": (
            str(inputs.gdelt_raw_root.resolve()) if inputs.gdelt_raw_root is not None else None
        ),
        "gdelt_license_class": inputs.gdelt_license_class,
        "model_semantic_version": _trimmed(inputs.model_semantic_version, "model_semantic_version"),
        "git_sha": _trimmed(inputs.git_sha, "git_sha"),
        "model_effective_from": aware_utc(
            inputs.model_effective_from, "model_effective_from"
        ).isoformat(),
    }
    identity = {
        "operations_profile_code": operations_profile.operations_profile_code,
        "operations_profile_checksum_sha256": operations_profile.checksum_sha256,
        "research_profile_code": research_profile.research_profile_code,
        "research_profile_checksum_sha256": research_profile.checksum_sha256,
        "model_family": persisted["model_family"],
        "fiscal_year": inputs.fiscal_year,
        "fiscal_quarter": inputs.fiscal_quarter,
        "financial_endpoint_policy_code": research_profile.financial_endpoint_policy_code,
        "financial_endpoint_policy_checksum_sha256": (
            research_profile.financial_endpoint_policy_checksum_sha256
        ),
        "financial_primitive_policy_checksum_sha256": (
            research_profile.financial_primitive_policy_checksum_sha256
        ),
        "scoring_configuration_identity": {
            "configuration_name": research_profile.research_profile_code,
            "configuration_version": research_profile.profile_version,
            "scoring_policy_asset": research_profile.scoring_policy_asset,
            "scoring_policy_asset_checksum_sha256": _asset_checksum(
                inputs.research_profile_path, research_profile.scoring_policy_asset
            ),
        },
        "cycle_at": cutoff.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "symbol_set_checksum_sha256": symbols_checksum,
        "gdelt_enabled": operations_profile.gdelt_enabled,
        "nse_license_class": persisted["nse_license_class"],
        "gdelt_license_class": persisted["gdelt_license_class"],
        "model_semantic_version": persisted["model_semantic_version"],
        "git_sha": persisted["git_sha"],
        "model_effective_from": persisted["model_effective_from"],
    }
    run_key = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CyclePlan(
        inputs=inputs,
        operations_profile=operations_profile,
        research_profile=research_profile,
        symbol_set_checksum_sha256=symbols_checksum,
        run_key_sha256=run_key,
        persisted_inputs=persisted,
    )


def cycle_inputs_from_persisted(value: dict[str, object]) -> CycleInputs:
    symbols = value.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
        raise OperationsStateError("persisted operational symbols are malformed")
    return CycleInputs(
        operations_profile_path=Path(_stored_text(value, "operations_profile_path")),
        research_profile_path=Path(_stored_text(value, "research_profile_path")),
        model_family=_stored_text(value, "model_family"),
        symbols_file=Path(_stored_text(value, "symbols_file")),
        symbols=tuple(cast(list[str], symbols)),
        fiscal_year=_stored_optional_int(value, "fiscal_year"),
        fiscal_quarter=_stored_optional_int(value, "fiscal_quarter"),
        cycle_at=datetime.fromisoformat(_stored_text(value, "cycle_at")),
        raw_root=Path(_stored_text(value, "raw_root")),
        nse_license_class=_stored_text(value, "nse_license_class"),
        gdelt_raw_root=(
            Path(str(value["gdelt_raw_root"])) if value.get("gdelt_raw_root") else None
        ),
        gdelt_license_class=(
            str(value["gdelt_license_class"]) if value.get("gdelt_license_class") else None
        ),
        model_semantic_version=_stored_text(value, "model_semantic_version"),
        git_sha=_stored_text(value, "git_sha"),
        model_effective_from=datetime.fromisoformat(_stored_text(value, "model_effective_from")),
    )


def doctor(
    session: Session,
    *,
    plan: CyclePlan,
    expected_migration: str = "20261003_0018",
) -> dict[str, object]:
    session.execute(text("SELECT 1"))
    migration = session.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    if migration != expected_migration:
        raise OperationsStateError(
            f"database migration head must be {expected_migration}; found {migration or 'none'}"
        )
    production_preflight(session, plan.inputs.nse_license_class)
    _writable_directory(plan.inputs.raw_root, "NSE raw root")
    if plan.operations_profile.gdelt_enabled:
        assert plan.inputs.gdelt_raw_root is not None
        _writable_directory(plan.inputs.gdelt_raw_root, "GDELT raw root")
    existing_models = list(
        session.scalars(
            select(ModelVersion).where(ModelVersion.model_family == plan.inputs.model_family)
        )
    )
    if len([item for item in existing_models if item.status == "active"]) > 1:
        raise OperationsStateError("multiple active models conflict for the requested family")
    bindings: dict[str, str | None]
    try:
        resolved = resolve_profile_datasets(session, plan.research_profile)
        bindings = {key: str(item) if item else None for key, item in resolved.items()}
        binding_status = "available"
    except RuntimeError:
        bindings = {}
        binding_status = "initialization_required"
    return {
        "status": "passed",
        "migration_head": migration,
        "operations_profile_code": plan.operations_profile.operations_profile_code,
        "operations_profile_checksum": plan.operations_profile.checksum_sha256,
        "research_profile_code": plan.research_profile.research_profile_code,
        "research_profile_checksum": plan.research_profile.checksum_sha256,
        "financial_endpoint_policy": {
            "code": plan.research_profile.financial_endpoint_policy_code,
            "checksum_sha256": plan.research_profile.financial_endpoint_policy_checksum_sha256,
        },
        "model_family": plan.inputs.model_family,
        "symbols": list(plan.inputs.symbols),
        "provider_bindings_status": binding_status,
        "provider_bindings": bindings,
    }


class AcceptedProductionStageExecutor:
    """Thin adapter over accepted Production A-D command/service entry points."""

    def execute(
        self,
        stage: str,
        *,
        session: Session,
        plan: CyclePlan,
    ) -> tuple[dict[str, object], bool]:
        inputs = plan.inputs
        profile = plan.operations_profile
        cutoff = inputs.knowledge_cutoff
        if stage == "preflight":
            return doctor(session, plan=plan), True
        if stage == "universe":
            summaries, succeeded = nse_cli._execute(  # noqa: SLF001
                Namespace(
                    command="ingest-universe",
                    database_url="managed-by-operations",
                    raw_root=inputs.raw_root,
                    license_class=inputs.nse_license_class,
                    file=None,
                    source_uri=None,
                ),
                session,
            )
            return {"stages": summaries}, succeeded
        if stage == "market_history":
            to_date = cutoff.date()
            from_date = to_date - timedelta(days=profile.market_history_calendar_lookback_days - 1)
            summaries, succeeded = nse_cli._execute_market_range(  # noqa: SLF001
                Namespace(
                    command="ingest-market-range",
                    raw_root=inputs.raw_root,
                    license_class=inputs.nse_license_class,
                    from_date=from_date,
                    to_date=to_date,
                    request_delay_seconds=profile.request_delay_seconds,
                ),
                session,
            )
            return {
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "stages": summaries,
            }, succeeded
        if stage == "delivery_history":
            lookback = profile.delivery_history_calendar_lookback_days
            if lookback is None:
                return {"status": "disabled_by_operations_profile"}, True
            to_date = cutoff.date()
            from_date = to_date - timedelta(days=lookback - 1)
            summaries, succeeded = nse_cli._execute_delivery_range(  # noqa: SLF001
                Namespace(
                    command="ingest-delivery-range",
                    raw_root=inputs.raw_root,
                    license_class=inputs.nse_license_class,
                    from_date=from_date,
                    to_date=to_date,
                    request_delay_seconds=profile.request_delay_seconds,
                ),
                session,
            )
            return {
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "stages": summaries,
            }, succeeded
        if stage == "financials":
            primitive_asset = plan.research_profile.financial_primitive_policy_asset
            repository_root = Path(__file__).resolve().parents[3]
            summaries, succeeded = nse_cli._execute_financials(  # noqa: SLF001
                Namespace(
                    command="ingest-financials",
                    raw_root=inputs.raw_root,
                    license_class=inputs.nse_license_class,
                    symbol=None,
                    symbols_file=inputs.symbols_file,
                    max_symbols=profile.maximum_symbols_per_cycle,
                    max_filings=profile.maximum_financial_filings,
                    financial_primitive_policy=(
                        repository_root / primitive_asset if primitive_asset else None
                    ),
                    from_date=None,
                    to_date=cutoff.date(),
                    request_delay_seconds=profile.request_delay_seconds,
                ),
                session,
            )
            symbol_failures = sorted(
                {
                    str(item["symbol"])
                    for item in summaries
                    if item.get("status") == "failed" and item.get("symbol")
                }
            )
            unscoped_failure = any(
                item.get("status") == "failed" and not item.get("symbol") for item in summaries
            )
            return {
                "stages": summaries,
                "symbol_operational_failures": symbol_failures,
                "upstream_status": ("completed" if succeeded else "completed_with_symbol_failures"),
            }, not unscoped_failure
        window_start = cutoff.date() - timedelta(
            days=plan.research_profile.catalyst_lookback_days - 1
        )
        if stage in {"corporate_actions", "catalyst_evidence"}:
            command = (
                "ingest-corporate-actions"
                if stage == "corporate_actions"
                else "ingest-catalyst-evidence"
            )
            summaries, succeeded = nse_cli._execute_corporate_filings(  # noqa: SLF001
                Namespace(
                    command=command,
                    raw_root=inputs.raw_root,
                    license_class=inputs.nse_license_class,
                    from_date=window_start,
                    to_date=cutoff.date(),
                    symbol=None,
                    file=None,
                    source_uri=None,
                    max_announcements=profile.maximum_announcements,
                    max_documents=profile.maximum_documents,
                ),
                session,
            )
            return {"stages": summaries}, succeeded
        if stage == "news_attention":
            if not profile.gdelt_enabled:
                return {"status": "disabled_by_operations_profile"}, True
            payload, succeeded = research_cli._execute(  # noqa: SLF001
                Namespace(
                    command="ingest-gdelt-news",
                    database_url="managed-by-operations",
                    research_profile=inputs.research_profile_path,
                    symbol=None,
                    symbols_file=inputs.symbols_file,
                    max_symbols=profile.maximum_symbols_per_cycle,
                    knowledge_cutoff=cutoff,
                    gdelt_raw_root=inputs.gdelt_raw_root,
                    gdelt_license_class=inputs.gdelt_license_class,
                ),
                session,
            )
            if not succeeded:
                return {"status": "attention_unavailable", "result": payload}, True
            return payload, True
        if stage == "model_initialization":
            payload, succeeded = research_cli._execute(  # noqa: SLF001
                Namespace(
                    command="init-model",
                    database_url="managed-by-operations",
                    research_profile=inputs.research_profile_path,
                    model_family=inputs.model_family,
                    model_semantic_version=inputs.model_semantic_version,
                    git_sha=inputs.git_sha,
                    effective_from=inputs.model_effective_from,
                ),
                session,
            )
            return payload, succeeded
        if stage == "current_research":
            automatic_endpoint = profile.automatic_financial_endpoint
            payload, succeeded = research_cli._execute(  # noqa: SLF001
                Namespace(
                    command="run-current-auto" if automatic_endpoint else "run-current",
                    database_url="managed-by-operations",
                    model_family=inputs.model_family,
                    research_profile=inputs.research_profile_path,
                    symbol=None,
                    symbols_file=inputs.symbols_file,
                    max_symbols=profile.maximum_symbols_per_cycle,
                    fiscal_year=inputs.fiscal_year,
                    fiscal_quarter=inputs.fiscal_quarter,
                    knowledge_cutoff=cutoff,
                    ingest_gdelt_news=False,
                    gdelt_raw_root=None,
                    gdelt_license_class=None,
                ),
                session,
            )
            # Per-symbol failure is recorded by the operations layer, not treated as a
            # dependency failure that erases successful later-symbol work.
            return payload, True
        raise ValueError(f"unsupported operational stage: {stage}")


class ProductionCycleService:
    def __init__(
        self,
        session: Session,
        executor: StageExecutor,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._session = session
        self._repository = OperationsRepository(session)
        self._executor = executor
        self._clock = clock

    def run(self, plan: CyclePlan, *, owner_token: str) -> tuple[dict[str, object], int]:
        existing = self._repository.get_by_key(plan.run_key_sha256)
        if existing is not None and existing.status in {
            "completed",
            "completed_with_symbol_failures",
        }:
            return cycle_summary(existing, at=self._clock(), already_completed=True), (
                2 if existing.status == "completed_with_symbol_failures" else 0
            )
        if existing is None:
            existing = self._repository.create_run(
                run_key_sha256=plan.run_key_sha256,
                operations_profile_code=plan.operations_profile.operations_profile_code,
                operations_profile_checksum_sha256=plan.operations_profile.checksum_sha256,
                research_profile_code=plan.research_profile.research_profile_code,
                research_profile_checksum_sha256=plan.research_profile.checksum_sha256,
                model_family=plan.inputs.model_family,
                fiscal_year=plan.inputs.fiscal_year,
                fiscal_quarter=plan.inputs.fiscal_quarter,
                cycle_at=plan.inputs.cycle_at,
                knowledge_cutoff=plan.inputs.knowledge_cutoff,
                symbol_set_checksum_sha256=plan.symbol_set_checksum_sha256,
                ordered_symbols=plan.inputs.symbols,
                inputs=plan.persisted_inputs,
                planned_at=self._clock(),
                stages=operational_stage_definitions(plan.operations_profile),
            )
            self._session.commit()
        else:
            self._repository.assert_immutable_inputs(existing, plan.persisted_inputs)
        self._repository.acquire_lease(
            run_id=existing.id,
            owner_token=owner_token,
            acquired_at=self._clock(),
            lease_seconds=plan.operations_profile.run_lease_seconds,
        )
        self._session.commit()
        return self._execute(existing.id, plan=plan, owner_token=owner_token)

    def resume(
        self, run_id: UUID, plan: CyclePlan, *, owner_token: str
    ) -> tuple[dict[str, object], int]:
        run = self._repository.get_run(run_id)
        if run is None:
            raise OperationsStateError("operational run not found")
        self._repository.assert_immutable_inputs(run, plan.persisted_inputs)
        if run.status in {"completed", "completed_with_symbol_failures"}:
            return cycle_summary(run, at=self._clock(), already_completed=True), (
                2 if run.status == "completed_with_symbol_failures" else 0
            )
        if run.status == "running":
            self._repository.recover_stale_lease(
                run_id=run.id,
                owner_token=owner_token,
                recovered_at=self._clock(),
                lease_seconds=plan.operations_profile.run_lease_seconds,
            )
        else:
            self._repository.acquire_lease(
                run_id=run.id,
                owner_token=owner_token,
                acquired_at=self._clock(),
                lease_seconds=plan.operations_profile.run_lease_seconds,
            )
        self._session.commit()
        return self._execute(run.id, plan=plan, owner_token=owner_token)

    def _execute(
        self, run_id: UUID, *, plan: CyclePlan, owner_token: str
    ) -> tuple[dict[str, object], int]:
        for definition_name, required in operational_stage_definitions(plan.operations_profile):
            run = self._required_run(run_id)
            stage = next(item for item in run.stages if item.stage_name == definition_name)
            if stage.status == "completed":
                continue
            if stage.attempt_count >= plan.operations_profile.stage_max_attempts:
                self._fail_run(run, code="stage_attempt_limit", message=definition_name)
                return cycle_summary(run, at=self._clock()), 1
            self._repository.start_stage(stage, at=self._clock())
            self._repository.renew_lease(
                run,
                owner_token=owner_token,
                at=self._clock(),
                lease_seconds=plan.operations_profile.run_lease_seconds,
            )
            self._session.commit()
            _log("stage_started", run, stage=definition_name)
            try:
                result, succeeded = self._executor.execute(
                    definition_name, session=self._session, plan=plan
                )
                result = json_safe(result)
            except Exception as error:
                self._session.rollback()
                result = {"status": "failed"}
                succeeded = False
                error_code = type(error).__name__
                error_message = str(error)
            else:
                error_code = None if succeeded else "stage_failed"
                error_message = None if succeeded else _result_error(result)
            run = self._required_run(run_id)
            stage = next(item for item in run.stages if item.stage_name == definition_name)
            self._repository.finish_stage(
                stage,
                status="completed" if succeeded else "failed",
                at=self._clock(),
                result=cast(dict[str, object], result),
                error_code=error_code,
                error_message=error_message,
            )
            self._session.commit()
            _log("stage_finished", run, stage=definition_name, status=stage.status)
            if not succeeded and required:
                run = self._required_run(run_id)
                self._repository.block_remaining_stages(
                    run, at=self._clock(), reason=f"required stage failed: {definition_name}"
                )
                self._fail_run(run, code=error_code or "stage_failed", message=error_message)
                return cycle_summary(run, at=self._clock()), 1

        run = self._required_run(run_id)
        current = next(item for item in run.stages if item.stage_name == "current_research")
        results_value = current.result_summary_json.get("results", [])
        results = results_value if isinstance(results_value, list) else []
        financial = next(item for item in run.stages if item.stage_name == "financials")
        upstream_value = financial.result_summary_json.get("symbol_operational_failures", [])
        upstream_failures = (
            {str(value) for value in upstream_value if isinstance(value, str)}
            if isinstance(upstream_value, list)
            else set()
        )
        symbol_failures = False
        for ordinal, symbol in enumerate(plan.inputs.symbols):
            item = next(
                (
                    value
                    for value in results
                    if isinstance(value, dict) and value.get("symbol") == symbol
                ),
                {"symbol": symbol, "status": "failed", "error": "symbol result missing"},
            )
            assert isinstance(item, dict)
            failed = item.get("status") == "failed" or symbol in upstream_failures
            symbol_failures = symbol_failures or failed
            snapshot_id = _optional_uuid(item.get("snapshot_id"))
            change: dict[str, object] = (
                snapshot_change_report(
                    self._session, snapshot_id, snapshot_created=item.get("created") is True
                )
                if snapshot_id is not None
                else {
                    "projection_version": RESEARCH_STATE_PROJECTION_VERSION,
                    "status": "unavailable",
                }
            )
            self._repository.upsert_symbol_result(
                run,
                symbol=symbol,
                ordinal=ordinal,
                status="failed" if failed else "completed",
                snapshot_id=snapshot_id,
                result=cast(dict[str, object], json_safe(item)),
                change=change,
                error_code=(
                    "upstream_symbol_stage_failed"
                    if symbol in upstream_failures
                    else ("symbol_execution_failed" if failed else None)
                ),
                error_message=(
                    "one or more issuer-scoped upstream operations failed"
                    if symbol in upstream_failures
                    else (str(item.get("error")) if failed else None)
                ),
            )
        final_status = "completed_with_symbol_failures" if symbol_failures else "completed"
        summary = {
            "symbol_failures": symbol_failures,
            "research_state_projection": RESEARCH_STATE_PROJECTION_VERSION,
        }
        self._repository.finish_run(run, status=final_status, at=self._clock(), summary=summary)
        self._session.commit()
        return cycle_summary(run, at=self._clock()), 2 if symbol_failures else 0

    def _fail_run(self, run: OperationalRun, *, code: str, message: str | None) -> None:
        self._repository.finish_run(
            run,
            status="failed",
            at=self._clock(),
            summary={"operational_failure": True},
            error_code=code,
            error_message=message,
        )
        self._session.commit()

    def _required_run(self, run_id: UUID) -> OperationalRun:
        run = self._repository.get_run(run_id)
        if run is None:
            raise OperationsStateError("operational run disappeared")
        return run


def snapshot_change_report(
    session: Session, snapshot_id: UUID, *, snapshot_created: bool = True
) -> dict[str, object]:
    current = session.get(ScoreSnapshot, snapshot_id)
    if current is None:
        raise OperationsStateError("current-research result references an unknown snapshot")
    model = session.get(ModelVersion, current.model_version_id)
    if model is None or current.algorithm_version != "score_snapshot_v5":
        raise OperationsStateError("current-research result is not a persisted V5 snapshot")
    semantic_predecessor = or_(
        ScoreSnapshot.knowledge_cutoff < current.knowledge_cutoff,
        and_(
            ScoreSnapshot.knowledge_cutoff == current.knowledge_cutoff,
            ScoreSnapshot.ending_fiscal_year < current.ending_fiscal_year,
        ),
        and_(
            ScoreSnapshot.knowledge_cutoff == current.knowledge_cutoff,
            ScoreSnapshot.ending_fiscal_year == current.ending_fiscal_year,
            ScoreSnapshot.ending_fiscal_quarter < current.ending_fiscal_quarter,
        ),
        and_(
            ScoreSnapshot.knowledge_cutoff == current.knowledge_cutoff,
            ScoreSnapshot.ending_fiscal_year == current.ending_fiscal_year,
            ScoreSnapshot.ending_fiscal_quarter == current.ending_fiscal_quarter,
            ScoreSnapshot.snapshot_fingerprint_sha256 > current.snapshot_fingerprint_sha256,
        ),
    )
    previous = session.scalar(
        select(ScoreSnapshot)
        .join(ModelVersion, ScoreSnapshot.model_version_id == ModelVersion.id)
        .where(
            ScoreSnapshot.id != current.id,
            ScoreSnapshot.company_id == current.company_id,
            ModelVersion.model_family == model.model_family,
            ScoreSnapshot.configuration_checksum_sha256 == current.configuration_checksum_sha256,
            ScoreSnapshot.algorithm_version == "score_snapshot_v5",
            semantic_predecessor,
        )
        .order_by(
            ScoreSnapshot.knowledge_cutoff.desc(),
            ScoreSnapshot.ending_fiscal_year.desc(),
            ScoreSnapshot.ending_fiscal_quarter.desc(),
            ScoreSnapshot.snapshot_fingerprint_sha256.asc(),
        )
    )
    current_projection = _snapshot_projection(current)
    if previous is None:
        return {
            "projection_version": RESEARCH_STATE_PROJECTION_VERSION,
            "status": "initial_no_baseline",
            "snapshot_created": snapshot_created,
            "snapshot_reused": not snapshot_created,
            "current_snapshot_id": str(current.id),
            "current_snapshot_fingerprint": current.snapshot_fingerprint_sha256,
        }
    previous_projection = _snapshot_projection(previous)
    return {
        "projection_version": RESEARCH_STATE_PROJECTION_VERSION,
        "status": "changed" if current_projection != previous_projection else "unchanged",
        "snapshot_created": snapshot_created,
        "snapshot_reused": not snapshot_created,
        "current_snapshot_id": str(current.id),
        "previous_snapshot_id": str(previous.id),
        "snapshot_fingerprint_changed": (
            current.snapshot_fingerprint_sha256 != previous.snapshot_fingerprint_sha256
        ),
        "before": previous_projection,
        "after": current_projection,
    }


def cycle_summary(
    run: OperationalRun, *, at: datetime, already_completed: bool = False
) -> dict[str, object]:
    started = _optional_aware(run.started_at)
    completed = _optional_aware(run.completed_at)
    lease_state = _lease_state(run, at)
    stage_failures = [
        item for item in run.stages if item.status == "failed" and item.error_code is not None
    ]
    integrity_failures = [
        {"stage": item.stage_name, "error_code": item.error_code}
        for item in stage_failures
        if "integrity" in (item.error_code or "").lower()
    ]
    return {
        "status": run.status,
        "already_completed": already_completed,
        "operational_run_id": str(run.id),
        "run_key_sha256": run.run_key_sha256,
        "operations_profile": {
            "code": run.operations_profile_code,
            "checksum_sha256": run.operations_profile_checksum_sha256,
        },
        "research_profile": {
            "code": run.research_profile_code,
            "checksum_sha256": run.research_profile_checksum_sha256,
        },
        "cycle_at": _persisted_utc(run.cycle_at).isoformat(),
        "knowledge_cutoff": _persisted_utc(run.knowledge_cutoff).isoformat(),
        "fiscal_year": run.fiscal_year or None,
        "fiscal_quarter": run.fiscal_quarter or None,
        "financial_endpoint_mode": run.inputs_json.get("financial_endpoint_mode", "explicit"),
        "model_family": run.model_family,
        "ordered_symbols": run.ordered_symbols_json,
        "symbol_set_checksum_sha256": run.symbol_set_checksum_sha256,
        "lease_state": lease_state,
        "started_at": started.isoformat() if started else None,
        "completed_at": completed.isoformat() if completed else None,
        "duration_seconds": (
            int((completed - started).total_seconds()) if started and completed else None
        ),
        "stages": [
            {
                "stage": item.stage_name,
                "status": item.status,
                "required": item.required,
                "attempt_count": item.attempt_count,
                "started_at": _iso(item.started_at),
                "completed_at": _iso(item.completed_at),
                "duration_seconds": _duration(item.started_at, item.completed_at),
                "result": item.result_summary_json,
                "error_code": item.error_code,
                "error": item.error_message,
            }
            for item in sorted(run.stages, key=lambda value: value.stage_order)
        ],
        "symbols": [
            {
                "symbol": item.symbol,
                "status": item.status,
                "snapshot_id": str(item.snapshot_id) if item.snapshot_id else None,
                "result": item.result_json,
                "change": item.change_json,
                "error_code": item.error_code,
                "error": item.error_message,
            }
            for item in sorted(run.symbols, key=lambda value: value.ordinal)
        ],
        "error_code": run.error_code,
        "error": run.error_message,
        "operational_failures": [
            {"stage": item.stage_name, "error_code": item.error_code} for item in stage_failures
        ],
        "integrity_failures": integrity_failures,
        "source_not_available_count": sum(
            _count_status(item.result_summary_json, "source_not_available") for item in run.stages
        ),
        "recovery_required": run.status in {"failed", "stale"} or lease_state == "expired",
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, datetime):
        return aware_utc(value, "datetime").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise TypeError(f"unsupported operational JSON value: {type(value).__name__}")


def aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _snapshot_projection(snapshot: ScoreSnapshot) -> dict[str, object]:
    return {
        "snapshot_status": snapshot.snapshot_status,
        "available_component_codes": snapshot.available_component_codes_json,
        "missing_component_codes": snapshot.missing_component_codes_json,
        "top_level_coverage": format(snapshot.top_level_component_weight_coverage, "f"),
        "confidence": format(snapshot.confidence, "f"),
        "final_score": format(snapshot.final_score, "f")
        if snapshot.final_score is not None
        else None,
    }


def _writable_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise OperationsStateError(f"{label} does not exist or is not a directory")
    try:
        probe = path / ".inflector_write_probe"
        with probe.open("xb"):
            pass
        probe.unlink()
    except OSError as error:
        raise OperationsStateError(f"{label} is not writable") from error


def _log(event: str, run: OperationalRun, **values: object) -> None:
    LOGGER.info(
        json.dumps(
            {"event": event, "run_id": str(run.id), "run_key": run.run_key_sha256, **values},
            sort_keys=True,
        )
    )


def _trimmed(value: str, field: str) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and trimmed")
    return value


def _stored_text(value: dict[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise OperationsStateError(f"persisted {field} is malformed")
    return item


def _stored_int(value: dict[str, object], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int):
        raise OperationsStateError(f"persisted {field} is malformed")
    return item


def _stored_optional_int(value: dict[str, object], field: str) -> int | None:
    item = value.get(field)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int):
        raise OperationsStateError(f"persisted {field} is malformed")
    return item


def _asset_checksum(research_profile_path: Path, asset: str) -> str:
    root = research_profile_path.resolve().parents[2]
    path = (root / asset).resolve()
    try:
        path.relative_to(root)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        raise ValueError("scoring policy asset is invalid") from error
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _result_error(result: dict[str, object]) -> str:
    value = result.get("error")
    return str(value) if value else "stage returned an operational failure"


def _optional_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _persisted_utc(value: datetime) -> datetime:
    """Interpret SQLite's timezone-naive round trip as the UTC value we persisted."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    item = _optional_aware(value)
    return item.isoformat() if item else None


def _duration(started: datetime | None, completed: datetime | None) -> int | None:
    start = _optional_aware(started)
    end = _optional_aware(completed)
    return int((end - start).total_seconds()) if start and end else None


def _lease_state(run: OperationalRun, at: datetime) -> str:
    expires = _optional_aware(run.lease_expires_at)
    if run.lease_owner_token is None or expires is None:
        return "none"
    return "expired" if expires <= aware_utc(at, "status_at") else "active"


def _count_status(value: object, status: str) -> int:
    if isinstance(value, dict):
        own = 1 if value.get("status") == status else 0
        return own + sum(_count_status(item, status) for item in value.values())
    if isinstance(value, list):
        return sum(_count_status(item, status) for item in value)
    return 0

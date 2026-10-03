"""Strict knowledge-time research dataset construction over accepted V5 services."""

from __future__ import annotations

import argparse
import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from inflector_core.score_audit import canonical_audit_value
from inflector_data.backtest_policy import (
    BacktestPolicy,
    HistoricalAvailabilityManifest,
    canonical_json_sha256,
)
from inflector_data.production_policy import resolve_profile_datasets
from inflector_data.production_research import ProductionSecurityContext
from inflector_data.research_cli import _run_symbol_auto
from inflector_data.research_profile import ProductionResearchProfile
from inflector_database.backtest_repository import (
    BacktestObservationWrite,
    BacktestRepository,
)
from inflector_database.models import (
    Company,
    ExchangeListing,
    PriceBar,
    ScoreSnapshot,
    Security,
    SourceRecord,
)
from inflector_database.scoring_repository import ScoringPolicyRepository

ResearchRunner = Callable[[Session, argparse.Namespace, str, Path], dict[str, object]]


@dataclass(frozen=True, slots=True)
class HistoricalUniverseMember:
    symbol: str
    company_id: UUID
    security_id: UUID
    listing_id: UUID


@dataclass(frozen=True, slots=True)
class HistoricalDatasetBuildResult:
    run_id: UUID
    run_key_sha256: str
    created_run: bool
    already_built: bool
    cutoff_count: int
    eligible_observations: int
    excluded_universe_decisions: int
    snapshot_observations: int
    partial_snapshots: int
    issuer_unavailable: int
    research_failures: int


def calendar_month_end_cutoffs(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """Enumerate explicit UTC month-end knowledge cutoffs inside an inclusive range."""

    first = _utc(start, "cutoff_start")
    last = _utc(end, "cutoff_end")
    if first > last:
        raise ValueError("cutoff_start must be on or before cutoff_end")
    year, month = first.year, first.month
    result: list[datetime] = []
    while (year, month) <= (last.year, last.month):
        final_day = calendar.monthrange(year, month)[1]
        cutoff = datetime.combine(date(year, month, final_day), time.max, tzinfo=UTC)
        if first <= cutoff <= last:
            result.append(cutoff)
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return tuple(result)


def normalize_symbols(values: Sequence[str], *, maximum: int) -> tuple[str, ...]:
    if maximum < 1 or maximum > 25:
        raise ValueError("historical maximum symbols must be between 1 and 25")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        symbol = raw.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    if not result:
        raise ValueError("at least one historical symbol is required")
    if len(result) > maximum:
        raise ValueError("historical symbol input exceeds the policy maximum")
    return tuple(result)


def symbol_set_checksum(symbols: tuple[str, ...]) -> str:
    return canonical_json_sha256(list(symbols))


def visible_source_state_checksum(
    session: Session,
    *,
    dataset_ids: tuple[UUID, ...],
    symbols: tuple[str, ...],
    cutoff: datetime,
) -> str:
    """Fingerprint only source/identity state that was visible by the range endpoint."""

    as_of = _utc(cutoff, "cutoff")
    records = session.execute(
        select(SourceRecord).where(
            SourceRecord.provider_dataset_id.in_(dataset_ids),
            SourceRecord.validation_status == "accepted",
            SourceRecord.available_at.is_not(None),
            SourceRecord.available_at <= as_of,
        )
    ).scalars()
    source_projection = sorted(
        (
            str(item.id),
            str(item.provider_dataset_id),
            item.external_record_id,
            item.content_sha256,
            _iso(item.available_at),
            _iso(item.revision_at),
        )
        for item in records
    )
    identities = session.execute(
        select(Company, Security, ExchangeListing)
        .join(Security, Security.company_id == Company.id)
        .join(ExchangeListing, ExchangeListing.security_id == Security.id)
        .where(
            ExchangeListing.exchange == "NSE",
            ExchangeListing.symbol.in_(symbols),
            Company.created_at <= as_of,
            Security.created_at <= as_of,
            ExchangeListing.created_at <= as_of,
        )
    ).tuples()
    identity_projection = sorted(
        (
            str(company.id),
            str(security.id),
            security.isin,
            str(listing.id),
            listing.symbol,
            listing.valid_from.isoformat(),
            listing.valid_to.isoformat() if listing.valid_to else None,
            listing.status,
        )
        for company, security, listing in identities
    )
    return canonical_json_sha256(
        {"accepted_sources": source_projection, "persisted_identities": identity_projection}
    )


class ObservedPitUniverse:
    """Conservative requested-symbol universe; never back-project current identities."""

    def __init__(self, session: Session, market_provider_dataset_id: UUID) -> None:
        self._session = session
        self._market_provider_dataset_id = market_provider_dataset_id

    def resolve(self, *, symbol: str, cutoff: datetime) -> HistoricalUniverseMember | None:
        as_of = _utc(cutoff, "knowledge_cutoff")
        matches = list(
            self._session.execute(
                select(Company, Security, ExchangeListing)
                .join(Security, Security.company_id == Company.id)
                .join(ExchangeListing, ExchangeListing.security_id == Security.id)
                .where(
                    ExchangeListing.exchange == "NSE",
                    ExchangeListing.symbol == symbol,
                    ExchangeListing.valid_from <= as_of.date(),
                    or_(
                        ExchangeListing.valid_to.is_(None),
                        ExchangeListing.valid_to >= as_of.date(),
                    ),
                    Company.created_at <= as_of,
                    Security.created_at <= as_of,
                    ExchangeListing.created_at <= as_of,
                )
            ).tuples()
        )
        if len(matches) != 1:
            return None
        company, security, listing = matches[0]
        observed_market = self._session.scalar(
            select(PriceBar.id)
            .join(SourceRecord, PriceBar.source_record_id == SourceRecord.id)
            .where(
                SourceRecord.provider_dataset_id == self._market_provider_dataset_id,
                SourceRecord.validation_status == "accepted",
                PriceBar.security_id == security.id,
                PriceBar.trading_date <= as_of.date(),
                PriceBar.available_at <= as_of,
            )
            .limit(1)
        )
        if observed_market is None:
            return None
        return HistoricalUniverseMember(
            symbol=symbol,
            company_id=company.id,
            security_id=security.id,
            listing_id=listing.id,
        )


def build_historical_dataset(
    session: Session,
    *,
    policy: BacktestPolicy,
    manifest: HistoricalAvailabilityManifest,
    research_profile: ProductionResearchProfile,
    model_family: str,
    symbols: tuple[str, ...],
    cutoff_start: datetime,
    cutoff_end: datetime,
    repository_root: Path,
    runner: ResearchRunner | None = None,
) -> HistoricalDatasetBuildResult:
    """Freeze PIT research states first; this function never computes outcomes."""

    start = _utc(cutoff_start, "cutoff_start")
    end = _utc(cutoff_end, "cutoff_end")
    cutoffs = calendar_month_end_cutoffs(start, end)
    if not cutoffs:
        raise ValueError("cutoff range contains no configured month-end observation")
    if research_profile.checksum_sha256 != policy.research_profile_checksum_sha256:
        raise ValueError("research profile checksum does not match backtest policy")
    if (
        research_profile.financial_primitive_policy_checksum_sha256
        != policy.financial_primitive_policy_checksum_sha256
        or research_profile.financial_endpoint_policy_checksum_sha256
        != policy.financial_endpoint_policy_checksum_sha256
    ):
        raise ValueError("research semantic policy checksums do not match backtest policy")
    if manifest.checksum_sha256 != policy.historical_availability_manifest_checksum_sha256:
        raise ValueError("availability manifest checksum does not match backtest policy")
    dataset_map = resolve_profile_datasets(session, research_profile)
    required_ids = tuple(value for value in dataset_map.values() if value is not None)
    market_id = dataset_map.get("market")
    if market_id is None:
        raise ValueError("historical research requires the exact market dataset")
    if manifest.classifications["market_cap"].classification in {"C", "D"}:
        unapproved_market_cap = session.scalar(
            select(PriceBar.id)
            .join(SourceRecord, PriceBar.source_record_id == SourceRecord.id)
            .where(
                SourceRecord.provider_dataset_id == market_id,
                SourceRecord.validation_status == "accepted",
                PriceBar.market_cap.is_not(None),
                PriceBar.available_at <= end,
            )
            .limit(1)
        )
        if unapproved_market_cap is not None:
            raise ValueError(
                "historically unavailable market-cap evidence cannot enter strict research"
            )
    configuration = ScoringPolicyRepository(session).resolve_active_configuration(
        model_family=model_family, at=end
    )
    if configuration is None:
        raise ValueError("active scoring configuration is unavailable at cutoff_end")
    if (
        configuration.record.configuration_name != research_profile.research_profile_code
        or configuration.record.configuration_version != research_profile.profile_version
    ):
        raise ValueError("active scoring configuration does not match the bound research profile")
    source_state = visible_source_state_checksum(
        session,
        dataset_ids=required_ids,
        symbols=symbols,
        cutoff=end,
    )
    identity = {
        "backtest_policy_checksum": policy.checksum_sha256,
        "availability_manifest_checksum": manifest.checksum_sha256,
        "scoring_configuration_id": str(configuration.record.id),
        "scoring_configuration_checksum": configuration.record.checksum_sha256,
        "research_profile_checksum": research_profile.checksum_sha256,
        "financial_primitive_policy_checksum": policy.financial_primitive_policy_checksum_sha256,
        "financial_endpoint_policy_checksum": policy.financial_endpoint_policy_checksum_sha256,
        "source_state_checksum": source_state,
        "model_family": model_family,
        "cutoff_start": start.isoformat(),
        "cutoff_end": end.isoformat(),
        "cutoff_cadence": policy.cutoff_cadence,
        "universe_policy": policy.eligible_universe_policy,
        "benchmark_code": policy.benchmark_code,
        "forward_return_horizons": list(policy.forward_return_horizons),
        "ordered_symbols": list(symbols),
    }
    run_key = canonical_json_sha256(identity)
    repository = BacktestRepository(session)
    run, created = repository.create_run(
        run_key_sha256=run_key,
        backtest_policy_code=policy.code,
        backtest_policy_checksum_sha256=policy.checksum_sha256,
        availability_manifest_code=manifest.code,
        availability_manifest_checksum_sha256=manifest.checksum_sha256,
        scoring_configuration_id=configuration.record.id,
        scoring_configuration_checksum_sha256=configuration.record.checksum_sha256,
        research_profile_code=research_profile.research_profile_code,
        research_profile_checksum_sha256=research_profile.checksum_sha256,
        financial_primitive_policy_checksum_sha256=(
            policy.financial_primitive_policy_checksum_sha256
        ),
        financial_endpoint_policy_checksum_sha256=(
            policy.financial_endpoint_policy_checksum_sha256
        ),
        source_state_checksum_sha256=source_state,
        model_family=model_family,
        cutoff_start=start,
        cutoff_end=end,
        cutoff_cadence=policy.cutoff_cadence,
        universe_policy=policy.eligible_universe_policy,
        benchmark_code=policy.benchmark_code,
        return_horizons=policy.forward_return_horizons,
        ordered_symbols=symbols,
        symbol_set_checksum_sha256=symbol_set_checksum(symbols),
        inputs_json=identity,
    )
    session.commit()
    if not created and run.status != "planned":
        return _result_from_run(run, created=False, already_built=True, cutoff_count=len(cutoffs))

    universe = ObservedPitUniverse(session, market_id)
    previous_states: dict[UUID, str] = {}
    counters = {
        "eligible": 0,
        "excluded": 0,
        "snapshots": 0,
        "partial": 0,
        "unavailable": 0,
        "failures": 0,
    }
    for cutoff in cutoffs:
        for symbol in symbols:
            member = universe.resolve(symbol=symbol, cutoff=cutoff)
            if member is None:
                counters["excluded"] += 1
                continue
            counters["eligible"] += 1
            args = argparse.Namespace(
                research_profile=repository_root / policy.research_profile_asset,
                model_family=model_family,
                knowledge_cutoff=cutoff,
                ingest_gdelt_news=False,
                gdelt_raw_root=None,
                gdelt_license_class=None,
            )
            try:
                summary = (
                    runner(session, args, symbol, repository_root)
                    if runner is not None
                    else _default_research_runner(
                        session,
                        args,
                        symbol,
                        repository_root,
                        member,
                    )
                )
            except Exception as error:
                session.rollback()
                summary = {
                    "status": "failed",
                    "symbol": symbol,
                    "error": str(error),
                    "snapshot_id": None,
                    "financial_endpoint_status": "research_failed",
                }
            snapshot = _snapshot_from_summary(session, summary)
            state_sha = research_state_sha256(snapshot, summary)
            state_changed = previous_states.get(member.security_id) != state_sha
            previous_states[member.security_id] = state_sha
            observation_status = _observation_status(summary, snapshot)
            if snapshot is not None:
                counters["snapshots"] += 1
                if snapshot.snapshot_status == "partial_component_set":
                    counters["partial"] += 1
                if snapshot.configuration_checksum_sha256 != (configuration.record.checksum_sha256):
                    raise ValueError("historical snapshot used an unbound scoring configuration")
                if _utc(snapshot.knowledge_cutoff, "snapshot cutoff") > cutoff:
                    raise ValueError("historical snapshot exceeds requested knowledge cutoff")
            elif observation_status == "issuer_unavailable":
                counters["unavailable"] += 1
            else:
                counters["failures"] += 1
            repository.add_observation(
                run,
                BacktestObservationWrite(
                    company_id=member.company_id,
                    security_id=member.security_id,
                    score_snapshot_id=snapshot.id if snapshot is not None else None,
                    symbol=symbol,
                    knowledge_cutoff=cutoff,
                    observation_status=observation_status,
                    selected_fiscal_year=_int_or_none(summary.get("selected_fiscal_year")),
                    selected_fiscal_quarter=_int_or_none(summary.get("selected_fiscal_quarter")),
                    selected_filing_scope=_str_or_none(summary.get("selected_filing_scope")),
                    selected_period_end=_date_or_none(summary.get("selected_period_end")),
                    snapshot_status=snapshot.snapshot_status if snapshot is not None else None,
                    snapshot_fingerprint_sha256=(
                        snapshot.snapshot_fingerprint_sha256 if snapshot is not None else None
                    ),
                    research_state_projection_version=(policy.research_state_projection_version),
                    research_state_sha256=state_sha,
                    research_state_changed=state_changed,
                    detail_json=_json_value(summary),
                ),
            )
            session.commit()
    summary_json: dict[str, object] = {
        "cutoffs": len(cutoffs),
        "eligible_observations": counters["eligible"],
        "excluded_universe_decisions": counters["excluded"],
        "snapshot_observations": counters["snapshots"],
        "partial_snapshots": counters["partial"],
        "issuer_unavailable": counters["unavailable"],
        "research_failures": counters["failures"],
    }
    repository.set_status(run, "snapshots_built", summary=summary_json)
    session.commit()
    return HistoricalDatasetBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        created_run=created,
        already_built=False,
        cutoff_count=len(cutoffs),
        eligible_observations=counters["eligible"],
        excluded_universe_decisions=counters["excluded"],
        snapshot_observations=counters["snapshots"],
        partial_snapshots=counters["partial"],
        issuer_unavailable=counters["unavailable"],
        research_failures=counters["failures"],
    )


def research_state_sha256(snapshot: ScoreSnapshot | None, summary: dict[str, object]) -> str:
    """Hash substantive research state without volatile IDs or observation timestamps."""

    if snapshot is None:
        projection: object = {
            "endpoint_status": summary.get("financial_endpoint_status"),
            "status": summary.get("status"),
            "unavailable_reasons": summary.get("unavailable_reasons"),
        }
    else:
        components = sorted(snapshot.components, key=lambda item: item.component_code)
        projection = {
            "selected_fiscal_year": snapshot.ending_fiscal_year,
            "selected_fiscal_quarter": snapshot.ending_fiscal_quarter,
            "selected_filing_scope": snapshot.selected_filing_scope,
            "snapshot_status": snapshot.snapshot_status,
            "eligibility_eligible": snapshot.eligibility_eligible,
            "financial_core_coverage": snapshot.financial_core_coverage,
            "confidence": snapshot.confidence,
            "top_level_component_weight_coverage": (snapshot.top_level_component_weight_coverage),
            "available_component_codes": snapshot.available_component_codes_json,
            "missing_component_codes": snapshot.missing_component_codes_json,
            "final_score": snapshot.final_score,
            "components": [
                {
                    "code": item.component_code,
                    "score": item.score,
                    "coverage": item.subfactor_weight_coverage,
                    "missing": item.missing_subfactors_json,
                    "final_contribution": item.final_contribution,
                }
                for item in components
            ],
        }
    canonical = canonical_audit_value(projection)
    return sha256(
        __import__("json").dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _default_research_runner(
    session: Session,
    args: argparse.Namespace,
    symbol: str,
    repository_root: Path,
    member: HistoricalUniverseMember,
) -> dict[str, object]:
    security = session.get(Security, member.security_id)
    listing = session.get(ExchangeListing, member.listing_id)
    if security is None or listing is None or listing.security_id != security.id:
        raise ValueError("resolved historical research identity is unavailable")
    return _run_symbol_auto(
        session,
        args=args,
        symbol=symbol,
        repository_root=repository_root,
        identity_override=ProductionSecurityContext(security=security, listing=listing),
    )


def _snapshot_from_summary(session: Session, summary: dict[str, object]) -> ScoreSnapshot | None:
    value = summary.get("snapshot_id")
    if value is None:
        return None
    snapshot_id = value if isinstance(value, UUID) else UUID(str(value))
    snapshot = session.get(ScoreSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError("research summary references a missing score snapshot")
    return snapshot


def _observation_status(summary: dict[str, object], snapshot: ScoreSnapshot | None) -> str:
    if snapshot is not None:
        return "snapshot_frozen"
    endpoint = summary.get("financial_endpoint_status")
    if endpoint == "no_pit_visible_financial_endpoint":
        return "issuer_unavailable"
    return "research_failed"


def _result_from_run(
    run: Any, *, created: bool, already_built: bool, cutoff_count: int
) -> HistoricalDatasetBuildResult:
    summary = run.summary_json
    nested = summary.get("dataset_build") if isinstance(summary, dict) else None
    if isinstance(nested, dict):
        summary = nested
    return HistoricalDatasetBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        created_run=created,
        already_built=already_built,
        cutoff_count=cutoff_count,
        eligible_observations=int(summary.get("eligible_observations", 0)),
        excluded_universe_decisions=int(summary.get("excluded_universe_decisions", 0)),
        snapshot_observations=int(summary.get("snapshot_observations", 0)),
        partial_snapshots=int(summary.get("partial_snapshots", 0)),
        issuer_unavailable=int(summary.get("issuer_unavailable", 0)),
        research_failures=int(summary.get("research_failures", 0)),
    )


def _json_value(value: object) -> Any:
    canonical = canonical_audit_value(value)
    return canonical


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _date_or_none(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _utc(value, "timestamp").isoformat()

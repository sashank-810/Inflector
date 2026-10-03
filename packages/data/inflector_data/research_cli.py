"""Explicit production commands for immutable model initialization and V5 research."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from inflector_core.component_scoring import FinancialInflectionComponentScorer
from inflector_core.providers import ProviderMetadata
from inflector_core.scoring_policy import (
    ConfidenceEvaluator,
    EligibilityEvaluator,
    FinancialContextPolicyResolver,
    scoring_policy_checksum,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_endpoint import (
    FinancialEndpointResolver,
    FinancialEndpointResult,
    endpoint_metadata,
)
from inflector_data.gdelt_attention import (
    GDELT_NEWS_DATASET_CODE,
    GDELT_PROVIDER_CODE,
    GDELTHttpClient,
    GDELTNewsAttentionProvider,
)
from inflector_data.nse_cli import production_preflight
from inflector_data.production_policy import (
    ProductionPolicyBindingError,
    bind_production_policy,
    initialize_production_model,
    resolve_profile_datasets,
)
from inflector_data.production_research import (
    ProductionResearchAssembler,
    ProductionResearchAssemblyError,
    ProductionSecurityContext,
)
from inflector_data.research_profile import (
    load_profile_financial_endpoint_policy,
    load_profile_financial_primitive_policy,
    load_research_profile,
)
from inflector_data.score_orchestration import ScoreSnapshotOrchestrator
from inflector_data.service import IngestionService
from inflector_database.models import DataProvider
from inflector_database.score_repository import ScoreSnapshotRepository
from inflector_database.scoring_repository import ScoringPolicyRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Current production V5 research")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init-model")
    init.add_argument("--database-url", required=True)
    init.add_argument("--model-family", required=True)
    init.add_argument("--model-semantic-version", required=True)
    init.add_argument("--git-sha", required=True)
    init.add_argument("--effective-from", type=datetime.fromisoformat, required=True)
    init.add_argument("--research-profile", type=Path, required=True)

    attention = subparsers.add_parser("ingest-gdelt-news")
    attention.add_argument("--database-url", required=True)
    attention.add_argument("--research-profile", type=Path, required=True)
    attention_targets = attention.add_mutually_exclusive_group(required=True)
    attention_targets.add_argument("--symbol")
    attention_targets.add_argument("--symbols-file", type=Path)
    attention.add_argument("--max-symbols", type=int, default=25)
    attention.add_argument("--knowledge-cutoff", type=datetime.fromisoformat, required=True)
    attention.add_argument("--gdelt-raw-root", type=Path, required=True)
    attention.add_argument("--gdelt-license-class", required=True)

    run = subparsers.add_parser("run-current")
    run.add_argument("--database-url", required=True)
    run.add_argument("--model-family", required=True)
    run.add_argument("--research-profile", type=Path, required=True)
    targets = run.add_mutually_exclusive_group(required=True)
    targets.add_argument("--symbol")
    targets.add_argument("--symbols-file", type=Path)
    run.add_argument("--max-symbols", type=int, default=25)
    run.add_argument("--fiscal-year", type=int, required=True)
    run.add_argument("--fiscal-quarter", type=int, required=True)
    run.add_argument("--knowledge-cutoff", type=datetime.fromisoformat, required=True)
    run.add_argument("--ingest-gdelt-news", action="store_true")
    run.add_argument("--gdelt-raw-root", type=Path)
    run.add_argument("--gdelt-license-class")
    auto = subparsers.add_parser("run-current-auto")
    auto.add_argument("--database-url", required=True)
    auto.add_argument("--model-family", required=True)
    auto.add_argument("--research-profile", type=Path, required=True)
    auto_targets = auto.add_mutually_exclusive_group(required=True)
    auto_targets.add_argument("--symbol")
    auto_targets.add_argument("--symbols-file", type=Path)
    auto.add_argument("--max-symbols", type=int, default=25)
    auto.add_argument("--knowledge-cutoff", type=datetime.fromisoformat, required=True)
    auto.add_argument("--ingest-gdelt-news", action="store_true")
    auto.add_argument("--gdelt-raw-root", type=Path)
    auto.add_argument("--gdelt-license-class")
    return parser


def _production_preflight(session: Session) -> None:
    provider = session.scalar(select(DataProvider).where(DataProvider.code == "nse_official"))
    if provider is None:
        raise ProductionResearchAssemblyError("nse_official provider is unavailable")
    production_preflight(session, provider.licence_name)


def _symbols(args: argparse.Namespace) -> tuple[str, ...]:
    if args.max_symbols < 1 or args.max_symbols > 100:
        raise ValueError("max-symbols must be between 1 and 100")
    raw = (
        [args.symbol]
        if args.symbol is not None
        else args.symbols_file.read_text(encoding="utf-8-sig").splitlines()
    )
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        symbol = value.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    if not result:
        raise ValueError("at least one symbol is required")
    if len(result) > args.max_symbols:
        raise ValueError("symbols input exceeds max-symbols")
    return tuple(result)


def _gdelt_metadata(licence_class: str) -> ProviderMetadata:
    if not licence_class.strip():
        raise ValueError("GDELT license/source classification must be non-empty")
    return ProviderMetadata(
        provider_code=GDELT_PROVIDER_CODE,
        provider_type="https",
        dataset_code=GDELT_NEWS_DATASET_CODE,
        licence_class=licence_class,
        licence_reference="Caller-supplied classification; GDELT source terms apply.",
        redistributable=False,
    )


def _ingest_gdelt(
    session: Session,
    *,
    assembler: ProductionResearchAssembler,
    symbol: str,
    cutoff: datetime,
    window_days: int,
    raw_root: Path,
    licence_class: str,
) -> dict[str, object]:
    identity = assembler.resolve_symbol(symbol)
    company = identity.security.company
    window_end = cutoff
    window_start = window_end - timedelta(days=window_days)
    provider = GDELTNewsAttentionProvider(
        client=GDELTHttpClient(),
        metadata=_gdelt_metadata(licence_class),
        company_legal_name=company.legal_name,
        window_start_at=window_start,
        window_end_at=window_end,
    )
    result = IngestionService(session, LocalRawObjectStore(raw_root)).ingest_attention_data(
        provider
    )
    return {
        "status": result.status,
        "run_id": str(result.run_id),
        "records_accepted": result.records_accepted,
        "records_quarantined": result.records_quarantined,
        "records_duplicated": result.records_duplicated,
        "source_uri": provider.source_uri,
        "retrieved_at": (
            provider.retrieved_at.isoformat() if provider.retrieved_at is not None else None
        ),
    }


def _orchestrator(session: Session) -> ScoreSnapshotOrchestrator:
    return ScoreSnapshotOrchestrator(
        ScoringPolicyRepository(session),
        ScoreSnapshotRepository(session),
        FinancialContextPolicyResolver(),
        EligibilityEvaluator(),
        ConfidenceEvaluator(),
        FinancialInflectionComponentScorer(),
    )


def _run_symbol(
    session: Session,
    *,
    args: argparse.Namespace,
    symbol: str,
    repository_root: Path,
    endpoint_result: FinancialEndpointResult | None = None,
    identity_override: ProductionSecurityContext | None = None,
) -> dict[str, object]:
    profile = load_research_profile(args.research_profile)
    cutoff = _aware_utc(args.knowledge_cutoff, "knowledge_cutoff")
    policy_repository = ScoringPolicyRepository(session)
    resolved = policy_repository.resolve_active_configuration(
        model_family=args.model_family,
        at=cutoff,
    )
    if resolved is None:
        raise ProductionPolicyBindingError("active scoring configuration is unavailable")
    expected = bind_production_policy(session, profile, repository_root=repository_root)
    if (
        resolved.record.configuration_name != profile.research_profile_code
        or resolved.record.configuration_version != profile.profile_version
        or resolved.record.checksum_sha256 != scoring_policy_checksum(expected.policy)
    ):
        raise ProductionPolicyBindingError(
            "active scoring configuration does not match the requested research profile"
        )
    primitive_policy = load_profile_financial_primitive_policy(profile, repository_root)
    assembler = ProductionResearchAssembler(session, profile, primitive_policy)
    gdelt_summary = None
    if args.ingest_gdelt_news:
        if args.gdelt_raw_root is None or args.gdelt_license_class is None:
            raise ValueError(
                "--ingest-gdelt-news requires --gdelt-raw-root and --gdelt-license-class"
            )
        gdelt_summary = _ingest_gdelt(
            session,
            assembler=assembler,
            symbol=symbol,
            cutoff=cutoff,
            window_days=profile.attention_news_window_days,
            raw_root=args.gdelt_raw_root,
            licence_class=args.gdelt_license_class,
        )
    dataset_ids = resolve_profile_datasets(session, profile)
    evidence = assembler.assemble(
        symbol=symbol,
        fiscal_year=args.fiscal_year,
        fiscal_quarter=args.fiscal_quarter,
        knowledge_cutoff=cutoff,
        dataset_ids=dataset_ids,
        policy=resolved.policy,
        financial_scope_override=(
            endpoint_result.filing_scope if endpoint_result is not None else None
        ),
        identity_override=identity_override,
    )
    security = evidence.identity.security
    result = _orchestrator(session).orchestrate_opportunity_score_and_persist(
        model_family=args.model_family,
        company_id=security.company_id,
        security_id=security.id,
        ending_fiscal_year=args.fiscal_year,
        ending_fiscal_quarter=args.fiscal_quarter,
        knowledge_cutoff=cutoff,
        eligibility_inputs=evidence.eligibility_inputs,
        confidence_inputs=evidence.confidence_inputs,
        cross_domain_context_candidates=evidence.cross_domain_context_candidates,
        business_catalyst_context_candidates=(evidence.business_catalyst_context_candidates),
        market_structure_evidence=evidence.market_structure_evidence,
        low_market_attention_evidence=evidence.low_market_attention_evidence,
    )
    session.commit()
    snapshot = result.record
    summary: dict[str, object] = {
        "status": "completed",
        "created": result.created,
        "company": security.company.legal_name,
        "symbol": symbol,
        "isin": security.isin,
        "knowledge_cutoff": cutoff,
        "fiscal_year": args.fiscal_year,
        "fiscal_quarter": args.fiscal_quarter,
        "model_family": args.model_family,
        "model_version_id": snapshot.model_version_id,
        "configuration_id": snapshot.scoring_configuration_id,
        "configuration_checksum": snapshot.configuration_checksum_sha256,
        "financial_contexts_attempted": [
            {"provider_dataset_id": provider, "filing_scope": scope}
            for provider, scope in evidence.attempted_financial_contexts
        ],
        "selected_financial_context": {
            "provider_dataset_id": snapshot.selected_provider_dataset_id,
            "filing_scope": snapshot.selected_filing_scope,
        }
        if snapshot.selected_provider_dataset_id is not None
        else None,
        "available_component_codes": snapshot.available_component_codes_json,
        "missing_component_codes": snapshot.missing_component_codes_json,
        "top_level_coverage": snapshot.top_level_component_weight_coverage,
        "confidence": snapshot.confidence,
        "snapshot_status": snapshot.snapshot_status,
        "snapshot_id": snapshot.id,
        "snapshot_fingerprint": snapshot.snapshot_fingerprint_sha256,
        "final_score": snapshot.final_score,
        "unavailable_reasons": evidence.unavailable_reasons,
        "attention_news_status": evidence.attention_news_status,
        "analyst_attention_status": evidence.analyst_attention_status,
        "delivery_status": evidence.delivery_status,
        "delivery_observation_count": evidence.delivery_observation_count,
        "delivery_pit_cutoff": evidence.delivery_pit_cutoff,
        "financial_primitive_policy": evidence.financial_primitive_policy,
        "revenue_source_status": evidence.revenue_source_status,
        "reported_ebitda_status": evidence.reported_ebitda_status,
        "debt_source_status": evidence.debt_source_status,
        "total_debt_status": evidence.total_debt_status,
        "market_cap_qualification_status": "not_approved",
        "gdelt_ingestion": gdelt_summary,
        "research_profile_checksum": profile.checksum_sha256,
        "repository_root": str(repository_root),
    }
    if endpoint_result is not None:
        summary.update(endpoint_metadata(endpoint_result))
    return summary


def _run_symbol_auto(
    session: Session,
    *,
    args: argparse.Namespace,
    symbol: str,
    repository_root: Path,
    identity_override: ProductionSecurityContext | None = None,
) -> dict[str, object]:
    profile = load_research_profile(args.research_profile)
    endpoint_policy = load_profile_financial_endpoint_policy(profile, repository_root)
    if endpoint_policy is None:
        raise ValueError("automatic research requires an explicit financial endpoint policy")
    primitive_policy = load_profile_financial_primitive_policy(profile, repository_root)
    assembler = ProductionResearchAssembler(session, profile, primitive_policy)
    identity = identity_override or assembler.resolve_symbol(symbol)
    dataset_ids = resolve_profile_datasets(session, profile)
    financial_id = dataset_ids.get("financial")
    if financial_id is None:
        raise ProductionResearchAssemblyError("financial provider binding is required")
    endpoint = FinancialEndpointResolver(session, endpoint_policy).resolve(
        provider_dataset_id=financial_id,
        company_id=identity.security.company_id,
        filing_scope_priority=profile.financial_scope_priority,
        knowledge_cutoff=args.knowledge_cutoff,
    )
    if endpoint.status != "selected":
        return {
            "status": "failed"
            if endpoint.status == "ambiguous_financial_endpoint"
            else "completed",
            "symbol": symbol,
            "company": identity.security.company.legal_name,
            "isin": identity.security.isin,
            "snapshot_id": None,
            "snapshot_status": None,
            "final_score": None,
            "unavailable_reasons": {"financial_endpoint": endpoint.status},
            **endpoint_metadata(endpoint),
        }
    assert endpoint.fiscal_year is not None and endpoint.fiscal_quarter is not None
    explicit_args = argparse.Namespace(
        **{
            **vars(args),
            "fiscal_year": endpoint.fiscal_year,
            "fiscal_quarter": endpoint.fiscal_quarter,
        }
    )
    if identity_override is None:
        return _run_symbol(
            session,
            args=explicit_args,
            symbol=symbol,
            repository_root=repository_root,
            endpoint_result=endpoint,
        )
    return _run_symbol(
        session,
        args=explicit_args,
        symbol=symbol,
        repository_root=repository_root,
        endpoint_result=endpoint,
        identity_override=identity,
    )


def _execute(args: argparse.Namespace, session: Session) -> tuple[dict[str, object], bool]:
    _production_preflight(session)
    repository_root = Path(__file__).resolve().parents[3]
    profile = load_research_profile(args.research_profile)
    if args.command == "init-model":
        initialized = initialize_production_model(
            session,
            profile=profile,
            repository_root=repository_root,
            model_family=args.model_family,
            model_semantic_version=args.model_semantic_version,
            git_sha=args.git_sha,
            effective_from=args.effective_from,
        )
        return {
            "status": "completed",
            "model_version_id": initialized.model_version.id,
            "scoring_configuration_id": initialized.scoring_configuration.id,
            "configuration_checksum": initialized.policy_checksum_sha256,
            "created_model": initialized.created_model,
            "created_configuration": initialized.created_configuration,
            "dataset_bindings": initialized.dataset_ids,
            "research_profile_code": profile.research_profile_code,
            "research_profile_checksum": profile.checksum_sha256,
        }, True

    if args.command == "ingest-gdelt-news":
        cutoff = _aware_utc(args.knowledge_cutoff, "knowledge_cutoff")
        assembler = ProductionResearchAssembler(session, profile)
        results: list[dict[str, object]] = []
        failed = False
        for symbol in _symbols(args):
            try:
                results.append(
                    {
                        "symbol": symbol,
                        **_ingest_gdelt(
                            session,
                            assembler=assembler,
                            symbol=symbol,
                            cutoff=cutoff,
                            window_days=profile.attention_news_window_days,
                            raw_root=args.gdelt_raw_root,
                            licence_class=args.gdelt_license_class,
                        ),
                    }
                )
            except Exception as error:
                session.rollback()
                failed = True
                results.append({"symbol": symbol, "status": "failed", "error": str(error)})
        return {"status": "failed" if failed else "completed", "results": results}, not failed

    results: list[dict[str, object]] = []
    operational_failure = False
    for symbol in _symbols(args):
        try:
            result = (
                _run_symbol_auto(
                    session,
                    args=args,
                    symbol=symbol,
                    repository_root=repository_root,
                )
                if args.command == "run-current-auto"
                else _run_symbol(
                    session,
                    args=args,
                    symbol=symbol,
                    repository_root=repository_root,
                )
            )
            results.append(result)
            operational_failure = operational_failure or result.get("status") == "failed"
        except Exception as error:
            session.rollback()
            operational_failure = True
            results.append({"status": "failed", "symbol": symbol, "error": str(error)})
    return {
        "status": "failed" if operational_failure else "completed",
        "results": results,
    }, not operational_failure


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            payload, succeeded = _execute(args, session)
        print(json.dumps(payload, default=_json_default, sort_keys=True))
        return 0 if succeeded else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, UUID, Decimal)):
        return str(value) if not isinstance(value, datetime) else value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


if __name__ == "__main__":
    sys.exit(main())

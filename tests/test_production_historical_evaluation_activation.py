"""Production S1 authoritative activation, temporal split, and holdout contracts."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.historical_activation_policy import (
    load_historical_evaluation_activation_policy,
    load_multibagger_error_analysis_policy,
    load_multibagger_temporal_split_policy,
)
from inflector_data.historical_evaluation_activation import (
    HistoricalEvaluationActivationError,
    HistoricalEvaluationDataBlockedError,
    build_multibagger_error_analysis,
    build_multibagger_temporal_split,
    historical_evaluation_readiness,
    official_session_semantic_projection,
    partition_temporal_cutoffs,
    verify_historical_session_calendar,
)
from inflector_data.historical_evaluation_policy import (
    canonical_json_sha256,
    load_historical_universe_policy,
)
from inflector_data.historical_universe import build_historical_universe
from inflector_data.nse_historical_mii import (
    NSE_HISTORICAL_MII_DATASET_CODE,
    NSEHistoricalMIISecurityProvider,
    evidence_for_ingestion,
    ingest_historical_mii,
    mii_security_filename,
)
from inflector_data.nse_http import AcquiredNSEArtifact, NSEHostNotAllowedError
from inflector_data.nse_providers import (
    HttpNSEArtifactSource,
    LocalNSEArtifactSource,
    NSEFormatError,
)
from inflector_database.models import (
    BenchmarkBar,
    BenchmarkSeries,
    DataProvider,
    HistoricalMIIArtifactAcquisition,
    HistoricalSessionArtifactAcquisition,
    HistoricalSessionCalendarDate,
    HistoricalSessionCalendarRun,
    HistoricalUniverseRun,
    IngestionRun,
    MultibaggerConfusionRow,
    MultibaggerErrorAnalysisRow,
    MultibaggerErrorAnalysisRun,
    MultibaggerEvaluationCohort,
    MultibaggerEvaluationRun,
    MultibaggerOutcomeLabel,
    MultibaggerPredictionRow,
    MultibaggerTemporalSplitCutoff,
    MultibaggerTemporalSplitRun,
    ProviderDataset,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
ACTIVATION_POLICY = ROOT / "config/backtest/production_historical_evaluation_activation_v1.json"
SPLIT_POLICY = ROOT / "config/backtest/production_multibagger_temporal_split_v1.json"
ERROR_POLICY = ROOT / "config/backtest/production_multibagger_error_analysis_v1.json"
SOURCE_DATE = date(2020, 1, 31)
RETRIEVED_AT = datetime(2026, 10, 10, 8, tzinfo=UTC)
MII_URI = "https://nsearchives.nseindia.com/content/cm/" + mii_security_filename(SOURCE_DATE)


class _StaticSource:
    def __init__(self, payload: bytes, uri: str = MII_URI) -> None:
        self.source_uri = uri
        self._artifact = AcquiredNSEArtifact(uri, payload, RETRIEVED_AT)

    def acquire(self) -> AcquiredNSEArtifact:
        return self._artifact


class _FakeHttpClient:
    def __init__(self, payload: bytes, uri: str) -> None:
        self.artifact = AcquiredNSEArtifact(uri, payload, RETRIEVED_AT)

    def acquire(self, source_uri: str) -> AcquiredNSEArtifact:
        assert source_uri == self.artifact.source_uri
        return self.artifact


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        provider_code="nse_official",
        provider_type="https",
        dataset_code=NSE_HISTORICAL_MII_DATASET_CODE,
        licence_class="official-source-terms-reviewed-locally",
        licence_reference="official NSE source terms",
        redistributable=False,
    )


def _mii_payload(
    *,
    first_symbol: str = "ALPHA",
    second_symbol: str = "BETA",
    include_unsupported: bool = True,
    duplicate: bool = False,
) -> bytes:
    rows = [
        "Sgmt,FinInstrmTp,ISIN,TckrSymb,SctySrs,FinInstrmNm",
        f"CM,STK,INE000A01001,{first_symbol},EQ,Alpha Limited",
        f"CM,STK,INE000B01002,{second_symbol},EQ,Beta Limited",
    ]
    if include_unsupported:
        rows.append("CM,STK,INE000C01003,GAMMA,BE,Gamma Limited")
    if duplicate:
        rows.append(f"CM,STK,INE000A01001,{first_symbol},EQ,Duplicate")
    return gzip.compress(("\n".join(rows) + "\n").encode())


def _provider(payload: bytes, *, uri: str = MII_URI, maximum: int = 75_000_000):
    return NSEHistoricalMIISecurityProvider(
        HttpNSEArtifactSource(_FakeHttpClient(payload, uri), uri),  # type: ignore[arg-type]
        _metadata(),
        SOURCE_DATE,
        maximum_decompressed_bytes=maximum,
    )


def test_s1_policy_assets_are_strict_checksum_bound() -> None:
    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_POLICY, repository_root=ROOT
    )
    split = load_multibagger_temporal_split_policy(SPLIT_POLICY)
    analysis = load_multibagger_error_analysis_policy(ERROR_POLICY)
    assert activation.code == "production_historical_evaluation_activation_v1"
    assert activation.mii_dataset_code == "nse_cm_mii_security_daily"
    assert activation.inherited_checksums["evaluation"] == (
        "121236d373fc7a6c7b7fe53881217ddce2cf607125f4ca20c3e71c3d128d3a11"
    )
    assert split.code == "production_multibagger_temporal_split_v1"
    assert (split.development_percent, split.validation_percent, split.holdout_percent) == (
        60,
        20,
        20,
    )
    assert analysis.allowed_split_codes == ("development", "validation")
    assert analysis.confusion_classes == ("fp", "fn")


def test_temporal_split_policy_rejects_foreign_activation_checksum(tmp_path: Path) -> None:
    value = json.loads(SPLIT_POLICY.read_text(encoding="utf-8"))
    value["activation_policy_checksum_sha256"] = "f" * 64
    altered = tmp_path / "foreign-activation.json"
    altered.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="temporal split policy semantics"):
        load_multibagger_temporal_split_policy(altered)


def test_valid_mii_artifact_archives_original_and_is_idempotent(session, tmp_path: Path) -> None:
    payload = _mii_payload()
    archive = LocalRawObjectStore(tmp_path / "raw")
    first = ingest_historical_mii(session, archive=archive, provider=_provider(payload))
    second = ingest_historical_mii(session, archive=archive, provider=_provider(payload))
    assert first.ingestion is not None and second.ingestion is not None
    assert first.ingestion.records_received == first.ingestion.records_accepted == 2
    assert first.unsupported_row_count == 1
    assert second.ingestion.run_id == first.ingestion.run_id
    assert second.reused is True
    sources = tuple(
        session.scalars(
            select(SourceRecord).where(SourceRecord.ingestion_run_id == first.ingestion.run_id)
        )
    )
    assert len(sources) == 2
    assert {item.external_record_id for item in sources} == {
        "nse-cm-mii-security:2020-01-31:INE000A01001:EQ",
        "nse-cm-mii-security:2020-01-31:INE000B01002:EQ",
    }
    assert all(item.available_at == RETRIEVED_AT.replace(tzinfo=None) for item in sources)
    assert all(item.raw_content_sha256 == sha256(payload).hexdigest() for item in sources)
    assert (tmp_path / "raw" / sources[0].raw_object_key).read_bytes() == payload
    evidence = evidence_for_ingestion(
        session, archive=archive, ingestion_run_id=first.ingestion.run_id
    )
    assert len(evidence) == 2
    assert {item.semantic_row["SctySrs"] for item in evidence} == {"EQ"}


def test_local_mii_acquisition_reports_first_create_then_reuse_without_q_evidence(
    session, tmp_path: Path
) -> None:
    artifact = tmp_path / mii_security_filename(SOURCE_DATE)
    artifact.write_bytes(_mii_payload())
    source = LocalNSEArtifactSource(artifact, MII_URI, clock=lambda: RETRIEVED_AT)
    provider = NSEHistoricalMIISecurityProvider(source, _metadata(), SOURCE_DATE)
    archive = LocalRawObjectStore(tmp_path / "raw")

    first = ingest_historical_mii(session, archive=archive, provider=provider)
    second = ingest_historical_mii(session, archive=archive, provider=provider)

    assert first.reused is False
    assert second.reused is True
    assert first.acquisition_id == second.acquisition_id
    assert first.acquisition_provenance == "operator_supplied_unverified"
    assert first.acquisition_status == "unverified"
    assert first.ingestion is None and second.ingestion is None
    assert first.evidence == second.evidence == ()


def test_s1_authoritative_mii_evidence_is_accepted_by_q_without_rewriting(
    session, tmp_path: Path
) -> None:
    archive = LocalRawObjectStore(tmp_path / "raw")
    result = ingest_historical_mii(session, archive=archive, provider=_provider(_mii_payload()))
    assert result.ingestion is not None and result.provider_dataset_id is not None
    policy = load_historical_universe_policy(
        ROOT / "config/backtest/production_historical_universe_v1.json"
    )
    built = build_historical_universe(
        session,
        policy=policy,
        cutoff=datetime(2020, 1, 31, 20, tzinfo=UTC),
        source_provider_dataset_id=result.provider_dataset_id,
        source_ingestion_run_id=result.ingestion.run_id,
        evidence=evidence_for_ingestion(
            session, archive=archive, ingestion_run_id=result.ingestion.run_id
        ),
        completed_at=RETRIEVED_AT,
    )
    assert built.total_members == 2


@pytest.mark.parametrize(
    ("payload", "uri", "maximum", "message"),
    (
        (_mii_payload(), MII_URI.replace("31012020", "30012020"), 75_000_000, "filename"),
        (b"not-gzip", MII_URI, 75_000_000, "valid gzip"),
        (gzip.compress(b"Sgmt,ISIN\nCM,X\n"), MII_URI, 75_000_000, "required headers"),
        (_mii_payload(duplicate=True), MII_URI, 75_000_000, "duplicate semantic"),
        (_mii_payload(), MII_URI, 8, "decompressed byte limit"),
        (
            gzip.compress(b"Sgmt,FinInstrmTp,ISIN,TckrSymb,SctySrs\nCM,STK,INE,X,EQ,EXTRA\n"),
            MII_URI,
            75_000_000,
            "row shape",
        ),
    ),
)
def test_mii_adapter_fails_closed_on_unsafe_artifacts(
    payload: bytes, uri: str, maximum: int, message: str
) -> None:
    with pytest.raises(NSEFormatError, match=message):
        _provider(payload, uri=uri, maximum=maximum).fetch()


def test_mii_adapter_requires_an_allowlisted_official_nse_source_uri() -> None:
    uri = "https://example.invalid/content/cm/NSE_CM_security_31012020.csv.gz"
    with pytest.raises(NSEHostNotAllowedError, match="not allowlisted"):
        _provider(_mii_payload(), uri=uri).fetch()


def test_changed_mii_artifact_creates_distinct_source_and_malformed_input_fails_closed(
    session, tmp_path: Path
) -> None:
    archive = LocalRawObjectStore(tmp_path / "raw")
    first_payload = _mii_payload(include_unsupported=False)
    first = ingest_historical_mii(session, archive=archive, provider=_provider(first_payload))
    changed_payload = _mii_payload(
        first_symbol="ALPHA2", second_symbol="BETA2", include_unsupported=False
    )
    changed = ingest_historical_mii(session, archive=archive, provider=_provider(changed_payload))
    assert first.ingestion is not None and changed.ingestion is not None
    assert changed.ingestion.run_id != first.ingestion.run_id
    assert changed.raw_content_sha256 != first.raw_content_sha256

    provenance_only_revision = _mii_payload().replace(b"Gamma Limited", b"Gamma Revised")
    revised = ingest_historical_mii(
        session,
        archive=archive,
        provider=_provider(provenance_only_revision),
    )
    assert revised.ingestion is not None
    assert revised.ingestion.run_id == first.ingestion.run_id
    assert revised.acquisition_id not in {first.acquisition_id, changed.acquisition_id}
    assert revised.acquisition_status == "reused_semantic_snapshot"
    assert {item.semantic_row["ISIN"] for item in revised.evidence} == {
        "INE000A01001",
        "INE000B01002",
    }

    malformed_payload = gzip.compress(b"Sgmt,ISIN\nCM,X\n")
    with pytest.raises(NSEFormatError, match="required headers"):
        ingest_historical_mii(session, archive=archive, provider=_provider(malformed_payload))


def _calendar_foundation(
    session,
    expected: tuple[date, ...],
    observed: tuple[date, ...],
    *,
    from_date: date = date(2020, 1, 1),
    to_date: date = date(2020, 1, 31),
):
    provider = DataProvider(
        code="nse_official",
        provider_type="https",
        licence_name="official",
        licence_reference="official",
    )
    session.add(provider)
    session.flush()
    source_dataset = ProviderDataset(
        provider_id=provider.id,
        code="nse_cm_official_session_calendar",
        licence_class="official",
        redistributable=False,
    )
    benchmark_dataset = ProviderDataset(
        provider_id=provider.id,
        code="nse_indices_daily",
        licence_class="official",
        redistributable=False,
    )
    session.add_all((source_dataset, benchmark_dataset))
    session.flush()
    source_run = IngestionRun(
        provider_dataset_id=source_dataset.id,
        status="completed",
        started_at=RETRIEVED_AT,
        finished_at=RETRIEVED_AT,
        records_received=1,
        records_accepted=1,
        records_quarantined=0,
        records_duplicated=0,
    )
    benchmark_run = IngestionRun(
        provider_dataset_id=benchmark_dataset.id,
        status="completed",
        started_at=RETRIEVED_AT,
        finished_at=RETRIEVED_AT,
        records_received=len(observed),
        records_accepted=len(observed),
        records_quarantined=0,
        records_duplicated=0,
    )
    session.add_all((source_run, benchmark_run))
    session.flush()
    projection = official_session_semantic_projection(
        from_date=from_date,
        to_date=to_date,
        session_dates=expected,
    )
    calendar_source = SourceRecord(
        ingestion_run_id=source_run.id,
        provider_dataset_id=source_dataset.id,
        external_record_id="nse-official-session-calendar:2020-01",
        source_uri="https://nsearchives.nseindia.com/content/official/session-calendar.csv",
        raw_object_key="sha256/aa/" + "a" * 64,
        raw_payload_reference="session-calendar.csv",
        raw_content_sha256="a" * 64,
        content_sha256=canonical_json_sha256(projection),
        retrieved_at=RETRIEVED_AT,
        reported_at=None,
        published_at=None,
        available_at=RETRIEVED_AT,
        revision_at=None,
        parse_status="parsed",
        validation_status="accepted",
    )
    series = BenchmarkSeries(
        provider_dataset_id=benchmark_dataset.id,
        code="NIFTY 50",
        display_name="NIFTY 50",
        currency="INR",
        status="active",
    )
    session.add_all((calendar_source, series))
    session.flush()
    for index, trading_date in enumerate(observed):
        source = SourceRecord(
            ingestion_run_id=benchmark_run.id,
            provider_dataset_id=benchmark_dataset.id,
            external_record_id=f"benchmark:{trading_date.isoformat()}",
            source_uri="https://nsearchives.nseindia.com/content/indices/daily.csv",
            raw_object_key=f"sha256/bb/{index:064x}",
            raw_payload_reference=f"row-{index + 2}",
            raw_content_sha256=f"{index:064x}",
            content_sha256=f"{index + 100:064x}",
            retrieved_at=RETRIEVED_AT,
            reported_at=None,
            published_at=None,
            available_at=RETRIEVED_AT,
            revision_at=None,
            parse_status="parsed",
            validation_status="accepted",
        )
        session.add(source)
        session.flush()
        session.add(
            BenchmarkBar(
                benchmark_series_id=series.id,
                source_record_id=source.id,
                trading_date=trading_date,
                interval="1d",
                open_value=Decimal("100"),
                high_value=Decimal("101"),
                low_value=Decimal("99"),
                close_value=Decimal("100"),
                available_at=RETRIEVED_AT,
                revision_at=None,
            )
        )
    session.flush()
    return calendar_source, benchmark_dataset


def test_session_calendar_cannot_be_self_certified_or_inferred_from_weekdays(session) -> None:
    with pytest.raises(TypeError, match="expected_session_dates"):
        verify_historical_session_calendar(
            session,
            policy=load_historical_evaluation_activation_policy(
                ACTIVATION_POLICY, repository_root=ROOT
            ),
            source_artifact_acquisition_id=uuid4(),
            archive=LocalRawObjectStore(Path("unused")),
            benchmark_provider_dataset_id=uuid4(),
            requested_from_date=date(2020, 1, 1),
            requested_to_date=date(2020, 1, 31),
            source_cutoff=RETRIEVED_AT,
            expected_session_dates=(date(2020, 1, 4),),  # type: ignore[call-arg]
        )


def _monthly_cutoffs(count: int, *, start_year: int = 2010) -> tuple[datetime, ...]:
    values: list[datetime] = []
    year = start_year
    month = 1
    for _ in range(count):
        values.append(datetime(year, month, 1, tzinfo=UTC))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(values)


def test_temporal_split_is_chronological_mature_and_label_independent() -> None:
    policy = load_multibagger_temporal_split_policy(SPLIT_POLICY)
    dates = _monthly_cutoffs(60)
    outcome_cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    first = partition_temporal_cutoffs(
        policy=policy,
        historical_cutoffs=tuple(reversed(dates)),
        outcome_data_cutoff=outcome_cutoff,
    )
    second = partition_temporal_cutoffs(
        policy=policy,
        historical_cutoffs=dates,
        outcome_data_cutoff=outcome_cutoff,
    )
    assert first == second
    assert [item.split_code for item in first].count("development") == 36
    assert [item.split_code for item in first].count("validation") == 12
    assert [item.split_code for item in first].count("holdout") == 12
    assert tuple(item.historical_cutoff for item in first) == dates
    assert canonical_json_sha256(
        [(item.historical_cutoff.isoformat(), item.split_code) for item in first]
    ) == canonical_json_sha256(
        [(item.historical_cutoff.isoformat(), item.split_code) for item in second]
    )


def test_temporal_split_blocks_insufficient_or_unmatured_cohorts() -> None:
    policy = load_multibagger_temporal_split_policy(SPLIT_POLICY)
    with pytest.raises(HistoricalEvaluationDataBlockedError, match="insufficient"):
        partition_temporal_cutoffs(
            policy=policy,
            historical_cutoffs=_monthly_cutoffs(59),
            outcome_data_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
        )


def _evaluation_foundation(session, *, cohort_count: int = 60):
    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_POLICY, repository_root=ROOT
    )
    evaluation = MultibaggerEvaluationRun(
        run_key_sha256="1" * 64,
        evaluation_policy_code="production_multibagger_evaluation_v1",
        evaluation_policy_checksum_sha256=(
            "121236d373fc7a6c7b7fe53881217ddce2cf607125f4ca20c3e71c3d128d3a11"
        ),
        backtest_policy_checksum_sha256=(
            "e1f1f886f686045f7f2774be355674a726b155db1a32c9080999c32becd35984"
        ),
        universe_policy_checksum_sha256=(
            "ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982"
        ),
        outcome_policy_checksum_sha256=(
            "3c901049e37dfa1764d70feb7e8fdfa8762b3c4733ea283f8b6d99c73475332f"
        ),
        research_profile_checksum_sha256=(
            "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
        ),
        model_family="opportunity_score_v5",
        scoring_configuration_checksum_sha256="b" * 64,
        ordered_bundle_checksums_json=[],
        selection_contracts_json=[],
        outcome_contracts_json=[],
        algorithm_versions_json={},
        inputs_json={},
        status="completed",
        summary_json={},
        completed_at=RETRIEVED_AT,
    )
    session.add(evaluation)
    session.flush()
    cutoffs = _monthly_cutoffs(cohort_count)
    cohorts: list[MultibaggerEvaluationCohort] = []
    for index, cutoff in enumerate(cutoffs):
        cohort = MultibaggerEvaluationCohort(
            multibagger_evaluation_run_id=evaluation.id,
            historical_universe_run_id=uuid4(),
            historical_cutoff=cutoff,
            universe_run_key_sha256=f"{index + 1:064x}",
            eligible_resolved_count=2,
            unresolved_count=0,
            ambiguous_count=0,
            unsupported_count=0,
            ordered_security_checksum_sha256=f"{index + 101:064x}",
            backtest_run_ids_json=[],
            label_run_ids_json=[],
            outcome_data_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
            bundle_checksum_sha256=f"{index + 201:064x}",
            rankable_count=1,
            unrankable_count=1,
            calendar_integrity_status="not_independently_verified",
            status="completed",
            summary_json={},
        )
        session.add(cohort)
        cohorts.append(cohort)
    calendar_source, benchmark_dataset = _calendar_foundation(
        session,
        (date(2010, 1, 1),),
        (date(2010, 1, 1),),
        from_date=date(2010, 1, 1),
        to_date=date(2020, 1, 1),
    )
    source_dataset = session.get(ProviderDataset, calendar_source.provider_dataset_id)
    source_run = session.get(IngestionRun, calendar_source.ingestion_run_id)
    series = session.scalar(
        select(BenchmarkSeries).where(
            BenchmarkSeries.provider_dataset_id == benchmark_dataset.id,
            BenchmarkSeries.code == "NIFTY 50",
        )
    )
    assert source_dataset is not None and source_run is not None and series is not None
    session_acquisition = HistoricalSessionArtifactAcquisition(
        acquisition_key_sha256="9" * 64,
        acquisition_provenance="verified_official_https",
        source_provider_dataset_id=source_dataset.id,
        source_ingestion_run_id=source_run.id,
        source_record_id=calendar_source.id,
        source_uri=calendar_source.source_uri,
        raw_object_key=calendar_source.raw_object_key,
        raw_content_sha256=calendar_source.raw_content_sha256,
        retrieved_at=RETRIEVED_AT,
        published_at=None,
        parser_version="fixture_official_parser_v1",
        provenance_status="production_authoritative",
        status="parsed",
        detail_json={},
    )
    session.add(session_acquisition)
    session.flush()
    verification_payload = {
        "policy_checksum_sha256": activation.checksum_sha256,
        "source_record_id": str(calendar_source.id),
        "source_content_sha256": calendar_source.content_sha256,
        "raw_content_sha256": calendar_source.raw_content_sha256,
        "benchmark_dataset_id": str(benchmark_dataset.id),
        "benchmark_series_id": str(series.id),
        "requested_from_date": "2010-01-01",
        "requested_to_date": "2020-01-01",
        "expected_sessions": ["2010-01-01"],
        "observed_sessions": ["2010-01-01"],
        "source_cutoff": RETRIEVED_AT.isoformat(),
        "algorithm": activation.calendar_verification_algorithm,
    }
    verification_checksum = canonical_json_sha256(verification_payload)
    calendar_run_key = canonical_json_sha256(
        {"verification_checksum_sha256": verification_checksum, "status": "verified"}
    )
    calendar = HistoricalSessionCalendarRun(
        run_key_sha256=calendar_run_key,
        activation_policy_code="production_historical_evaluation_activation_v1",
        activation_policy_checksum_sha256=activation.checksum_sha256,
        source_provider_dataset_id=source_dataset.id,
        source_ingestion_run_id=source_run.id,
        source_record_id=calendar_source.id,
        source_artifact_acquisition_id=session_acquisition.id,
        acquisition_provenance="verified_official_https",
        parser_version="fixture_official_parser_v1",
        benchmark_provider_dataset_id=benchmark_dataset.id,
        benchmark_series_id=series.id,
        raw_object_key=calendar_source.raw_object_key,
        raw_content_sha256=calendar_source.raw_content_sha256,
        source_uri=calendar_source.source_uri,
        source_published_at=None,
        source_retrieved_at=RETRIEVED_AT,
        requested_from_date=date(2010, 1, 1),
        requested_to_date=date(2020, 1, 1),
        expected_count=1,
        observed_count=1,
        missing_dates_json=[],
        unexpected_dates_json=[],
        first_expected_session=date(2010, 1, 1),
        last_expected_session=date(2010, 1, 1),
        first_observed_session=date(2010, 1, 1),
        last_observed_session=date(2010, 1, 1),
        source_cutoff=RETRIEVED_AT,
        verification_checksum_sha256=verification_checksum,
        status="verified",
        summary_json={},
    )
    session.add(calendar)
    session.flush()
    session.add(
        HistoricalSessionCalendarDate(
            historical_session_calendar_run_id=calendar.id,
            session_date=date(2010, 1, 1),
            benchmark_observed=True,
            source_record_id=calendar_source.id,
            date_fingerprint_sha256=canonical_json_sha256(
                {
                    "run_key_sha256": calendar_run_key,
                    "session_date": "2010-01-01",
                    "benchmark_observed": True,
                }
            ),
        )
    )
    session.commit()
    return evaluation, cohorts, calendar


def _attach_authoritative_universe_provenance(session, cohorts) -> None:
    source_dataset = session.scalar(
        select(ProviderDataset).where(
            ProviderDataset.code == "nse_cm_official_session_calendar"
        )
    )
    assert source_dataset is not None
    provider = session.get(DataProvider, source_dataset.provider_id)
    assert provider is not None and provider.code == "nse_official"
    mii_dataset = ProviderDataset(
        provider_id=provider.id,
        code="nse_cm_mii_security_daily",
        licence_class="official",
        redistributable=False,
    )
    session.add(mii_dataset)
    session.flush()
    for index, cohort in enumerate(cohorts):
        source_date = cohort.historical_cutoff.date()
        ingestion = IngestionRun(
            provider_dataset_id=mii_dataset.id,
            status="completed",
            started_at=RETRIEVED_AT,
            finished_at=RETRIEVED_AT,
            records_received=2,
            records_accepted=2,
            records_quarantined=0,
            records_duplicated=0,
        )
        session.add(ingestion)
        session.flush()
        raw_sha = f"{index + 5000:064x}"
        artifact_uri = (
            "https://nsearchives.nseindia.com/content/cm/"
            + mii_security_filename(source_date)
        )
        for row_index, isin in enumerate(("INE000A01001", "INE000B01002"), start=1):
            session.add(
                SourceRecord(
                    ingestion_run_id=ingestion.id,
                    provider_dataset_id=mii_dataset.id,
                    external_record_id=(
                        f"nse-cm-mii-security:{source_date.isoformat()}:{isin}:EQ"
                    ),
                    source_uri=artifact_uri,
                    raw_object_key=f"sha256/{raw_sha[:2]}/{raw_sha}",
                    raw_payload_reference=(
                        f"{mii_security_filename(source_date)}#row-{row_index + 1}"
                    ),
                    raw_content_sha256=raw_sha,
                    content_sha256=f"{index + row_index + 9000:064x}",
                    retrieved_at=RETRIEVED_AT,
                    reported_at=None,
                    published_at=None,
                    available_at=RETRIEVED_AT,
                    revision_at=None,
                    parse_status="parsed",
                    validation_status="accepted",
                )
            )
        session.flush()
        session.add(
            HistoricalMIIArtifactAcquisition(
                acquisition_key_sha256=f"{index + 6000:064x}",
                requested_source_date=source_date,
                acquisition_provenance="verified_official_https",
                source_uri=artifact_uri,
                raw_object_key=f"sha256/{raw_sha[:2]}/{raw_sha}",
                raw_content_sha256=raw_sha,
                retrieved_at=RETRIEVED_AT,
                expected_filename=mii_security_filename(source_date),
                parser_version="nse_cm_mii_security_csv_gzip_v1",
                provenance_status="production_authoritative",
                status="accepted",
                linked_ingestion_run_id=ingestion.id,
                detail_json={"eligible_row_count": 2},
            )
        )
        universe = HistoricalUniverseRun(
            run_key_sha256=f"{index + 7000:064x}",
            universe_policy_code="production_historical_universe_v1",
            universe_policy_checksum_sha256=(
                "ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982"
            ),
            cutoff=cohort.historical_cutoff,
            source_provider_dataset_id=mii_dataset.id,
            source_state_checksum_sha256=f"{index + 8000:064x}",
            member_count=2,
            status="completed",
            inputs_json={
                "source_ingestion_run_id": str(ingestion.id),
                "source_date": source_date.isoformat(),
            },
            summary_json={"eligible": 2},
            completed_at=RETRIEVED_AT,
        )
        session.add(universe)
        session.flush()
        cohort.historical_universe_run_id = universe.id
    session.commit()


@pytest.mark.parametrize(
    "corruption",
    (
        "arbitrary_source_dataset",
        "acquisition_provenance",
        "raw_sha",
        "source_uri",
        "source_record",
        "wrong_provider",
        "wrong_session_dataset",
        "wrong_benchmark_dataset",
        "wrong_benchmark_series",
        "missing_dates",
        "unexpected_dates",
        "row_count",
        "foreign_activation_checksum",
    ),
)
def test_persisted_verified_calendar_fails_closed_on_graph_corruption(
    session, corruption: str
) -> None:
    evaluation, _, calendar = _evaluation_foundation(session)
    acquisition = session.get(
        HistoricalSessionArtifactAcquisition,
        calendar.source_artifact_acquisition_id,
    )
    source_dataset = session.get(ProviderDataset, calendar.source_provider_dataset_id)
    benchmark_dataset = session.get(
        ProviderDataset, calendar.benchmark_provider_dataset_id
    )
    benchmark_series = session.get(BenchmarkSeries, calendar.benchmark_series_id)
    assert (
        acquisition is not None
        and source_dataset is not None
        and benchmark_dataset is not None
        and benchmark_series is not None
    )
    provider = session.get(DataProvider, source_dataset.provider_id)
    assert provider is not None

    if corruption == "arbitrary_source_dataset":
        arbitrary = uuid4()
        calendar.source_provider_dataset_id = arbitrary
        acquisition.source_provider_dataset_id = arbitrary
    elif corruption == "acquisition_provenance":
        acquisition.acquisition_provenance = "operator_supplied_unverified"
    elif corruption == "raw_sha":
        calendar.raw_content_sha256 = "f" * 64
    elif corruption == "source_uri":
        calendar.source_uri += "?spoofed=1"
    elif corruption == "source_record":
        calendar.source_record_id = uuid4()
        acquisition.source_record_id = calendar.source_record_id
    elif corruption == "wrong_provider":
        provider.code = "not_nse_official"
    elif corruption == "wrong_session_dataset":
        source_dataset.code = "wrong_session_dataset"
    elif corruption == "wrong_benchmark_dataset":
        benchmark_dataset.code = "wrong_benchmark_dataset"
    elif corruption == "wrong_benchmark_series":
        benchmark_series.code = "NOT NIFTY 50"
    elif corruption == "missing_dates":
        calendar.missing_dates_json = ["2010-01-02"]
    elif corruption == "unexpected_dates":
        calendar.unexpected_dates_json = ["2010-01-02"]
    elif corruption == "row_count":
        calendar.expected_count = 2
        calendar.observed_count = 2
    elif corruption == "foreign_activation_checksum":
        calendar.activation_policy_checksum_sha256 = "0" * 64
    else:  # pragma: no cover - exhaustive parameter guard
        raise AssertionError(corruption)
    session.flush()

    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_POLICY, repository_root=ROOT
    )
    split_policy = load_multibagger_temporal_split_policy(SPLIT_POLICY)
    with pytest.raises(HistoricalEvaluationActivationError):
        build_multibagger_temporal_split(
            session,
            policy=split_policy,
            source_evaluation_run_id=evaluation.id,
            session_calendar_run_id=calendar.id,
            completed_at=RETRIEVED_AT,
        )
    readiness = historical_evaluation_readiness(
        session,
        policy=activation,
        split_policy=split_policy,
        source_evaluation_run_id=evaluation.id,
        session_calendar_run_id=calendar.id,
    )
    assert readiness["status"] == "DATA-BLOCKED"
    blockers = readiness["blockers"]
    assert isinstance(blockers, list)
    assert "exact_production_verified_session_calendar_unavailable" in blockers


@pytest.mark.parametrize(("cohort_count", "expected_status"), ((59, "DATA-BLOCKED"), (60, "READY")))
def test_readiness_uses_exact_temporal_split_sufficiency(
    session, cohort_count: int, expected_status: str
) -> None:
    evaluation, cohorts, calendar = _evaluation_foundation(
        session, cohort_count=cohort_count
    )
    _attach_authoritative_universe_provenance(session, cohorts)
    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_POLICY, repository_root=ROOT
    )
    split_policy = load_multibagger_temporal_split_policy(SPLIT_POLICY)

    result = historical_evaluation_readiness(
        session,
        policy=activation,
        split_policy=split_policy,
        source_evaluation_run_id=evaluation.id,
        session_calendar_run_id=calendar.id,
    )

    assert result["status"] == expected_status
    assert result["temporal_split_policy_checksum_sha256"] == split_policy.checksum_sha256
    blockers = result["blockers"]
    assert isinstance(blockers, list)
    if cohort_count == 59:
        assert "insufficient_fully_matured_cutoffs_for_temporal_split" in blockers
    else:
        assert blockers == []


@pytest.mark.parametrize(
    "corruption",
    (
        "acquisition_provenance",
        "acquisition_source_uri",
        "acquisition_raw_sha",
        "acquisition_raw_object_key",
        "acquisition_requested_source_date",
        "acquisition_expected_filename",
        "acquisition_parser_version",
        "acquisition_linked_ingestion_run",
        "source_raw_sha",
        "source_raw_object_key",
        "source_source_uri",
        "source_provider_dataset",
        "source_ingestion_run",
        "source_external_identity",
        "source_validation_status",
        "source_parse_status",
        "source_available_at_precedes_retrieval",
        "ingestion_provider_dataset",
        "ingestion_status",
    ),
)
def test_readiness_rejects_each_mii_persisted_graph_corruption(
    session, corruption: str
) -> None:
    evaluation, cohorts, calendar = _evaluation_foundation(session)
    _attach_authoritative_universe_provenance(session, cohorts)
    cohort = cohorts[0]
    universe = session.get(HistoricalUniverseRun, cohort.historical_universe_run_id)
    assert universe is not None
    ingestion_id = UUID(str(universe.inputs_json["source_ingestion_run_id"]))
    ingestion = session.get(IngestionRun, ingestion_id)
    source = session.scalar(
        select(SourceRecord)
        .where(SourceRecord.ingestion_run_id == ingestion_id)
        .order_by(SourceRecord.id)
    )
    acquisition = session.scalar(
        select(HistoricalMIIArtifactAcquisition).where(
            HistoricalMIIArtifactAcquisition.linked_ingestion_run_id == ingestion_id
        )
    )
    assert ingestion is not None and source is not None and acquisition is not None

    if corruption == "acquisition_provenance":
        acquisition.acquisition_provenance = "operator_supplied_unverified"
        acquisition.linked_ingestion_run_id = None
    elif corruption == "acquisition_source_uri":
        acquisition.source_uri += "?mismatch=1"
    elif corruption == "acquisition_raw_sha":
        acquisition.raw_content_sha256 = "f" * 64
    elif corruption == "acquisition_raw_object_key":
        acquisition.raw_object_key += ".mismatch"
    elif corruption == "acquisition_requested_source_date":
        acquisition.requested_source_date += timedelta(days=1)
    elif corruption == "acquisition_expected_filename":
        acquisition.expected_filename = "NSE_CM_security_01011999.csv.gz"
    elif corruption == "acquisition_parser_version":
        acquisition.parser_version = "wrong_parser"
    elif corruption == "acquisition_linked_ingestion_run":
        acquisition.linked_ingestion_run_id = uuid4()
    elif corruption == "source_raw_sha":
        source.raw_content_sha256 = "f" * 64
    elif corruption == "source_raw_object_key":
        source.raw_object_key += ".mismatch"
    elif corruption == "source_source_uri":
        source.source_uri += "?mismatch=1"
    elif corruption == "source_provider_dataset":
        source.provider_dataset_id = uuid4()
    elif corruption == "source_ingestion_run":
        source.ingestion_run_id = uuid4()
    elif corruption == "source_external_identity":
        source.external_record_id = "wrong:identity"
    elif corruption == "source_validation_status":
        source.validation_status = "quarantined"
    elif corruption == "source_parse_status":
        source.parse_status = "failed"
    elif corruption == "source_available_at_precedes_retrieval":
        source.available_at = RETRIEVED_AT - timedelta(seconds=1)
    elif corruption == "ingestion_provider_dataset":
        ingestion.provider_dataset_id = uuid4()
    elif corruption == "ingestion_status":
        ingestion.status = "failed"
    else:  # pragma: no cover - exhaustive parameter guard
        raise AssertionError(corruption)
    session.flush()

    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_POLICY, repository_root=ROOT
    )
    result = historical_evaluation_readiness(
        session,
        policy=activation,
        split_policy=load_multibagger_temporal_split_policy(SPLIT_POLICY),
        source_evaluation_run_id=evaluation.id,
        session_calendar_run_id=calendar.id,
    )
    assert result["status"] == "DATA-BLOCKED"
    blockers = result["blockers"]
    assert isinstance(blockers, list)
    assert f"cohort_{cohort.id}_authoritative_mii_provenance_unavailable" in blockers


def _outcome_label(session, *, classification: str, ordinal: int) -> MultibaggerOutcomeLabel:
    label = MultibaggerOutcomeLabel(
        multibagger_label_run_id=uuid4(),
        backtest_observation_id=uuid4(),
        contract_code="MB_2X_2Y",
        classification=classification,
        unavailable_reason=None,
        threshold_multiple=Decimal("2"),
        horizon_calendar_years=2,
        entry_trading_date=date(2010, 1, 4),
        adjusted_entry_close=Decimal("100"),
        horizon_end_date=date(2012, 1, 4),
        first_threshold_hit_trading_date=(
            date(2011, 1, 4) if classification == "positive" else None
        ),
        trading_observations_available=500,
        peak_adjusted_close=Decimal("210" if classification == "positive" else "120"),
        peak_price_multiple=Decimal("2.1" if classification == "positive" else "1.2"),
        maximum_forward_price_return=Decimal("1.1" if classification == "positive" else "0.2"),
        endpoint_adjusted_close=Decimal("110"),
        endpoint_return=Decimal("0.1"),
        calendar_days_to_threshold=(365 if classification == "positive" else None),
        trading_observations_to_threshold=(250 if classification == "positive" else None),
        maximum_drawdown=Decimal("-0.3"),
        listing_valid_to=None,
        label_matured_at=RETRIEVED_AT,
        outcome_data_cutoff=RETRIEVED_AT,
        provenance_json={"fixture": "factual"},
        label_fingerprint_sha256=f"{ordinal:064x}",
    )
    session.add(label)
    session.flush()
    return label


def _prediction(
    session,
    *,
    cohort: MultibaggerEvaluationCohort,
    rankable: bool,
    ordinal: int,
) -> MultibaggerPredictionRow:
    row = MultibaggerPredictionRow(
        multibagger_evaluation_cohort_id=cohort.id,
        backtest_observation_id=uuid4(),
        company_id=uuid4(),
        security_id=uuid4(),
        historical_symbol=f"SEC{ordinal}",
        score_snapshot_id=None,
        snapshot_fingerprint_sha256=None,
        snapshot_status="final_score_available" if rankable else None,
        rankable=rankable,
        unranked_reason=None if rankable else "issuer_unavailable",
        final_score=Decimal("90") if rankable else None,
        dense_score_rank=1 if rankable else None,
        display_order=1 if rankable else None,
        available_component_codes_json=["quality", "growth"] if rankable else [],
        prediction_fingerprint_sha256=f"{ordinal + 1000:064x}",
    )
    session.add(row)
    session.flush()
    return row


def _confusion(
    session,
    *,
    prediction: MultibaggerPredictionRow,
    label: MultibaggerOutcomeLabel,
    confusion_class: str,
    ordinal: int,
) -> MultibaggerConfusionRow:
    row = MultibaggerConfusionRow(
        multibagger_prediction_row_id=prediction.id,
        multibagger_outcome_label_id=label.id,
        outcome_contract_code="MB_2X_2Y",
        selection_contract_code="TOP_5",
        evaluation_view="end_to_end",
        selected=confusion_class == "fp",
        actual_label="negative" if confusion_class == "fp" else "positive",
        confusion_class=confusion_class,
        threshold_hit_trading_date=label.first_threshold_hit_trading_date,
        endpoint_return=label.endpoint_return,
        peak_price_multiple=label.peak_price_multiple,
        maximum_drawdown=label.maximum_drawdown,
        label_provenance_json=label.provenance_json,
        confusion_fingerprint_sha256=f"{ordinal + 2000:064x}",
    )
    session.add(row)
    session.flush()
    return row


def test_split_freezes_holdout_before_outcomes_and_analysis_never_queries_it(
    session,
) -> None:
    evaluation, cohorts, calendar = _evaluation_foundation(session)
    split = build_multibagger_temporal_split(
        session,
        policy=load_multibagger_temporal_split_policy(SPLIT_POLICY),
        source_evaluation_run_id=evaluation.id,
        session_calendar_run_id=calendar.id,
        completed_at=RETRIEVED_AT,
    )
    assignments = tuple(
        session.scalars(
            select(MultibaggerTemporalSplitCutoff).where(
                MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id == split.run_id
            )
        )
    )
    assert sum(item.split_code == "development" for item in assignments) == 36
    assert sum(item.split_code == "validation" for item in assignments) == 12
    assert sum(item.split_code == "holdout" for item in assignments) == 12

    development = next(item for item in assignments if item.split_code == "development")
    holdout = next(item for item in assignments if item.split_code == "holdout")
    dev_cohort = next(
        item for item in cohorts if item.id == development.multibagger_evaluation_cohort_id
    )
    holdout_cohort = next(
        item for item in cohorts if item.id == holdout.multibagger_evaluation_cohort_id
    )
    fp_prediction = _prediction(session, cohort=dev_cohort, rankable=True, ordinal=1)
    fn_prediction = _prediction(session, cohort=dev_cohort, rankable=False, ordinal=2)
    holdout_prediction = _prediction(session, cohort=holdout_cohort, rankable=True, ordinal=3)
    fp = _confusion(
        session,
        prediction=fp_prediction,
        label=_outcome_label(session, classification="negative", ordinal=1),
        confusion_class="fp",
        ordinal=1,
    )
    fn = _confusion(
        session,
        prediction=fn_prediction,
        label=_outcome_label(session, classification="positive", ordinal=2),
        confusion_class="fn",
        ordinal=2,
    )
    holdout_confusion = _confusion(
        session,
        prediction=holdout_prediction,
        label=_outcome_label(session, classification="negative", ordinal=3),
        confusion_class="fp",
        ordinal=3,
    )
    session.commit()

    outcome_parameters: list[str] = []

    def observe_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        if "multibagger_confusion_rows" in normalized or "multibagger_outcome_labels" in normalized:
            outcome_parameters.append(str(parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        result = build_multibagger_error_analysis(
            session,
            policy=load_multibagger_error_analysis_policy(ERROR_POLICY),
            temporal_split_run_id=split.run_id,
            completed_at=RETRIEVED_AT,
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert result.row_count == 2
    assert result.development_rows == 2 and result.validation_rows == 0
    assert result.summary["holdout_outcomes_read"] is False
    rows = tuple(
        session.scalars(
            select(MultibaggerErrorAnalysisRow).where(
                MultibaggerErrorAnalysisRow.multibagger_error_analysis_run_id == result.run_id
            )
        )
    )
    assert {item.source_confusion_row_id for item in rows} == {fp.id, fn.id}
    assert holdout_confusion.id not in {item.source_confusion_row_id for item in rows}
    assert all(item.split_code in {"development", "validation"} for item in rows)
    parameter_text = " ".join(outcome_parameters)
    assert str(holdout_cohort.id) not in parameter_text
    assert holdout_cohort.id.hex not in parameter_text
    counts = result.summary["error_counts"]
    assert isinstance(counts, dict)
    assert counts["development"] == {
        "fp": 1,
        "fn": 1,
        "unrankable_positive_fn": 1,
        "rankable_positive_fn_below_boundary": 0,
    }


def test_error_analysis_is_idempotent_and_never_persists_holdout_split(session) -> None:
    evaluation, _, calendar = _evaluation_foundation(session)
    split = build_multibagger_temporal_split(
        session,
        policy=load_multibagger_temporal_split_policy(SPLIT_POLICY),
        source_evaluation_run_id=evaluation.id,
        session_calendar_run_id=calendar.id,
        completed_at=RETRIEVED_AT,
    )
    policy = load_multibagger_error_analysis_policy(ERROR_POLICY)
    first = build_multibagger_error_analysis(
        session,
        policy=policy,
        temporal_split_run_id=split.run_id,
        completed_at=RETRIEVED_AT,
    )
    second = build_multibagger_error_analysis(
        session,
        policy=policy,
        temporal_split_run_id=split.run_id,
        completed_at=RETRIEVED_AT,
    )
    assert first.run_id == second.run_id
    assert second.created is False
    assert (
        session.scalar(
            select(MultibaggerErrorAnalysisRow.id).where(
                MultibaggerErrorAnalysisRow.split_code == "holdout"
            )
        )
        is None
    )
    with pytest.raises(HistoricalEvaluationDataBlockedError, match="insufficient"):
        partition_temporal_cutoffs(
            policy=load_multibagger_temporal_split_policy(SPLIT_POLICY),
            historical_cutoffs=_monthly_cutoffs(60, start_year=2018),
            outcome_data_cutoff=datetime(2022, 1, 1, tzinfo=UTC),
        )


def test_historical_evaluation_readiness_is_deterministic_and_read_only(session) -> None:
    policy = load_historical_evaluation_activation_policy(ACTIVATION_POLICY, repository_root=ROOT)
    tables = (
        IngestionRun,
        HistoricalSessionCalendarRun,
        MultibaggerEvaluationRun,
        MultibaggerTemporalSplitRun,
        MultibaggerErrorAnalysisRun,
    )
    before = tuple(session.scalar(select(func.count()).select_from(table)) for table in tables)
    split_policy = load_multibagger_temporal_split_policy(SPLIT_POLICY)
    first = historical_evaluation_readiness(
        session, policy=policy, split_policy=split_policy
    )
    second = historical_evaluation_readiness(
        session, policy=policy, split_policy=split_policy
    )
    after = tuple(session.scalar(select(func.count()).select_from(table)) for table in tables)
    assert first == second
    assert first == {
        "status": "DATA-BLOCKED",
        "source_evaluation_run_id": None,
        "session_calendar_run_id": None,
        "temporal_split_policy_checksum_sha256": split_policy.checksum_sha256,
        "blockers": [
            "exact_completed_production_r_evaluation_unavailable",
            "exact_production_verified_session_calendar_unavailable",
        ],
    }
    assert before == after == (0, 0, 0, 0, 0)
    assert not session.new and not session.dirty and not session.deleted


def test_migration_0025_adds_only_s1_tables_and_downgrades(tmp_path: Path) -> None:
    database = tmp_path / "s1-migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "20261005_0024")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "20261006_0025")
    after = set(inspect(engine).get_table_names())
    assert after - before == {
        "historical_mii_artifact_acquisitions",
        "historical_session_artifact_acquisitions",
        "historical_session_calendar_runs",
        "historical_session_calendar_dates",
        "multibagger_temporal_split_runs",
        "multibagger_temporal_split_cutoffs",
        "multibagger_error_analysis_runs",
        "multibagger_error_analysis_rows",
    }
    command.downgrade(config, "20261005_0024")
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()

"""Production I fail-closed evidence-gap qualification regressions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from inflector_core.providers import ProviderMetadata
from inflector_data.attention_features import AttentionFeaturePrimitives, AttentionSeriesIdentity
from inflector_data.attention_pit import PointInTimeAttentionReader
from inflector_data.nse_http import AcquiredNSEArtifact
from inflector_data.nse_providers import (
    NSE_MARKET_DATASET_CODE,
    NSEMarketDataProvider,
    nse_market_filename,
)
from inflector_data.operations_profile import load_operations_profile
from inflector_data.research_profile import load_research_profile

ROOT = Path(__file__).parents[1]
RETRIEVED_AT = datetime(2026, 10, 3, 12, tzinfo=UTC)
TRADE_DATE = date(2026, 9, 30)


class _StaticSource:
    source_uri = "https://nsearchives.nseindia.com/fictional/market.zip"

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def acquire(self) -> AcquiredNSEArtifact:
        return AcquiredNSEArtifact(self.source_uri, self._payload, RETRIEVED_AT)


def test_accepted_profiles_remain_immutable_and_no_gap_profiles_are_created() -> None:
    research_expected = (
        "182d139eaf6a6603b3fa817cdc590676454d2e4051e49a495a32591747f51f80",
        "ed14c7c3aa73278793da16d2d6a1de23ea19274f98bd91b64908fedbee9677b8",
        "a1510f311babe3edcab242b228f79fc6353e34a399bc8145aec6764909eb4676",
        "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1",
    )
    operations_expected = (
        "ffefef5ba249ef93eb8ac7db26297a579ed6d59ad02204ad263a7bc354d39d4c",
        "129392617256d4808b03bacf138ef54d020329fcece9d6738c28d28169716f23",
        "fc2db825f7397f4b6aee76155c45ba01e3db082d15b0418badee9c47b8c50976",
    )
    research = tuple(
        load_research_profile(ROOT / f"config/research/production_research_v{version}.json")
        for version in range(1, 5)
    )
    operations = tuple(
        load_operations_profile(ROOT / f"config/operations/production_operations_v{version}.json")
        for version in range(1, 4)
    )
    assert tuple(profile.checksum_sha256 for profile in research) == research_expected
    assert tuple(profile.checksum_sha256 for profile in operations) == operations_expected
    assert research[-1].analyst_coverage_source is None
    assert research[-1].provider_datasets["analyst_attention"] is None
    assert not (ROOT / "config/research/production_research_v5.json").exists()


def test_official_udiff_market_evidence_does_not_fabricate_market_cap() -> None:
    csv_payload = (ROOT / "tests/fixtures/nse_udiff_synthetic.csv").read_bytes()
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(nse_market_filename(TRADE_DATE), csv_payload)
    batch = NSEMarketDataProvider(
        _StaticSource(stream.getvalue()),
        ProviderMetadata(
            "nse_official",
            "https",
            NSE_MARKET_DATASET_CODE,
            "fictional-reviewed-source-terms",
        ),
        TRADE_DATE,
    ).fetch_market_data()
    assert len(batch.records) == 1
    assert batch.records[0].record.market_cap is None


def test_missing_analyst_source_remains_unavailable_not_zero(session) -> None:
    cutoff = datetime(2026, 10, 3, 12, tzinfo=UTC)
    bundle = AttentionFeaturePrimitives(PointInTimeAttentionReader(session)).features_as_of(
        company_id=uuid4(),
        security_id=None,
        company_level_only=True,
        as_of=cutoff,
        news_series=AttentionSeriesIdentity(uuid4(), "news", "news_v1", "a" * 64),
        news_window_start_at=cutoff - timedelta(days=30),
        news_window_end_at=cutoff,
        analyst_series=AttentionSeriesIdentity(
            uuid4(), "unconfigured_analyst_coverage_source", "unconfigured_v1", "0" * 64
        ),
        analyst_observation_on_or_before=None,
    )
    assert bundle.analyst_observation is None
    assert bundle.analyst_coverage_count.value is None
    assert bundle.analyst_coverage_count.warnings == ("missing_analyst_coverage_observation",)
    assert bundle.analyst_snapshot_age_days.value is None

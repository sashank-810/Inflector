"""Phase 2C action, listing-history, and security-succession tests."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from sqlalchemy import func, select

from inflector_core.providers import (
    CorporateActionRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
    UniverseRecord,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import (
    CSVCorporateActionProvider,
    CSVUniverseProvider,
    MockCorporateActionProvider,
    MockUniverseProvider,
)
from inflector_data.service import IngestionService
from inflector_database.models import (
    CorporateAction,
    DataQualityIssue,
    ExchangeListing,
    Security,
    SecurityRelationship,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 7, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
ACTIONS = ProviderMetadata(
    "synthetic_csv", "csv", "corporate_actions", "synthetic-development-only"
)
SECOND_ACTIONS = ProviderMetadata(
    "synthetic_csv_b", "csv", "corporate_actions", "synthetic-development-only"
)
AURORA_ISIN = "INF0AUR01018"


def _service(session, tmp_path: Path) -> IngestionService:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    return service


def _at(month: int, day: int) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


def _action_record(**overrides: object) -> CorporateActionRecord:
    return replace(
        CorporateActionRecord(
            security_isin=AURORA_ISIN,
            action_type="cash_dividend",
            announcement_date=None,
            ex_date=None,
            record_date=None,
            effective_date=None,
            ratio_numerator=None,
            ratio_denominator=None,
            cash_amount=Decimal("5"),
            cash_currency="INR",
            cash_unit="INR/share",
            subscription_price=None,
            subscription_currency=None,
            exchange=None,
            old_symbol=None,
            new_symbol=None,
            successor_isin=None,
        ),
        **overrides,
    )


def _action_provider(
    entries: list[tuple[str, CorporateActionRecord, datetime, datetime | None]],
    metadata: ProviderMetadata = ACTIONS,
) -> MockCorporateActionProvider:
    raw_rows = [
        f"{external_id}:{record!r}:{available_at!r}:{revision_at!r}"
        for external_id, record, available_at, revision_at in entries
    ]
    raw_payload = "\n".join(raw_rows).encode()
    envelopes = tuple(
        IngestionEnvelope(
            provider=metadata,
            external_record_id=external_id,
            source_uri="fixture://corporate-actions",
            raw_payload_reference=f"row-{index}",
            content_sha256=sha256(raw_rows[index - 1].encode()).hexdigest(),
            retrieved_at=RETRIEVED_AT,
            record=record,
            available_at=available_at,
            revision_at=revision_at,
        )
        for index, (external_id, record, available_at, revision_at) in enumerate(entries, start=1)
    )
    return MockCorporateActionProvider(
        ProviderBatch(metadata, "fixture://corporate-actions", raw_payload, RETRIEVED_AT, envelopes)
    )


def _universe_provider(record: UniverseRecord) -> MockUniverseProvider:
    raw_payload = repr(record).encode()
    return MockUniverseProvider(
        ProviderBatch(
            UNIVERSE,
            "fixture://universe",
            raw_payload,
            RETRIEVED_AT,
            (
                IngestionEnvelope(
                    provider=UNIVERSE,
                    external_record_id="SYN-OVERLAP",
                    source_uri="fixture://universe",
                    raw_payload_reference="row-1",
                    content_sha256=sha256(raw_payload).hexdigest(),
                    retrieved_at=RETRIEVED_AT,
                    record=record,
                ),
            ),
        )
    )


def test_actions_preserve_terms_history_and_security_successor(session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    provider = CSVCorporateActionProvider(
        FIXTURES / "corporate_actions_synthetic.csv", ACTIONS, RETRIEVED_AT
    )
    result = service.ingest_corporate_actions(provider)
    repeat = service.ingest_corporate_actions(provider)

    actions = list(session.scalars(select(CorporateAction)))
    split = next(action for action in actions if action.action_type == "split")
    dividend = next(action for action in actions if action.action_type == "cash_dividend")
    rights = next(action for action in actions if action.action_type == "rights")
    listings = list(
        session.scalars(select(ExchangeListing).where(ExchangeListing.exchange == "NSE"))
    )
    assert result.records_accepted == 6 and repeat.records_duplicated == 6
    assert (split.ratio_numerator, split.ratio_denominator) == (2, 1)
    assert dividend.cash_amount == Decimal("5") and dividend.cash_unit == "INR/share"
    assert (rights.ratio_numerator, rights.ratio_denominator, rights.subscription_price) == (
        1,
        4,
        Decimal("80"),
    )
    assert {(listing.symbol, listing.valid_to) for listing in listings} >= {
        ("AURORA", datetime(2026, 5, 10).date()),
        ("AURORANEW", None),
    }
    assert session.scalar(select(Security).where(Security.isin == "INF0AUR01018")) is not None
    assert session.scalar(select(SecurityRelationship)) is not None


def test_invalid_action_rows_are_quarantined(session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    result = service.ingest_corporate_actions(
        CSVCorporateActionProvider(
            FIXTURES / "corporate_actions_invalid_synthetic.csv", ACTIONS, RETRIEVED_AT
        )
    )
    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert result.records_quarantined == 2
    assert rules == {"invalid_ratio", "unknown_security"}


def test_cash_dividend_event_anchors_use_ex_date_then_effective_date_fallback(
    session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    ex_date = date(2026, 3, 10)
    fallback_date = date(2026, 4, 10)
    distinct_fallback_date = date(2026, 5, 10)
    first = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "DIV-EX",
                    _action_record(ex_date=ex_date, effective_date=date(2026, 3, 11)),
                    _at(3, 1),
                    None,
                ),
                ("DIV-FALLBACK", _action_record(effective_date=fallback_date), _at(4, 1), None),
                (
                    "DIV-FALLBACK-DISTINCT",
                    _action_record(effective_date=distinct_fallback_date),
                    _at(5, 1),
                    None,
                ),
            ]
        )
    )
    alternate = service.ingest_corporate_actions(
        _action_provider(
            [("DIV-FALLBACK-ALT", _action_record(effective_date=fallback_date), _at(4, 2), None)]
        )
    )
    correction = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "DIV-FALLBACK",
                    _action_record(effective_date=fallback_date, cash_amount=Decimal("6")),
                    _at(4, 3),
                    _at(4, 3),
                )
            ]
        )
    )

    dividends = list(
        session.scalars(
            select(CorporateAction)
            .where(CorporateAction.action_type == "cash_dividend")
            .order_by(CorporateAction.available_at)
        )
    )
    alternate_source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "DIV-FALLBACK-ALT")
    )
    assert (first.records_accepted, alternate.records_duplicated, correction.records_accepted) == (
        3,
        1,
        1,
    )
    assert [
        (action.ex_date, action.effective_date, action.cash_amount) for action in dividends
    ] == [
        (ex_date, date(2026, 3, 11), Decimal("5")),
        (None, fallback_date, Decimal("5")),
        (None, fallback_date, Decimal("6")),
        (None, distinct_fallback_date, Decimal("5")),
    ]
    assert alternate_source is not None
    assert alternate_source.validation_status == "duplicate_economic"


def test_distinct_same_type_actions_can_arrive_out_of_chronological_order(
    session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    result = service.ingest_corporate_actions(
        _action_provider(
            [
                ("DIV-LATE", _action_record(ex_date=date(2026, 9, 10)), _at(9, 1), None),
                ("DIV-EARLY", _action_record(ex_date=date(2026, 3, 10)), _at(3, 1), None),
            ]
        )
    )
    assert result.records_accepted == 2
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 2


def test_ordered_and_ambiguous_action_corrections_are_distinguished(
    session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    initial = _action_record(ex_date=date(2026, 6, 10))
    service.ingest_corporate_actions(
        _action_provider([("DIV-CORR", initial, _at(6, 1), None)])
    )
    ordered = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "DIV-CORR",
                    _action_record(ex_date=date(2026, 6, 10), cash_amount=Decimal("6")),
                    _at(6, 2),
                    _at(6, 2),
                )
            ]
        )
    )
    ambiguous = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "DIV-AMBIGUOUS",
                    _action_record(ex_date=date(2026, 6, 10), cash_amount=Decimal("7")),
                    _at(6, 2),
                    None,
                )
            ]
        )
    )

    assert ordered.records_accepted == 1 and ambiguous.records_quarantined == 1
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 2
    assert session.scalar(
        select(DataQualityIssue).where(
            DataQualityIssue.rule_code == "ambiguous_action_revision"
        )
    ) is not None


def test_same_external_id_with_corrected_event_date_is_an_ordered_revision(
    session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    service.ingest_corporate_actions(
        _action_provider(
            [("DIV-DATE-CORR", _action_record(ex_date=date(2026, 7, 10)), _at(7, 1), None)]
        )
    )
    corrected = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "DIV-DATE-CORR",
                    _action_record(ex_date=date(2026, 7, 11)),
                    _at(7, 2),
                    _at(7, 2),
                )
            ]
        )
    )

    assert corrected.records_accepted == 1
    assert {action.ex_date for action in session.scalars(select(CorporateAction))} == {
        date(2026, 7, 10),
        date(2026, 7, 11),
    }


def test_corporate_action_provider_datasets_are_independent(session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    record = _action_record(ex_date=date(2026, 8, 10))
    first = service.ingest_corporate_actions(
        _action_provider([("DIV-A", record, _at(8, 1), None)])
    )
    second = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "DIV-B",
                    _action_record(ex_date=date(2026, 8, 10), cash_amount=Decimal("9")),
                    _at(8, 1),
                    None,
                )
            ],
            SECOND_ACTIONS,
        )
    )
    assert (first.records_accepted, second.records_accepted) == (1, 1)
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 2


def test_action_specific_identity_terms_are_not_silently_equivalent(
    session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    symbol = _action_record(
        action_type="symbol_change",
        effective_date=date(2026, 5, 10),
        exchange="NSE",
        old_symbol="AURORA",
        new_symbol="AURORANEW",
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    service.ingest_corporate_actions(
        _action_provider([("SYMBOL-ONE", symbol, _at(5, 1), None)])
    )
    different_symbol = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "SYMBOL-TWO",
                    _action_record(
                        action_type="symbol_change",
                        effective_date=date(2026, 5, 10),
                        exchange="NSE",
                        old_symbol="AURORA",
                        new_symbol="AURORAALT",
                        cash_amount=None,
                        cash_currency=None,
                        cash_unit=None,
                    ),
                    _at(5, 2),
                    _at(5, 2),
                )
            ]
        )
    )
    replacement = _action_record(
        action_type="security_replacement",
        effective_date=date(2026, 6, 10),
        successor_isin="INF0AUR02017",
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    service.ingest_corporate_actions(
        _action_provider([("REPLACE-ONE", replacement, _at(6, 1), None)])
    )
    different_replacement = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "REPLACE-TWO",
                    _action_record(
                        action_type="security_replacement",
                        effective_date=date(2026, 6, 10),
                        successor_isin="INF0AUR03016",
                        cash_amount=None,
                        cash_currency=None,
                        cash_unit=None,
                    ),
                    _at(6, 2),
                    _at(6, 2),
                )
            ]
        )
    )
    symbol_source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "SYMBOL-TWO")
    )
    assert different_symbol.records_quarantined == 1
    assert different_replacement.records_accepted == 1
    assert symbol_source is not None and symbol_source.validation_status != "duplicate_economic"


def test_listing_interval_validation_and_symbol_identity_failures(session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    overlapping = UniverseRecord(
        "Aurora Fabrication Limited",
        "Aurora Fabrication",
        "Industrials",
        "Engineering",
        AURORA_ISIN,
        "equity",
        "active",
        "NSE",
        "AURORA-OVERLAP",
        "active",
        date(2025, 6, 1),
        None,
    )
    overlap_result = service.ingest_universe(_universe_provider(overlapping))
    wrong_symbol = service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "SYMBOL-WRONG-OLD",
                    _action_record(
                        action_type="symbol_change",
                        effective_date=date(2026, 5, 10),
                        exchange="NSE",
                        old_symbol="NOTAURORA",
                        new_symbol="AURORANEW",
                        cash_amount=None,
                        cash_currency=None,
                        cash_unit=None,
                    ),
                    _at(5, 1),
                    None,
                )
            ]
        )
    )
    listing = session.scalar(
        select(ExchangeListing).where(
            ExchangeListing.security_id
            == session.scalar(select(Security.id).where(Security.isin == AURORA_ISIN)),
            ExchangeListing.exchange == "NSE",
        )
    )
    assert listing is not None
    listing.valid_to = date(2026, 6, 1)
    session.flush()
    adjacent = UniverseRecord(
        "Aurora Fabrication Limited",
        "Aurora Fabrication",
        "Industrials",
        "Engineering",
        AURORA_ISIN,
        "equity",
        "active",
        "NSE",
        "AURORA-NEXT",
        "active",
        date(2026, 6, 1),
        None,
    )
    adjacent_result = service.ingest_universe(_universe_provider(adjacent))

    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert (overlap_result.records_quarantined, wrong_symbol.records_quarantined) == (1, 1)
    assert adjacent_result.records_accepted == 1
    assert {"overlapping_listing_interval", "invalid_symbol_change"}.issubset(rules)


def test_security_succession_cycle_guards_and_raw_lineage(session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    self_edge = _action_record(
        action_type="security_replacement",
        effective_date=date(2026, 1, 10),
        successor_isin=AURORA_ISIN,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    a_to_b = _action_record(
        action_type="security_replacement",
        effective_date=date(2026, 2, 10),
        successor_isin="INF0AUR02017",
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    b_to_a = _action_record(
        security_isin="INF0AUR02017",
        action_type="security_replacement",
        effective_date=date(2026, 3, 10),
        successor_isin=AURORA_ISIN,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    b_to_c = _action_record(
        security_isin="INF0AUR02017",
        action_type="security_replacement",
        effective_date=date(2026, 4, 10),
        successor_isin="INF0AUR03016",
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    c_to_a = _action_record(
        security_isin="INF0AUR03016",
        action_type="security_replacement",
        effective_date=date(2026, 5, 10),
        successor_isin=AURORA_ISIN,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
    )
    result = service.ingest_corporate_actions(
        _action_provider(
            [
                ("SELF", self_edge, _at(1, 1), None),
                ("A-B", a_to_b, _at(2, 1), None),
                ("B-A", b_to_a, _at(3, 1), None),
                ("B-C", b_to_c, _at(4, 1), None),
                ("C-A", c_to_a, _at(5, 1), None),
            ]
        )
    )
    source = session.scalar(select(SourceRecord).where(SourceRecord.external_record_id == "A-B"))
    assert (result.records_accepted, result.records_quarantined) == (2, 3)
    assert source is not None and source.raw_payload_reference == "row-2"
    assert (tmp_path / "raw" / source.raw_object_key).exists()
    assert session.scalar(select(func.count()).select_from(SecurityRelationship)) == 2

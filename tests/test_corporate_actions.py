"""Phase 2C action, listing-history, and security-succession tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import CSVCorporateActionProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import (
    CorporateAction,
    DataQualityIssue,
    ExchangeListing,
    Security,
    SecurityRelationship,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 7, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
ACTIONS = ProviderMetadata(
    "synthetic_csv", "csv", "corporate_actions", "synthetic-development-only"
)


def _service(session, tmp_path: Path) -> IngestionService:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    return service


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

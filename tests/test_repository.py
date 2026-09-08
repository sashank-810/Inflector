from inflector_database.repositories import CompanyRepository
from inflector_database.seed import seed_database


def test_seed_data_is_idempotent_and_preserves_listing_identity(session) -> None:
    assert seed_database(session) == 3
    assert seed_database(session) == 0

    companies, total = CompanyRepository(session).list(limit=10, offset=0)

    assert total == 3
    aranya = next(
        company for company in companies if company.display_name == "Aranya Engineering Limited"
    )
    assert len(aranya.securities) == 1
    assert {(listing.exchange, listing.symbol) for listing in aranya.securities[0].listings} == {
        ("NSE", "ARANYA"),
        ("BSE", "543901"),
    }

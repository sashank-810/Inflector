"""Idempotent fictional identity data for local Phase 1 development."""

from __future__ import annotations

from datetime import date
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_database.models import Company, ExchangeListing, Security

type ListingSeed = tuple[str, str]


class SeedCompany(TypedDict):
    """Required fields for one fictional local company identity."""

    legal_name: str
    display_name: str
    sector: str
    industry: str
    isin: str
    listings: tuple[ListingSeed, ...]


SEED_COMPANIES: tuple[SeedCompany, ...] = (
    {
        "legal_name": "Aranya Engineering Limited",
        "display_name": "Aranya Engineering Limited",
        "sector": "Industrials",
        "industry": "Engineering",
        "isin": "INE0ARA01018",
        "listings": (("NSE", "ARANYA"), ("BSE", "543901")),
    },
    {
        "legal_name": "Suryanet Components Limited",
        "display_name": "Suryanet Components Limited",
        "sector": "Industrials",
        "industry": "Auto Components",
        "isin": "INE0SUR01015",
        "listings": (("NSE", "SURYANET"),),
    },
    {
        "legal_name": "Pragati Industrial Systems Limited",
        "display_name": "Pragati Industrial Systems Limited",
        "sector": "Capital Goods",
        "industry": "Industrial Machinery",
        "isin": "INE0PRA01012",
        "listings": (("BSE", "544126"),),
    },
)


def seed_database(session: Session) -> int:
    """Insert the fictional universe once and return its newly created count."""

    created = 0
    for item in SEED_COMPANIES:
        existing = session.scalar(select(Security).where(Security.isin == item["isin"]))
        if existing is not None:
            continue

        company = Company(
            legal_name=item["legal_name"],
            display_name=item["display_name"],
            sector=item["sector"],
            industry=item["industry"],
        )
        security = Security(isin=item["isin"], security_type="equity", status="active")
        security.listings = [
            ExchangeListing(
                exchange=exchange,
                symbol=symbol,
                valid_from=date(2020, 1, 1),
                status="active",
            )
            for exchange, symbol in item["listings"]
        ]
        company.securities.append(security)
        session.add(company)
        created += 1

    session.commit()
    return created


def main() -> None:
    """Seed the database using the configured session factory."""

    from inflector_database.session import SessionLocal

    with SessionLocal() as session:
        created = seed_database(session)
    print(f"Seed complete: {created} company record(s) created.")


if __name__ == "__main__":
    main()

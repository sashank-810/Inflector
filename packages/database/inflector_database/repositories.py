"""Readable data access for canonical company identity."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import Company, Security


class CompanyRepository:
    """Persistence queries for the Phase 1 company read model."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list(self, *, limit: int, offset: int) -> tuple[list[Company], int]:
        statement = (
            select(Company)
            .options(selectinload(Company.securities).selectinload(Security.listings))
            .order_by(Company.display_name)
            .limit(limit)
            .offset(offset)
        )
        companies = list(self._session.scalars(statement))
        total = self._session.scalar(select(func.count()).select_from(Company))
        return companies, total or 0

    def get(self, company_id: UUID) -> Company | None:
        statement = (
            select(Company)
            .where(Company.id == company_id)
            .options(selectinload(Company.securities).selectinload(Security.listings))
        )
        return self._session.scalar(statement)

"""Small application services for company identity reads."""

from uuid import UUID

from sqlalchemy.orm import Session

from inflector_database.models import Company
from inflector_database.repositories import CompanyRepository


class CompanyNotFoundError(Exception):
    """Raised when a requested canonical company does not exist."""


class CompanyService:
    """Coordinates read-only company queries without HTTP concerns."""

    def __init__(self, session: Session) -> None:
        self._repository = CompanyRepository(session)

    def list_companies(self, *, limit: int, offset: int) -> tuple[list[Company], int]:
        return self._repository.list(limit=limit, offset=offset)

    def get_company(self, company_id: UUID) -> Company:
        company = self._repository.get(company_id)
        if company is None:
            raise CompanyNotFoundError(str(company_id))
        return company

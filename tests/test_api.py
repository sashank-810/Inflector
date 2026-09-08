from collections.abc import Generator
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from inflector_api.main import app
from inflector_database.seed import seed_database
from inflector_database.session import get_db_session


def test_health_reports_database_connectivity(session: Session) -> None:
    def override() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db_session] = override
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_company_endpoints_return_canonical_identity_graph(session: Session) -> None:
    seed_database(session)

    def override() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db_session] = override
    try:
        client = TestClient(app)
        listing_response = client.get("/api/v1/companies")
        first_company = listing_response.json()["items"][0]
        detail_response = client.get(f"/api/v1/companies/{first_company['id']}")
    finally:
        app.dependency_overrides.clear()

    assert listing_response.status_code == 200
    assert listing_response.json()["total"] == 3
    assert detail_response.status_code == 200
    assert detail_response.json()["securities"]
    assert "listings" in detail_response.json()["securities"][0]


def test_unknown_company_returns_clean_not_found(session: Session) -> None:
    def override() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db_session] = override
    try:
        response = TestClient(app).get(f"/api/v1/companies/{uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Company not found"}

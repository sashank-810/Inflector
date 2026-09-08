"""Database engine and request-session factory."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from inflector_core.settings import get_settings

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db_session() -> Generator[Session, None, None]:
    """Yield a database session for one API request."""

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

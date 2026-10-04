"""Shared SQLite database fixture."""

import os
import shutil
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from inflector_database.base import Base


@pytest.fixture(scope="session")
def powershell_executable() -> str:
    """Resolve portable PowerShell Core in CI and supported shells on Windows."""

    candidates = (
        ("pwsh", "powershell.exe", "powershell") if os.name == "nt" else ("pwsh",)
    )
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            return executable
    pytest.fail(f"PowerShell executable is required; checked: {', '.join(candidates)}")


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    """Create an isolated in-memory database for one test."""

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as database_session:
        yield database_session
    Base.metadata.drop_all(engine)
    engine.dispose()

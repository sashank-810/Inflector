"""Declarative base shared by all SQLAlchemy models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for PostgreSQL-backed domain models."""


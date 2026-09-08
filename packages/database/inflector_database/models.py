"""Minimum canonical issuer identity schema for Phase 1."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inflector_database.base import Base


class TimestampMixin:
    """Creation and modification timestamps for operational identity records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Company(TimestampMixin, Base):
    """Canonical issuer identity, intentionally independent of trading symbols."""

    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    legal_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sector: Mapped[str] = mapped_column(String(120), nullable=False)
    industry: Mapped[str] = mapped_column(String(120), nullable=False)

    securities: Mapped[list[Security]] = relationship(
        back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )


class Security(TimestampMixin, Base):
    """A security issued by a canonical company."""

    __tablename__ = "securities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    isin: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    security_type: Mapped[str] = mapped_column(String(64), nullable=False, default="equity")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    company: Mapped[Company] = relationship(back_populates="securities")
    listings: Mapped[list[ExchangeListing]] = relationship(
        back_populates="security", cascade="all, delete-orphan", lazy="selectin"
    )


class ExchangeListing(TimestampMixin, Base):
    """A dated exchange-specific symbol for a security."""

    __tablename__ = "exchange_listings"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "exchange", "symbol", "valid_from", name="uq_listing_identity_period"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    security: Mapped[Security] = relationship(back_populates="listings")


"""Explicit API response schemas for company identity reads."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ListingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exchange: str
    symbol: str
    valid_from: date
    valid_to: date | None
    status: str


class SecurityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    isin: str
    security_type: str
    status: str
    listings: list[ListingRead]


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    legal_name: str
    display_name: str
    sector: str
    industry: str
    created_at: datetime
    updated_at: datetime
    securities: list[SecurityRead]


class CompanyListResponse(BaseModel):
    items: list[CompanyRead]
    total: int
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class HealthResponse(BaseModel):
    status: str
    database: str

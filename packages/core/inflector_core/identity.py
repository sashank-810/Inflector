"""Identity constants shared by the first vertical slice."""

from enum import StrEnum


class SecurityStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ListingStatus(StrEnum):
    ACTIVE = "active"
    DELISTED = "delisted"


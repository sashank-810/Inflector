"""Pure deterministic rules for explicit quantitative business-event facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

BUSINESS_EVENT_QUANT_RULESET_CODE: Final = "business_event_quantitative_rules"
BUSINESS_EVENT_QUANT_RULESET_VERSION: Final = "business_event_quantitative_rules_v1"
BUSINESS_EVENT_QUANT_RULE_VERSION: Final = "business_event_quantitative_rule_v1"

BUSINESS_EVENT_QUANT_FACT_ORDER: Final = (
    "order_value",
    "capex_value",
    "capacity_before",
    "capacity_after",
    "additional_capacity",
    "acquisition_consideration",
    "acquisition_stake_fraction",
    "commercial_commencement_date",
)

BUSINESS_EVENT_QUANT_FACT_KINDS: Final = ("monetary", "capacity", "fraction", "date")

EVENT_FACT_COMPATIBILITY: Final = {
    "order_award": frozenset({"order_value"}),
    "capex_announcement": frozenset({"capex_value"}),
    "capacity_expansion": frozenset(
        {"capacity_before", "capacity_after", "additional_capacity"}
    ),
    "acquisition_agreement": frozenset(
        {"acquisition_consideration", "acquisition_stake_fraction"}
    ),
    "commercial_commencement": frozenset({"commercial_commencement_date"}),
    "regulatory_approval": frozenset(),
}


@dataclass(frozen=True, slots=True)
class BusinessEventQuantitativeMatch:
    fact_code: str
    fact_kind: str
    rule_code: str
    rule_semantic_version: str
    local_start_offset: int
    local_end_offset: int
    raw_text: str
    reported_value: Decimal | None
    reported_scale: str | None
    reported_unit: str | None
    reported_currency: str | None
    normalized_value: Decimal | None
    normalized_unit: str | None
    date_value: date | None
    warnings: tuple[str, ...]


class BusinessEventQuantitativeRuleset(Protocol):
    ruleset_code: str
    ruleset_semantic_version: str

    def match(
        self, *, event_type: str, text: str
    ) -> tuple[BusinessEventQuantitativeMatch, ...]: ...


_PLAIN_NUMBER = r"\d+(?:\.\d+)?"
_WESTERN_GROUPED = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"
_INDIAN_GROUPED = r"\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?"
_NUMBER = rf"(?<![\d,])(?:{_INDIAN_GROUPED}|{_WESTERN_GROUPED}|{_PLAIN_NUMBER})(?![\d,])"
_CURRENCY = r"(?:₹|INR|Rs\.|Rs|USD|EUR)"
_MONEY_SCALE = r"(?:thousand|lakhs?|lacs?|crores?|cr|million|mn|billion|bn)"

_SCALE_FACTORS: Final = {
    "unit": Decimal("1"),
    "thousand": Decimal("1000"),
    "lakh": Decimal("100000"),
    "crore": Decimal("10000000"),
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
}

_CAPACITY_UNITS: Final = {
    "tpa": ("TPA", Decimal("1"), "tonnes_per_annum"),
    "ktpa": ("KTPA", Decimal("1000"), "tonnes_per_annum"),
    "mtpa": ("MTPA", Decimal("1000000"), "tonnes_per_annum"),
    "tonnes per annum": ("tonnes per annum", Decimal("1"), "tonnes_per_annum"),
    "thousand tonnes per annum": (
        "thousand tonnes per annum",
        Decimal("1000"),
        "tonnes_per_annum",
    ),
    "million tonnes per annum": (
        "million tonnes per annum",
        Decimal("1000000"),
        "tonnes_per_annum",
    ),
    "tpd": ("TPD", Decimal("1"), "tonnes_per_day"),
    "tonnes per day": ("tonnes per day", Decimal("1"), "tonnes_per_day"),
    "mw": ("MW", Decimal("1"), "megawatt"),
    "gw": ("GW", Decimal("1000"), "megawatt"),
    "klpd": ("KLPD", Decimal("1"), "kilolitres_per_day"),
}

_CAPACITY_UNIT_PATTERN = (
    r"(?:thousand\s+tonnes\s+per\s+annum|million\s+tonnes\s+per\s+annum|"
    r"tonnes\s+per\s+annum|tonnes\s+per\s+day|KTPA|MTPA|TPA|TPD|KLPD|MW|GW)"
)

_MONTHS: Final = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_PATTERN = "(?:" + "|".join(_MONTHS) + ")"
_DATE_PATTERN = (
    rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\s+{_MONTH_PATTERN}\s+\d{{4}}|"
    rf"{_MONTH_PATTERN}\s+\d{{1,2}},\s+\d{{4}})"
)


def _money_token() -> str:
    return (
        rf"(?P<amount>(?P<currency>{_CURRENCY})\s*"
        rf"(?P<number>{_NUMBER})(?:\s+(?P<scale>{_MONEY_SCALE}))?)"
    )


def _capacity_token(prefix: str) -> str:
    return (
        rf"(?P<{prefix}_amount>(?P<{prefix}_number>{_NUMBER})\s+"
        rf"(?P<{prefix}_unit>{_CAPACITY_UNIT_PATTERN}))"
    )


def _compile(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE)


_MONEY_RULES: Final = {
    "order_award": (
        (
            "order_value_relationship_v1",
            _compile(
                rf"\b(?:order|contract)\s+(?:worth|valued\s+at|value\s+(?:of|is)|"
                rf"amounting\s+to)\s+{_money_token()}"
            ),
            "order_value",
        ),
        (
            "order_value_purchase_order_v1",
            _compile(rf"\bpurchase\s+order\s+of\s+{_money_token()}"),
            "order_value",
        ),
    ),
    "capex_announcement": (
        (
            "capex_value_relationship_v1",
            _compile(
                rf"\b(?:approved\s+(?:capex|capital\s+expenditure)|"
                rf"(?:capex|capital\s+expenditure)\s+plan|announced\s+capex|capex)"
                rf"\s+(?:of|amounting\s+to)\s+{_money_token()}"
            ),
            "capex_value",
        ),
    ),
    "acquisition_agreement": (
        (
            "acquisition_consideration_v1",
            _compile(
                rf"\b(?:(?:purchase|acquisition)\s+consideration\s+of|"
                rf"for\s+a\s+consideration\s+of|(?:transaction|deal)\s+value\s+of)"
                rf"\s+{_money_token()}"
            ),
            "acquisition_consideration",
        ),
    ),
}

_CAPACITY_PAIR_RULES: Final = (
    (
        "capacity_from_to_v1",
        _compile(
            rf"\b(?:(?:increase|increased|expand|expanded)\s+capacity|"
            rf"capacity\s+(?:will\s+)?(?:increase|increased|expand|expanded))"
            rf"\s+from\s+{_capacity_token('before')}\s+to\s+{_capacity_token('after')}"
        ),
    ),
)

_ADDITIONAL_CAPACITY_RULES: Final = (
    (
        "additional_capacity_explicit_v1",
        _compile(rf"\badditional\s+capacity\s+of\s+{_capacity_token('value')}"),
    ),
    (
        "capacity_expansion_by_v1",
        _compile(rf"\bcapacity\s+expansion\s+by\s+{_capacity_token('value')}"),
    ),
)

_STAKE_RULES: Final = (
    (
        "acquisition_stake_v1",
        _compile(
            rf"\b(?:acquire(?:\s+a)?|acquisition\s+of|purchase\s+of)\s+"
            rf"(?P<stake>{_NUMBER})%\s+(?:equity\s+)?stake\b"
        ),
    ),
)

_DATE_RULES: Final = (
    (
        "commercial_commencement_date_v1",
        _compile(
            rf"\b(?:commercial\s+(?:production|operations)\s+(?:has\s+)?commenced\s+on|"
            rf"commenced\s+commercial\s+(?:production|operations)\s+on|"
            rf"commencement\s+of\s+commercial\s+(?:production|operations)\s+"
            rf"with\s+effect\s+from)\s+(?P<date>{_DATE_PATTERN})\b"
        ),
    ),
)


class BusinessEventQuantitativeRuleEngine:
    """Extract only explicit values tied to an already detected event context."""

    ruleset_code = BUSINESS_EVENT_QUANT_RULESET_CODE
    ruleset_semantic_version = BUSINESS_EVENT_QUANT_RULESET_VERSION

    def match(
        self, *, event_type: str, text: str
    ) -> tuple[BusinessEventQuantitativeMatch, ...]:
        if event_type not in EVENT_FACT_COMPATIBILITY:
            raise ValueError(f"unsupported business event type: {event_type}")
        matches: list[BusinessEventQuantitativeMatch] = []
        matches.extend(self._money_matches(event_type, text))
        if event_type == "capacity_expansion":
            matches.extend(self._capacity_matches(text))
        elif event_type == "acquisition_agreement":
            matches.extend(self._stake_matches(text))
        elif event_type == "commercial_commencement":
            matches.extend(self._date_matches(text))
        deduplicated = {
            (
                item.fact_code,
                item.rule_code,
                item.local_start_offset,
                item.local_end_offset,
                item.raw_text,
                item.reported_value,
                item.reported_unit,
                item.reported_currency,
                item.date_value,
            ): item
            for item in matches
        }
        rank = {code: index for index, code in enumerate(BUSINESS_EVENT_QUANT_FACT_ORDER)}
        return tuple(
            sorted(
                deduplicated.values(),
                key=lambda item: (
                    rank[item.fact_code],
                    item.local_start_offset,
                    item.local_end_offset,
                    item.rule_code,
                ),
            )
        )

    @staticmethod
    def _money_matches(
        event_type: str, text: str
    ) -> list[BusinessEventQuantitativeMatch]:
        values: list[BusinessEventQuantitativeMatch] = []
        for rule_code, pattern, fact_code in _MONEY_RULES.get(event_type, ()):
            for matched in pattern.finditer(text):
                start, end = matched.span("amount")
                if _ambiguous_money(text, start, end):
                    continue
                number = _parse_number(matched.group("number"))
                if number is None:
                    continue
                scale = _canonical_scale(matched.group("scale"))
                currency = _canonical_currency(matched.group("currency"))
                values.append(
                    BusinessEventQuantitativeMatch(
                        fact_code=fact_code,
                        fact_kind="monetary",
                        rule_code=rule_code,
                        rule_semantic_version=BUSINESS_EVENT_QUANT_RULE_VERSION,
                        local_start_offset=start,
                        local_end_offset=end,
                        raw_text=text[start:end],
                        reported_value=number,
                        reported_scale=scale,
                        reported_unit=None,
                        reported_currency=currency,
                        normalized_value=number * _SCALE_FACTORS[scale],
                        normalized_unit="currency_major",
                        date_value=None,
                        warnings=(),
                    )
                )
        return values

    @staticmethod
    def _capacity_matches(text: str) -> list[BusinessEventQuantitativeMatch]:
        values: list[BusinessEventQuantitativeMatch] = []
        for rule_code, pattern in _CAPACITY_PAIR_RULES:
            for matched in pattern.finditer(text):
                before = _capacity_value(matched, "before")
                after = _capacity_value(matched, "after")
                if before is None or after is None or before[3] != after[3]:
                    continue
                values.extend(
                    (
                        _capacity_match("capacity_before", rule_code, matched, "before", before),
                        _capacity_match("capacity_after", rule_code, matched, "after", after),
                    )
                )
        for rule_code, pattern in _ADDITIONAL_CAPACITY_RULES:
            for matched in pattern.finditer(text):
                value = _capacity_value(matched, "value")
                if value is not None:
                    values.append(
                        _capacity_match(
                            "additional_capacity", rule_code, matched, "value", value
                        )
                    )
        return values

    @staticmethod
    def _stake_matches(text: str) -> list[BusinessEventQuantitativeMatch]:
        values: list[BusinessEventQuantitativeMatch] = []
        for rule_code, pattern in _STAKE_RULES:
            for matched in pattern.finditer(text):
                number = _parse_number(matched.group("stake"))
                if number is None or not Decimal("0") < number <= Decimal("100"):
                    continue
                start, end = matched.span("stake")
                end += 1  # retain the literal percent sign
                values.append(
                    BusinessEventQuantitativeMatch(
                        fact_code="acquisition_stake_fraction",
                        fact_kind="fraction",
                        rule_code=rule_code,
                        rule_semantic_version=BUSINESS_EVENT_QUANT_RULE_VERSION,
                        local_start_offset=start,
                        local_end_offset=end,
                        raw_text=text[start:end],
                        reported_value=number,
                        reported_scale=None,
                        reported_unit="percent",
                        reported_currency=None,
                        normalized_value=number / Decimal("100"),
                        normalized_unit="fraction",
                        date_value=None,
                        warnings=(),
                    )
                )
        return values

    @staticmethod
    def _date_matches(text: str) -> list[BusinessEventQuantitativeMatch]:
        values: list[BusinessEventQuantitativeMatch] = []
        for rule_code, pattern in _DATE_RULES:
            for matched in pattern.finditer(text):
                raw = matched.group("date")
                parsed = _parse_date(raw)
                if parsed is None:
                    continue
                start, end = matched.span("date")
                values.append(
                    BusinessEventQuantitativeMatch(
                        fact_code="commercial_commencement_date",
                        fact_kind="date",
                        rule_code=rule_code,
                        rule_semantic_version=BUSINESS_EVENT_QUANT_RULE_VERSION,
                        local_start_offset=start,
                        local_end_offset=end,
                        raw_text=text[start:end],
                        reported_value=None,
                        reported_scale=None,
                        reported_unit=None,
                        reported_currency=None,
                        normalized_value=None,
                        normalized_unit=None,
                        date_value=parsed,
                        warnings=(),
                    )
                )
        return values


def _parse_number(raw: str) -> Decimal | None:
    if not (
        re.fullmatch(_PLAIN_NUMBER, raw)
        or re.fullmatch(_WESTERN_GROUPED, raw)
        or re.fullmatch(_INDIAN_GROUPED, raw)
    ):
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _canonical_currency(raw: str) -> str:
    lowered = raw.lower()
    if raw == "₹" or lowered in {"inr", "rs", "rs."}:
        return "INR"
    return raw.upper()


def _canonical_scale(raw: str | None) -> str:
    if raw is None:
        return "unit"
    lowered = raw.lower()
    if lowered in {"lakh", "lakhs", "lac", "lacs"}:
        return "lakh"
    if lowered in {"crore", "crores", "cr"}:
        return "crore"
    if lowered in {"million", "mn"}:
        return "million"
    if lowered in {"billion", "bn"}:
        return "billion"
    return "thousand"


def _ambiguous_money(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 30) : start]
    suffix = text[end : min(len(text), end + 30)]
    return bool(
        re.search(
            r"(?:approximately|about|around|up\s+to|more\s+than|less\s+than|between)\s*$",
            prefix,
            re.IGNORECASE,
        )
        or re.match(r"\s*(?:-|–|—|to\b)", suffix, re.IGNORECASE)
    )


def _capacity_value(
    matched: re.Match[str], prefix: str
) -> tuple[Decimal, str, Decimal, str] | None:
    number = _parse_number(matched.group(f"{prefix}_number"))
    if number is None:
        return None
    raw_unit = " ".join(matched.group(f"{prefix}_unit").lower().split())
    unit = _CAPACITY_UNITS.get(raw_unit)
    if unit is None:
        return None
    reported_unit, factor, normalized_unit = unit
    return number, reported_unit, number * factor, normalized_unit


def _capacity_match(
    fact_code: str,
    rule_code: str,
    matched: re.Match[str],
    prefix: str,
    value: tuple[Decimal, str, Decimal, str],
) -> BusinessEventQuantitativeMatch:
    start, end = matched.span(f"{prefix}_amount")
    reported_value, reported_unit, normalized_value, normalized_unit = value
    return BusinessEventQuantitativeMatch(
        fact_code=fact_code,
        fact_kind="capacity",
        rule_code=rule_code,
        rule_semantic_version=BUSINESS_EVENT_QUANT_RULE_VERSION,
        local_start_offset=start,
        local_end_offset=end,
        raw_text=matched.string[start:end],
        reported_value=reported_value,
        reported_scale=None,
        reported_unit=reported_unit,
        reported_currency=None,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        date_value=None,
        warnings=(),
    )


def _parse_date(raw: str) -> date | None:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        day_first = re.fullmatch(
            rf"(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})",
            raw,
            re.IGNORECASE,
        )
        if day_first:
            return date(
                int(day_first.group("year")),
                _MONTHS[day_first.group("month").lower()],
                int(day_first.group("day")),
            )
        month_first = re.fullmatch(
            rf"(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}}),\s+"
            rf"(?P<year>\d{{4}})",
            raw,
            re.IGNORECASE,
        )
        if month_first:
            return date(
                int(month_first.group("year")),
                _MONTHS[month_first.group("month").lower()],
                int(month_first.group("day")),
            )
    except ValueError:
        return None
    return None

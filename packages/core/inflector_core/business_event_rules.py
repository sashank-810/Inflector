"""Versioned, deterministic rules for neutral business-event detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

BUSINESS_EVENT_RULESET_CODE: Final = "business_event_rules"
BUSINESS_EVENT_RULESET_VERSION: Final = "business_event_rules_v1"
BUSINESS_EVENT_RULE_VERSION: Final = "business_event_rule_v1"

BUSINESS_EVENT_TYPE_ORDER: Final = (
    "order_award",
    "capacity_expansion",
    "commercial_commencement",
    "capex_announcement",
    "acquisition_agreement",
    "regulatory_approval",
)


@dataclass(frozen=True, slots=True)
class BusinessEventRuleMatch:
    """Exact trigger coordinates in the original source text."""

    event_type: str
    rule_code: str
    rule_semantic_version: str
    start_offset: int
    end_offset: int


class BusinessEventRuleset(Protocol):
    """Stable interface accepted by the detection service."""

    ruleset_code: str
    ruleset_semantic_version: str

    def match(self, text: str) -> tuple[BusinessEventRuleMatch, ...]: ...


@dataclass(frozen=True, slots=True)
class _Rule:
    event_type: str
    code: str
    pattern: re.Pattern[str]


_REGULATOR = r"(?:US\s+FDA|USFDA|FDA|CDSCO|DCGI|RBI|SEBI|IRDAI|DGCA|CCI|NCLT)"


def _pattern(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE)


_RULES: Final = (
    _Rule(
        "order_award",
        "order_award_received_order_v1",
        _pattern(r"\b(?:has\s+)?received\s+(?:an?\s+)?order\b"),
    ),
    _Rule(
        "order_award",
        "order_award_secured_v1",
        _pattern(r"\bsecured\s+(?:an?\s+)?(?:order|contract)\b"),
    ),
    _Rule(
        "order_award",
        "order_award_awarded_v1",
        _pattern(r"\bhas\s+been\s+awarded\s+(?:an?\s+)?(?:contract|order)\b"),
    ),
    _Rule(
        "order_award",
        "order_award_letter_v1",
        _pattern(r"\bletter\s+of\s+(?:award|acceptance)\b"),
    ),
    _Rule(
        "order_award",
        "order_award_purchase_order_v1",
        _pattern(r"\bpurchase\s+order\s+(?:has\s+been\s+)?received\b"),
    ),
    _Rule(
        "capacity_expansion",
        "capacity_expansion_phrase_v1",
        _pattern(
            r"\b(?:capacity\s+expansion|(?:expand|expanding|increase|increasing)\s+"
            r"its\s+capacity|additional\s+capacity)\b"
        ),
    ),
    _Rule(
        "commercial_commencement",
        "commercial_commencement_production_v1",
        _pattern(
            r"\b(?:commenced\s+commercial\s+production|commercial\s+production\s+"
            r"has\s+commenced|started\s+commercial\s+production|"
            r"commencement\s+of\s+commercial\s+production)\b"
        ),
    ),
    _Rule(
        "commercial_commencement",
        "commercial_commencement_operations_v1",
        _pattern(
            r"\b(?:commenced\s+commercial\s+operations|"
            r"commencement\s+of\s+commercial\s+operations)\b"
        ),
    ),
    _Rule(
        "capex_announcement",
        "capex_approved_v1",
        _pattern(r"\bapproved\s+(?:capital\s+expenditure|capex)\b"),
    ),
    _Rule(
        "capex_announcement",
        "capex_announced_v1",
        _pattern(
            r"\bannounced\s+(?:capital\s+expenditure|capex(?:\s+plan)?)\b"
        ),
    ),
    _Rule(
        "capex_announcement",
        "capex_plan_v1",
        _pattern(
            r"\b(?:capital\s+expenditure\s+plan|capex\s+(?:programme|program))\b"
        ),
    ),
    _Rule(
        "acquisition_agreement",
        "acquisition_share_purchase_agreement_v1",
        _pattern(
            r"\b(?:entered\s+into|signed)\s+a\s+share\s+purchase\s+agreement\b"
        ),
    ),
    _Rule(
        "acquisition_agreement",
        "acquisition_agreement_to_acquire_v1",
        _pattern(r"\bentered\s+into\s+an?\s+agreement\s+to\s+acquire\b"),
    ),
    _Rule(
        "acquisition_agreement",
        "acquisition_agreed_to_acquire_v1",
        _pattern(r"\bagreed\s+to\s+acquire\b"),
    ),
    _Rule(
        "regulatory_approval",
        "regulatory_approval_received_v1",
        _pattern(rf"\b(?:received|obtained)\s+approval\s+from\s+({_REGULATOR})\b"),
    ),
    _Rule(
        "regulatory_approval",
        "regulatory_approval_regulator_approved_v1",
        _pattern(rf"\b({_REGULATOR})\s+(?:has\s+)?approved\b"),
    ),
)


class BusinessEventRuleEngine:
    """Apply immutable v1 regex rules without rewriting the source text."""

    ruleset_code = BUSINESS_EVENT_RULESET_CODE
    ruleset_semantic_version = BUSINESS_EVENT_RULESET_VERSION

    def match(self, text: str) -> tuple[BusinessEventRuleMatch, ...]:
        matches: list[BusinessEventRuleMatch] = []
        for rule in _RULES:
            for match in rule.pattern.finditer(text):
                matches.append(
                    BusinessEventRuleMatch(
                        event_type=rule.event_type,
                        rule_code=rule.code,
                        rule_semantic_version=BUSINESS_EVENT_RULE_VERSION,
                        start_offset=match.start(),
                        end_offset=match.end(),
                    )
                )
        type_rank = {event_type: rank for rank, event_type in enumerate(BUSINESS_EVENT_TYPE_ORDER)}
        matches.sort(
            key=lambda item: (
                type_rank[item.event_type],
                item.start_offset,
                item.end_offset,
                item.rule_code,
            )
        )
        return tuple(matches)

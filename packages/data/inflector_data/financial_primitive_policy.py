"""Versioned semantic qualification for production financial primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

_STATUSES = frozenset({"APPROVED_DIRECT", "APPROVED_DERIVED", "NOT_APPROVED", "NOT_APPLICABLE"})
_CLASSIFICATIONS = frozenset({"direct", "normalized_identity", "unavailable"})
_PERIOD_TYPES = frozenset({"duration", "instant"})
_REQUIRED_METRICS = frozenset(
    {
        "revenue",
        "ebitda_reported",
        "borrowings_current",
        "borrowings_non_current",
        "total_debt",
        "finance_cost",
        "depreciation_amortisation",
    }
)


@dataclass(frozen=True, slots=True)
class FinancialPrimitiveQualification:
    """One explicit source-to-primitive semantic decision."""

    normalized_metric_code: str
    status: str
    classification: str
    source_concepts: tuple[str, ...]
    source_metric_codes: tuple[str, ...]
    required_constituents: tuple[str, ...]
    period_type: str
    reported_unit: str
    calculation: str | None
    rationale: str
    missingness_behavior: str


@dataclass(frozen=True, slots=True)
class FinancialPrimitivePolicy:
    """Controlled production policy loaded from a canonical JSON asset."""

    financial_primitive_policy_code: str
    policy_version: str
    source_family: str
    allowed_taxonomy_namespaces: tuple[str, ...]
    filing_scopes: tuple[str, ...]
    qualifications: dict[str, FinancialPrimitiveQualification]
    checksum_sha256: str

    def direct_source_mappings(self) -> dict[str, str]:
        """Return exact local-QName mappings approved as directly reported facts."""

        return {
            qualification.source_concepts[0]: qualification.normalized_metric_code
            for qualification in self.qualifications.values()
            if qualification.status == "APPROVED_DIRECT"
        }

    def normalized_identity_source(self, metric_code: str) -> str | None:
        """Return the one source metric approved for a non-arithmetic normalization."""

        qualification = self.qualifications.get(metric_code)
        if (
            qualification is None
            or qualification.status != "APPROVED_DERIVED"
            or qualification.classification != "normalized_identity"
        ):
            return None
        return qualification.source_metric_codes[0]

    def status(self, metric_code: str) -> str:
        qualification = self.qualifications.get(metric_code)
        return qualification.status if qualification is not None else "NOT_APPLICABLE"


def load_financial_primitive_policy(path: Path) -> FinancialPrimitivePolicy:
    """Load and fail closed on an ambiguous semantic policy."""

    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("financial primitive policy is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("financial primitive policy root must be an object")
    raw_qualifications = value.get("qualifications")
    if (
        not isinstance(raw_qualifications, dict)
        or frozenset(raw_qualifications) != _REQUIRED_METRICS
    ):
        raise ValueError("financial primitive policy qualification matrix is incomplete")
    qualifications = {code: _qualification(code, item) for code, item in raw_qualifications.items()}
    namespaces = _strings(value, "allowed_taxonomy_namespaces")
    scopes = _strings(value, "filing_scopes")
    if scopes != ("standalone", "consolidated"):
        raise ValueError("financial primitive policy filing scopes must be explicit")
    return FinancialPrimitivePolicy(
        financial_primitive_policy_code=_text(value, "financial_primitive_policy_code"),
        policy_version=_text(value, "policy_version"),
        source_family=_text(value, "source_family"),
        allowed_taxonomy_namespaces=namespaces,
        filing_scopes=scopes,
        qualifications=qualifications,
        checksum_sha256=sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


def _qualification(code: str, value: object) -> FinancialPrimitiveQualification:
    if not isinstance(value, dict):
        raise ValueError(f"financial primitive qualification {code} must be an object")
    status = _text(value, "status")
    classification = _text(value, "classification")
    if status not in _STATUSES or classification not in _CLASSIFICATIONS:
        raise ValueError(f"financial primitive qualification {code} is unsupported")
    source_concepts = _strings(value, "source_concepts", allow_empty=True)
    source_metrics = _strings(value, "source_metric_codes", allow_empty=True)
    constituents = _strings(value, "required_constituents", allow_empty=True)
    period_type = _text(value, "period_type")
    reported_unit = _text(value, "reported_unit")
    if period_type not in _PERIOD_TYPES or reported_unit != "INR":
        raise ValueError(f"financial primitive qualification {code} has unsupported semantics")
    calculation_raw = value.get("calculation")
    calculation = None if calculation_raw is None else _text(value, "calculation")
    if status == "APPROVED_DIRECT":
        if classification != "direct" or len(source_concepts) != 1 or source_metrics != (code,):
            raise ValueError(f"direct financial primitive qualification {code} is ambiguous")
        if constituents or calculation is not None:
            raise ValueError(f"direct financial primitive qualification {code} cannot derive")
    elif status == "APPROVED_DERIVED":
        if (
            classification != "normalized_identity"
            or len(source_concepts) != 1
            or len(source_metrics) != 1
            or constituents != source_metrics
            or calculation != "identity"
        ):
            raise ValueError(f"derived financial primitive qualification {code} is ambiguous")
    elif classification != "unavailable" or calculation is not None:
        raise ValueError(f"unapproved financial primitive qualification {code} must be unavailable")
    return FinancialPrimitiveQualification(
        normalized_metric_code=code,
        status=status,
        classification=classification,
        source_concepts=source_concepts,
        source_metric_codes=source_metrics,
        required_constituents=constituents,
        period_type=period_type,
        reported_unit=reported_unit,
        calculation=calculation,
        rationale=_text(value, "rationale"),
        missingness_behavior=_text(value, "missingness_behavior"),
    )


def _text(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or item != item.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return item


def _strings(value: dict[str, Any], field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or (not item and not allow_empty):
        raise ValueError(f"{field} must be a list of strings")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result

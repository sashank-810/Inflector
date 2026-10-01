"""Official NSE Integrated Filing financial discovery and controlled XBRL parsing."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
from typing import Final
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree as ET

from inflector_core.providers import (
    FinancialRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.nse_http import AcquiredNSEArtifact, NSEHttpClient, validate_nse_url
from inflector_data.nse_providers import NSEArtifactSource

NSE_FINANCIAL_DATASET_CODE = "nse_integrated_financials_xbrl"
NSE_INTEGRATED_FINANCIAL_MAPPING_VERSION = "nse_integrated_financial_mapping_v1"
NSE_INTEGRATED_FINANCIAL_DISCOVERY_URL = (
    "https://www.nseindia.com/api/integrated-filing-results"
)
MINIMUM_SUPPORTED_QUARTER_END = date(2025, 3, 31)
MAXIMUM_DISCOVERY_DATE_RANGE_DAYS = 366
MAXIMUM_FINANCIAL_XBRL_BYTES = 10_000_000

XBRL_INSTANCE_NAMESPACE: Final = "http://www.xbrl.org/2003/instance"
XLINK_NAMESPACE: Final = "http://www.w3.org/1999/xlink"
ISO4217_NAMESPACE: Final = "http://www.xbrl.org/2003/iso4217"
SUPPORTED_TAXONOMY_NAMESPACES: Final = (
    "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt",
    "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt",
)
SUPPORTED_SCHEMA_REFERENCES: Final = frozenset(
    {"in-capmkt-ent-2025-01-31.xsd", "in-capmkt-ent-2026-01-31.xsd"}
)

_CONCEPT_TO_METRIC_LOCAL_NAMES: Final = {
    "RevenueFromOperations": "operating_revenue",
    "OtherIncome": "other_income",
    "FinanceCosts": "finance_cost",
    "ExceptionalItemsBeforeTax": "exceptional_items",
    "ProfitBeforeTax": "profit_before_tax",
    "TaxExpense": "tax_expense",
    "ProfitLossForPeriod": "pat",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
    "Assets": "total_assets",
    "Liabilities": "total_liabilities",
    "Equity": "total_equity",
    "CashAndCashEquivalents": "cash_and_equivalents",
    "Inventories": "inventory",
    "CashFlowsFromUsedInOperatingActivities": "cash_flow_from_operations",
    "CashFlowsFromUsedInInvestingActivities": "cash_flow_from_investing",
    "CashFlowsFromUsedInFinancingActivities": "cash_flow_from_financing",
}
NSE_INTEGRATED_FINANCIAL_MAPPING_V1: Final = {
    f"{{{namespace}}}{concept}": metric
    for namespace in SUPPORTED_TAXONOMY_NAMESPACES
    for concept, metric in _CONCEPT_TO_METRIC_LOCAL_NAMES.items()
}
NSE_INTENTIONALLY_UNMAPPED_TARGET_METRICS: Final = (
    "revenue",
    "operating_profit",
    "ebitda_reported",
    "ebit",
    "total_debt",
    "trade_receivables",
    "trade_payables",
    "capex_reported",
)

_EPS_METRICS = frozenset({"eps_basic", "eps_diluted"})
_INSTANT_METRICS = frozenset(
    {
        "total_assets",
        "total_liabilities",
        "total_equity",
        "cash_and_equivalents",
        "inventory",
    }
)
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9&-]{1,64}$")
_ENGLISH_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


class NSEFinancialFormatError(ValueError):
    """An official discovery response or XBRL is structurally unsafe."""


@dataclass(frozen=True, slots=True)
class NSEFinancialFiling:
    """Strict discovery metadata used only to locate and bind one official XBRL."""

    sequence_id: str
    symbol: str
    company_name: str
    quarter_end: date
    scope: str
    submission_type: str
    xbrl_uri: str

    @property
    def filing_external_id(self) -> str:
        return f"nse-integrated-financials:{self.sequence_id}"


@dataclass(frozen=True, slots=True)
class _Context:
    context_id: str
    start: date | None
    end: date
    has_dimensions: bool


@dataclass(frozen=True, slots=True)
class _Period:
    kind: str
    start: date
    end: date
    fiscal_year: int
    fiscal_quarter: int
    is_ytd: bool


@dataclass(frozen=True, slots=True)
class _FactCandidate:
    qname: str
    context: _Context
    unit_id: str
    value: Decimal
    locator: str
    decimals: str | None


class NSEIntegratedFinancialDiscovery:
    """Bounded client for the official structured Integrated Filing index."""

    def __init__(self, client: NSEHttpClient) -> None:
        self._client = client

    def discover(
        self,
        *,
        symbol: str,
        max_filings: int,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> tuple[NSEFinancialFiling, ...]:
        normalized_symbol = symbol.strip().upper()
        if _SYMBOL_PATTERN.fullmatch(normalized_symbol) is None:
            raise ValueError("symbol must be a controlled NSE symbol")
        if max_filings < 1 or max_filings > 100:
            raise ValueError("max_filings must be between 1 and 100")
        if (from_date is None) != (to_date is None):
            raise ValueError("from_date and to_date must be supplied together")
        if from_date is not None and to_date is not None:
            if to_date < from_date:
                raise ValueError("to_date must be on or after from_date")
            if (to_date - from_date).days > MAXIMUM_DISCOVERY_DATE_RANGE_DAYS:
                raise ValueError("financial discovery date range exceeds 366 days")

        query = {
            "index": "equities",
            "symbol": normalized_symbol,
            "period_ended": "all",
            "type": "Integrated Filing- Financials",
            "page": "1",
            "size": str(max_filings),
        }
        if from_date is not None and to_date is not None:
            query["from_date"] = from_date.strftime("%d-%m-%Y")
            query["to_date"] = to_date.strftime("%d-%m-%Y")
        artifact = self._client.acquire(
            f"{NSE_INTEGRATED_FINANCIAL_DISCOVERY_URL}?{urlencode(query)}"
        )
        return parse_financial_discovery(
            artifact.payload, requested_symbol=normalized_symbol, max_filings=max_filings
        )


def parse_financial_discovery(
    payload: bytes, *, requested_symbol: str, max_filings: int
) -> tuple[NSEFinancialFiling, ...]:
    """Validate the observed official JSON response without using broadcast time."""

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NSEFinancialFormatError("NSE financial discovery response is not JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise NSEFinancialFormatError("NSE financial discovery response has no data list")
    filings: list[NSEFinancialFiling] = []
    required = {
        "seq_Id",
        "symbol",
        "smName",
        "qe_Date",
        "consolidated",
        "type_Sub",
        "xbrl",
    }
    for item in document["data"]:
        if not isinstance(item, dict) or not required.issubset(item):
            raise NSEFinancialFormatError("NSE financial discovery row is incomplete")
        symbol = _required_text(item["symbol"], "symbol").upper()
        if symbol != requested_symbol:
            raise NSEFinancialFormatError("NSE financial discovery returned another symbol")
        scope_text = _required_text(item["consolidated"], "consolidated").lower()
        if scope_text not in {"standalone", "consolidated"}:
            raise NSEFinancialFormatError("NSE financial discovery scope is unsupported")
        submission_type = _required_text(item["type_Sub"], "type_Sub")
        if submission_type not in {"Original", "Revision"}:
            raise NSEFinancialFormatError("NSE financial submission type is unsupported")
        xbrl_uri = _required_text(item["xbrl"], "xbrl")
        _validate_xbrl_uri(xbrl_uri)
        filings.append(
            NSEFinancialFiling(
                sequence_id=_required_text(item["seq_Id"], "seq_Id"),
                symbol=symbol,
                company_name=_required_text(item["smName"], "smName"),
                quarter_end=_nse_text_date(_required_text(item["qe_Date"], "qe_Date")),
                scope=scope_text,
                submission_type=submission_type,
                xbrl_uri=xbrl_uri,
            )
        )
    filings.sort(key=lambda filing: (filing.quarter_end, filing.sequence_id), reverse=True)
    return tuple(filings[:max_filings])


class NSEIntegratedFinancialsProvider:
    """One official XBRL filing mapped to provider-neutral financial records."""

    def __init__(
        self,
        source: NSEArtifactSource,
        metadata: ProviderMetadata,
        filing: NSEFinancialFiling,
        *,
        expected_isin: str,
    ) -> None:
        if source.source_uri != filing.xbrl_uri:
            raise ValueError("financial source URI must equal the discovered XBRL URI")
        self._source = source
        self._metadata = metadata
        self._filing = filing
        self._expected_isin = expected_isin
        self._retrieved_at: datetime | None = None
        self._recognized_source_facts = 0
        self._unsupported_reason: str | None = None
        self._mapped_metric_codes: tuple[str, ...] = ()

    @property
    def source_uri(self) -> str:
        return self._source.source_uri

    @property
    def retrieved_at(self) -> datetime | None:
        return self._retrieved_at

    @property
    def skipped_rows(self) -> int:
        return 1 if self._unsupported_reason is not None else 0

    @property
    def recognized_source_facts(self) -> int:
        return self._recognized_source_facts

    @property
    def unsupported_reason(self) -> str | None:
        return self._unsupported_reason

    @property
    def mapped_metric_codes(self) -> tuple[str, ...]:
        return self._mapped_metric_codes

    def fetch_financials(self) -> ProviderBatch[FinancialRecord]:
        artifact = self._source.acquire()
        self._retrieved_at = artifact.retrieved_at
        if len(artifact.payload) > MAXIMUM_FINANCIAL_XBRL_BYTES:
            raise NSEFinancialFormatError("financial XBRL exceeds byte limit")
        if self._filing.quarter_end < MINIMUM_SUPPORTED_QUARTER_END:
            self._unsupported_reason = "pre_integrated_financial_generation"
            return self._empty_batch(artifact)
        if "INTEGRATED_FILING_INDAS_" not in urlparse(artifact.source_uri).path.upper():
            self._unsupported_reason = "unsupported_financial_taxonomy"
            return self._empty_batch(artifact)
        records = self._parse_records(artifact)
        self._mapped_metric_codes = tuple(
            dict.fromkeys(
                record.record.metric_code
                for record in records
                if record.record.metric_code is not None
            )
        )
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            records,
        )

    def _empty_batch(self, artifact: AcquiredNSEArtifact) -> ProviderBatch[FinancialRecord]:
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            (),
        )

    def _parse_records(
        self, artifact: AcquiredNSEArtifact
    ) -> tuple[IngestionEnvelope[FinancialRecord], ...]:
        root = _secure_xml_root(artifact.payload)
        _validate_taxonomy(root)
        contexts = _contexts(root)
        units = _units(root, _namespace_bindings(artifact.payload))
        symbol = _unique_text_fact(root, "Symbol")
        isin = _unique_text_fact(root, "ISIN")
        if symbol != self._filing.symbol or isin != self._expected_isin:
            raise NSEFinancialFormatError("XBRL symbol or ISIN contradicts the official EQ master")
        scopes = _context_scopes(root)
        periods = {
            context_id: _period_for_context(context, contexts)
            for context_id, context in contexts.items()
        }
        candidates: dict[tuple[str, str], list[_FactCandidate]] = defaultdict(list)
        for ordinal, element in enumerate(root, start=1):
            metric = NSE_INTEGRATED_FINANCIAL_MAPPING_V1.get(element.tag)
            if metric is None:
                continue
            context_id = element.attrib.get("contextRef")
            unit_id = element.attrib.get("unitRef")
            if context_id is None or unit_id is None or context_id not in contexts:
                continue
            context = contexts[context_id]
            period = periods[context_id]
            if context.has_dimensions or period is None:
                continue
            expected_unit = "INR/share" if metric in _EPS_METRICS else "INR"
            if units.get(unit_id) != expected_unit:
                continue
            if (metric in _INSTANT_METRICS) != (context.start is None):
                continue
            text = (element.text or "").strip()
            if element.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
                continue
            try:
                value = Decimal(text)
            except InvalidOperation:
                continue
            qname = element.tag
            candidates[(metric, context_id)].append(
                _FactCandidate(
                    qname=qname,
                    context=context,
                    unit_id=unit_id,
                    value=value,
                    locator=f"fact-{ordinal:06d}",
                    decimals=element.attrib.get("decimals") or element.attrib.get("precision"),
                )
            )
        self._recognized_source_facts = sum(len(values) for values in candidates.values())

        envelopes: list[IngestionEnvelope[FinancialRecord]] = []
        for (metric, context_id), grouped in sorted(candidates.items()):
            period = periods[context_id]
            assert period is not None
            scope = _scope_for_context(context_id, contexts, scopes)
            errors: list[str] = []
            if scope is None or scope != self._filing.scope:
                errors.append("financial_filing_scope_mismatch")
            values = {(candidate.value, units[candidate.unit_id]) for candidate in grouped}
            selected = min(grouped, key=lambda candidate: candidate.locator)
            if len(values) != 1:
                errors.append("conflicting_duplicate_financial_fact")
            record = FinancialRecord(
                company_legal_name=None,
                security_isin=self._expected_isin,
                filing_external_id=self._filing.filing_external_id,
                filing_type="integrated_financial_results_xbrl",
                filing_scope=scope or self._filing.scope,
                is_restatement=self._filing.submission_type == "Revision",
                period_kind=period.kind,
                period_start=period.start,
                period_end=period.end,
                fiscal_year=period.fiscal_year,
                fiscal_quarter=period.fiscal_quarter,
                is_ytd=period.is_ytd,
                metric_code=metric,
                reported_value=selected.value if len(values) == 1 else None,
                reported_unit=units[selected.unit_id],
                reported_scale="ones",
                reported_currency="INR",
                parse_errors=tuple(errors),
            )
            semantic = {
                "mapping_version": NSE_INTEGRATED_FINANCIAL_MAPPING_VERSION,
                "qname": selected.qname,
                "context_id": context_id,
                "unit_id": selected.unit_id,
                "value": str(selected.value),
                "duplicates": [
                    {
                        "locator": candidate.locator,
                        "value": str(candidate.value),
                        "decimals_or_precision": candidate.decimals,
                    }
                    for candidate in sorted(grouped, key=lambda item: item.locator)
                ],
            }
            envelopes.append(
                IngestionEnvelope(
                    provider=self._metadata,
                    external_record_id=_source_fact_external_id(
                        self._filing.filing_external_id,
                        selected.qname,
                        context_id,
                        selected.unit_id,
                    ),
                    source_uri=artifact.source_uri,
                    raw_payload_reference=(
                        f"xbrl:{selected.qname}:context:{context_id}:"
                        f"unit:{selected.unit_id}:{selected.locator}"
                    ),
                    content_sha256=sha256(
                        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    retrieved_at=artifact.retrieved_at,
                    available_at=artifact.retrieved_at,
                    revision_at=None,
                    published_at=None,
                    record=record,
                )
            )
        return tuple(envelopes)


def _secure_xml_root(payload: bytes) -> ET.Element:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise NSEFinancialFormatError("financial XBRL document types and entities are forbidden")
    try:
        return ET.parse(BytesIO(payload)).getroot()
    except ET.ParseError as error:
        raise NSEFinancialFormatError("financial XBRL is malformed XML") from error


def _namespace_bindings(payload: bytes) -> dict[str, str]:
    bindings: dict[str, str] = {}
    try:
        for _, declaration in ET.iterparse(BytesIO(payload), events=("start-ns",)):
            prefix, namespace = declaration
            bindings[prefix] = namespace
    except ET.ParseError as error:
        raise NSEFinancialFormatError("financial XBRL is malformed XML") from error
    return bindings


def _validate_taxonomy(root: ET.Element) -> None:
    if root.tag != f"{{{XBRL_INSTANCE_NAMESPACE}}}xbrl":
        raise NSEFinancialFormatError("financial document is not an XBRL instance")
    schema_refs = [
        element.attrib.get(f"{{{XLINK_NAMESPACE}}}href")
        for element in root
        if _local_name(element.tag) == "schemaRef"
    ]
    if len(schema_refs) != 1 or schema_refs[0] not in SUPPORTED_SCHEMA_REFERENCES:
        raise NSEFinancialFormatError("financial XBRL taxonomy generation is unsupported")


def _contexts(root: ET.Element) -> dict[str, _Context]:
    contexts: dict[str, _Context] = {}
    for element in root:
        if element.tag != f"{{{XBRL_INSTANCE_NAMESPACE}}}context":
            continue
        context_id = element.attrib.get("id")
        if not context_id or context_id in contexts:
            raise NSEFinancialFormatError("financial XBRL context identity is invalid")
        period = element.find(f"{{{XBRL_INSTANCE_NAMESPACE}}}period")
        if period is None:
            continue
        start_element = period.find(f"{{{XBRL_INSTANCE_NAMESPACE}}}startDate")
        end_element = period.find(f"{{{XBRL_INSTANCE_NAMESPACE}}}endDate")
        instant_element = period.find(f"{{{XBRL_INSTANCE_NAMESPACE}}}instant")
        try:
            if instant_element is not None and instant_element.text:
                start = None
                end = date.fromisoformat(instant_element.text.strip())
            elif (
                start_element is not None
                and start_element.text
                and end_element is not None
                and end_element.text
            ):
                start = date.fromisoformat(start_element.text.strip())
                end = date.fromisoformat(end_element.text.strip())
            else:
                continue
        except ValueError:
            continue
        has_dimensions = any(_local_name(descendant.tag) == "scenario" for descendant in element)
        contexts[context_id] = _Context(context_id, start, end, has_dimensions)
    return contexts


def _units(root: ET.Element, namespaces: dict[str, str]) -> dict[str, str]:
    units: dict[str, str] = {}
    for element in root:
        if element.tag != f"{{{XBRL_INSTANCE_NAMESPACE}}}unit":
            continue
        unit_id = element.attrib.get("id")
        if not unit_id:
            continue
        measures = [
            _expanded_lexical_qname((descendant.text or "").strip(), namespaces)
            for descendant in element.iter()
            if _local_name(descendant.tag) == "measure"
        ]
        has_divide = any(_local_name(descendant.tag) == "divide" for descendant in element.iter())
        if measures == [f"{{{ISO4217_NAMESPACE}}}INR"] and not has_divide:
            units[unit_id] = "INR"
        elif measures == [
            f"{{{ISO4217_NAMESPACE}}}INR",
            f"{{{XBRL_INSTANCE_NAMESPACE}}}shares",
        ] and has_divide:
            units[unit_id] = "INR/share"
    return units


def _expanded_lexical_qname(value: str, namespaces: dict[str, str]) -> str:
    if ":" not in value:
        return value
    prefix, local_name = value.split(":", 1)
    namespace = namespaces.get(prefix)
    return f"{{{namespace}}}{local_name}" if namespace is not None else value


def _context_scopes(root: ET.Element) -> dict[str, str]:
    scopes: dict[str, str] = {}
    for element in root:
        if _local_name(element.tag) != "NatureOfReportStandaloneConsolidated":
            continue
        context_id = element.attrib.get("contextRef")
        value = (element.text or "").strip().lower()
        if context_id and value in {"standalone", "consolidated"}:
            if context_id in scopes and scopes[context_id] != value:
                raise NSEFinancialFormatError("financial XBRL context has conflicting scope")
            scopes[context_id] = value
    return scopes


def _scope_for_context(
    context_id: str, contexts: dict[str, _Context], scopes: dict[str, str]
) -> str | None:
    direct = scopes.get(context_id)
    if direct is not None:
        return direct
    context = contexts[context_id]
    if context.start is not None:
        return None
    matching = {
        scope
        for candidate_id, scope in scopes.items()
        if candidate_id in contexts
        and contexts[candidate_id].end == context.end
        and not contexts[candidate_id].has_dimensions
    }
    return next(iter(matching)) if len(matching) == 1 else None


def _period_for_context(
    context: _Context, contexts: dict[str, _Context]
) -> _Period | None:
    if context.has_dimensions:
        return None
    if context.start is not None:
        return _duration_period(context.start, context.end)
    candidates = [
        period
        for other in contexts.values()
        if other.start is not None
        and other.end == context.end
        and not other.has_dimensions
        and (period := _duration_period(other.start, other.end)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda period: (period.end - period.start).days)


def _duration_period(start: date, end: date) -> _Period | None:
    if (start.month, start.day, end.month, end.day) == (4, 1, 6, 30) and end.year == start.year:
        return _Period("quarter", start, end, start.year, 1, False)
    if (start.month, start.day, end.month, end.day) == (7, 1, 9, 30) and end.year == start.year:
        return _Period("quarter", start, end, start.year, 2, False)
    if (start.month, start.day, end.month, end.day) == (10, 1, 12, 31) and end.year == start.year:
        return _Period("quarter", start, end, start.year, 3, False)
    if (
        (start.month, start.day, end.month, end.day) == (1, 1, 3, 31)
        and end.year == start.year
    ):
        return _Period("quarter", start, end, start.year - 1, 4, False)
    if (start.month, start.day, end.month, end.day) == (4, 1, 9, 30) and end.year == start.year:
        return _Period("half_year", start, end, start.year, 2, True)
    if (start.month, start.day, end.month, end.day) == (4, 1, 12, 31) and end.year == start.year:
        return _Period("nine_month", start, end, start.year, 3, True)
    if (
        (start.month, start.day, end.month, end.day) == (4, 1, 3, 31)
        and end.year == start.year + 1
    ):
        return _Period("annual", start, end, start.year, 4, False)
    return None


def _unique_text_fact(root: ET.Element, local_name: str) -> str:
    values = {
        (element.text or "").strip()
        for element in root
        if _local_name(element.tag) == local_name and (element.text or "").strip()
    }
    if len(values) != 1:
        raise NSEFinancialFormatError(f"financial XBRL {local_name} identity is ambiguous")
    return next(iter(values))


def _validate_xbrl_uri(uri: str) -> None:
    validate_nse_url(uri)
    parsed = urlparse(uri)
    if (
        parsed.hostname != "nsearchives.nseindia.com"
        or not parsed.path.startswith("/corporate/xbrl/")
        or not parsed.path.lower().endswith(".xml")
        or parsed.query
        or parsed.fragment
    ):
        raise NSEFinancialFormatError("NSE financial XBRL URI is not an official archive path")


def _source_fact_external_id(
    filing_external_id: str,
    qname: str,
    context_id: str,
    unit_id: str,
) -> str:
    identity = sha256(f"{qname}|{context_id}|{unit_id}".encode()).hexdigest()
    return f"{filing_external_id}:xbrl-fact:{identity}"


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise NSEFinancialFormatError(f"NSE financial discovery {field} is empty")
    return str(value).strip()


def _nse_text_date(value: str) -> date:
    parts = value.split("-")
    if len(parts) != 3 or parts[1].upper() not in _ENGLISH_MONTHS:
        raise NSEFinancialFormatError("NSE financial quarter-end date is invalid")
    try:
        return date(int(parts[2]), _ENGLISH_MONTHS[parts[1].upper()], int(parts[0]))
    except ValueError as error:
        raise NSEFinancialFormatError("NSE financial quarter-end date is invalid") from error


def _local_name(qname: str) -> str:
    return qname.rsplit("}", 1)[-1]

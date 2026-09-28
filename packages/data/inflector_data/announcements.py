"""Deterministic announcement/document metadata validation without interpretation."""

from __future__ import annotations

from re import fullmatch

from inflector_core.providers import AnnouncementDocumentRecord, AnnouncementRecord
from inflector_data.validation import ValidationIssue

ANNOUNCEMENT_DOCUMENT_ROLES = frozenset({"primary", "attachment", "supporting"})
ANNOUNCEMENT_DOCUMENT_TYPES = frozenset(
    {
        "announcement_attachment",
        "exchange_filing",
        "press_release",
        "investor_presentation",
        "other",
    }
)


def validate_announcement(record: AnnouncementRecord) -> list[ValidationIssue]:
    """Validate structural source metadata only; make no catalyst inference."""

    issues = [ValidationIssue(code, code.replace("_", " ")) for code in record.parse_errors]
    if not record.company_legal_name and not record.security_isin:
        issues.append(
            ValidationIssue(
                "missing_company_identity",
                "company legal name or security ISIN is required",
            )
        )
    if record.headline is None or not record.headline.strip():
        issues.append(ValidationIssue("missing_headline", "headline is required"))
    for document in record.documents:
        issues.extend(validate_announcement_document(document))
    return issues


def validate_announcement_document(
    document: AnnouncementDocumentRecord,
) -> list[ValidationIssue]:
    issues = [ValidationIssue(code, code.replace("_", " ")) for code in document.parse_errors]
    if document.role not in ANNOUNCEMENT_DOCUMENT_ROLES:
        issues.append(ValidationIssue("invalid_document_role", "unsupported document role"))
    if document.document_type not in ANNOUNCEMENT_DOCUMENT_TYPES:
        issues.append(ValidationIssue("invalid_document_type", "unsupported document type"))
    if document.title is None or not document.title.strip():
        issues.append(ValidationIssue("missing_document_title", "document title is required"))
    if document.document_uri is None or not document.document_uri.strip():
        issues.append(ValidationIssue("missing_document_uri", "document URI is required"))
    if (
        document.document_content_sha256 is not None
        and fullmatch(r"[0-9a-fA-F]{64}", document.document_content_sha256) is None
    ):
        issues.append(
            ValidationIssue(
                "invalid_document_sha256",
                "document hash must be a SHA-256 hexadecimal digest",
            )
        )
    return issues

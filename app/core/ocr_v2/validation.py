"""Fail-closed validators for page, document, and product profiles."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum

from .contracts import DocumentResult, PageResult, PageStatus, ResultCapability, Validation, ValidationIssue
from .errors import DocumentValidationError, PageValidationError


class OCRProfile(str, Enum):
    OCR_TEXT_V2 = "OCR_TEXT_V2"
    SEARCHABLE_PDF_V2 = "SEARCHABLE_PDF_V2"
    DOCUMENT_EXTRACTION_V2 = "DOCUMENT_EXTRACTION_V2"


def _page_issues(page: PageResult, profile: OCRProfile) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if page.geometry.width <= 0 or page.geometry.height <= 0:
        issues.append(ValidationIssue("INVALID_PAGE_GEOMETRY", "page dimensions must be positive", path=f"pages[{page.page_index}].geometry"))
    if page.status is PageStatus.FAILED:
        issues.append(ValidationIssue(page.failure_code or "PAGE_FAILED", page.failure_message or "page processing failed", path=f"pages[{page.page_index}]"))
    elif page.status is PageStatus.BLANK and page.text.strip():
        issues.append(ValidationIssue("BLANK_PAGE_HAS_TEXT", "blank page cannot contain text", path=f"pages[{page.page_index}].text"))
    elif page.status in (PageStatus.SUCCESS, PageStatus.PARTIAL) and not isinstance(page.text, str):
        issues.append(ValidationIssue("TEXT_NOT_STRING", "page text must be a string", path=f"pages[{page.page_index}].text"))
    if profile is OCRProfile.OCR_TEXT_V2 and page.status is PageStatus.PARTIAL:
        issues.append(ValidationIssue("PARTIAL_PAGE", "OCR Text V2 requires complete text for every processed page", path=f"pages[{page.page_index}]"))
    for token in page.tokens:
        if not token.text.strip():
            issues.append(ValidationIssue("TOKEN_TEXT_EMPTY", "token text must be non-empty", path=f"pages[{page.page_index}].tokens"))
        bbox = token.bbox
        if bbox.width <= 0 or bbox.height <= 0 or bbox.x < 0 or bbox.y < 0 or bbox.x1 > page.geometry.width + 1e-6 or bbox.y1 > page.geometry.height + 1e-6:
            issues.append(ValidationIssue("TOKEN_BBOX_OUT_OF_BOUNDS", "token geometry is outside the visible page", path=f"pages[{page.page_index}].tokens.{token.id}"))
    if profile is OCRProfile.SEARCHABLE_PDF_V2 and page.status is not PageStatus.BLANK:
        if not page.tokens:
            issues.append(ValidationIssue("WORD_GEOMETRY_EMPTY", "searchable PDF requires actual word geometry"))
        if len(page.reading_order) != len(page.tokens) or set(page.reading_order) != {token.id for token in page.tokens}:
            issues.append(ValidationIssue("READING_ORDER_INCOMPLETE", "reading order must reference every token exactly once"))
        if ResultCapability.WORD_GEOMETRY.value not in page.capabilities:
            issues.append(ValidationIssue("WORD_GEOMETRY_CAPABILITY_MISSING", "page does not advertise actual word geometry"))
    return issues


def validate_page(page: PageResult, profile: OCRProfile = OCRProfile.OCR_TEXT_V2) -> PageResult:
    issues = tuple(_page_issues(page, profile))
    validation = Validation(not issues, issues)
    result = replace(page, validation=validation)
    if issues:
        raise PageValidationError("; ".join(issue.code for issue in issues))
    return result


def validate_document(result: DocumentResult, profile: OCRProfile = OCRProfile.OCR_TEXT_V2) -> DocumentResult:
    issues: list[ValidationIssue] = []
    if len(result.pages) != result.source.page_count:
        issues.append(ValidationIssue("PAGE_COVERAGE_INCOMPLETE", "document does not contain one result per source page"))
    if tuple(page.page_index for page in result.pages) != tuple(range(result.source.page_count)):
        issues.append(ValidationIssue("PAGE_ORDER_INVALID", "page results are not in source order"))
    for page in result.pages:
        issues.extend(_page_issues(page, profile))
    if profile is OCRProfile.OCR_TEXT_V2 and ResultCapability.TEXT.value not in result.capabilities:
        issues.append(ValidationIssue("TEXT_CAPABILITY_MISSING", "OCR Text V2 requires TEXT capability"))
    if profile is OCRProfile.SEARCHABLE_PDF_V2:
        required = {ResultCapability.TEXT.value, ResultCapability.WORD_GEOMETRY.value, ResultCapability.READING_ORDER.value}
        issues.extend(ValidationIssue(f"DOCUMENT_CAPABILITY_MISSING:{value}", f"missing {value}") for value in sorted(required - set(result.capabilities)))
    return replace(result, validation=Validation(not issues, tuple(issues)))


def require_profile(result: DocumentResult, profile: OCRProfile) -> DocumentResult:
    checked = validate_document(result, profile)
    if not checked.validation.valid:
        raise DocumentValidationError("; ".join(issue.code for issue in checked.validation.issues))
    return checked


def profile_disposition(result: DocumentResult, profile: OCRProfile) -> str:
    checked = validate_document(result, profile)
    if checked.validation.valid:
        return "ELIGIBLE"
    if profile is OCRProfile.SEARCHABLE_PDF_V2 and any(issue.code.startswith("DOCUMENT_CAPABILITY_MISSING:WORD_GEOMETRY") for issue in checked.validation.issues):
        return "NOT_ELIGIBLE:WORD_GEOMETRY_NOT_AVAILABLE"
    return "NOT_ELIGIBLE"

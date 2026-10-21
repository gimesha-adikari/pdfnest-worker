"""Explicit snapshot of mounted worker routes at 931de889; unknowns fail closed."""
from enum import Enum
import re

from .errors import AdmissionClosed, ProtocolError
from .target import RequestTarget


class RouteKind(Enum):
    PROCESSING = 'processing'
    CONTROL = 'control'
    UNKNOWN = 'unknown'


PROCESSING_POSTS = frozenset({
    '/api/v1/office/convert', '/api/v1/office-to-pdf/convert',
    '/api/v1/ocr/extract-text', '/api/v1/ocr/to-text-pdf', '/api/v1/ocr/to-text-pdf-r2',
    '/api/v1/markdown/convert', '/internal/ocr/v2/text', '/internal/ocr/v2/markup/preview',
    '/internal/ocr/v2/jobs', '/api/v1/editor/extract', '/api/v1/editor/compile',
    '/api/v1/markup/highlight', '/api/v1/markup/underline', '/api/v1/markup/strikeout',
    '/api/v1/jobs/test', '/api/v1/jobs/submit', '/api/v1/render/page', '/api/v1/render/sessions',
    '/api/v1/analyzer/python-ast', '/api/v1/analyzer/analyze', '/api/v1/metadata/read',
    '/api/v1/metadata/write', '/api/v1/metadata/preserve', '/api/v1/redact', '/api/v1/sign',
})
CONTROL_GETS = frozenset({
    '/', '/home', '/health', '/health/live', '/health/ready', '/docs', '/docs/oauth2-redirect',
    '/openapi.json', '/redoc', '/internal/ocr/v2/capabilities', '/api/v1/ocr/languages',
    '/api/v1/render/metrics', '/api/v1/jobs',
})
_SEGMENT = r'[A-Za-z0-9_~.\-]+'
_CONTROL_PATTERNS = (
    ('GET', rf'/api/v1/jobs/{_SEGMENT}'),
    ('GET', rf'/internal/ocr/v2/jobs/{_SEGMENT}(?:/result)?'),
    ('GET', rf'/api/v1/(?:editor|markup)/jobs/{_SEGMENT}/download'),
    ('POST', rf'/(?:api/v1|internal/ocr/v2)/jobs/{_SEGMENT}/cancel'),
    ('DELETE', rf'/api/v1/render/sessions/{_SEGMENT}'),
)


def classify(target: RequestTarget) -> RouteKind:
    path = target.path.rstrip('/') or '/'
    if (target.method == 'POST' and path in PROCESSING_POSTS
            or target.method == 'GET' and re.fullmatch(rf'/api/v1/render/sessions/{_SEGMENT}/page/{_SEGMENT}', path)):
        return RouteKind.PROCESSING
    if target.method == 'GET' and path in CONTROL_GETS:
        return RouteKind.CONTROL
    if any(target.method == method and re.fullmatch(pattern, path) for method, pattern in _CONTROL_PATTERNS):
        return RouteKind.CONTROL
    return RouteKind.UNKNOWN


def require_admission(target: RequestTarget, *, admission_open: bool) -> None:
    kind = classify(target)
    if kind is RouteKind.UNKNOWN:
        raise ProtocolError()
    if kind is RouteKind.PROCESSING and admission_open is not True:
        raise AdmissionClosed()

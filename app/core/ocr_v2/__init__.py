"""PDFNest OCR V2 worker-core namespace.

This package is intentionally not mounted as a production endpoint in Phase
3A. It supplies the typed contracts and isolated execution pipeline that a
later API integration can adopt after managed/runtime validation.
"""

from .contracts import *
from .geometry import PreparedRaster, RasterPreparer, normalize_rotation, page_geometry_from_pdf, pixel_rect_to_points
from .image_pages import build_image_source_pdf, normalize_image
from .native import NativeDecision, NativeExtractor, NativeValidator
from .orchestration import OCRV2Worker
from .profiles import product_verdict, searchable_pdf_reason
from .routing import OCRRouter, RoutePlan, RoutePolicy
from .validation import OCRProfile, profile_disposition, require_profile, validate_document, validate_page

__all__ = [
    "OCRV2Worker", "OCRProfile", "OCRRouter", "RoutePlan", "RoutePolicy", "NativeDecision", "NativeExtractor", "NativeValidator", "PreparedRaster", "RasterPreparer", "normalize_rotation", "page_geometry_from_pdf", "pixel_rect_to_points", "normalize_image", "build_image_source_pdf", "product_verdict", "searchable_pdf_reason", "profile_disposition", "require_profile", "validate_document", "validate_page",
]

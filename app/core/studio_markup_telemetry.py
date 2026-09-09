"""Safe aggregate telemetry for Studio region-markup processing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping


logger = logging.getLogger("pdfnest.ocr_v2")

_NATIVE_SOURCE = "NATIVE_EXTRACTION"
_OCR_SOURCES = frozenset({"OCR_RECOGNITION", "HYBRID"})


@dataclass
class StudioMarkupProcessingTelemetry:
    """Count page contexts observed at the OCR result callback boundary.

    The callback is invoked by the internal OCR V2 worker or the public SDK
    adapter after a page result has been built.  The counters therefore report
    actual page-context work rather than repeating the requested page list.
    """

    mode: str
    region_count: int
    _processed_page_indexes: set[int] = field(default_factory=set)
    native_page_context_count: int = 0
    ocr_page_context_count: int = 0
    smart_ocr_fallback_page_count: int = 0

    @property
    def processed_page_indexes(self) -> tuple[int, ...]:
        return tuple(sorted(self._processed_page_indexes))

    def observe_page(self, page: Any) -> None:
        """Observe one completed or failed page result exactly once."""
        page_index = getattr(page, "page_index", None)
        if not isinstance(page_index, int) or page_index < 0:
            return
        if page_index in self._processed_page_indexes:
            return
        self._processed_page_indexes.add(page_index)

        source = getattr(page, "processing_source", "")
        source_value = str(getattr(source, "value", source)).upper()
        if source_value in {_NATIVE_SOURCE, "HYBRID"}:
            self.native_page_context_count += 1
        if source_value in _OCR_SOURCES:
            self.ocr_page_context_count += 1
            if self.mode == "smart":
                # The internal Smart route first validates the native layer,
                # then routes non-trusted pages to OCR.  This is the exact
                # fallback boundary of the deployed algorithm.
                self.smart_ocr_fallback_page_count += 1

    def observe_page_source(self, page_source: Mapping[str, Any]) -> None:
        """Observe an SDK page-source record when no callback was delivered."""
        page_index = page_source.get("page_index")
        source_type = str(page_source.get("source_type", "")).lower()
        source_type = {
            "native": _NATIVE_SOURCE,
            "ocr": "OCR_RECOGNITION",
            "hybrid": "HYBRID",
        }.get(source_type, source_type)

        class PageSource:
            pass

        page = PageSource()
        page.page_index = page_index
        page.processing_source = source_type
        self.observe_page(page)

    def summary(
        self,
        *,
        source_page_count: int,
        selected_page_indexes: list[int] | None = None,
    ) -> dict[str, Any]:
        selected = (
            list(self.processed_page_indexes)
            if selected_page_indexes is None
            else sorted({index for index in selected_page_indexes if isinstance(index, int) and index >= 0})
        )
        return {
            "source_page_count": int(source_page_count),
            "region_count": int(self.region_count),
            "affected_page_count": len(selected),
            "selected_page_indexes": selected,
            "native_page_context_count": int(self.native_page_context_count),
            "ocr_page_context_count": int(self.ocr_page_context_count),
            "smart_ocr_fallback_page_count": int(self.smart_ocr_fallback_page_count),
            "processed_page_context_count": len(self._processed_page_indexes),
        }


def emit_studio_markup_processing_summary(
    *,
    job_id: str,
    action: str,
    mode: str,
    engine: str,
    summary: Mapping[str, Any],
) -> None:
    """Emit one non-sensitive aggregate event for a completed operation."""
    payload = {
        "event": "studio_markup_processing_summary",
        "job_id": job_id,
        "action": action,
        "mode": mode,
        "engine": engine,
        **dict(summary),
    }
    logger.info(
        "STUDIO_MARKUP_PROCESSING_SUMMARY %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


__all__ = [
    "StudioMarkupProcessingTelemetry",
    "emit_studio_markup_processing_summary",
]

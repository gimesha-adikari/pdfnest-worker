"""Future structured-document adapter slot; intentionally no runtime."""

from __future__ import annotations

from typing import Any

from .base import EngineAdapter, EngineAvailability
from ..errors import EngineUnavailableError
from ..geometry import PreparedRaster
from ..contracts import UnnormalizedPageOutput


class StructuredDocumentAdapterSlot(EngineAdapter):
    """A typed extension point for a future structured engine."""

    def describe(self) -> dict[str, Any]:
        return {"id": "structured_document_future_slot", "status": "not_implemented", "capabilities": []}

    def availability(self) -> EngineAvailability:
        return EngineAvailability(False, "structured-document runtime is not part of Phase 3A")

    def initialize(self) -> None:
        raise EngineUnavailableError(self.availability().reason)

    def readiness(self) -> bool:
        return False

    def recognize_page(self, page_id: str, raster: PreparedRaster) -> UnnormalizedPageOutput:
        raise EngineUnavailableError(self.availability().reason)

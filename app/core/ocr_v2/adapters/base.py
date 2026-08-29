"""Adapter protocol and availability metadata for OCR V2."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..contracts import Provenance, UnnormalizedPageOutput
from ..geometry import PreparedRaster


class EngineAdapter(ABC):
    @abstractmethod
    def describe(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def readiness(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def recognize_page(self, page_id: str, raster: PreparedRaster) -> UnnormalizedPageOutput:
        raise NotImplementedError

    def shutdown(self) -> None:
        return None


class EngineAvailability:
    def __init__(self, available: bool, reason: str = "") -> None:
        self.available = available
        self.reason = reason


def provenance_from_description(description: dict[str, Any]) -> Provenance:
    return Provenance(
        producer_id=str(description.get("id", "unknown")),
        producer_version=description.get("version"),
        model_identity=description.get("model_identity"),
        source=description.get("source"),
        configuration=dict(description.get("configuration", {})),
    )

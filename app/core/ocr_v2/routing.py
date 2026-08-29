"""Profile-driven native/OCR routing with finite, observable fallbacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .adapters.base import EngineAdapter
from .contracts import PageContentClassification
from .errors import EngineUnavailableError, NativeTextUndecidedError
from .native import NativeDecision, NativeValidationResult
from .validation import OCRProfile


@dataclass(frozen=True)
class RoutePlan:
    action: str
    engine_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class RoutePolicy:
    preferred_engine: str = "ppocrv6_medium_v2"
    fallback_engine: str = "tesseract_v2"
    allow_native: bool = True


class OCRRouter:
    def __init__(self, adapters: Mapping[str, EngineAdapter], policy: RoutePolicy | None = None) -> None:
        self.adapters = adapters
        self.policy = policy or RoutePolicy()

    def _available(self, engine_id: str) -> bool:
        adapter = self.adapters.get(engine_id)
        availability = getattr(adapter, "availability", None)
        return adapter is not None and (availability is None or bool(availability().available))

    def plan(self, decision: NativeValidationResult, profile: OCRProfile) -> RoutePlan:
        if self.policy.allow_native and decision.decision == NativeDecision.TRUST_NATIVE:
            return RoutePlan("NATIVE", reason="validated native text layer")
        if decision.classification is PageContentClassification.BLANK:
            return RoutePlan("NATIVE", reason="blank page has no OCR content")
        if decision.decision == NativeDecision.UNDECIDED:
            raise NativeTextUndecidedError("native validator could not safely select native text or visual OCR")
        candidates = [self.policy.preferred_engine, self.policy.fallback_engine]
        if profile is OCRProfile.SEARCHABLE_PDF_V2:
            candidates = [self.policy.preferred_engine, self.policy.fallback_engine]
        for engine_id in candidates:
            if self._available(engine_id):
                return RoutePlan("OCR", engine_id=engine_id, reason=f"{decision.decision}:{decision.classification.value}")
        raise EngineUnavailableError("no configured OCR adapter is available for the requested profile")

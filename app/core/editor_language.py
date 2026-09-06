"""Public, consumer-neutral language intent for Editor V2 extraction."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence

SUPPORTED_EDITOR_LANGUAGES = frozenset({"eng", "sin", "tam"})

class EditorLanguageConfigurationError(ValueError):
    pass

class EditorLanguageRequiredError(RuntimeError):
    """AUTO could not select a safe language; the user must choose explicitly."""

@dataclass(frozen=True)
class EditorLanguageIntent:
    mode: str
    languages: tuple[str, ...]
    @property
    def expression(self) -> str:
        return "auto" if self.mode == "AUTO" else "+".join(self.languages)

def editor_language_intent(mode: str = "EXPLICIT", languages: Sequence[str] | None = None) -> EditorLanguageIntent:
    normalized_mode = str(mode or "EXPLICIT").strip().upper()
    if normalized_mode not in {"AUTO", "EXPLICIT"}:
        raise EditorLanguageConfigurationError("unsupported editor language mode")
    values = tuple(dict.fromkeys(str(value).strip().lower() for value in (languages or ("eng",)) if str(value).strip()))
    if not values or any(value not in SUPPORTED_EDITOR_LANGUAGES for value in values):
        raise EditorLanguageConfigurationError("unsupported editor language")
    return EditorLanguageIntent(normalized_mode, values)

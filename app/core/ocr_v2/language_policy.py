"""Shared OCR V2 language policy, canonicalization, and bounded detection.

The public request may still use the historical ``language`` string for
backward compatibility.  This module is the single place where that legacy
shape becomes a typed policy and where a policy becomes a Tesseract language
expression.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping, Sequence


class OCRLanguageMode(str, Enum):
    EXPLICIT = "EXPLICIT"
    AUTO = "AUTO"


class LanguageDecisionStatus(str, Enum):
    REQUESTED = "REQUESTED"
    DETECTED = "DETECTED"
    MULTILINGUAL_DETECTED = "MULTILINGUAL_DETECTED"
    UNCERTAIN = "UNCERTAIN"
    UNDETERMINED = "UNDETERMINED"


class LanguagePolicyError(ValueError):
    """A safe request-language validation error."""


CANONICAL_LANGUAGE_NAMES: Mapping[str, str] = {
    "eng": "English",
    "sin": "Sinhala",
    "tam": "Tamil",
}

_LANGUAGE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def canonicalize_language_ids(
    values: Iterable[str],
    *,
    installed_languages: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Validate, deduplicate, and sort canonical Tesseract language IDs."""

    installed = {str(item).strip().lower() for item in installed_languages or () if str(item).strip()}
    result: set[str] = set()
    for raw in values:
        code = str(raw).strip().lower()
        if not code or code in {"auto", "automatic", "detect"}:
            continue
        if not _LANGUAGE_RE.fullmatch(code):
            raise LanguagePolicyError("Unsupported OCR language identifier")
        if installed and code not in installed:
            raise LanguagePolicyError(f"Unsupported OCR language: {code}")
        result.add(code)
    if not result:
        raise LanguagePolicyError("At least one OCR language is required")
    return tuple(sorted(result))


def split_language_expression(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part for part in re.split(r"[+,\s]+", value.strip().lower()) if part)


@dataclass(frozen=True)
class OCRLanguagePolicy:
    mode: OCRLanguageMode = OCRLanguageMode.EXPLICIT
    languages: tuple[str, ...] = ()

    @classmethod
    def from_request(
        cls,
        language: str | None,
        *,
        mode: str | OCRLanguageMode | None = None,
        languages: Sequence[str] | None = None,
        installed_languages: Iterable[str] | None = None,
    ) -> "OCRLanguagePolicy":
        requested_mode = mode if isinstance(mode, OCRLanguageMode) else OCRLanguageMode(str(mode).upper()) if mode is not None else None
        raw_language = (language or "").strip()
        if raw_language.lower() in {"auto", "automatic", "detect"} and requested_mode in {None, OCRLanguageMode.EXPLICIT}:
            requested_mode = OCRLanguageMode.AUTO
        if requested_mode is OCRLanguageMode.AUTO:
            allowed = canonicalize_language_ids(languages or (), installed_languages=installed_languages) if languages else ()
            return cls(OCRLanguageMode.AUTO, allowed)

        raw_values = tuple(languages or ()) or split_language_expression(raw_language or "eng")
        return cls(
            OCRLanguageMode.EXPLICIT,
            canonicalize_language_ids(raw_values, installed_languages=installed_languages),
        )

    @property
    def engine_expression(self) -> str:
        if self.mode is OCRLanguageMode.AUTO:
            raise LanguagePolicyError("AUTO policy must be resolved before Tesseract execution")
        return "+".join(self.languages)

    @property
    def semantic_value(self) -> str:
        return "AUTO" if self.mode is OCRLanguageMode.AUTO and not self.languages else (
            "AUTO:" + "+".join(self.languages)
            if self.mode is OCRLanguageMode.AUTO
            else "+".join(self.languages)
        )

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode.value, "languages": list(self.languages)}


def script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for char in text:
        if char.isspace() or char.isdigit() or not char.isprintable():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        script = "LATIN" if "LATIN" in name else "SINHALA" if "SINHALA" in name else "TAMIL" if "TAMIL" in name else "OTHER"
        counts[script] = counts.get(script, 0) + 1
    return counts


def scripts_for_languages(languages: Sequence[str]) -> set[str]:
    scripts: set[str] = set()
    if "eng" in languages:
        scripts.add("LATIN")
    if "sin" in languages:
        scripts.add("SINHALA")
    if "tam" in languages:
        scripts.add("TAMIL")
    return scripts


@dataclass(frozen=True)
class LanguageProbe:
    language: tuple[str, ...]
    text: str
    confidence: float
    samples: int = 1


@dataclass(frozen=True)
class LanguageDetection:
    policy: OCRLanguagePolicy | None
    status: LanguageDecisionStatus
    confidence: float
    scripts: tuple[str, ...]
    probes: int
    reason: str


class LanguageCandidateRanker:
    """Rank bounded single-language and small-set candidates by safe priors."""

    def __init__(self, usage: Mapping[str, float] | None = None) -> None:
        self.usage = {str(key).lower(): float(value) for key, value in (usage or {}).items()}

    def rank(self, languages: Iterable[str]) -> tuple[tuple[str, ...], ...]:
        canonical = tuple(sorted({str(value).lower() for value in languages if str(value).strip()}))
        if not canonical:
            return ()
        candidates = [(code,) for code in canonical]
        if len(canonical) > 1:
            # Small combinations only; never enumerate arbitrary language sets.
            for index, left in enumerate(canonical):
                for right in canonical[index + 1:]:
                    candidates.append((left, right))
        return tuple(sorted(candidates, key=lambda item: (-sum(self.usage.get(code, 0.0) for code in item), len(item), item)))


Probe = Callable[[tuple[str, ...]], LanguageProbe]


class BoundedLanguageDetector:
    """Choose a language set using a bounded number of cheap OCR probes."""

    def __init__(self, *, max_probes: int = 5, min_confidence: float = 55.0, min_text_chars: int = 3, usage: Mapping[str, float] | None = None) -> None:
        self.max_probes = max(1, max_probes)
        self.min_confidence = min_confidence
        self.min_text_chars = max(1, min_text_chars)
        self.usage = usage or {}

    def detect(self, candidates: Iterable[str], probe: Probe) -> LanguageDetection:
        ranker = LanguageCandidateRanker(self.usage)
        ranked = ranker.rank(candidates)[: self.max_probes]
        if not ranked:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, 0.0, (), 0, "no candidates")

        scored: list[tuple[float, LanguageProbe, tuple[str, ...]]] = []
        for candidate in ranked:
            result = probe(candidate)
            counts = script_counts(result.text)
            expected = scripts_for_languages(candidate)
            expected_count = sum(counts.get(script, 0) for script in expected)
            unexpected_count = sum(count for script, count in counts.items() if script not in expected and script != "OTHER")
            usable = sum(1 for token in result.text.split() if any(char.isalnum() for char in token))
            score = float(result.confidence) + min(usable, 50) * 0.25 + min(expected_count, 200) * 0.15 - min(unexpected_count, 100) * 0.4
            scored.append((score, result, candidate))

        scored.sort(key=lambda item: (-item[0], len(item[2]), item[2]))
        best_score, best_probe, best_candidate = scored[0]
        counts = script_counts(best_probe.text)
        scripts = tuple(sorted(script for script, count in counts.items() if count > 0 and script != "OTHER"))
        if len(best_probe.text.strip()) < self.min_text_chars or best_probe.confidence < self.min_confidence:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "insufficient OCR evidence")
        if not scripts:
            # Digits and punctuation can produce a high-confidence OCR result
            # without providing any language/script evidence.  Treating that
            # output as a detected language is unsafe for AUTO routing.
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "no script evidence")
        expected = scripts_for_languages(best_candidate)
        if scripts and not (set(scripts) & expected):
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best_probe.confidence), scripts, len(scored), "script mismatch")
        status = LanguageDecisionStatus.MULTILINGUAL_DETECTED if len(best_candidate) > 1 else LanguageDecisionStatus.DETECTED
        return LanguageDetection(OCRLanguagePolicy(OCRLanguageMode.EXPLICIT, best_candidate), status, max(0.0, min(100.0, best_score)), scripts, len(scored), "best bounded candidate")

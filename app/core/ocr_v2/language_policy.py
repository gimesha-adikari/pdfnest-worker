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


@dataclass(frozen=True)
class ObservedLanguageEvidence:
    """Safe, text-free evidence contributed by one language probe."""

    language: str
    script: str
    confidence: float
    usable_tokens: int
    script_characters: int
    probe_source: str = "single"


@dataclass(frozen=True)
class ObservedScriptEvidence:
    """Aggregated script evidence used for bounded AUTO coverage decisions."""

    script: str
    cumulative_strength: float
    independent_probe_count: int
    usable_tokens: int
    script_characters: int


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


def _probe_score(result: LanguageProbe, candidate: tuple[str, ...]) -> tuple[float, dict[str, int], int]:
    counts = script_counts(result.text)
    expected = scripts_for_languages(candidate)
    expected_count = sum(counts.get(script, 0) for script in expected)
    unexpected_count = sum(count for script, count in counts.items() if script not in expected and script != "OTHER")
    usable = sum(1 for token in result.text.split() if any(char.isalnum() for char in token))
    score = float(result.confidence) + min(usable, 50) * 0.25 + min(expected_count, 200) * 0.15 - min(unexpected_count, 100) * 0.4
    return score, counts, expected_count


class AdaptiveLanguageDetector:
    """Use cheap singles first, expanding only for unexplained script evidence.

    The current product language boundary is deliberately small.  Adaptive
    expansion can add a supported pair or the one current three-language set;
    it never enumerates arbitrary future combinations.
    """

    def __init__(
        self,
        *,
        initial_probes: int = 3,
        normal_max_probes: int = 5,
        expanded_max_probes: int = 7,
        min_confidence: float = 45.0,
        min_text_chars: int = 3,
        min_linguistic_chars: int = 2,
        min_linguistic_tokens: int = 2,
        min_script_evidence_confidence: float = 25.0,
        min_score_margin: float = 12.0,
        usage: Mapping[str, float] | None = None,
    ) -> None:
        self.initial_probes = max(1, initial_probes)
        self.normal_max_probes = max(self.initial_probes, normal_max_probes)
        self.expanded_max_probes = max(self.normal_max_probes, expanded_max_probes)
        self.min_confidence = min_confidence
        self.min_text_chars = max(1, min_text_chars)
        self.min_linguistic_chars = max(1, min_linguistic_chars)
        self.min_linguistic_tokens = max(1, min_linguistic_tokens)
        self.min_script_evidence_confidence = min_script_evidence_confidence
        self.min_score_margin = max(0.0, min_score_margin)
        self.usage = usage or {}

    def detect(self, candidates: Iterable[str], probe: Probe) -> LanguageDetection:
        ranker = LanguageCandidateRanker(self.usage)
        canonical = tuple(sorted({str(value).lower() for value in candidates if str(value).strip()}))
        ranked = ranker.rank(canonical)
        singles = [candidate for candidate in ranked if len(candidate) == 1]
        if not singles:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, 0.0, (), 0, "no candidates")

        scored: dict[tuple[str, ...], tuple[float, LanguageProbe, dict[str, int], int]] = {}

        def evaluate(candidate: tuple[str, ...]) -> None:
            if candidate in scored:
                return
            result = probe(candidate)
            score, counts, expected_count = _probe_score(result, candidate)
            scored[candidate] = (score, result, counts, expected_count)

        # Stage 1 is intentionally all currently supported singles.  History
        # orders these probes but cannot suppress a language from coverage.
        for candidate in singles[: self.initial_probes]:
            evaluate(candidate)

        single_scores = sorted(((value[0], candidate) for candidate, value in scored.items()), reverse=True)
        single_evidence = []
        for candidate, (score, result, counts, expected_count) in scored.items():
            alphabetic_tokens = sum(1 for token in result.text.split() if any(char.isalpha() for char in token))
            if (
                result.confidence >= self.min_script_evidence_confidence
                and expected_count >= self.min_linguistic_chars
                # Coverage evidence may be one token when a low-quality
                # secondary script is materially present.  Final acceptance
                # below retains the stronger linguistic-token floor.
                and alphabetic_tokens >= 1
            ):
                single_evidence.append((score, candidate, result, counts, expected_count))

        # The first two evidence-bearing singles establish the ordinary pair
        # expansion. A third evidence-bearing script is treated as an
        # ambiguity signal: evaluate the one bounded triple, then fail closed
        # if the runtime cannot reproduce that script. This avoids both
        # probing every combination and accepting an incomplete two-language
        # result on a three-language page.
        single_evidence.sort(key=lambda item: (-item[0], item[1]))
        meaningful_scripts: set[str] = set()
        for _, candidate, _, counts, _ in single_evidence[:2]:
            meaningful_scripts.update(script for script in scripts_for_languages(candidate) if counts.get(script, 0) > 0)
        tail_scripts: set[str] = set()
        for _, candidate, result, counts, _ in single_evidence[2:]:
            if result.confidence >= self.min_confidence:
                tail_scripts.update(script for script in scripts_for_languages(candidate) if counts.get(script, 0) > 0)

        language_for_script = {"LATIN": "eng", "SINHALA": "sin", "TAMIL": "tam"}
        required_scripts = meaningful_scripts | tail_scripts
        required_languages = tuple(sorted(language_for_script[script] for script in required_scripts if script in language_for_script))
        expansion: tuple[str, ...] | None = None
        if len(required_languages) >= 3 and set(required_languages) == {"eng", "sin", "tam"} and set(canonical) >= {"eng", "sin", "tam"}:
            expansion = ("eng", "sin", "tam")
        elif len(required_languages) == 2:
            expansion = required_languages
        elif len(required_languages) == 1 and len(single_scores) > 1:
            winner_score, winner = single_scores[0]
            runner_score, _ = single_scores[1]
            if winner_score - runner_score < self.min_score_margin:
                expansion = tuple(sorted({winner[0], single_scores[1][1][0]}))

        # Stage 2/3: evaluate only the evidence-justified pair/triple.  A
        # weak/ambiguous single may use one bounded fallback pair; it does not
        # force every page through the maximum budget.
        if expansion and len(scored) < self.expanded_max_probes:
            evaluate(expansion)

        if not scored:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, 0.0, (), 0, "no candidates")

        # Prefer a candidate that explains every meaningful script.  This is
        # the safety rule that prevents a strong incomplete single/pair from
        # hiding a second or third observed script.
        covering = [
            (value[0], candidate, value)
            for candidate, value in scored.items()
            if required_scripts <= scripts_for_languages(candidate)
        ]
        if required_scripts and not covering:
            best = max(scored.values(), key=lambda item: item[0])
            scripts = tuple(sorted(script for script, count in best[2].items() if count > 0 and script != "OTHER"))
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best[1].confidence), scripts, len(scored), "unexplained script evidence")

        best_score, best_candidate, best_value = max(covering or [(value[0], candidate, value) for candidate, value in scored.items()], key=lambda item: (item[0], -len(item[1]), item[1]))
        best_probe = best_value[1]
        scripts = tuple(sorted(script for script, count in best_value[2].items() if count > 0 and script != "OTHER"))
        if len(best_probe.text.strip()) < self.min_text_chars or best_probe.confidence < self.min_confidence:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "insufficient OCR evidence")
        if not required_scripts:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "no script evidence")
        if sum(1 for token in best_probe.text.split() if any(char.isalpha() for char in token)) < self.min_linguistic_tokens:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "insufficient linguistic evidence")
        expected = scripts_for_languages(best_candidate)
        if scripts and not (set(scripts) & expected):
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best_probe.confidence), scripts, len(scored), "script mismatch")
        if required_scripts - set(scripts):
            # A low-quality secondary probe may suggest a script that the
            # expanded candidate cannot reproduce. A one-script final output
            # can safely fall back to its strong matching single candidate;
            # mixed output with unexplained scripts must remain uncertain.
            if len(scripts) == 1:
                fallback = next(
                    (
                        (candidate, value)
                        for candidate, value in scored.items()
                        if len(candidate) == 1
                        and scripts_for_languages(candidate) == set(scripts)
                        and value[1].confidence >= self.min_confidence
                        and sum(1 for token in value[1].text.split() if any(char.isalpha() for char in token)) >= self.min_linguistic_tokens
                    ),
                    None,
                )
                if fallback is not None:
                    candidate, value = fallback
                    return LanguageDetection(OCRLanguagePolicy(OCRLanguageMode.EXPLICIT, candidate), LanguageDecisionStatus.DETECTED, max(0.0, min(100.0, value[0])), scripts, len(scored), "secondary script probe was not reproduced")
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best_probe.confidence), scripts, len(scored), "unexplained script evidence")
        status = LanguageDecisionStatus.MULTILINGUAL_DETECTED if len(best_candidate) > 1 else LanguageDecisionStatus.DETECTED
        return LanguageDetection(OCRLanguagePolicy(OCRLanguageMode.EXPLICIT, best_candidate), status, max(0.0, min(100.0, best_score)), scripts, len(scored), "adaptive bounded candidate")


class FusedLanguageDetector:
    """Fuse strong single-language evidence before accepting a bounded set.

    A single probe can emit a script because the requested Tesseract language
    is a poor match.  Two strong singles plus a quality-improving pair are
    therefore treated as independent evidence for a bilingual decision.  A
    weak third script is not silently accepted: it triggers the one supported
    triple and remains uncertain if that triple cannot reproduce it.
    """

    def __init__(
        self,
        *,
        initial_probes: int = 3,
        normal_max_probes: int = 5,
        expanded_max_probes: int = 7,
        min_confidence: float = 45.0,
        min_text_chars: int = 3,
        min_linguistic_tokens: int = 2,
        min_script_evidence_confidence: float = 25.0,
        min_pair_gain: float = 3.0,
        relaxed_pair_gain: float = 1.0,
        min_pair_script_characters: int = 30,
        material_alt_margin: float = 25.0,
        usage: Mapping[str, float] | None = None,
    ) -> None:
        self.initial_probes = max(1, initial_probes)
        self.normal_max_probes = max(self.initial_probes, normal_max_probes)
        self.expanded_max_probes = max(self.normal_max_probes, expanded_max_probes)
        self.min_confidence = min_confidence
        self.min_text_chars = max(1, min_text_chars)
        self.min_linguistic_tokens = max(1, min_linguistic_tokens)
        self.min_script_evidence_confidence = min_script_evidence_confidence
        self.min_pair_gain = max(0.0, min_pair_gain)
        self.relaxed_pair_gain = max(0.0, relaxed_pair_gain)
        self.min_pair_script_characters = max(1, min_pair_script_characters)
        self.material_alt_margin = max(0.0, material_alt_margin)
        self.usage = usage or {}

    @staticmethod
    def _tokens(text: str) -> int:
        return sum(1 for token in text.split() if any(char.isalpha() for char in token))

    @staticmethod
    def _script_tuple(counts: Mapping[str, int]) -> tuple[str, ...]:
        return tuple(sorted(script for script, count in counts.items() if count > 0 and script != "OTHER"))

    def detect(self, candidates: Iterable[str], probe: Probe) -> LanguageDetection:
        ranker = LanguageCandidateRanker(self.usage)
        canonical = tuple(sorted({str(value).lower() for value in candidates if str(value).strip()}))
        ranked = ranker.rank(canonical)
        singles = [candidate for candidate in ranked if len(candidate) == 1]
        if not singles:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, 0.0, (), 0, "no candidates")

        scored: dict[tuple[str, ...], tuple[float, LanguageProbe, dict[str, int], int]] = {}

        def evaluate(candidate: tuple[str, ...]) -> None:
            if candidate in scored:
                return
            result = probe(candidate)
            score, counts, expected_count = _probe_score(result, candidate)
            scored[candidate] = (score, result, counts, expected_count)

        for candidate in singles[: self.initial_probes]:
            evaluate(candidate)

        single_rows: list[tuple[float, tuple[str, ...], LanguageProbe, dict[str, int], int, int]] = []
        for candidate, (score, result, counts, expected_count) in scored.items():
            tokens = self._tokens(result.text)
            if (
                result.confidence >= self.min_script_evidence_confidence
                and expected_count >= 2
                and tokens >= 1
            ):
                single_rows.append((score, candidate, result, counts, expected_count, tokens))
        single_rows.sort(key=lambda item: (-item[0], item[1]))
        if not single_rows:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, 0.0, (), len(scored), "no script evidence")

        language_for_script = {"LATIN": "eng", "SINHALA": "sin", "TAMIL": "tam"}
        by_script: dict[str, ObservedScriptEvidence] = {}
        best_for_script: dict[str, tuple[float, tuple[str, ...], LanguageProbe, dict[str, int], int, int]] = {}
        for row in single_rows:
            score, candidate, result, counts, _, tokens = row
            for script in scripts_for_languages(candidate):
                chars = counts.get(script, 0)
                if chars <= 0:
                    continue
                language = candidate[0]
                evidence = ObservedLanguageEvidence(language, script, result.confidence, tokens, chars)
                strength = max(0.0, evidence.confidence) * min(chars, 80) / 80.0
                previous = by_script.get(script)
                by_script[script] = ObservedScriptEvidence(
                    script,
                    (previous.cumulative_strength if previous else 0.0) + strength,
                    (previous.independent_probe_count if previous else 0) + 1,
                    (previous.usable_tokens if previous else 0) + tokens,
                    (previous.script_characters if previous else 0) + chars,
                )
                if script not in best_for_script or score > best_for_script[script][0]:
                    best_for_script[script] = row

        supported_scripts = [script for script in by_script if script in language_for_script]
        supported_scripts.sort(key=lambda script: (-best_for_script[script][0], script))
        primary_scripts = supported_scripts[:2]
        if len(primary_scripts) == 1:
            winner = best_for_script[primary_scripts[0]]
            second = next((script for script in supported_scripts[1:] if best_for_script[script][2].confidence >= self.min_confidence), None)
            if second:
                primary_scripts.append(second)

        primary_languages = tuple(sorted(language_for_script[script] for script in primary_scripts))
        pair_candidate = primary_languages if len(primary_languages) == 2 else None
        if pair_candidate and set(pair_candidate) <= set(canonical) and len(scored) < self.expanded_max_probes:
            evaluate(pair_candidate)

        unresolved_weak: set[str] = set()
        unresolved_material: set[str] = set()
        pair_score = scored[pair_candidate][0] if pair_candidate and pair_candidate in scored else None
        for script in supported_scripts[2:]:
            score, _, result, counts, _, tokens = best_for_script[script]
            # A low-confidence, short third-script trace is precisely the
            # ambiguous trilingual signal that must not become a confident
            # incomplete pair.
            if result.confidence >= self.min_confidence and (counts.get(script, 0) <= 3 or tokens <= 1):
                unresolved_weak.add(script)
            elif score >= self.min_confidence and (pair_score is None or pair_score - score < self.material_alt_margin):
                unresolved_material.add(script)

        unresolved = unresolved_weak | unresolved_material
        if unresolved and set(canonical) >= {"eng", "sin", "tam"} and len(scored) < self.expanded_max_probes:
            evaluate(("eng", "sin", "tam"))
            triple = scored[("eng", "sin", "tam")]
            triple_scripts = set(self._script_tuple(triple[2]))
            if set(unresolved) <= triple_scripts and triple[1].confidence >= self.min_confidence and self._tokens(triple[1].text) >= self.min_linguistic_tokens:
                return LanguageDetection(
                    OCRLanguagePolicy(OCRLanguageMode.EXPLICIT, ("eng", "sin", "tam")),
                    LanguageDecisionStatus.MULTILINGUAL_DETECTED,
                    max(0.0, min(100.0, triple[0])),
                    self._script_tuple(triple[2]),
                    len(scored),
                    "fused trilingual evidence",
                )
            best = max(scored.values(), key=lambda item: item[0])
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best[1].confidence), self._script_tuple(best[2]), len(scored), "unexplained script evidence")
        if unresolved:
            best = max(scored.values(), key=lambda item: item[0])
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best[1].confidence), self._script_tuple(best[2]), len(scored), "unexplained script evidence")

        best_single = single_rows[0]
        if pair_candidate and pair_candidate in scored:
            pair_score, pair_probe, pair_counts, _ = scored[pair_candidate]
            pair_tokens = self._tokens(pair_probe.text)
            # Preserve the stricter gain for a pair that merely inherits
            # single-language evidence.  When the combined pair itself
            # materially reproduces both independently observed scripts, a
            # smaller gain is enough to avoid omitting a genuine secondary
            # language because Tesseract's pair quality score is near-tied.
            pair_materially_covers_scripts = all(
                pair_counts.get(script, 0) >= self.min_pair_script_characters
                for script in primary_scripts
            )
            required_pair_gain = self.relaxed_pair_gain if pair_materially_covers_scripts else self.min_pair_gain
            if (
                pair_probe.confidence >= self.min_confidence
                and len(pair_probe.text.strip()) >= self.min_text_chars
                and pair_tokens >= self.min_linguistic_tokens
                and pair_score >= best_single[0] + required_pair_gain
            ):
                return LanguageDetection(
                    OCRLanguagePolicy(OCRLanguageMode.EXPLICIT, pair_candidate),
                    LanguageDecisionStatus.MULTILINGUAL_DETECTED,
                    max(0.0, min(100.0, pair_score)),
                    tuple(sorted(set(primary_scripts) | set(self._script_tuple(pair_counts)))),
                    len(scored),
                    "fused independent script evidence",
                )

        best_score, best_candidate, best_probe, best_counts, _, _ = best_single
        scripts = self._script_tuple(best_counts)
        if len(best_probe.text.strip()) < self.min_text_chars or best_probe.confidence < self.min_confidence:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "insufficient OCR evidence")
        if not scripts:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "no script evidence")
        if self._tokens(best_probe.text) < self.min_linguistic_tokens:
            return LanguageDetection(None, LanguageDecisionStatus.UNDETERMINED, max(0.0, best_probe.confidence), scripts, len(scored), "insufficient linguistic evidence")
        expected = scripts_for_languages(best_candidate)
        if not set(scripts) & expected:
            return LanguageDetection(None, LanguageDecisionStatus.UNCERTAIN, max(0.0, best_probe.confidence), scripts, len(scored), "script mismatch")
        return LanguageDetection(
            OCRLanguagePolicy(OCRLanguageMode.EXPLICIT, best_candidate),
            LanguageDecisionStatus.DETECTED,
            max(0.0, min(100.0, best_score)),
            scripts,
            len(scored),
            "fused single-language evidence",
        )

from __future__ import annotations

from app.core.ocr_v2.language_policy import (
    FusedLanguageDetector,
    LanguageDecisionStatus,
    LanguageProbe,
    OCRLanguageMode,
    OCRLanguagePolicy,
)


def test_explicit_language_policy_preserves_canonical_codes() -> None:
    policy = OCRLanguagePolicy.from_request("eng+sin")
    assert policy.mode is OCRLanguageMode.EXPLICIT
    assert policy.languages == ("eng", "sin")
    assert policy.engine_expression == "eng+sin"


def test_auto_policy_is_distinct_from_explicit_selection() -> None:
    policy = OCRLanguagePolicy.from_request("auto", mode="AUTO", languages=["eng", "sin"])
    assert policy.mode is OCRLanguageMode.AUTO
    assert policy.languages == ("eng", "sin")


def test_detector_keeps_safe_undetermined_state_when_evidence_is_empty() -> None:
    detector = FusedLanguageDetector(initial_probes=1, normal_max_probes=1, expanded_max_probes=1, min_confidence=95)
    result = detector.detect(("eng", "sin"), lambda languages: LanguageProbe(languages, "", -1.0))
    assert result.status is LanguageDecisionStatus.UNDETERMINED


def _minority_script_probes(third_language: str, combined_words: int = 5):
    """Synthetic evidence with enough minority words but a wide aggregate-score gap."""
    from types import SimpleNamespace

    texts = {
        "eng": ("English source text and a report " * 10, 56, "LATIN"),
        "sin": ("මෙය පොදු වාර්තාවකි " * 12, 32 if third_language == "sin" else 49, "SINHALA"),
        "tam": ("இது ஒரு பொதுவான அறிக்கை " * 12, 32 if third_language == "tam" else 49, "TAMIL"),
    }
    pair = tuple(lang for lang in ("eng", "sin", "tam") if lang != third_language)
    triple = ("eng", "sin", "tam")
    script = texts[third_language][2]
    calls = []

    def probe(langs):
        calls.append(langs)
        if len(langs) == 1:
            text, mean, name = texts[langs[0]]
            words = {name: 3 if langs[0] == third_language else 20}
        elif langs == pair:
            text, mean = " ".join(texts[lang][0] for lang in pair), 80
            words = {texts[lang][2]: 20 for lang in pair}
        else:
            assert langs == triple
            text, mean = " ".join(texts[lang][0] for lang in triple), 82
            words = {texts[lang][2]: 20 for lang in pair} | {script: combined_words}
        return SimpleNamespace(language=langs, text=text, confidence=mean, confident_script_words=words)

    return pair, triple, calls, probe


def test_fused_detector_confirms_minority_script_despite_aggregate_score_gap() -> None:
    for minority in ("sin", "tam"):
        pair, triple, calls, probe = _minority_script_probes(minority)
        result = FusedLanguageDetector().detect(triple, probe)
        assert result.policy is not None
        assert result.policy.languages == triple
        assert result.primary_languages == pair
        assert calls.count(triple) == 1
        assert len(calls) == 5


def test_fused_detector_rejects_unconfirmed_third_script_and_respects_budget() -> None:
    pair, triple, calls, probe = _minority_script_probes("sin", combined_words=1)
    result = FusedLanguageDetector().detect(triple, probe)
    assert result.policy is not None
    assert result.policy.languages == pair
    assert not result.primary_languages
    assert calls.count(triple) == 1

    pair, triple, calls, probe = _minority_script_probes("sin")
    result = FusedLanguageDetector(normal_max_probes=4, expanded_max_probes=4).detect(triple, probe)
    assert result.policy is not None
    assert result.policy.languages == pair
    assert not result.primary_languages
    assert len(calls) == 4


def test_tesseract_probe_counts_only_confident_single_script_words(monkeypatch) -> None:
    from PIL import Image
    from types import SimpleNamespace

    from app.core.ocr_v2.adapters import tesseract

    tsv = "conf\ttext\n90\tpublic\n85\tවාර්තාව\n80\tஅறிக்கை\n69\tஅறிக்கை\n90\taஅ\n90\tக123\n80\tகி\n"
    monkeypatch.setattr(tesseract, "run_hardened_subprocess",
                        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=tsv))
    adapter = tesseract.TesseractAdapter("eng+sin+tam")
    result = adapter.probe(Image.new("RGB", (8, 8)), ("eng", "sin", "tam"))
    assert result.confidence == (90 + 85 + 80 + 69 + 90 + 90 + 80) / 7
    assert result.confident_script_words == {"LATIN": 1, "SINHALA": 1, "TAMIL": 2}

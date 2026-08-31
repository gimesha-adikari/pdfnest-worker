from app.core.ocr_v2.language_policy import (
    AdaptiveLanguageDetector,
    BoundedLanguageDetector,
    FusedLanguageDetector,
    LanguageDecisionStatus,
    LanguageProbe,
    LanguageCandidateRanker,
    OCRLanguageMode,
    OCRLanguagePolicy,
    canonicalize_language_ids,
)
from app.core.ocr_v2.adapters.tesseract import TesseractAdapter
from app.core.ocr_v2.contracts import PageGeometry
from app.core.ocr_v2.geometry import PreparedRaster

import io
import shutil
from pathlib import Path

import pytest
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from app.core.ocr_v2.orchestration import OCRV2Worker
from app.core.ocr_v2.validation import OCRProfile


def test_explicit_language_sets_are_canonical_and_order_independent():
    assert canonicalize_language_ids(["sin", "eng", "eng"]) == ("eng", "sin")
    first = OCRLanguagePolicy.from_request("sin+eng")
    second = OCRLanguagePolicy.from_request("eng+sin")
    assert first == second
    assert first.engine_expression == "eng+sin"


def test_auto_policy_is_distinct_from_explicit_default():
    policy = OCRLanguagePolicy.from_request("auto")
    assert policy.mode is OCRLanguageMode.AUTO
    assert policy.languages == ()
    assert policy.semantic_value == "AUTO"


def test_bounded_detector_can_select_multilingual_candidate():
    calls: list[tuple[str, ...]] = []

    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        calls.append(candidate)
        if candidate == ("eng", "sin"):
            return LanguageProbe(candidate, "English සිංහල", 92.0)
        return LanguageProbe(candidate, "English", 76.0)

    result = BoundedLanguageDetector(max_probes=5, min_confidence=40).detect(("eng", "sin"), probe)
    assert result.status is LanguageDecisionStatus.MULTILINGUAL_DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "eng+sin"
    assert len(calls) <= 5


def test_usage_history_is_only_a_bounded_candidate_ordering_prior():
    ranked = LanguageCandidateRanker({"sin": 10.0, "eng": 1.0}).rank(("eng", "sin", "tam"))
    assert ranked.index(("sin",)) < ranked.index(("eng",))
    assert ("eng", "sin") in ranked
    assert len(ranked) == 6


def test_detector_returns_undetermined_for_sparse_evidence():
    result = BoundedLanguageDetector(max_probes=2, min_confidence=40, min_text_chars=3).detect(
        ("eng",), lambda candidate: LanguageProbe(candidate, "", -1.0)
    )
    assert result.status is LanguageDecisionStatus.UNDETERMINED
    assert result.policy is None


def test_detector_returns_undetermined_for_scriptless_high_confidence_output():
    result = BoundedLanguageDetector(max_probes=2, min_confidence=40).detect(
        ("eng", "sin"),
        lambda candidate: LanguageProbe(candidate, "4826 184.50 2026-08-30", 99.0),
    )
    assert result.status is LanguageDecisionStatus.UNDETERMINED
    assert result.policy is None
    assert result.reason == "no script evidence"


def test_adaptive_detector_expands_sin_tam_when_both_scripts_are_present():
    calls: list[tuple[str, ...]] = []

    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        calls.append(candidate)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ 4821", 80.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ் உரை 4821", 82.0)
        if candidate == ("eng",):
            return LanguageProbe(candidate, "", 35.0)
        return LanguageProbe(candidate, "සිංහල தமிழ் 4821", 94.0)

    result = AdaptiveLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.MULTILINGUAL_DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "sin+tam"
    assert ("sin", "tam") in calls
    assert len(calls) == 4


def test_adaptive_detector_expands_the_current_three_language_candidate():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample 4824", 80.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ 4824", 80.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ் உரை 4824", 80.0)
        return LanguageProbe(candidate, "English සිංහල தமிழ் 4824", 96.0)

    result = AdaptiveLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.MULTILINGUAL_DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "eng+sin+tam"
    assert result.probes == 4


def test_adaptive_detector_does_not_confidently_accept_an_incomplete_triple():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample", 80.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ", 58.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ்", 48.0)
        return LanguageProbe(candidate, "English සිංහල", 96.0)

    result = AdaptiveLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.UNCERTAIN
    assert result.policy is None
    assert result.reason == "unexplained script evidence"


def test_adaptive_detector_prior_cannot_suppress_script_expansion():
    calls: list[tuple[str, ...]] = []

    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        calls.append(candidate)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ தமிழ் உரை", 80.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ் உரை 4825", 82.0)
        if candidate == ("sin", "tam"):
            return LanguageProbe(candidate, "සිංහල පෙළ தமிழ் உரை 4825", 88.0)
        return LanguageProbe(candidate, "English", 20.0)

    result = AdaptiveLanguageDetector(usage={"eng": 1000, "sin": 1, "tam": 1}).detect(("eng", "sin", "tam"), probe)
    assert result.policy is not None
    assert result.policy.engine_expression == "sin+tam"
    assert ("sin", "tam") in calls


def test_adaptive_detector_returns_uncertain_when_script_expansion_budget_is_exhausted():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample", 80.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ", 80.0)
        return LanguageProbe(candidate, "தமிழ் உரை", 80.0)

    result = AdaptiveLanguageDetector(normal_max_probes=3, expanded_max_probes=3).detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.UNCERTAIN
    assert result.policy is None
    assert result.reason == "unexplained script evidence"


def test_adaptive_detector_early_accepts_clean_single_language_after_single_probes():
    calls: list[tuple[str, ...]] = []

    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        calls.append(candidate)
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample text", 95.0)
        return LanguageProbe(candidate, "", -1.0)

    result = AdaptiveLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "eng"
    assert len(calls) == 3


def test_fused_detector_accepts_pair_from_independent_single_evidence():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample text", 82.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ පරීක්ෂණය", 78.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ்", 25.0)
        if candidate == ("eng", "sin"):
            # The fused decision may trust the independent singles even when
            # the combined sample under-reports one script.
            return LanguageProbe(candidate, "English sample text", 96.0)
        return LanguageProbe(candidate, "English sample text", 70.0)

    result = FusedLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.MULTILINGUAL_DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "eng+sin"
    assert result.reason == "fused independent script evidence"


def test_fused_detector_relaxes_pair_gain_only_when_pair_reproduces_both_scripts():
    english = " ".join(["English"] * 30)
    sinhala = " ".join(["සිංහල"] * 20)

    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, english, 80.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, sinhala, 74.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ்", 25.0)
        if candidate == ("eng", "sin"):
            # The pair only improves the score by one point, but visibly
            # contains material Latin and Sinhala evidence itself.
            return LanguageProbe(candidate, f"{english} {sinhala}", 81.0)
        return LanguageProbe(candidate, english, 70.0)

    result = FusedLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.MULTILINGUAL_DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "eng+sin"


def test_fused_detector_keeps_weak_unresolved_third_script_uncertain():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample text", 80.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ", 70.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ்", 47.0)
        if candidate == ("eng", "sin"):
            return LanguageProbe(candidate, "English සිංහල", 96.0)
        return LanguageProbe(candidate, "English සිංහල", 96.0)

    result = FusedLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.UNCERTAIN
    assert result.policy is None


def test_fused_detector_recovers_noisy_dominant_single_without_pair_gain():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ පරීක්ෂණය", 82.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ் உரை", 53.0)
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English noise", 45.0)
        return LanguageProbe(candidate, "සිංහල පෙළ", 86.0)

    result = FusedLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "sin"


def test_fused_detector_evaluates_strong_third_language():
    def probe(candidate: tuple[str, ...]) -> LanguageProbe:
        if candidate == ("eng",):
            return LanguageProbe(candidate, "English sample text", 80.0)
        if candidate == ("sin",):
            return LanguageProbe(candidate, "සිංහල පෙළ පරීක්ෂණය", 78.0)
        if candidate == ("tam",):
            return LanguageProbe(candidate, "தமிழ் உரை பக்கம்", 78.0)
        if candidate == ("eng", "sin"):
            return LanguageProbe(candidate, "English සිංහල", 88.0)
        return LanguageProbe(candidate, "English සිංහල தமிழ்", 96.0)

    result = FusedLanguageDetector().detect(("eng", "sin", "tam"), probe)
    assert result.status is LanguageDecisionStatus.MULTILINGUAL_DETECTED
    assert result.policy is not None
    assert result.policy.engine_expression == "eng+sin+tam"


def test_installed_language_set_is_a_real_capability_boundary():
    with pytest.raises(ValueError):
        canonicalize_language_ids(["tam"], installed_languages=["eng", "sin"])


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="system Tesseract is not installed")
def test_tesseract_executes_with_stale_prefix_fallback(monkeypatch):
    image = Image.new("RGB", (720, 220), "white")
    ImageDraw.Draw(image).text((30, 70), "English routing probe 123", fill="black")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    raster = PreparedRaster(
        image=image,
        png_bytes=encoded.getvalue(),
        geometry=PageGeometry(width=360, height=110, pixel_width=image.width, pixel_height=image.height),
        dpi=144,
    )
    monkeypatch.setenv("TESSDATA_PREFIX", "/tesseract/tessdata")
    adapter = TesseractAdapter("eng")
    assert adapter.availability().available
    adapter.initialize()
    output = adapter.recognize_page("fallback", raster)
    assert any(item.get("text") for item in output.items if item.get("kind") != "line")


@pytest.mark.skipif(
    not Path("/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf").is_file(),
    reason="Sinhala test font is not installed",
)
def test_auto_routes_a_real_mixed_script_page_to_a_multilingual_policy(tmp_path):
    image = Image.new("RGB", (1400, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 45), "English routing evidence", fill="black")
    sinhala_font = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf", 52)
    draw.text((40, 190), "සිංහල පෙළ පරීක්ෂණය", fill="black", font=sinhala_font)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    pdf_path = tmp_path / "mixed.pdf"
    document = fitz.open()
    document.new_page(width=700, height=180).insert_image(fitz.Rect(0, 0, 700, 180), stream=encoded.getvalue())
    document.save(str(pdf_path))
    document.close()

    result = OCRV2Worker().process_document(pdf_path, language="auto", language_mode="AUTO", languages=["eng", "sin"], profile=OCRProfile.OCR_TEXT_V2)
    page = result.pages[0]
    assert page.status.value == "SUCCESS"
    assert page.language.language_status == "MULTILINGUAL_DETECTED"
    assert set(page.language.detected_languages) == {"eng", "sin"}
    assert {"LATIN", "SINHALA"}.issubset(set(page.language.detected_scripts))
    assert "සිංහල" in page.text

from __future__ import annotations

import fitz
import pytest

from app.api.tools.editor.document import (
    is_element_dirty,
    resolve_surgical_targets,
)


def test_phase4_target_not_found_safety_rule():
    """Verify that specifying a target_substring that doesn't exist returns empty targets (no whole-line fallback)."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Built a production-ready PDF platform", fontname="helv", fontsize=12)

    element = {
        "text": "Built a production-ready PDF platform",
        "original_text": "Built a production-ready PDF platform",
        "target_substring": "NonExistentWord",
        "x": 50, "y": 40, "width": 250, "height": 15,
        "style": {"bold": True},
    }

    assert is_element_dirty(element) is True

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 0  # CRITICAL SAFETY RULE: Zero targets returned, no line fallback!
    doc.close()


def test_phase4_explicit_target_word_selection():
    """Verify that specifying an explicit target_substring correctly targets strictly that word."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Built a production-ready PDF platform", fontname="helv", fontsize=12)

    element = {
        "text": "Built a production-ready PDF platform",
        "original_text": "Built a production-ready PDF platform",
        "target_substring": "production-ready",
        "x": 50, "y": 40, "width": 250, "height": 15,
        "style": {"bold": True},
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1
    assert targets[0]["original_substring"] == "production-ready"
    doc.close()


def test_phase10_duplicate_word_occurrence_targeting():
    """Verify that selection_start and selection_end target the exact duplicate occurrence of a word."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    # Original text: "Hello world, world, world"
    # Indices: "world" 1 = 6..11, "world" 2 = 13..18, "world" 3 = 20..25
    orig_text = "Hello world, world, world"
    page.insert_text((50, 50), orig_text, fontname="helv", fontsize=12)

    # Target 2nd 'world' (start=13, end=18)
    element = {
        "text": orig_text,
        "original_text": orig_text,
        "target_substring": "world",
        "selection_start": 13,
        "selection_end": 18,
        "x": 50, "y": 40, "width": 250, "height": 15,
        "style": {"bold": True},
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1
    assert targets[0]["original_start"] == 13
    assert targets[0]["original_end"] == 18
    assert targets[0]["original_substring"] == "world"
    doc.close()

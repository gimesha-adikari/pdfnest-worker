from __future__ import annotations

from types import SimpleNamespace

from app.core.ocr_v2.adapters import tesseract
from PIL import Image


def _tsv(*words):
    rows = ["block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext"]
    rows += ["\t".join(map(str, (1, 1, line, x, y, width, 20, confidence, text)))
             for line, x, y, width, confidence, text in words]
    return "\n".join(rows)


def test_auto_minority_replaces_only_an_aligned_weaker_span(monkeypatch):
    primary = _tsv(
        (1, 10, 10, 30, 92, "English"),
        (1, 50, 10, 14, 40, "bad"),
        (1, 65, 10, 20, 44, "guess"),
        (1, 100, 10, 40, 90, "தமிழ்நாடு"),
        (2, 10, 40, 30, 95, "Neighbor"),
    )
    combined = _tsv(
        (8, 10, 10, 30, 80, "Changed"),
        (8, 50, 10, 35, 92, "ශ්‍රී"),
        (8, 131, 10, 20, 97, "කී"),  # Partly covers an unrelated word.
        (8, 150, 10, 30, 96, "සිංහල"),  # No anchored span.
        (9, 10, 40, 30, 85, "වාර්තාව"),  # Weaker than the primary word.
    )
    calls = []
    def run(args, **kwargs):
        languages = args[4]
        calls.append(languages)
        return SimpleNamespace(returncode=0, stdout={"eng+tam": primary, "eng+sin+tam": combined}[languages])

    monkeypatch.setattr(tesseract, "run_hardened_subprocess", run)
    monkeypatch.setattr(tesseract.TesseractAdapter, "readiness", lambda self: True)
    adapter = tesseract.TesseractAdapter("eng+sin+tam")
    image = Image.new("RGB", (200, 80))
    raster = SimpleNamespace(image=image, png_bytes=b"stubbed raster")

    result = adapter.recognize_page_with_minority("test", raster, ("eng", "tam"))
    assert calls == ["eng+tam", "eng+sin+tam"]
    assert result.text == "English ශ්‍රී தமிழ்நாடு\nNeighbor"
    tokens = [item for item in result.items if item.get("kind") != "line"]
    assert [item["bbox"] for item in tokens] == [[10, 10, 40, 30], [50, 10, 85, 30], [100, 10, 140, 30], [10, 40, 40, 60]]
    assert [item["id"] for item in tokens] == ["test-token-0", "test-token-1", "test-token-2", "test-token-3"]
    assert result.items[-2]["token_ids"] == [item["id"] for item in tokens[:3]]

    calls.clear()
    assert adapter.recognize_page("test", raster).text.startswith("Changed")
    assert calls == ["eng+sin+tam"]

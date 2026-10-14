import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from kicad_rag.advanced import ocr_copy, source_diff
from kicad_rag.config import Document, Project, Settings, digest
from kicad_rag.ingest import extract
from kicad_rag.store import Store, index_project


def scanned_page(path, text="VOUT maximum 3.3 V at 25 C", line_y=180):
    image = Image.new("RGB", (1600, 1000), "white")
    font_paths = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    font = ImageFont.truetype(next(p for p in font_paths if Path(p).exists()), 44)
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), text, fill="black", font=font)
    draw.text((100, 240), "TEST FIXTURE: original page review required.", fill="black", font=font)
    draw.line((100, line_y, 1200, line_y), fill="black", width=3)
    image.save(path, "PDF", resolution=160)


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract integration")
def test_scanned_pdf_ocr_original_coordinates_and_cache(tmp_path):
    original = tmp_path / "scanned.pdf"
    scanned_page(original)
    before = digest(original)
    with pytest.raises(ValueError, match="No extractable"):
        extract(original)
    settings = Settings(ocr_mode="auto")
    pages, warnings = extract(original, settings)
    assert "3.3 V" in pages[0]["text"]
    assert pages[0]["extraction"] == "tesseract" and warnings
    assert all(0 <= w["x0"] < w["x1"] <= pages[0]["width"] for w in pages[0]["words"])
    project = Project(
        name="OCR test",
        documents=[Document(id="test", path=original.name, title="Fixture")],
        settings=settings,
    )
    assert index_project(tmp_path, project)["pages"] == 1
    assert index_project(tmp_path, project)["unchanged"]
    project.settings.ocr_dpi = 200
    with Store(tmp_path) as store, pytest.raises(ValueError, match="stale"):
        store.require_fresh(tmp_path, project)
    assert digest(original) == before
    result = ocr_copy(original, tmp_path / "derived.pdf")
    assert result["original_sha256"] == before
    assert "3.3" in extract(tmp_path / "derived.pdf")[0][0]["text"]
    assert digest(original) == before


def test_visual_diff_finds_image_only_change(tmp_path):
    # Digital text is identical (absent); original graphics differ.
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    scanned_page(a)
    scanned_page(b, line_y=200)
    assert source_diff(a, b)["visual_changes"]
    # Explicit OCR is unnecessary to compare pixels, but source_diff also extracts text.
    # Use an OCR derived copy to give both PDFs a text layer while preserving changed image content.
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract integration")
    a2, b2 = tmp_path / "a2.pdf", tmp_path / "b2.pdf"
    ocr_copy(a, a2)
    ocr_copy(b, b2)
    result = source_diff(a2, b2)
    assert result["visual_changes"] and result["visual_changes"][0]["page"] == 1
    assert not source_diff(a2, a2)["visual_changes"]

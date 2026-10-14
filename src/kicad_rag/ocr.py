"""Bounded page OCR with original PDF coordinates and explicit transcription uncertainty."""

import csv
import io
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path

from .config import digest


def engine_identity(language="eng"):
    executable = shutil.which("tesseract")
    if not executable:
        raise ValueError("Selective OCR requires tesseract; install it or set ocr_mode=off")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
    if version.returncode:
        raise ValueError("Cannot determine OCR engine identity")
    # Include trained data bytes so an engine/model change invalidates cached transcriptions.
    info = subprocess.run([executable, "--list-langs"], capture_output=True, text=True, timeout=10)
    import re

    match = re.search(r'"([^"]+)"', info.stdout + info.stderr)
    folder = Path(match.group(1)) if match else None
    data = {}
    for lang in language.split("+"):
        path = folder / f"{lang}.traineddata" if folder else None
        if path is None or not path.is_file():
            raise ValueError(f"OCR language data unavailable: {lang}")
        data[lang] = digest(path)
    return {"version": version.stdout.splitlines()[0], "traineddata": data}


def read_page(path, number, dpi=300, language="eng"):
    import pypdfium2 as pdfium

    identity = engine_identity(language)
    with pdfium.PdfDocument(path) as pdf:
        if not 1 <= number <= len(pdf):
            raise ValueError("OCR page outside original PDF")
        page = pdf[number - 1]
        width, height = page.get_size()
        if width * height * (dpi / 72) ** 2 > 40_000_000:
            raise ValueError("OCR page exceeds 40 megapixel resource bound")
        bitmap = page.render(scale=dpi / 72)
        picture = bitmap.to_pil()
        with tempfile.TemporaryDirectory(prefix="kicad-ocr-") as temp:
            image_path = Path(temp) / "page.png"
            picture.save(image_path)
            result = subprocess.run(
                [
                    shutil.which("tesseract"),
                    str(image_path),
                    "stdout",
                    "-l",
                    language,
                    "--dpi",
                    str(dpi),
                    "--psm",
                    "3",
                    "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
        bitmap.close()
        page.close()
    if result.returncode:
        raise ValueError(f"OCR failed on page {number}: {result.stderr[-2000:]}")
    lines, words = OrderedDict(), []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row["level"] != "5" or not row["text"].strip():
            continue
        x, y, w, h = (float(row[k]) * 72 / dpi for k in ("left", "top", "width", "height"))
        confidence = float(row["conf"])
        words.append(
            {
                "text": row["text"],
                "x0": x,
                "top": y,
                "x1": min(width, x + w),
                "bottom": min(height, y + h),
                "ocr_confidence": confidence,
            }
        )
        key = tuple(row[k] for k in ("block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(row["text"])
    if not words:
        return {
            "text": "",
            "words": [],
            "tables": [],
            "table_regions": [],
            "extraction": "tesseract",
            "quality": "no_text_detected",
            "ocr_engine": identity,
        }
    return {
        "text": "\n".join(" ".join(v) for v in lines.values()),
        "words": words,
        "tables": [],
        "table_regions": [],
        "extraction": "tesseract",
        "quality": "needs_original_review",
        "ocr_engine": identity,
        "low_confidence_words": sum(w["ocr_confidence"] < 80 for w in words),
    }

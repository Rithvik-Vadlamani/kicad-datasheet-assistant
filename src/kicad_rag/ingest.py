from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from .config import Document, Settings, canonical_hash

PIPELINE_VERSION = f"page-regions-v3:pdfplumber={pdfplumber.__version__}"


def split_text(text: str, size: int, overlap: int = 0) -> list[str]:
    """Bounded character chunks, with line boundaries preferred and guaranteed progress."""
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + size // 2, end)
            if newline > start:
                end = newline
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return out


def extract(path: Path, settings: Settings | None = None) -> tuple[list[dict], list[str]]:
    settings = settings or Settings()
    warnings, pages = [], []
    if path.suffix.lower() in (".md", ".txt"):
        for number, text in enumerate(path.read_text(encoding="utf-8").split("\f"), 1):
            pages.append({"page": number, "text": text, "tables": []})
    elif path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, 1):
                text = page.extract_text(layout=False) or ""
                tables, table_regions = [], []
                try:
                    for found in page.find_tables():
                        table = found.extract()
                        table_regions.append({"bbox": list(found.bbox), "rows": table,
                                              "cells": [list(c) if c else None for row in found.rows for c in row.cells]})
                        rows = [" | ".join(re.sub(r"\s+", " ", str(c or "")).strip() for c in row)
                                for row in table if any(row)]
                        if rows:
                            tables.append(rows)
                except (ValueError, TypeError):
                    warnings.append(f"{path.name} PDF page {number}: table extraction failed; inspect original.")
                pages.append({"page": number, "text": text, "tables": tables,
                              "width": page.width, "height": page.height,
                              "words": [{k: w[k] for k in ("text", "x0", "top", "x1", "bottom")}
                                        for w in page.extract_words()], "table_regions": table_regions,
                              "extraction": "pdfplumber", "quality": "needs_original_review"})
                poor_text = len(re.sub(r"\s", "", text)) < 80 or text.count("\ufffd") > 5
                if settings.ocr_mode == "always" or (settings.ocr_mode == "auto" and poor_text):
                    from .ocr import read_page
                    derived = read_page(path, number, settings.ocr_dpi, settings.ocr_language)
                    pages[-1]["digital_text"] = text
                    pages[-1].update(derived)
                    warnings.append(f"{path.name} PDF page {number}: OCR transcription requires original review; columns/units may be wrong.")
                if poor_text and settings.ocr_mode == "off":
                    warnings.append(f"{path.name} PDF page {number}: little selectable text; inspect/perform OCR.")
    else:
        raise ValueError(f"Unsupported document: {path.name}; use PDF, Markdown or UTF-8 text")
    if not any(p["text"].strip() or p["tables"] for p in pages):
        raise ValueError(f"No extractable text in {path.name}. OCR a copy first, then add that copy.")
    return pages, warnings


def chunk_document(doc: Document, pages: list[dict], settings: Settings, sha: str) -> list[dict]:
    chunks = []
    for page in pages:
        segments = [("text", t) for t in split_text(page["text"], settings.chunk_chars, settings.overlap_chars)]
        for rows in page["tables"]:
            header = rows[0]
            # Repeat the first extracted row as a candidate header; extraction is explicitly unverified.
            for body in split_text("\n".join(rows[1:]) or header, max(200, settings.chunk_chars - len(header[:500]) - 80)):
                segments.append(("table", "Extracted table (verify column alignment):\n" + header[:500] + "\n" + body))
        for i, (kind, text) in enumerate(segments):
            key = canonical_hash([PIPELINE_VERSION, doc.model_dump(), sha, page["page"], kind, i, text])[:24]
            chunks.append({"id": key, "doc_id": doc.id, "page": page["page"], "kind": kind,
                           "text": text, "parts": doc.parts, "title": doc.title, "revision": doc.revision,
                           "source_kind": doc.kind, "path": doc.path, "sha256": sha,
                           "parent_id": f"{doc.id}:{sha}:{page['page']}",
                           "extraction_quality": page.get("quality", "text_source")})
    return chunks


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()

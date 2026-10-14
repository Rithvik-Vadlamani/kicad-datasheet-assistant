"""Optional OCR of a derived source; original manufacturer bytes remain immutable."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import atomic_json, digest


def ocr_copy(source, output):
    executable = shutil.which("ocrmypdf")
    if not executable and not shutil.which("tesseract"):
        raise ValueError("OCR requires ocrmypdf or tesseract with pypdf; original source unchanged")
    if source.suffix.lower() != ".pdf" or output.suffix.lower() != ".pdf":
        raise ValueError("OCR requires PDF input and output")
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("OCR output must be a new derived file")
    before = digest(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".ocr-") as temp:
        target = Path(temp) / "ocr.pdf"
        if not executable:
            return _tesseract_copy(source, output, before, Path(temp))
        result = subprocess.run(
            [executable, "--skip-text", "--output-type", "pdf", "--jobs", "2", str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode or not target.exists():
            raise ValueError(f"OCR did not complete: {result.stderr}")
        if digest(source) != before:
            raise ValueError("Original source changed during OCR")
        shutil.move(target, output)
    lineage = {
        "schema_version": 1,
        "original": str(source.resolve()),
        "original_sha256": before,
        "derived": str(output.resolve()),
        "derived_sha256": digest(output),
        "method": "ocrmypdf --skip-text",
        "state": "needs_original_page_review",
        "stderr": result.stderr,
    }
    atomic_json(output.with_suffix(".lineage.json"), lineage)
    return lineage


def source_diff(previous, current, *, visual=True):
    """Page-aware textual revision comparison; figures still require original visual comparison."""
    import difflib

    from .ingest import extract

    old_hash, new_hash = digest(previous), digest(current)
    def compare_pages(path):
        try:
            return extract(path)
        except ValueError as error:
            if path.suffix.lower() != ".pdf" or not str(error).startswith("No extractable text"):
                raise
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(path) as pdf:
                pages = [{"page": i+1, "text": "", "tables": []} for i in range(len(pdf))]
            return pages, ["No selectable text; use the visual changes or selective OCR for this source"]
    old, old_warnings = compare_pages(previous)
    new, new_warnings = compare_pages(current)

    def text(page):
        return page["text"] + "\n" + "\n".join("\n".join(t) for t in page["tables"])

    visual_changes = []
    if visual and previous.suffix.lower() == current.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        from PIL import ImageChops, ImageStat
        with pdfium.PdfDocument(previous) as a_pdf, pdfium.PdfDocument(current) as b_pdf:
            for i in range(max(len(a_pdf), len(b_pdf))):
                if i >= min(len(a_pdf), len(b_pdf)):
                    visual_changes.append({"page": i+1, "change": "added_or_removed_page"})
                    continue
                images = []
                for pdf in (a_pdf, b_pdf):
                    page = pdf[i]
                    bitmap = page.render(scale=min(1, 1600 / max(page.get_size())))
                    images.append(bitmap.to_pil().convert("RGB"))
                    bitmap.close()
                    page.close()
                if images[0].size != images[1].size:
                    visual_changes.append({"page": i+1, "change": "page_dimensions"})
                else:
                    delta = ImageChops.difference(*images)
                    box = delta.getbbox()
                    if box:
                        visual_changes.append({"page": i+1, "change": "rendered_pixels", "bbox_pixels": box,
                                               "mean_channel_difference": ImageStat.Stat(delta).mean})
    changes = []
    for number in range(max(len(old), len(new))):
        a = text(old[number]) if number < len(old) else ""
        b = text(new[number]) if number < len(new) else ""
        if a != b:
            changes.append(
                {
                    "page": number + 1,
                    "diff": "\n".join(
                        difflib.unified_diff(
                            a.splitlines(), b.splitlines(), fromfile="previous", tofile="current", lineterm=""
                        )
                    ),
                }
            )
    if digest(previous) != old_hash or digest(current) != new_hash:
        raise ValueError("A source changed during comparison")
    return {
        "schema_version": 1,
        "previous_sha256": old_hash,
        "current_sha256": new_hash,
        "changed_pages": changes,
        "visual_changes": visual_changes,
        "visual_comparison": "bounded_page_render" if visual else "not_run",
        "warnings": old_warnings + new_warnings,
        "scope": "Extracted text/tables and bounded page-render differences. Visual changes locate review work; no automatic curve interpretation or semantic equivalence is asserted.",
    }


def _tesseract_copy(source, output, before, temp):
    import pypdfium2 as pdfium
    from pypdf import PdfReader, PdfWriter

    from .ocr import engine_identity

    identity = engine_identity()
    writer = PdfWriter()
    reader = PdfReader(source)
    processed = []
    with pdfium.PdfDocument(source) as pdf:
        for i, original_page in enumerate(reader.pages):
            if len((original_page.extract_text() or "").strip()) >= 80:
                writer.add_page(original_page)
                continue
            page = pdf[i]
            if page.get_width() * page.get_height() * (300/72)**2 > 40_000_000:
                raise ValueError("OCR page exceeds 40 megapixel resource bound")
            bitmap = page.render(scale=300/72)
            raster = temp / f"page-{i}.png"
            bitmap.to_pil().save(raster)
            bitmap.close()
            page.close()
            prefix = temp / f"page-{i}"
            run = subprocess.run([shutil.which("tesseract"), str(raster), str(prefix),
                                  "-l", "eng", "--dpi", "300", "pdf"],
                                 capture_output=True, text=True, timeout=90)
            if run.returncode or not prefix.with_suffix(".pdf").exists():
                raise ValueError("Tesseract PDF generation failed: " + run.stderr[-2000:])
            writer.add_page(PdfReader(prefix.with_suffix(".pdf")).pages[0])
            processed.append(i+1)
    target = temp / "combined.pdf"
    with target.open("wb") as stream:
        writer.write(stream)
    if digest(source) != before:
        raise ValueError("Original source changed during OCR")
    shutil.move(target, output)
    lineage = {"schema_version": 1, "original": str(source.resolve()), "original_sha256": before,
               "derived": str(output.resolve()), "derived_sha256": digest(output),
               "method": "selective tesseract PDF + pypdf", "engine": identity,
               "ocr_pages": processed, "state": "needs_original_page_review"}
    atomic_json(output.with_suffix(".lineage.json"), lineage)
    return lineage

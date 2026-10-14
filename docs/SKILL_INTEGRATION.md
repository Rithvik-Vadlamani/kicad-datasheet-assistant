# KiCad Design skill integration — 0.3.0

The app owns extraction, retrieval, evidence records, calculations and native CAD helpers. The installed skill is a workflow plus a thin runtime wrapper. Existing CLI commands and project schema version 1 remain supported; unknown configuration fields still fail validation.

After upgrading an existing evidence project, run `index` to refresh the extraction/search fingerprint, then regenerate evidence packets. Original files and review records are preserved; stale packets must not be reused. Existing explicitly configured retrieval settings remain in effect.

## New working paths

- `search/ask --parts EXACT-MPN ...` selects provisional parts before a netlist exists. Candidate selection is never presented as actual topology; unregistered exact parts do not inherit a similar part's datasheet.
- `configure --ocr-mode auto --ocr-language eng --ocr-dpi 300` uses Tesseract on low-text pages. Original PDFs remain immutable, and word boxes use original PDF coordinates. Engine, trained-data hashes and settings invalidate the extraction cache. `always` is explicit full OCR; default `off` needs no OCR executable. `ocr-copy` supports an OCRmyPDF installation or Tesseract + pypdf fallback. `source-diff` compares text and rendered-page pixels, including image-only PDFs.
- `extract-parameters --request request.json --output proposals.json` creates numeric review candidates with source identity and optional table-cell bindings. Conditions remain unreviewed source context, separate from requested conditions. Exit 2 means review is required. Import the proposal file in the GUI, use **Edit fields** beside the original page, and correct applicability before accepting. No automatic numerical acceptance occurs.
- Local embeddings accept a predownloaded safetensors Sentence Transformer through `configure --embedding-provider local --embedding-model /model/folder`. Install `.[local-models]`; inference is local, with no remote custom code. Token-window mean pooling covers long inputs. Model file identity is included in index freshness. No model is downloaded merely by configuring a project.
- Same-page context restoration defaults to true for new settings. It reserves one retrieval slot per restorable page and packs each page once. Old explicitly configured false values remain false. FTS additionally separates fused CamelCase headings while retaining the exact original evidence text.

## Native authoring

`generate-schematic` supports repeated physical references with distinct units and matching component metadata. All physical pins and explicit no-connects must match independent intent. New hierarchies use `generate-hierarchy`; parent wires address `SheetName.PortName`, and child ports address native pins or coordinates. Each generated sheet file and reference/unit instance is unique. See `examples/native/hierarchy-spec.json` and `schema hierarchy-spec`.

The PCB writer supports KiCad 10, 2/4/6/8 copper layers, rotated and bottom-side footprints, through vias, explicit tracks and zones. Native `pcbnew` applies footprint transformations and returns pad positions. Set `KICAD_PYTHON` when its interpreter is not found in the standard macOS bundle. This compatibility adapter depends on the installed KiCad 10 bindings; later API versions require native regression testing. The app itself stays in its Python 3.11+ environment.

Independent verification now resolves logical intent names through complete endpoint groups rather than stripping hierarchy prefixes. It checks the actual schematic against intent before trusting PCB parity or pad names. Actual native ERC/DRC, all pin/pad checks and visual inspection remain separate requirements.

These helpers create new projects and boards. They do not rewrite existing designs or autoroute. Buses, repeated-file sheet instantiation, multiple physical pads sharing one pad number, blind/buried vias, arbitrary layer counts, impedance solving and fabrication-specific stackup/rule configuration use native KiCad. The skill guides that work; unsupported helper input is not discarded.

## Simulation and release

`simulate deck.cir --models models.json --output new-run` accepts a manifest of project-local `.include` files with SHA-256, source URL and review note. Includes are bounded and flattened; cycles, undeclared dependencies, control blocks and plugin loading fail. `ngspice -n` ignores startup scripts. External library-corner selection and simulator-specific scripting remain native workflows. A successful run records execution, not model applicability or electrical approval.

`export-manufacturing` exports every enabled copper layer for supported layer counts, mask, silkscreen, outline and plated/non-plated drills after fresh passing board checks. `export-assembly` exports default-variant BOM and KiCad-native mm placement CSV, excludes DNP, checks reference coverage and reports missing MPNs. Actual assembler origin/rotation conventions and fabrication files still need review; no order is submitted.

## Reproduce validation

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check src tests scripts
python scripts/download_benchmark.py /new/corpus
kicad-rag --project /new/corpus index
python scripts/evaluate_benchmark.py /new/corpus examples/evaluation/expanded-200.jsonl --split holdout --output holdout.json
```

Native tests require KiCad/libraries/bindings, ngspice and Tesseract; missing integration dependencies must be reported as skips, never implied passes. The source download manifest pins original bytes, so a changed manufacturer PDF requires new annotation review. PDF originals and model weights are excluded from the distributable release.

The expanded suite covers 25 PDFs, 777 pages, 2,309 chunks and four manufacturers. It contains 150 section/row navigation probes, 25 multiple-prerequisite probes and 25 synthetic missing-source probes. Families are split before evaluation. Anchors were inspected in text; this is not independent engineering-answer annotation. Final keyword/parent retrieval found all required regions in 53/56 development, 40/42 calibration and 74/77 held-out questions, with all missing-source cases handled correctly. The held-out case-level Wilson 95% interval is approximately 89.2–98.7%.

The actual local BGE-small hybrid experiment scored 47/56 development and 35/42 calibration, below the keyword/parent path, so embeddings remain optional. Earlier MiniLM reranking also failed to beat the simpler pilot configuration. Neither model's availability establishes improved accuracy. Detailed reports include configurations, code hashes, source hashes, family uncertainty and cold/warm timings.

Automatic fact acceptance, 98% supported answer correctness, visual curve interpretation and physical hardware performance have not been established. Original-page engineering review is an intentional part of the working system. Docling, visual embeddings and fine-tuning are optional research alternatives, not required dependencies of this release.

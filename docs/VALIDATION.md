# Delivery validation and activation status

Validated on macOS with Python 3.13.1 and installed KiCad 10.0.3. Exact Python package versions are in `requirements-lock.txt`.

## Results

| Check | Result | What it establishes |
|---|---|---|
| Automated suite | 38 tests passed | Indexing, retrieval plumbing, provenance, graph validation, rules and provider request handling behave as exercised |
| Ruff | Passed | Selected syntax, import, undefined-name and bug-pattern checks |
| Python compilation | Passed | All application modules compile |
| Offline demo | Indexed 3 fictional documents and generated evidence/check reports | Works without an LLM service or API key |
| Teaching logic rule | −1.1 V high margin, +1.1 V low margin; violation | Expected arithmetic and failure classification |
| Teaching LDO rule | 1.8 W, 220 °C simple estimate; violation | Expected conditional thermal calculation, not physical simulation |
| Retrieval fixture | 6/6 cases passed | Expected document coverage plus one empty-result case on a tiny synthetic corpus |
| Real KiCad export | Arduino Nano template exported; 2 components, 28 nets parsed | Installed CLI export and real XML parsing work |
| Real manufacturer PDF | TI LM1117: 46 pages, 110 chunks | Actual PDF ingestion and passage retrieval work |
| PDF quality signal | Low-text pages 34 and 39 flagged | Incomplete text coverage is surfaced |
| PDF visual spot-check | Rendered and inspected physical page 4 | Absolute, recommended and thermal sections are visually present; corresponding extracted section names verified |
| Native GUI startup | Window created, Tk event loop ran, clean smoke-test shutdown | Tkinter runtime and initial construction work |

The manufacturer document used for integration was downloaded from [TI's LM1117 datasheet link](https://www.ti.com/lit/ds/symlink/lm1117.pdf). It is kept in the local temporary integration directory, not included in the portable source archive. The demo contains original fictional text instead of redistributing a manufacturer datasheet.

A capacitor-stability/ESR query over the real PDF retrieved 12 passages spanning physical pages 1, 6, 8, 9, 13, 14, 15 and 17. This is a retrieval smoke check, not an independently scored answer-quality benchmark. The Arduino template and TI document are unrelated fixtures and were not treated as a valid circuit review.

Native UI automation could not attach to the Python window through the available computer-use service. GUI construction/event-loop startup were tested; the complete GUI interaction flow was not visually verified. Core operations used by its buttons were tested through Python and the CLI.

## What was not tested live

No live embedding or answer model was invoked. Ollama was not available on PATH and no provider was configured for the delivered demo. The tests use explicitly labeled deterministic provider doubles and mocked HTTP responses to exercise vector handling, request schemas, generated-answer parsing and citation rejection.

Consequently, the delivered test results do not establish:

- The quality or latency of a particular local/cloud language model.
- General recall across arbitrary real datasheets or multilingual PDFs.
- That a model's valid quotation logically supports every generated claim.
- That an answer is robust against every form of document prompt injection.
- A complete electrical review of one of your actual boards; none was supplied.
- Windows/Linux installation or a full visual desktop interaction test.

## Activation steps

1. Configure an embedding model and an answer model, or keep keyword-only search if preferred.
2. Add the actual board's exact documents and save/export its schematic.
3. Resolve missing or ambiguous MPN mappings with `coverage` and `map-part`.
4. Index the project and confirm status is current.
5. Ask several questions with independently verified answers. Inspect retrieved pages first, then evaluate generated claims.
6. Enter reviewed parameter facts and conditions for numerical checks as needed.

## Repeat the automated checks

```bash
source .venv/bin/activate
pytest -q
ruff check src tests scripts
python -m compileall -q src
kicad-rag --project examples/demo index
python scripts/evaluate_retrieval.py --project examples/demo
kicad-rag --project examples/demo check
```

The final command intentionally returns 2. This is the expected demonstration of engineering violations, not an application crash.

To validate a real PDF and a chosen KiCad file together as independent ingestion fixtures:

```bash
python scripts/verify_integrations.py \
  --pdf /path/to/datasheet.pdf \
  --schematic /path/to/schematic.kicad_sch \
  --output tmp/my-integration
```

The script indexes into an isolated test folder. It does not modify the source schematic or establish that the chosen document describes any component in it.

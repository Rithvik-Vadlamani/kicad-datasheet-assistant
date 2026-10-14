# KiCad Datasheet Assistant

A reusable, project-scoped RAG application for researching components and reviewing how connected parts may interact. It includes a native desktop interface, a command-line interface, PDF/text ingestion, hybrid retrieval, a KiCad connection graph, cited model answers, and five explicit engineering calculations.

**This is an application around existing models, not a newly trained neural network.** Each new board gets its own documents, index and design assumptions. You reuse the software and model backend across boards.

**Start here:** the offline teaching demo runs immediately. Generated answers require a configured local Ollama model or an OpenAI API key. No model weights or credentials are bundled. The fictional demo is not a hardware design.

## Run the installed copy on this machine

```bash
cd "/path/to/kicad-datasheet-assistant"
source .venv/bin/activate
kicad-rag --project examples/demo gui
```

In the desktop app, click **Find evidence** or **Run design checks**. Use **Model settings** to enable generation. The desktop app is local Tkinter, with no web server or hosted site.

Terminal demo:

```bash
kicad-rag --project examples/demo index
kicad-rag --project examples/demo search "Can U1 drive U2? Compare VIH VIL VOH VOL."
kicad-rag --project examples/demo check
kicad-rag --project examples/demo path U1 U2
```

The demo check command returns exit code **2** because it intentionally contains design violations. The expected high-level noise margin is **−1.1 V**. The separate thermal scenario dissipates **1.8 W**, yielding a simple steady-state estimate of **220 °C**, above its fictional limit. This extrapolated estimate indicates an unacceptable thermal scenario; it does not predict actual temperature after shutdown or failure.

## Install on another computer

Python 3.11+ is required. KiCad is needed for schematic export; imported XML and datasheet-only use do not need KiCad installed.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
kicad-rag doctor
```

On Windows, activate with `.venv\Scripts\Activate.ps1`; use `python` if that is your installed Python command. If Tkinter is not included in your Python distribution, install its Tk package or use the CLI. `requirements-lock.txt` records the exact dependency versions used during delivery testing; the editable package remains installed with `pip install -e .` after installing those dependencies.

## Create a workspace for each KiCad project

Use a folder beside the schematic, for example `my-board/assistant/`. You can also keep all assistant folders in a central directory. Keep one `project.json` per physical design/revision context.

```bash
kicad-rag init /path/to/my-board/assistant --name "My sensor board"

kicad-rag --project /path/to/my-board/assistant sync-kicad /path/to/my-board/main.kicad_sch

kicad-rag --project /path/to/my-board/assistant add /path/to/regulator.pdf \
  --id regulator --parts EXACT-MPN-INCLUDING-SUFFIX --revision "Rev B"

kicad-rag --project /path/to/my-board/assistant add /path/to/sensor.pdf \
  --id sensor --parts EXACT-SENSOR-MPN --revision "Rev D"

kicad-rag --project /path/to/my-board/assistant coverage
kicad-rag --project /path/to/my-board/assistant index
kicad-rag --project /path/to/my-board/assistant gui
```

The file is copied into the assistant folder with a content-hash prefix. Original PDFs are preserved. Calling `add` with an existing document ID replaces its active registration and keeps the previous source file on disk. `remove ID` removes a document from the next searchable index while preserving its file.

Assign manufacturer part numbers to KiCad symbols using `MPN` or `Manufacturer Part Number` fields. If missing, the symbol's Value is the fallback and may be ambiguous. Fix mappings explicitly:

```bash
kicad-rag --project /path/to/my-board/assistant map-part U1 EXACT-MPN-INCLUDING-SUFFIX
kicad-rag --project /path/to/my-board/assistant index
```

Mappings match complete part strings case-insensitively. There is no fuzzy substitution from a similar part. A document can cover multiple exact parts: `--parts PART-A PART-B`. Coverage reports passives too; a generic resistor value is not an exact MPN or a verified rating.

If the schematic cannot be exported here, export KiCad XML yourself and use:

```bash
kicad-rag --project /path/to/my-board/assistant import-netlist /path/to/netlist.xml
```

An imported XML file is a snapshot. Its originating schematic cannot be monitored automatically. With `sync-kicad`, the application tracks the root and recursively referenced child schematic hashes and rejects answers after they change. **Save the schematic first**: unsaved editor changes are not on disk. Resync after library, project-variable, variant or external configuration changes too; those dependencies are not exhaustively tracked.

## Enable real semantic retrieval and generated answers

The embedding model and answer model have different jobs and are configured independently. The default `none`/`none` mode provides keyword search and deterministic checks with no inference calls.

### Fully local with Ollama

Install Ollama, start it, and download an embedding model and an instruction/chat model supported by your computer. Consult the [Ollama model library](https://ollama.com/library) for model names and hardware sizes. The following are examples, not a claim that they are optimal for every computer:

```bash
ollama pull embeddinggemma
ollama pull qwen3:8b

kicad-rag --project /path/to/my-board/assistant configure \
  --embedding-provider ollama --embedding-model embeddinggemma \
  --chat-provider ollama --chat-model qwen3:8b

kicad-rag --project /path/to/my-board/assistant index
kicad-rag --project /path/to/my-board/assistant ask \
  "Can U1 drive U2 under the stated supply conditions? Discuss logic thresholds, overvoltage and startup." \
  --refs U1 U2
```

Ollama endpoints are restricted to loopback addresses. Use locally downloaded model tags; routing through an Ollama cloud model would defeat fully local inference. The application does not automatically install Ollama or download multi-gigabyte models. Generation requests ask for a 32k context window; the installed model and available memory still determine what is practical. Reduce context size or choose a model with adequate context if needed. Small models may produce malformed JSON or weak engineering answers even when retrieval is correct.

### Optional OpenAI API

Set `OPENAI_API_KEY` in your shell or secret manager before starting the application. Do not put it in `project.json`, the source repository, or a question. Set the chat model to a model available to your API account.

```bash
kicad-rag --project /path/to/my-board/assistant configure \
  --embedding-provider openai --embedding-model text-embedding-3-small \
  --chat-provider openai --chat-model YOUR_AVAILABLE_CHAT_MODEL

kicad-rag --project /path/to/my-board/assistant index
```

You can also use local Ollama embeddings and a cloud answer model. **Cloud embeddings send all indexed chunk text to the API. Cloud generation sends the question, retrieved excerpts, relevant netlist context, assumptions and check reports.** The local SQLite database stays on your computer. Responses requests set `store=false`; that setting is not a claim about all provider retention policies. This application does not use your Codex/ChatGPT session as an API credential.

Changing the embedding model or provider requires reindexing. A model name reused for changed local weights requires `index --force`; model weight identity is not discovered automatically. Changing only the answer model does not require re-embedding.

## Useful questions

- “What are U1's recommended operating voltage and absolute maximum supply voltage? Distinguish the two.”
- “Can U1.3 drive U2.7 at minimum supply and maximum temperature? What is missing?”
- “U3 gets 12 V and supplies 200 mA at 3.3 V. What evidence is needed to estimate its temperature?”
- “Will this I²C pull-up work at the stated bus capacitance and speed?”
- “What could happen if U2 is powered while U1 is off?”
- “Does this regulator require a particular output capacitor ESR range?”
- “Which parts share this net, and which datasheets are still missing?”
- “What measurements would distinguish a power-sequencing problem from a logic threshold problem?”

Use explicit references and complete questions. Version 1 answers each question independently; it does not maintain conversational memory or automatically rewrite follow-up pronouns. The `--refs` option scopes the graph and documents even when the question does not name a component.

## Five calculation types

These are deterministic Python checks, separate from the language model. They run only when configured in `design.json` with reviewed input values and operating conditions.

| Type | Calculation | Important boundary |
|---|---|---|
| `logic` | Guaranteed high and low noise margins | Does not establish overvoltage tolerance or timing |
| `pin_voltage` | Applied voltage interval versus recommended and absolute limits | Transients and powered-off states must be represented in inputs |
| `ldo_thermal` | Linear regulator dissipation and estimated junction temperature | Requires appropriate board thermal resistance and steady-state assumptions |
| `power_budget` | Available current minus total worst-case load | Requires complete load accounting and thermal derating |
| `i2c_pullup` | Sink-current lower resistance bound and RC rise-time upper bound | A first-order open-drain model with explicit bus capacitance and resistance tolerance |

See [the design-check guide](docs/DESIGN_CHECKS.md) for fields, units, source requirements and examples. The example `design.json` is populated and runnable. You do not need to fill `design.json` to search datasheets or ask questions. You do need to fill it for reproducible numerical checks.

## Evidence and limitations

- Citation IDs must exist in the actual evidence packet. Quoted source text must appear in that source. Invalid answers are withheld. These mechanical checks do **not** prove the cited material supports the conclusion.
- Every excerpt has a source file, document ID, revision and one-based physical PDF page. The PDF viewer's printed page label may differ. Text files use logical pages separated by form-feed characters.
- PDF text and line-table extraction can lose column alignment, superscripts, formulas and footnotes. `page DOCUMENT NUMBER --render output.png` renders the source page for visual review. Graphs and schematic images are not interpreted by a vision model.
- Low-text pages are flagged; completely unextractable documents are rejected. OCR is an explicit preprocessing step, described in the guide. OCR values still need review.
- A netlist establishes connectivity, not circuit behavior. It does not contain all loads, supply tolerances, firmware states, parasitics, PCB copper geometry, thermal environment or device models.
- Neighbor expansion is one hop. Large nets limit expansion; omission warnings are exposed. Retrieval top-k and context budgets limit how much evidence any single answer sees.
- No live SPICE, ERC, PCB DRC, transient, EMI, signal-integrity or thermal-field simulation is performed. No automatic BOM procurement, web crawling, component substitution, schematic edits or circuit approval is implemented.
- Indexing is intended for one writer at a time per project. This desktop tool is not a multi-user server. SQLite searches and vector scans are appropriate for modest project corpora; very large libraries need a different storage/retrieval scale.

## Files and commands

```text
kicad-rag/
  src/kicad_rag/       Implementation: CLI, desktop UI, retrieval and checks
  examples/demo/      Fictional parts, connected netlist, reviewed teaching checks
  tests/             Unit and integration-style tests with explicit model doubles
  docs/              Detailed architecture, operating workflow and validation notes
  scripts/           Fixture generation and retrieval evaluation
  requirements-lock.txt
  Start Demo.command
```

Each project folder contains:

```text
project.json         Registered documents, part mappings and model settings
design.json          Reviewed values, conditions and requested calculations
datasheets/          Preserved source copies
netlist.xml          KiCad connection snapshot
.rag/index.sqlite3   Extraction cache, pages, keyword index and optional vectors
reports/             Timestamped answer/evidence Markdown + JSON, check results
```

`kicad-rag --help` lists all commands. Put `--project PATH` **before** the subcommand. `status` explains freshness; `coverage` shows missing mappings; `doctor` checks executable availability without inference calls. `schema project` and `schema design` emit JSON schemas for editor validation.

For the detailed explanation requested with this project, read [HOW_RAG_WORKS.md](HOW_RAG_WORKS.md). For the tested scope and remaining activation steps, read [VALIDATION.md](VALIDATION.md).

## KiCad skill integration (0.2.0)

The app now exposes typed evidence packets and reviewed parameter records, shared calculator binding, native wired schematic generation, constrained explicit PCB generation, ERC/DRC/connectivity checks, revision impact, source-page review and artifact-bound release bookkeeping. See [SKILL_INTEGRATION.md](SKILL_INTEGRATION.md) for setup, supported scope, migration and measured validation. This is a guarded engineering assistant, not automatic proof that a circuit will work.

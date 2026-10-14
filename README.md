# Datasheet assistant for circuit design

A Python application that connects circuit netlists with datasheet information. I built it to answer component questions with source-page references and check how connected parts may interact.

It combines PDF extraction, keyword/vector retrieval, and KiCad netlist tracing. Each project keeps its own documents and index, so a changed schematic or datasheet can be flagged before an old answer is reused.

## Try the offline demo

Requires Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
kicad-rag --project examples/demo index
kicad-rag --project examples/demo search "Can U1 drive U2? Compare VIH VIL VOH VOL."
kicad-rag --project examples/demo check
```

The demo uses fictional parts and intentionally failing electrical checks. The check command returning exit code 2 is expected. Search and calculations work without an API key; generated answers need your own OpenAI credentials or a configured local Ollama model.

For the desktop interface, run `kicad-rag --project examples/demo gui`. It requires Tkinter.

## What it checks

Logic margins, pin voltages, regulator thermal limits, current budgets, and I2C pull-ups. These calculations use explicit assumptions and retrieved documents. They do not replace circuit simulation or hardware measurements.

## Where to look

- [src/kicad_rag/](src/kicad_rag/): retrieval, model providers, connectivity, calculations, and interfaces.
- [examples/demo/](examples/demo/): fictional documents and a connected sample circuit.
- [tests/](tests/): retrieval, netlist, calculation, and provider tests.
- [How retrieval works](docs/HOW_RAG_WORKS.md): indexing and evidence flow.
- [Implementation guide](docs/IMPLEMENTATION.md): full workflow and limitations.
- [PCB automation skill](https://github.com/Rithvik-Vadlamani/pcb-automation-skill): the companion instructions and examples.

## Tests

```sh
python -m pytest -q
```

The repository includes source and fictional examples. It excludes personal project workspaces, API credentials, cached indexes, and model weights.

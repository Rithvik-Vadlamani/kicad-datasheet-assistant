"""Bounded ngspice batch execution for a reviewed self-contained deck."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import atomic_json, digest


def simulate(deck, output, *, models=None):
    executable = shutil.which("ngspice")
    if not executable:
        raise ValueError("ngspice is unavailable; simulation not performed")
    if output.exists():
        raise ValueError("Simulation output must be a new directory")
    before = digest(deck)
    text = deck.read_text()
    model_hashes, unused = {}, []
    if models is not None:
        from .simulation_models import flatten
        text, model_hashes, unused = flatten(deck, models)
    # The portable interface intentionally excludes control scripts and external dependencies.
    # Use a separately reviewed native simulation workflow when models need include files.
    blocked = re.compile(
        r"^\s*(?:\.(?:control|include|inc|lib|pre_osdi)|(?:shell|source|cd|wrdata|write)\b)", re.I | re.M
    )
    if blocked.search(text):
        raise ValueError(
            "Self-contained simulation interface does not allow control scripts, external includes or file commands"
        )
    if digest(deck) != before:
        raise ValueError("Simulation source changed during preparation")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".ngspice-") as temp:
        work = Path(temp)
        (work / "circuit.cir").write_text(text)
        result = subprocess.run(
            [executable, "-n", "-b", "-o", "simulation.log", "circuit.cir"],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=60,
        )
        log = (work / "simulation.log").read_text() if (work / "simulation.log").exists() else result.stderr
        if digest(deck) != before or any(digest(Path(path)) != sha for path, sha in model_hashes.items()):
            raise ValueError("Simulation deck changed during execution")
        output.mkdir()
        (output / "circuit.cir").write_text(text)
        shutil.copyfile(deck, output / "original.cir")
        (output / "simulation.log").write_text(log)
    failed = result.returncode != 0 or bool(re.search(r"(?im)^\s*(?:fatal|error)\b", log))
    report = {
        "schema_version": 1,
        "status": "fail" if failed else "executed_requires_measurement_review",
        "input_sha256": before,
        "model_hashes": model_hashes,
        "unused_declared_models": unused,
        "expanded_deck_sha256": digest(output / "circuit.cir"),
        "returncode": result.returncode,
        "tool_version": subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10
        ).stdout,
        "scope": "Simulator execution only. Review measurements, convergence, model and topology before drawing a conclusion.",
    }
    atomic_json(output / "simulation.json", report)
    return report

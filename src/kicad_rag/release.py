"""Release bookkeeping: require fresh machine reports and explicit artifact-bound inspections."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .config import StrictModel, atomic_json, digest
from .native import design_dependencies, manifest


class Inspection(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["visual", "physical"]
    actor: str = Field(min_length=1)
    timestamp: str
    verdict: Literal["pass", "fail", "unassessed"]
    input_hashes: dict[str, str] = Field(min_length=1)
    inspected: list[str] = Field(min_length=1)
    findings: list[str] = Field(min_length=1)


def release(folder, schematic, intent_path, visual_path, *, board=None, physical_path=None):
    from .config import canonical_hash
    from .native import Intent

    dependencies = design_dependencies(schematic)
    sch_report = json.loads((folder / "reports/schematic-checks.json").read_text())
    intent = Intent.model_validate_json(intent_path.read_text())
    if sch_report.get("status") != "pass" or sch_report.get("intent_hash") != canonical_hash(
        intent.model_dump()
    ):
        raise ValueError("Schematic checks fail or intent changed")
    if sch_report.get("input_hashes") != dependencies:
        raise ValueError("Schematic check dependencies changed; rerun final native checks")
    reports = [sch_report]
    inspections = [Inspection.model_validate_json(visual_path.read_text())]
    if inspections[0].kind != "visual":
        raise ValueError("Expected a visual inspection")
    if board:
        if physical_path is None:
            raise ValueError("A board release requires an actual recorded physical inspection")
        dependencies[str(board)] = digest(board)
        board_report = json.loads((folder / "reports/board-checks.json").read_text())
        if (
            board_report.get("status") != "pass"
            or board_report.get("input_hashes") != dependencies
            or board_report.get("intent_hash") != canonical_hash(intent.model_dump())
        ):
            raise ValueError("Board checks fail or inputs changed")
        if board_report.get("pad_map_review") != "checked":
            raise ValueError("Independent pad map was not checked")
        reports.append(board_report)
        inspections.append(Inspection.model_validate_json(physical_path.read_text()))
        if inspections[-1].kind != "physical":
            raise ValueError("Expected a physical inspection")
    for review in inspections:
        expected = dependencies if review.kind == "physical" else design_dependencies(schematic)
        if review.verdict != "pass" or review.input_hashes != expected:
            raise ValueError(f"{review.kind} review missing, failed or stale")
    for path, sha in dependencies.items():
        if digest(Path(path)) != sha:
            raise ValueError("A design input changed during release")
    result = {
        "schema_version": 1,
        "scope": "Native connectivity/rule checks and recorded inspections only; electrical evidence and hardware validation remain separate.",
        "files": manifest(folder),
        "tools": sorted({r["tool_version"] for r in reports}),
        "inspections": [i.model_dump() for i in inspections],
        "status": "native_checks_and_recorded_inspections_pass",
    }
    atomic_json(folder / "release-manifest.json", result)
    return result


def export_manufacturing(board, output):
    import sexpdata as sx

    from .native import child, execute

    layers_node = child(sx.loads(board.read_text()), "layers")
    copper = [str(item[1]) for item in layers_node[1:] if isinstance(item, list) and str(item[1]).endswith(".Cu")]
    if len(copper) not in (2, 4, 6, 8):
        raise ValueError("Supported export stacks have 2, 4, 6 or 8 copper layers")
    if output.exists():
        raise ValueError("Manufacturing output must be a new directory")
    checked = json.loads((board.parent / "reports/board-checks.json").read_text())
    if checked.get("status") != "pass" or checked["input_hashes"].get(str(board)) != digest(board):
        raise ValueError("Fresh passing board checks are required before export")
    for path, sha in checked["input_hashes"].items():
        if digest(Path(path)) != sha:
            raise ValueError("Design dependencies changed since board checks")
    before = digest(board)
    output.mkdir(parents=True)
    layers = copper + ["F.Mask", "B.Mask", "F.SilkS", "B.SilkS", "Edge.Cuts"]
    execute(
        ["pcb", "export", "gerbers", "--no-protel-ext", "--layers", ",".join(layers), "-o", output, board]
    )
    execute(
        [
            "pcb",
            "export",
            "drill",
            "--format",
            "excellon",
            "--excellon-units",
            "mm",
            "--excellon-separate-th",
            "--generate-map",
            "--map-format",
            "svg",
            "--generate-report",
            "--report-path",
            output / "drill-report.txt",
            "-o",
            output,
            board,
        ]
    )
    gerbers = list(output.glob("*.gbr"))
    drills = list(output.glob("*.drl"))
    if len(gerbers) != len(layers) or not drills or digest(board) != before:
        raise ValueError("Expected manufacturing outputs missing or source board changed")
    result = {
        "schema_version": 1,
        "board_sha256": before,
        "files": manifest(output),
        "layers": layers,
        "status": "exported_requires_manufacturing_review",
        "scope": "All enabled copper layers plus mask/silkscreen/outline; independently review actual fabrication files, dimensions and drill data. No order submitted.",
    }
    atomic_json(output / "export-manifest.json", result)
    return result


def export_assembly(board, output):
    """Default-variant BOM plus KiCad-native placement coordinates, with freshness and ref coverage."""
    import csv

    from .kicad import Graph
    from .native import execute

    if output.exists():
        raise ValueError("Assembly output must be a new directory")
    report = json.loads((board.parent / "reports/board-checks.json").read_text())
    if report.get("status") != "pass" or report.get("input_hashes", {}).get(str(board)) != digest(board):
        raise ValueError("Fresh passing board checks are required before assembly export")
    for path, sha in report["input_hashes"].items():
        if digest(Path(path)) != sha:
            raise ValueError("Assembly source dependencies changed")
    schematic = board.with_suffix(".kicad_sch")
    output.mkdir(parents=True)
    xml = output / "assembly-netlist.xml"
    execute(["sch", "export", "netlist", "--format", "kicadxml", "-o", xml, schematic], expected=xml)
    graph = Graph.from_xml(xml, {})
    included = {ref: c for ref, c in graph.components.items() if not c.get("dnp")}
    with (output / "bom.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Reference", "Value", "MPN", "Footprint", "Quantity"])
        for ref, c in sorted(included.items()):
            writer.writerow([ref, c["value"], c["fields"].get("mpn", ""), c["footprint"], 1])
    positions = output / "placements.csv"
    execute(["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
             "--exclude-dnp", "-o", positions, board], expected=positions)
    with positions.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    references = [row.get("Ref") for row in rows]
    if len(references) != len(set(references)) or set(references) != set(included):
        raise ValueError("Placement/BOM reference coverage differs; inspect footprint exclusion and variant settings")
    for path, sha in report["input_hashes"].items():
        if digest(Path(path)) != sha:
            raise ValueError("Assembly source changed during export")
    result = {"schema_version": 1, "board_sha256": digest(board), "references": sorted(included),
              "missing_mpn": sorted(ref for ref, c in included.items() if not c["fields"].get("mpn")),
              "files": manifest(output), "status": "exported_requires_assembly_review",
              "scope": "Default assembly variant, DNP excluded. KiCad native mm coordinates and angles; verify assembler origin, rotation conventions, parts and polarity before use."}
    atomic_json(output / "assembly-manifest.json", result)
    return result

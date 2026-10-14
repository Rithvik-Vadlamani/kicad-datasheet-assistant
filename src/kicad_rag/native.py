"""Small native KiCad authoring backend and independent checks; no shell execution."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

import sexpdata as sx
from pydantic import Field, model_validator

from .config import StrictModel, atomic_json, canonical_hash, digest
from .kicad import Graph, kicad_cli, schematic_hashes


def node(tag, *values):
    return [sx.Symbol(tag), *values]


def isnode(value, tag):
    return isinstance(value, list) and bool(value) and str(value[0]) == tag


def children(value, tag):
    return [v for v in value if isnode(v, tag)]


def child(value, tag):
    return next((v for v in value if isnode(v, tag)), None)


def dump(path, value):
    path.write_text(sx.dumps(value) + "\n", encoding="utf-8")


def library_root(kind):
    candidates = [
        os.getenv("KICAD_SYMBOL_DIR" if kind == "symbols" else "KICAD_FOOTPRINT_DIR"),
        f"/Applications/KiCad/KiCad.app/Contents/SharedSupport/{kind}",
        f"/usr/share/kicad/{kind}",
        f"/usr/local/share/kicad/{kind}",
    ]
    for value in candidates:
        if value and Path(value).is_dir():
            return Path(value)
    raise ValueError(f"Cannot find KiCad {kind}; configure the corresponding KICAD_*_DIR")


def native_symbol(identifier):
    lib, name = identifier.split(":", 1)
    if any(x in lib for x in ("/", "\\", "..")):
        raise ValueError("Invalid library name")
    root = sx.loads((library_root("symbols") / f"{lib}.kicad_sym").read_text())
    symbols = {s[1]: s for s in children(root, "symbol")}

    def expand(key, seen):
        if key in seen or key not in symbols:
            raise ValueError(f"Missing or cyclic symbol inheritance: {identifier}")
        item = copy.deepcopy(symbols[key])
        parent = child(item, "extends")
        if not parent:
            return item
        result = expand(parent[1], seen | {key})
        old = result[1]
        result[1] = key
        for sub in children(result, "symbol"):
            sub[1] = key + sub[1][len(old) :]
        for prop in children(item, "property"):
            previous = next((p for p in children(result, "property") if p[1] == prop[1]), None)
            if previous:
                result.remove(previous)
            result.append(prop)
        return result

    return expand(name, set())


class Component(StrictModel):
    ref: str = Field(pattern=r"^#?[A-Za-z][A-Za-z0-9_]*[0-9]+$")
    symbol: str = Field(pattern=r"^[^:]+:[^:]+$")
    value: str
    at: tuple[float, float]
    angle: Literal[0, 90, 180, 270] = 0
    mirror: Literal["x", "y"] | None = None
    unit: int = Field(default=1, ge=1)
    footprint: str = ""
    part: str = ""
    dnp: bool = False
    field_positions: dict[str, tuple[float, float]] = Field(default_factory=dict)


class Wire(StrictModel):
    start: str | tuple[float, float]
    end: str | tuple[float, float]


class Label(StrictModel):
    name: str
    at: str | tuple[float, float]


class SchematicSpec(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str
    components: list[Component] = Field(default_factory=list)
    wires: list[Wire] = Field(default_factory=list)
    labels: list[Label] = Field(default_factory=list)
    junctions: list[tuple[float, float]] = Field(default_factory=list)
    no_connect: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        instances = [(c.ref, c.unit) for c in self.components]
        if len(instances) != len(set(instances)):
            raise ValueError("Duplicate reference/unit instance")
        metadata = {}
        for c in self.components:
            shared = (c.symbol, c.value, c.footprint, c.part, c.dnp)
            if c.ref in metadata and metadata[c.ref] != shared:
                raise ValueError("Units of one physical component must share symbol/value/footprint/MPN/DNP")
            metadata[c.ref] = shared
        if not self.wires:
            raise ValueError("A circuit must contain visible wires")
        return self


class Intent(StrictModel):
    """Authored independently, before wire geometry; contains ALL physical symbol pins."""

    schema_version: Literal[1] = 1
    nets: dict[str, list[str]] = Field(min_length=1)
    no_connect: list[str] = Field(default_factory=list)
    # Exact numeric pin to footprint pad map, independently checked against manufacturer data.
    pad_map: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique(self):
        endpoints = [p for pins in self.nets.values() for p in pins] + self.no_connect
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("An endpoint occurs in multiple intended nets/no-connects")
        if any(len(pins) < 1 for pins in self.nets.values()):
            raise ValueError("Empty intended net")
        return self


def pin_locations(component, symbol):
    result = {}
    for sub in children(symbol, "symbol"):
        _, unit, style = sub[1].rsplit("_", 2)
        if int(unit) not in (0, component.unit) or int(style) not in (0, 1):
            continue
        for pin in children(sub, "pin"):
            n = str(child(pin, "number")[1])
            x, y = map(float, child(pin, "at")[1:3])
            if component.mirror == "y":
                x = -x
            if component.mirror == "x":
                y = -y
            a = math.radians(component.angle)
            point = (
                round(component.at[0] + x * math.cos(a) - y * math.sin(a), 6),
                round(component.at[1] - x * math.sin(a) - y * math.cos(a), 6),
            )
            if n in result and result[n] != point:
                raise ValueError(f"Stacked/duplicate pin {component.ref}.{n} needs native review")
            result[n] = point
    if not result:
        raise ValueError(f"No pins found for {component.ref} unit {component.unit}")
    return result


def between(point, start, end):
    return (
        start[0] == end[0] == point[0]
        and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])
        or start[1] == end[1] == point[1]
        and min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
    )


def build_schematic(spec, destination):
    """Destination must be a new staging folder; callers verify it before promotion."""
    destination.mkdir(parents=True, exist_ok=False)
    root_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kicad-design:{spec.name}"))
    serial = 0

    def uid(label):
        nonlocal serial
        serial += 1
        return str(uuid.uuid5(uuid.UUID(root_id), f"{label}:{serial}"))

    symbols = {c.symbol: native_symbol(c.symbol) for c in spec.components}
    for c in spec.components:
        if c.ref.startswith("#") != bool(child(symbols[c.symbol], "power")):
            raise ValueError(
                "Only native power symbols use # references; physical components need ordinary references"
            )
    pins = {}
    for c in spec.components:
        for n, p in pin_locations(c, symbols[c.symbol]).items():
            key = f"{c.ref}.{n}"
            if key in pins and pins[key] != p:
                raise ValueError("A shared physical pin has multiple drawn locations; use native review")
            pins[key] = p

    def point(endpoint):
        if isinstance(endpoint, str):
            if endpoint not in pins:
                raise ValueError(f"Unknown symbol pin {endpoint}")
            return pins[endpoint]
        return tuple(endpoint)

    embedded = []
    for identifier, symbol in symbols.items():
        s = copy.deepcopy(symbol)
        s[1] = identifier
        embedded.append(s)
    items = []
    for c in spec.components:
        sym = symbols[c.symbol]
        props = []
        for key, value, x, y, hide in [
            ("Reference", c.ref, c.at[0], c.at[1] - 5.08, False),
            ("Value", c.value, c.at[0], c.at[1] + 5.08, False),
            ("Footprint", c.footprint, *c.at, True),
            ("MPN", c.part, *c.at, True),
        ]:
            if c.ref.startswith("#") and key == "Reference":
                hide = True
            if c.symbol in ("Device:C", "Device:R", "Device:L") and c.angle in (0, 180) and not hide:
                x, y = c.at[0] + 6.35, c.at[1] + (-1.27 if key == "Reference" else 1.27)
            if key in c.field_positions:
                x, y = c.field_positions[key]
            effects = node("effects", node("font", node("size", 1.27, 1.27)))
            if hide:
                effects.append(node("hide", sx.Symbol("yes")))
            props.append(node("property", key, value, node("at", x, y, c.angle), effects))
        item = node(
            "symbol",
            node("lib_id", c.symbol),
            node("at", *c.at, c.angle),
            node("unit", c.unit),
            node("in_bom", sx.Symbol("no" if c.ref.startswith("#") else "yes")),
            node("on_board", sx.Symbol("no" if c.ref.startswith("#") else "yes")),
            node("dnp", sx.Symbol("yes" if c.dnp else "no")),
            node("uuid", uid(c.ref)),
            *props,
            *[node("pin", n, node("uuid", uid(c.ref + n))) for n in pin_locations(c, sym)],
            node(
                "instances",
                node(
                    "project",
                    spec.name,
                    node("path", "/" + root_id, node("reference", c.ref), node("unit", c.unit)),
                ),
            ),
        )
        if c.mirror:
            item.append(node("mirror", sx.Symbol(c.mirror)))
        items.append(item)
    segments = [(point(w.start), point(w.end)) for w in spec.wires]
    for a, b in segments:
        if a == b or (a[0] != b[0] and a[1] != b[1]):
            raise ValueError(f"Use nonzero orthogonal wire segments: {a} -> {b}")
    points = (
        set(pins.values())
        | {p for s in segments for p in s}
        | {point(label.at) for label in spec.labels}
        | set(spec.junctions)
    )
    edges = set()
    for a, b in segments:
        cuts = sorted(p for p in points if between(p, a, b))
        for x, y in zip(cuts, cuts[1:], strict=False):
            edges.add((x, y))
    for a, b in sorted(edges):
        items.append(
            node(
                "wire",
                node("pts", node("xy", *a), node("xy", *b)),
                node("stroke", node("width", 0), node("type", sx.Symbol("default"))),
                node("uuid", uid("wire")),
            )
        )
    for p in sorted(points):
        if sum(p in edge for edge in edges) >= 3:
            items.append(
                node(
                    "junction",
                    node("at", *p),
                    node("diameter", 0),
                    node("color", 0, 0, 0, 0),
                    node("uuid", uid("junction")),
                )
            )
    for label in spec.labels:
        items.append(
            node(
                "label",
                label.name,
                node("at", *point(label.at), 0),
                node(
                    "effects",
                    node("font", node("size", 1.27, 1.27)),
                    node("justify", sx.Symbol("left"), sx.Symbol("bottom")),
                ),
                node("uuid", uid("label")),
            )
        )
    for endpoint in spec.no_connect:
        p = point(endpoint)
        if any(between(p, *edge) for edge in edges):
            raise ValueError(f"No-connect pin is wired: {endpoint}")
        items.append(node("no_connect", node("at", *p), node("uuid", uid(endpoint))))
    for i, note in enumerate(spec.notes):
        items.append(
            node(
                "text",
                note,
                node("at", 30, 25 + 4 * i, 0),
                node("effects", node("font", node("size", 1.27, 1.27)), node("justify", sx.Symbol("left"))),
                node("uuid", uid("note")),
            )
        )
    document = node(
        "kicad_sch",
        node("version", 20250114),
        node("generator", "kicad_design"),
        node("uuid", root_id),
        node("paper", "A4"),
        node("title_block", node("title", spec.title)),
        node("lib_symbols", *embedded),
        *items,
        node("sheet_instances", node("path", "/", node("page", "1"))),
        node("embedded_fonts", sx.Symbol("no")),
    )
    path = destination / f"{spec.name}.kicad_sch"
    dump(path, document)
    atomic_json(destination / f"{spec.name}.kicad_pro", {})
    # Pin the actual libraries used, so this project remains editable on other machines.
    libs = {}
    for identifier, symbol in symbols.items():
        libs.setdefault(identifier.split(":")[0], []).append(symbol)
    for lib, entries in libs.items():
        dump(
            destination / f"{lib}.kicad_sym",
            node("kicad_symbol_lib", node("version", 20250114), node("generator", "kicad_design"), *entries),
        )
    dump(
        destination / "sym-lib-table",
        node(
            "sym_lib_table",
            node("version", 7),
            *[
                node(
                    "lib",
                    node("name", lib),
                    node("type", "KiCad"),
                    node("uri", "${KIPRJMOD}/" + lib + ".kicad_sym"),
                    node("options", ""),
                    node("descr", "Project snapshot"),
                )
                for lib in libs
            ],
        ),
    )
    return path, pins


def execute(arguments, *, expected=None):
    result = subprocess.run([kicad_cli(), *map(str, arguments)], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ValueError(f"KiCad command failed ({result.returncode}): {result.stderr or result.stdout}")
    if expected and not expected.exists():
        raise ValueError(f"KiCad returned success without producing {expected}")
    return result.stdout


def compare_intent(graph, intent):
    actual = {frozenset(f"{p['ref']}.{p['pin']}" for p in endpoints) for endpoints in graph.nets.values()}
    expected = {frozenset(pins) for pins in intent.nets.values()}
    # KiCad may export a marked unused pin as a singleton unconnected net.
    actual -= {frozenset([p]) for p in intent.no_connect}
    all_pins = {f"{ref}.{pin}" for ref, comp in graph.components.items() for pin in comp["pins"]}
    declared = {p for group in intent.nets.values() for p in group} | set(intent.no_connect)
    return {
        "status": "pass" if actual == expected and all_pins == declared else "fail",
        "undeclared_pins": sorted(all_pins - declared),
        "unknown_pins": sorted(declared - all_pins),
        "missing_groups": [sorted(p) for p in sorted(expected - actual, key=lambda s: sorted(s))],
        "unexpected_groups": [sorted(p) for p in sorted(actual - expected, key=lambda s: sorted(s))],
    }


def verify_schematic(schematic, intent, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    before = design_dependencies(schematic)
    xml = report_dir / "netlist.xml"
    execute(["sch", "export", "netlist", "--format", "kicadxml", "-o", xml, schematic], expected=xml)
    graph = Graph.from_xml(xml, {})
    connectivity = compare_intent(graph, intent)
    erc = report_dir / "erc.json"
    execute(["sch", "erc", "--format", "json", "--severity-all", "-o", erc, schematic], expected=erc)
    erc_data = json.loads(erc.read_text())
    violations = [v for sheet in erc_data.get("sheets", []) for v in sheet.get("violations", [])]
    if "sheets" not in erc_data:
        raise ValueError("Unexpected ERC report format; cannot assess")
    svg = report_dir / "schematic-svg"
    svg.mkdir(exist_ok=True)
    execute(["sch", "export", "svg", "-o", svg, schematic])
    renders = sorted(svg.glob("*.svg"))
    if not renders:
        raise ValueError("No rendered schematic pages")
    if before != design_dependencies(schematic):
        raise ValueError("Schematic changed during verification")
    result = {
        "schema_version": 1,
        "tool_version": execute(["version"]).strip(),
        "input_hashes": before,
        "intent_hash": canonical_hash(intent.model_dump()),
        "connectivity": connectivity,
        "erc": {"status": "pass" if not violations else "fail", "violations": violations},
        "rendered_pages": [str(p) for p in renders],
        "visual_review": "not_performed",
        "status": "pass" if connectivity["status"] == "pass" and not violations else "fail",
    }
    atomic_json(report_dir / "schematic-checks.json", result)
    return result


def generate_checked(spec, intent, output):
    if output.exists():
        raise ValueError("Output already exists; use a new directory to preserve existing designs")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".kicad-stage-", dir=output.parent) as temp:
        stage = Path(temp) / "project"
        schematic, pins = build_schematic(spec, stage)
        declared = {p for group in intent.nets.values() for p in group} | set(intent.no_connect)
        physical_pins = {p for p in pins if not p.startswith("#")}
        if physical_pins != declared or set(spec.no_connect) != set(intent.no_connect):
            raise ValueError(
                f"Intent must account for every physical pin and explicit no-connect: missing={sorted(physical_pins - declared)}, unknown={sorted(declared - physical_pins)}"
            )
        atomic_json(stage / "intent.json", intent.model_dump())
        atomic_json(stage / "schematic-spec.json", spec.model_dump())
        result = verify_schematic(schematic, intent, stage / "reports")
        # Preserve a failed candidate for inspection, but never label it verified.
        shutil.move(str(stage), output)
    # Regenerate reports after promotion: paths and hashes refer to delivered artifacts.
    result = verify_schematic(output / schematic.name, intent, output / "reports")
    return {**result, "project": str(output)}


def verify_board(board, intent, schematic, report_dir, *, refill=False):
    report_dir.mkdir(parents=True, exist_ok=True)
    before = design_dependencies(schematic)
    if board.stem != schematic.stem or board.parent != schematic.parent:
        raise ValueError(
            "DRC parity requires board and schematic with the same stem in the same project directory"
        )
    args = ["pcb", "drc", "--format", "json", "--all-track-errors", "--schematic-parity", "--severity-all"]
    if refill:
        args.extend(["--refill-zones", "--save-board"])
    target = report_dir / "drc.json"
    execute([*args, "-o", target, board], expected=target)
    data = json.loads(target.read_text())
    required = ("violations", "unconnected_items", "schematic_parity")
    if not all(k in data for k in required):
        raise ValueError("Unexpected DRC report schema; cannot assess")
    design = sx.loads(board.read_text())
    pad_issues = []
    xml = report_dir / "board-netlist.xml"
    execute(["sch", "export", "netlist", "--format", "kicadxml", "-o", xml, schematic], expected=xml)
    graph = Graph.from_xml(xml, {})
    independent_connectivity = compare_intent(graph, intent)
    if independent_connectivity["status"] != "pass":
        pad_issues.append("Actual schematic connectivity differs from independent physical intent")
    native_groups = {frozenset(f"{p['ref']}.{p['pin']}" for p in endpoints): name
                     for name, endpoints in graph.nets.items()}
    aliases = {name: native_groups.get(frozenset(endpoints)) for name, endpoints in intent.nets.items()}
    for ref, comp in graph.components.items():
        if comp["footprint"] and (ref not in intent.pad_map or set(intent.pad_map[ref]) != set(comp["pins"])):
            pad_issues.append(f"{ref}: independent map must account for every symbol pin")
    footprints = children(design, "footprint")
    for ref, mapping in intent.pad_map.items():
        found = [f for f in footprints if any(p[1:3] == ["Reference", ref] for p in children(f, "property"))]
        if len(found) != 1:
            pad_issues.append(f"{ref}: missing/duplicate footprint")
            continue
        # Map logical intent names to exact native hierarchy names by complete endpoint groups.
        # Never strip a hierarchy prefix or accept a changed schematic merely because PCB parity passes.
        for pin, pad in mapping.items():
            logical = next((n for n, endpoints in intent.nets.items() if f"{ref}.{pin}" in endpoints), None)
            expected = aliases.get(logical) if logical else graph.components.get(ref, {}).get("pins", {}).get(pin, {}).get("net")
            matches = [p for p in children(found[0], "pad") if str(p[1]) == pad]
            if not matches or any((str(child(p, "net")[-1]) if child(p, "net") else None) != expected for p in matches):
                pad_issues.append(f"{ref}.{pin}: expected pad {pad} on {expected or logical or 'unconnected'}")
    if before != design_dependencies(schematic):
        raise ValueError("Schematic or project dependencies changed during board verification")
    result = {
        "schema_version": 1,
        "tool_version": execute(["version"]).strip(),
        "intent_hash": canonical_hash(intent.model_dump()),
        "input_hashes": {str(board): digest(board), **design_dependencies(schematic)},
        "drc": {k: data[k] for k in required},
        "independent_connectivity": independent_connectivity,
        "pad_map_issues": pad_issues,
        "pad_map_review": "checked" if intent.pad_map else "not_configured",
        "physical_review": "not_performed",
        "status": "pass" if not any(data[k] for k in required) and not pad_issues else "fail",
    }
    atomic_json(report_dir / "board-checks.json", result)
    return result


def manifest(folder):
    return {
        str(p.relative_to(folder)): digest(p)
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != "release-manifest.json" and not p.name.endswith((".lck", ".zip"))
    }


def design_dependencies(schematic):
    result = schematic_hashes(schematic)
    for p in schematic.parent.rglob("*"):
        if p.is_file() and (
            p.suffix in (".kicad_sym", ".kicad_mod")
            or p in (schematic.with_suffix(".kicad_pro"), schematic.with_suffix(".kicad_dru"))
            or p.name in ("sym-lib-table", "fp-lib-table")
        ):
            result[str(p.resolve())] = digest(p)
    return result

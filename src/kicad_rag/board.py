"""Explicit native board authoring. KiCad performs footprint transforms; routing is supplied."""

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

import sexpdata as sx
from pydantic import Field, model_validator

from .config import StrictModel
from .kicad import Graph
from .native import child, children, dump, execute, library_root, node


class Placement(StrictModel):
    ref: str
    at: tuple[float, float]
    angle: float = Field(default=0, ge=-360, le=360)
    side: Literal["top", "bottom"] = "top"


class Route(StrictModel):
    net: str
    points: list[str | tuple[float, float]] = Field(min_length=2)
    layer: str = "F.Cu"
    width: float = Field(gt=0)


class Zone(StrictModel):
    net: str
    layer: str
    points: list[tuple[float, float]] = Field(min_length=3)
    clearance: float = Field(gt=0)
    thermal_gap: float = Field(gt=0)
    thermal_width: float = Field(gt=0)


class Via(StrictModel):
    net: str
    at: tuple[float, float]
    diameter: float = Field(gt=0)
    drill: float = Field(gt=0)

    @model_validator(mode="after")
    def annulus(self):
        if self.diameter <= self.drill:
            raise ValueError("Via diameter must exceed its drill")
        return self


class BoardSpec(StrictModel):
    schema_version: Literal[1] = 1
    outline: list[tuple[float, float]] = Field(min_length=3)
    placements: list[Placement] = Field(min_length=1)
    routes: list[Route] = Field(min_length=1)
    zones: list[Zone] = Field(default_factory=list)
    thickness: float = Field(default=1.6, gt=0)
    copper_layers: Literal[2, 4, 6, 8] = 2
    vias: list[Via] = Field(default_factory=list)

    @model_validator(mode="after")
    def layer_scope(self):
        allowed = {"F.Cu", "B.Cu"} | {f"In{i}.Cu" for i in range(1, self.copper_layers - 1)}
        if any(item.layer not in allowed for item in [*self.routes, *self.zones]):
            raise ValueError("Route/zone refers to a copper layer outside the declared stack")
        return self


def transform_footprints(spec, graph):
    candidates = [os.environ.get("KICAD_PYTHON"),
                  "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9",
                  shutil.which("python3")]
    interpreter = next((p for p in candidates if p and Path(p).is_file()), None)
    if not interpreter:
        raise ValueError("Configure KICAD_PYTHON with KiCad 10 pcbnew bindings")
    entries = []
    for placement in spec.placements:
        identifier = graph.components[placement.ref]["footprint"]
        if ":" not in identifier:
            raise ValueError(f"{placement.ref} has no qualified footprint")
        lib, name = identifier.split(":", 1)
        if any(c in lib + name for c in ("/", "\\")) or ".." in lib + name:
            raise ValueError("Invalid footprint identifier")
        entries.append({**placement.model_dump(), "library": str(library_root("footprints") / (lib + ".pretty")), "name": name})
    with tempfile.TemporaryDirectory(prefix="kicad-placements-") as temp:
        output = Path(temp) / "placements.kicad_pcb"
        run = subprocess.run([interpreter, str(Path(__file__).with_name("native_pcb_worker.py"))],
                             input=json.dumps({"footprints": entries, "copper_layers": spec.copper_layers, "output": str(output)}),
                             capture_output=True, text=True, timeout=90)
        if run.returncode or not output.exists():
            raise ValueError("Native footprint transform failed: " + run.stderr[-3000:])
        result = json.loads(run.stdout)
        board = sx.loads(output.read_text())
    footprints = {next(p[2] for p in children(f, "property") if p[1] == "Reference"): f for f in children(board, "footprint")}
    return footprints, result["positions"], child(board, "layers")


def build_board(schematic: Path, spec: BoardSpec, intent):
    if execute(["version"]).strip().split(".")[0] != "10":
        raise ValueError("Board writer currently targets KiCad 10. Use native KiCad for other versions.")
    path = schematic.with_suffix(".kicad_pcb")
    if path.exists():
        raise ValueError("Board already exists; native editing must preserve existing placement and routing")
    if (schematic.parent / "fp-lib-table").exists():
        raise ValueError(
            "Existing project footprint table must be preserved; use native board authoring for this project"
        )
    xml = schematic.parent / "reports" / "board-source-netlist.xml"
    xml.parent.mkdir(exist_ok=True)
    execute(["sch", "export", "netlist", "--format", "kicadxml", "-o", xml, schematic], expected=xml)
    graph = Graph.from_xml(xml, {})
    if {p.ref for p in spec.placements} != set(graph.components) or len(spec.placements) != len(
        graph.components
    ):
        raise ValueError("Placement must account for every component exactly once")
    sch = sx.loads(schematic.read_text())
    root_uuid = child(sch, "uuid")[1]
    board = node(
        "kicad_pcb",
        node("version", 20260206),
        node("generator", "kicad_design"),
        node("general", node("thickness", spec.thickness)),
        node("paper", "A4"),
        node(
            "layers",
            *[
                [n, name, sx.Symbol(kind)]
                for n, name, kind in [
                    (0, "F.Cu", "signal"),
                    (2, "B.Cu", "signal"),
                    (13, "F.Paste", "user"),
                    (15, "B.Paste", "user"),
                    (5, "F.SilkS", "user"),
                    (7, "B.SilkS", "user"),
                    (1, "F.Mask", "user"),
                    (3, "B.Mask", "user"),
                    (25, "Edge.Cuts", "user"),
                    (31, "F.CrtYd", "user"),
                    (29, "B.CrtYd", "user"),
                    (35, "F.Fab", "user"),
                    (33, "B.Fab", "user"),
                ]
            ],
        ),
        node("setup", node("pad_to_mask_clearance", 0)),
    )
    transformed, native_positions, native_layers = transform_footprints(spec, graph)
    board.remove(child(board, "layers"))
    board.append(native_layers)
    pad_positions, pad_nets, libraries = {}, {}, set()
    for placement in spec.placements:
        ref = placement.ref
        component = graph.components[ref]
        identifier = component["footprint"]
        if ":" not in identifier:
            raise ValueError(f"{ref} has no qualified footprint")
        lib, name = identifier.split(":", 1)
        if any(c in lib + name for c in ("/", "\\")) or ".." in lib + name:
            raise ValueError("Invalid footprint identifier")
        original = library_root("footprints") / (lib + ".pretty") / (name + ".kicad_mod")
        footprint = transformed[ref]
        footprint[1] = identifier
        for tag in ("version", "generator", "generator_version", "uuid", "path", "sheetname", "sheetfile"):
            for item in children(footprint, tag):
                footprint.remove(item)
        footprint.extend(
            [
                node("uuid", str(uuid.uuid4())),
                node("path", f"/{root_uuid}{component['sheet_uuid_path']}{component['symbol_uuids'][0]}"),
                node("sheetname", component["fields"].get("sheetname", "")),
                node("sheetfile", component["fields"].get("sheetfile", schematic.name)),
            ]
        )
        for prop in children(footprint, "property"):
            if prop[1] in ("Reference", "Value"):
                prop[2] = ref if prop[1] == "Reference" else component["value"]
        footprint.append(
            node(
                "property",
                "MPN",
                component["fields"].get("mpn", ""),
                node("at", 0, 0, 0),
                node("layer", "F.Fab"),
                node("hide", sx.Symbol("yes")),
                node("effects", node("font", node("size", 1, 1), node("thickness", 0.15))),
            )
        )
        if component.get("dnp"):
            attr = child(footprint, "attr")
            if attr is None:
                footprint.append(node("attr", sx.Symbol("dnp")))
            else:
                attr.append(sx.Symbol("dnp"))
        mapping = intent.pad_map.get(ref)
        if mapping is None or set(mapping) != set(component["pins"]):
            raise ValueError(f"{ref} needs an independently reviewed pin-to-pad mapping for every pin")
        for pin, pad in mapping.items():
            found = [p for p in children(footprint, "pad") if str(p[1]) == pad]
            if not found:
                raise ValueError(f"{ref}.{pin}: footprint pad {pad} missing")
            net = component["pins"][pin]["net"]
            for item in found:
                if net:
                    item.append(node("net", net))
            if len(found) != 1:
                raise ValueError("Routing endpoints with multiple physical pads require native board editing")
            key = f"{ref}.{pin}"
            pad_positions[key] = tuple(native_positions[ref][pad][0])
            pad_nets[key] = net
        board.append(footprint)
        local = schematic.parent / (lib + ".pretty")
        local.mkdir(exist_ok=True)
        target = local / original.name
        if target.exists() and target.read_bytes() != original.read_bytes():
            raise ValueError(
                f"Existing project footprint differs: {target}; preserve and review it in native KiCad"
            )
        target.write_bytes(original.read_bytes())
        libraries.add(lib)

    def resolve_net(name):
        if name in intent.nets:
            expected = set(intent.nets[name])
            matches = [net for net, endpoints in graph.nets.items()
                       if {f"{p['ref']}.{p['pin']}" for p in endpoints} == expected]
            if len(matches) != 1:
                raise ValueError(f"Logical routed net {name} does not match one complete native endpoint group")
            if name in graph.nets and name != matches[0]:
                raise ValueError(f"Ambiguous native and logical net name {name}")
            return matches[0]
        if name in graph.nets:
            return name
        if "/" + name in graph.nets:
            return "/" + name
        raise ValueError(f"Unknown routed net {name}")

    for via in spec.vias:
        board.append(node("via", node("at", *via.at), node("size", via.diameter),
                          node("drill", via.drill), node("layers", "F.Cu", "B.Cu"),
                          node("net", resolve_net(via.net)), node("uuid", str(uuid.uuid4()))))
    for route in spec.routes:
        net = resolve_net(route.net)
        points = []
        for point in route.points:
            if isinstance(point, str):
                if point not in pad_positions or pad_nets[point] != net:
                    raise ValueError(f"Route endpoint {point} is not on net {net}")
                points.append(pad_positions[point])
            else:
                points.append(point)
        for start, end in zip(points, points[1:], strict=False):
            if start == end:
                raise ValueError("Zero-length track")
            board.append(
                node(
                    "segment",
                    node("start", *start),
                    node("end", *end),
                    node("width", route.width),
                    node("layer", route.layer),
                    node("net", net),
                    node("uuid", str(uuid.uuid4())),
                )
            )
    for start, end in zip(spec.outline, spec.outline[1:] + spec.outline[:1], strict=True):
        board.append(
            node(
                "gr_line",
                node("start", *start),
                node("end", *end),
                node("stroke", node("width", 0.05), node("type", sx.Symbol("default"))),
                node("layer", "Edge.Cuts"),
                node("uuid", str(uuid.uuid4())),
            )
        )
    for z in spec.zones:
        board.append(
            node(
                "zone",
                node("net", resolve_net(z.net)),
                node("layer", z.layer),
                node("uuid", str(uuid.uuid4())),
                node("hatch", sx.Symbol("edge"), 0.5),
                node("connect_pads", node("clearance", z.clearance)),
                node("min_thickness", 0.25),
                node("filled_areas_thickness", sx.Symbol("no")),
                node(
                    "fill",
                    sx.Symbol("yes"),
                    node("thermal_gap", z.thermal_gap),
                    node("thermal_bridge_width", z.thermal_width),
                ),
                node("polygon", node("pts", *[node("xy", *p) for p in z.points])),
            )
        )
    dump(
        schematic.parent / "fp-lib-table",
        node(
            "fp_lib_table",
            node("version", 7),
            *[
                node(
                    "lib",
                    node("name", lib),
                    node("type", "KiCad"),
                    node("uri", "${KIPRJMOD}/" + lib + ".pretty"),
                    node("options", ""),
                    node("descr", "Project snapshot"),
                )
                for lib in sorted(libraries)
            ],
        ),
    )
    dump(path, board)
    return path

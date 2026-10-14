"""Hierarchical new-project authoring with real sheet pins and independent flat physical intent."""

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Literal

import sexpdata as sx
from pydantic import Field, model_validator

from .config import StrictModel, atomic_json
from .native import SchematicSpec, build_schematic, child, children, dump, node, verify_schematic


class Port(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_+-]*$")
    at: str | tuple[float, float]
    direction: Literal["input", "output", "bidirectional", "tri_state", "passive"] = "passive"


class SheetDesign(StrictModel):
    schematic: SchematicSpec
    ports: list[Port] = Field(default_factory=list)
    sheets: list["SheetInstance"] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        for names in ([p.name for p in self.ports], [s.name for s in self.sheets]):
            if len(names) != len(set(names)):
                raise ValueError("Duplicate port or sheet name")
        return self


class SheetInstance(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    at: tuple[float, float]
    size: tuple[float, float] = (40.64, 30.48)
    design: SheetDesign

    @model_validator(mode="after")
    def dimensions(self):
        if min(self.size) <= 0:
            raise ValueError("Sheet dimensions must be positive")
        if len(self.design.ports) * 5.08 + 5.08 > self.size[1]:
            raise ValueError("Sheet height must fit visible pins on the 50 mil grid")
        return self


class HierarchySpec(StrictModel):
    schema_version: Literal[1] = 1
    root: SheetDesign

    @model_validator(mode="after")
    def tree_identity(self):
        names, instances, physical_metadata = set(), set(), {}

        def visit(design, depth):
            if depth > 8:
                raise ValueError("Hierarchy exceeds eight nested sheets")
            name = design.schematic.name
            if name in names:
                raise ValueError("Every generated sheet file needs a unique name")
            names.add(name)
            for comp in design.schematic.components:
                shared = (comp.symbol, comp.value, comp.footprint, comp.part, comp.dnp)
                if comp.ref in physical_metadata and physical_metadata[comp.ref] != shared:
                    raise ValueError("Physical component units disagree across sheets")
                physical_metadata[comp.ref] = shared
                key = (comp.ref, comp.unit)
                if key in instances:
                    raise ValueError("Reference/unit reused across sheet instances")
                instances.add(key)
            for sheet in design.sheets:
                if sheet.name in {c.ref for c in design.schematic.components}:
                    raise ValueError("A sheet endpoint name conflicts with a physical component reference")
                visit(sheet.design, depth + 1)

        visit(self.root, 0)
        if self.root.ports:
            raise ValueError("The root sheet has no parent ports")
        return self


def build_hierarchy(spec, destination):
    root_name = spec.root.schematic.name
    root_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kicad-design:{root_name}"))
    all_pins, all_nc = {}, set()
    page_count = 0

    def create(design, folder, instance_path):
        nonlocal page_count
        page_count += 1
        page_number = page_count
        geometry = design.schematic.model_copy(deep=True)
        sheet_items, external = [], {}
        for sheet in design.sheets:
            sid = str(uuid.uuid5(uuid.UUID(root_uuid), instance_path + "/" + sheet.name))
            port_nodes = []
            for i, port in enumerate(sheet.design.ports):
                right = port.direction == "output"
                at = (
                    round(sheet.at[0] + (sheet.size[0] if right else 0), 6),
                    round(sheet.at[1] + (i + 1) * 5.08, 6),
                )
                external[f"{sheet.name}.{port.name}"] = at
                port_nodes.append(
                    node(
                        "pin",
                        port.name,
                        sx.Symbol(port.direction),
                        node("at", *at, 0 if right else 180),
                        node("effects", node("font", node("size", 1.27, 1.27)), node("justify", sx.Symbol("right" if right else "left"))),
                        node("uuid", str(uuid.uuid5(uuid.UUID(sid), port.name))),
                    )
                )
            sheet_items.append((sheet, sid, port_nodes))
        for wire in geometry.wires:
            if isinstance(wire.start, str) and wire.start in external:
                wire.start = external[wire.start]
            if isinstance(wire.end, str) and wire.end in external:
                wire.end = external[wire.end]
        for label in geometry.labels:
            if isinstance(label.at, str) and label.at in external:
                label.at = external[label.at]
        path, pins = build_schematic(geometry, folder)
        for key, point in pins.items():
            if key in all_pins:
                raise ValueError("Shared physical pin drawn on multiple sheets requires native editing")
            all_pins[key] = point
        all_nc.update(geometry.no_connect)
        document = sx.loads(path.read_text())
        # Every unit is associated with its actual root/sheet instance path.
        for symbol in children(document, "symbol"):
            projects = child(symbol, "instances")
            project = child(projects, "project")
            project[1] = root_name
            child(project, "path")[1] = instance_path
        for port in design.ports:
            at = pins.get(port.at) if isinstance(port.at, str) else port.at
            if at is None:
                raise ValueError(f"Unknown hierarchical port endpoint {port.at}")
            document.append(
                node(
                    "hierarchical_label",
                    port.name,
                    node("shape", sx.Symbol(port.direction)),
                    node("at", *at, 0),
                    node(
                        "effects", node("font", node("size", 1.27, 1.27)), node("justify", sx.Symbol("right"), sx.Symbol("bottom"))
                    ),
                    node("uuid", str(uuid.uuid5(uuid.UUID(root_uuid), instance_path + port.name))),
                )
            )
        for sheet, sid, port_nodes in sheet_items:
            child_folder = folder / sheet.design.schematic.name
            child_path, child_number = create(sheet.design, child_folder, instance_path + "/" + sid)
            document.append(
                node(
                    "sheet",
                    node("at", *sheet.at),
                    node("size", *sheet.size),
                    node("stroke", node("width", 0), node("type", sx.Symbol("default"))),
                    node("fill", node("color", 0, 0, 0, 0)),
                    node("uuid", sid),
                    node(
                        "property",
                        "Sheetname",
                        sheet.name,
                        node("at", sheet.at[0], sheet.at[1] - 1.27, 0),
                        node(
                            "effects",
                            node("font", node("size", 1.27, 1.27)),
                            node("justify", sx.Symbol("left"), sx.Symbol("bottom")),
                        ),
                    ),
                    node(
                        "property",
                        "Sheetfile",
                        str(child_path.relative_to(folder)),
                        node("at", sheet.at[0], sheet.at[1] + sheet.size[1] + 1.27, 0),
                        node(
                            "effects",
                            node("font", node("size", 1.27, 1.27)),
                            node("justify", sx.Symbol("left"), sx.Symbol("top")),
                        ),
                    ),
                    *port_nodes,
                    node(
                        "instances",
                        node(
                            "project", root_name, node("path", instance_path, node("page", str(child_number)))
                        ),
                    ),
                )
            )
        instance = child(document, "sheet_instances")
        if instance:
            document.remove(instance)
        if instance_path == "/" + root_uuid:
            document.append(node("sheet_instances", node("path", "/", node("page", str(page_number)))))
        dump(path, document)
        return path, page_number

    path, _ = create(spec.root, destination, "/" + root_uuid)
    # Root library tables resolve all hierarchical symbols; preserve a union of exact snapshots.
    libraries = {}
    for library in sorted(destination.rglob("*.kicad_sym")):
        values = libraries.setdefault(library.stem, {})
        for symbol in children(sx.loads(library.read_text()), "symbol"):
            if symbol[1] in values and sx.dumps(values[symbol[1]]) != sx.dumps(symbol):
                raise ValueError("Conflicting project symbol snapshots")
            values[symbol[1]] = symbol
    for name, entries in libraries.items():
        dump(
            destination / f"{name}.kicad_sym",
            node(
                "kicad_symbol_lib",
                node("version", 20250114),
                node("generator", "kicad_design"),
                *entries.values(),
            ),
        )
    dump(
        destination / "sym-lib-table",
        node(
            "sym_lib_table",
            node("version", 7),
            *[
                node(
                    "lib",
                    node("name", name),
                    node("type", "KiCad"),
                    node("uri", "${KIPRJMOD}/" + name + ".kicad_sym"),
                    node("options", ""),
                    node("descr", "Project snapshot"),
                )
                for name in libraries
            ],
        ),
    )
    return path, all_pins, all_nc


def generate_hierarchy_checked(spec, intent, output):
    if output.exists():
        raise ValueError("Output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hierarchy-stage-", dir=output.parent) as temp:
        stage = Path(temp) / "project"
        schematic, pins, no_connect = build_hierarchy(spec, stage)
        physical = {p for p in pins if not p.startswith("#")}
        declared = {p for group in intent.nets.values() for p in group} | set(intent.no_connect)
        if physical != declared or no_connect != set(intent.no_connect):
            raise ValueError("Hierarchical intent must account for every physical pin and no-connect")
        atomic_json(stage / "intent.json", intent.model_dump())
        atomic_json(stage / "hierarchy-spec.json", spec.model_dump())
        verify_schematic(schematic, intent, stage / "reports")
        shutil.move(str(stage), output)
    return verify_schematic(output / schematic.name, intent, output / "reports")

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections import deque
from pathlib import Path

from defusedxml import ElementTree as ET

from .config import Project, digest, local_path, save_project


def kicad_cli() -> str:
    executable = shutil.which("kicad-cli")
    mac = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if executable:
        return executable
    if mac.exists():
        return str(mac)
    raise ValueError("kicad-cli not found. Install KiCad or add its executable folder to PATH.")


def schematic_hashes(schematic: Path) -> dict[str, str]:
    """Resolve Sheetfile references recursively, including child sheets outside the root folder."""
    pending, result = [schematic.resolve()], {}
    while pending:
        path = pending.pop()
        if str(path) in result:
            continue
        result[str(path)] = digest(path)
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'\(property\s+"Sheet\s*file"\s+"((?:[^"\\]|\\.)*)"', text, re.I):
            child = match.group(1).replace('\\"', '"').replace('\\\\', '\\')
            child = child.replace("${KIPRJMOD}", str(schematic.parent.resolve()))
            if "${" in child:
                raise ValueError(f"Unresolved sheet path variable: {child}. Export XML manually instead.")
            pending.append((path.parent / child).resolve())
    return result


def export_schematic(root: Path, project: Project, schematic: Path):
    schematic = schematic.resolve()
    before = schematic_hashes(schematic)
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "netlist.xml"
        result = subprocess.run([kicad_cli(), "sch", "export", "netlist", "--format", "kicadxml",
                                 "--output", str(output), str(schematic)], capture_output=True, text=True,
                                timeout=120, check=False)
        if result.returncode:
            raise ValueError(f"KiCad export failed: {result.stderr.strip() or result.stdout.strip()}")
        Graph.from_xml(output, project.component_parts)
        after = schematic_hashes(schematic)
        if before != after:
            raise ValueError("Schematic changed during export. Save it and try again.")
        destination = root / "netlist.xml"
        shutil.copyfile(output, destination)
    project.netlist = "netlist.xml"
    project.schematic = str(schematic)
    project.schematic_hashes = after
    save_project(root, project)


class Graph:
    def __init__(self, components=None, nets=None):
        self.components = components or {}
        self.nets = nets or {}

    @classmethod
    def from_xml(cls, path: Path, overrides: dict[str, str] | None = None):
        root = ET.parse(path).getroot()
        if root.tag != "export":
            raise ValueError("Expected KiCad XML netlist with <export> root")
        components, nets = {}, {}
        libraries = {}
        for lib in root.findall("./libparts/libpart"):
            libraries[(lib.get("lib"), lib.get("part"))] = {
                p.get("num"): {"name": p.get("name", ""), "type": p.get("type", "unknown")}
                for p in lib.findall("./pins/pin")}
        for comp in root.findall("./components/comp"):
            ref = comp.get("ref")
            if not ref or ref in components:
                raise ValueError(f"Missing or duplicate component reference: {ref}")
            fields = {f.get("name", "").casefold(): f.text or "" for f in comp.findall("./fields/field")}
            fields.update({p.get("name", "").casefold(): p.get("value", "") for p in comp.findall("property")})
            value = comp.findtext("value", "")
            part = (overrides or {}).get(ref) or next((fields[k] for k in
                   ("mpn", "manufacturer part number", "manufacturer_part_number", "part number") if fields.get(k)), value)
            source = comp.find("libsource")
            pin_defs = libraries.get((source.get("lib"), source.get("part")), {}) if source is not None else {}
            sheet = comp.find("sheetpath")
            components[ref] = {"ref": ref, "value": value, "part": part,
                               "identity_source": "override" if ref in (overrides or {}) else "field_or_value",
                               "footprint": comp.findtext("footprint", ""), "fields": fields,
                               "sheet": sheet.get("names", "") if sheet is not None else "",
                               "sheet_uuid_path": sheet.get("tstamps", "/") if sheet is not None else "/",
                               "symbol_uuids": comp.findtext("tstamps", "").split(),
                               "dnp": "dnp" in fields or comp.find("dnp") is not None,
                               "pins": {num: {**p, "net": None} for num, p in pin_defs.items()}}
        for net in root.findall("./nets/net"):
            name = net.get("name") or f"net-{net.get('code')}"
            if name in nets:
                raise ValueError(f"Duplicate net name: {name}")
            nodes = []
            for node in net.findall("node"):
                ref, pin = node.get("ref"), node.get("pin")
                if ref not in components or not pin:
                    raise ValueError(f"Net {name} references missing component or pin: {ref}.{pin}")
                old = components[ref]["pins"].get(pin, {})
                if old.get("net") and old["net"] != name:
                    raise ValueError(f"Pin {ref}.{pin} appears on more than one net")
                info = {"name": node.get("pinfunction", old.get("name", "")),
                        "type": node.get("pintype", old.get("type", "unknown")), "net": name}
                components[ref]["pins"][pin] = info
                nodes.append({"ref": ref, "pin": pin, "name": info["name"], "type": info["type"]})
            nets[name] = nodes
        return cls(components, nets)

    @classmethod
    def load(cls, root: Path, project: Project):
        if not project.netlist:
            return cls()
        return cls.from_xml(local_path(root, project.netlist), project.component_parts)

    def pin_net(self, endpoint: str) -> str | None:
        ref, _, pin = endpoint.partition(".")
        if ref not in self.components or pin not in self.components[ref]["pins"]:
            raise ValueError(f"Unknown endpoint {endpoint}; use reference.pin-number, e.g. U1.3")
        return self.components[ref]["pins"][pin]["net"]

    def resolve(self, question: str, refs: list[str] | None = None) -> list[str]:
        selected = set(refs or [])
        for ref, comp in self.components.items():
            for term in (ref, comp["part"]):
                if term and re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", question, re.I):
                    selected.add(ref)
        unknown = selected - self.components.keys()
        if unknown:
            raise ValueError(f"Unknown component references: {', '.join(sorted(unknown))}")
        return sorted(selected)

    def context(self, question: str, refs: list[str] | None = None) -> dict:
        seeds = self.resolve(question, refs)
        selected = set(seeds)
        chosen_nets = {name for name in self.nets if name and re.search(
            r"(?<![\w])" + re.escape(name) + r"(?![\w])", question, re.I)}
        for ref in seeds:
            chosen_nets.update(p["net"] for p in self.components[ref]["pins"].values() if p["net"])
        warnings = []
        for name in sorted(chosen_nets):
            nodes = self.nets[name]
            if len(nodes) <= 12:
                selected.update(n["ref"] for n in nodes)
            else:
                warnings.append(f"{name}: {len(nodes)} pins; neighbor expansion limited on this large net.")
        if not self.components:
            warnings.append("No KiCad netlist loaded. Actual component connections are unknown.")
        elif not selected:
            warnings.append("No component/net selected. Mention references, a net name, or pass --refs U1 U2.")
        return {"seeds": seeds, "components": {r: self.components[r] for r in sorted(selected)},
                "nets": {n: self.nets[n] for n in sorted(chosen_nets)}, "warnings": warnings}

    def path(self, start: str, end: str) -> list[str]:
        if start not in self.components or end not in self.components:
            raise ValueError("Both path endpoints must be component references")
        queue, seen = deque([(start, [start])]), {start}
        while queue:
            ref, route = queue.popleft()
            if ref == end:
                return route
            for pin in self.components[ref]["pins"].values():
                if pin["net"] is None:
                    continue
                for node in self.nets[pin["net"]]:
                    if node["ref"] not in seen:
                        seen.add(node["ref"])
                        queue.append((node["ref"], route + [pin["net"], node["ref"]]))
        return []

    def coverage(self, project: Project) -> list[dict]:
        rows = []
        for ref, comp in sorted(self.components.items()):
            matches = [d.id for d in project.documents if comp["part"].casefold() in
                       [p.casefold() for p in d.parts]]
            rows.append({"ref": ref, "part": comp["part"], "documents": matches,
                         "dnp": comp["dnp"], "status": "mapped" if matches else "missing"})
        return rows

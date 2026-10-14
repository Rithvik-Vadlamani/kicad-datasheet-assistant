from pathlib import Path

import pytest
import sexpdata as sx
from test_native import installed

from kicad_rag.board import BoardSpec, build_board
from kicad_rag.hierarchy import HierarchySpec, generate_hierarchy_checked
from kicad_rag.kicad import Graph
from kicad_rag.native import (
    Intent,
    SchematicSpec,
    child,
    children,
    dump,
    generate_checked,
    verify_board,
    verify_schematic,
)
from kicad_rag.release import export_manufacturing

FIXTURE = Path(__file__).parents[1] / "examples/native"
pytestmark = pytest.mark.skipif(not installed(), reason="KiCad native integration required")


def test_hierarchy_actual_sheet_ports_and_child_fault(tmp_path):
    spec = HierarchySpec.model_validate_json((FIXTURE / "hierarchy-spec.json").read_text())
    intent = Intent.model_validate_json((FIXTURE / "intent.json").read_text())
    root = tmp_path / "hierarchy"
    result = generate_hierarchy_checked(spec, intent, root)
    assert result["status"] == "pass" and len(result["rendered_pages"]) == 2
    graph = Graph.from_xml(root / "reports/netlist.xml", {})
    assert graph.components["R1"]["sheet"] == "/Filter/"
    assert len(graph.components["R1"]["symbol_uuids"]) == 1
    board = build_board(
        root / "hierarchical-filter.kicad_sch",
        BoardSpec.model_validate_json((FIXTURE / "board-spec.json").read_text()),
        intent,
    )
    assert (
        verify_board(board, intent, root / "hierarchical-filter.kicad_sch", root / "reports", refill=True)[
            "status"
        ]
        == "pass"
    )
    changed = root / "filter-stage/filter-stage.kicad_sch"
    value = sx.loads(changed.read_text())
    children(value, "hierarchical_label")[0][1] = "BROKEN_PORT"
    dump(changed, value)
    failure = verify_schematic(root / "hierarchical-filter.kicad_sch", intent, root / "broken")
    assert failure["connectivity"]["status"] == "fail" and failure["erc"]["status"] == "fail"
    assert failure["input_hashes"] != result["input_hashes"]


def test_multiple_units_one_physical_component(tmp_path):
    spec = SchematicSpec.model_validate_json((FIXTURE / "multiunit-spec.json").read_text())
    intent = Intent.model_validate_json((FIXTURE / "multiunit-intent.json").read_text())
    result = generate_checked(spec, intent, tmp_path / "multiunit")
    assert result["status"] == "pass"
    graph = Graph.from_xml(tmp_path / "multiunit/reports/netlist.xml", {})
    assert set(graph.components) == {"U1", "J1", "J2", "J3"}
    assert set(graph.components["U1"]["pins"]) == set(map(str, range(1, 9)))
    inconsistent = spec.model_dump()
    inconsistent["components"][1]["part"] = "different device"
    with pytest.raises(ValueError, match="share symbol"):
        SchematicSpec.model_validate(inconsistent)


def test_rotated_bottom_multilayer_via_native_export_and_open(tmp_path):
    spec = SchematicSpec.model_validate_json((FIXTURE / "schematic-spec.json").read_text())
    intent = Intent.model_validate_json((FIXTURE / "intent.json").read_text())
    root = tmp_path / "board"
    assert generate_checked(spec, intent, root)["status"] == "pass"
    sch = root / "rc-filter.kicad_sch"
    board = build_board(
        sch, BoardSpec.model_validate_json((FIXTURE / "multilayer-board-spec.json").read_text()), intent
    )
    report = verify_board(board, intent, sch, root / "reports", refill=True)
    assert report["status"] == "pass"
    parsed = sx.loads(board.read_text())
    c = next(
        f
        for f in children(parsed, "footprint")
        if any(p[1:3] == ["Reference", "C1"] for p in children(f, "property"))
    )
    assert child(c, "layer")[1] == "B.Cu"
    assert abs(child(c, "at")[3]) == 90
    output = root / "manufacturing"
    manifest = export_manufacturing(board, output)
    assert "In1.Cu" in manifest["layers"] and "In2.Cu" in manifest["layers"]
    assert len(list(output.glob("*.gbr"))) == 9
    from kicad_rag.release import export_assembly
    assembly = export_assembly(board, root / "assembly")
    assert set(assembly["references"]) == {"J1", "J2", "R1", "C1"}
    assert (root / "assembly/placements.csv").exists()
    parsed.remove(children(parsed, "via")[0])
    dump(board, parsed)
    broken = verify_board(board, intent, sch, root / "broken")
    assert broken["status"] == "fail" and broken["drc"]["unconnected_items"]
    with pytest.raises(ValueError, match="Fresh passing"):
        export_manufacturing(board, root / "stale-output")

import json
from pathlib import Path

import pytest

from kicad_rag.board import BoardSpec, build_board
from kicad_rag.kicad import Graph, kicad_cli
from kicad_rag.native import (
    Component,
    Intent,
    SchematicSpec,
    child,
    children,
    compare_intent,
    dump,
    generate_checked,
    native_symbol,
    pin_locations,
    verify_board,
    verify_schematic,
)

FIXTURE = Path(__file__).parents[1] / "examples/native"


def installed():
    try:
        return bool(kicad_cli())
    except ValueError:
        return False


@pytest.mark.skipif(not installed(), reason="Native KiCad installation required")
@pytest.mark.parametrize(
    "angle,mirror,expected",
    [
        (0, None, (50, 46.19)),
        (90, None, (46.19, 50)),
        (180, None, (50, 53.81)),
        (270, None, (53.81, 50)),
        (0, "x", (50, 53.81)),
        (90, "y", (46.19, 50)),
    ],
)
def test_real_library_pin_transforms(angle, mirror, expected):
    c = Component(ref="R1", symbol="Device:R", value="1k", at=(50, 50), angle=angle, mirror=mirror)
    assert pin_locations(c, native_symbol(c.symbol))["1"] == expected


def test_independent_intent_detects_short_and_undeclared_pin():
    graph = Graph(
        {"R1": {"pins": {"1": {}, "2": {}}}},
        {"short": [{"ref": "R1", "pin": "1"}, {"ref": "R1", "pin": "2"}]},
    )
    intent = Intent(nets={"a": ["R1.1"], "b": ["R1.2"]})
    assert compare_intent(graph, intent)["status"] == "fail"
    partial = Intent(nets={"a": ["R1.1"]})
    assert compare_intent(graph, partial)["undeclared_pins"] == ["R1.2"]


@pytest.mark.skipif(not installed(), reason="Native KiCad installation required")
def test_native_schematic_board_and_fault_injection(tmp_path):
    spec = SchematicSpec.model_validate_json((FIXTURE / "schematic-spec.json").read_text())
    intent = Intent.model_validate_json((FIXTURE / "intent.json").read_text())
    root = tmp_path / "native"
    assert generate_checked(spec, intent, root)["status"] == "pass"
    sch = root / "rc-filter.kicad_sch"
    board = build_board(sch, BoardSpec.model_validate_json((FIXTURE / "board-spec.json").read_text()), intent)
    report = verify_board(board, intent, sch, root / "reports", refill=True)
    assert report["status"] == "pass"
    from kicad_rag.config import atomic_json
    from kicad_rag.release import export_manufacturing, release

    sch_report = verify_schematic(sch, intent, root / "reports")
    # Synthetic review attestations test bookkeeping, not actual visual inspection.
    for kind, hashes in [("visual", sch_report["input_hashes"]), ("physical", report["input_hashes"])]:
        atomic_json(
            root / f"{kind}.json",
            {
                "kind": kind,
                "actor": "unit-test-fixture",
                "timestamp": "2026-09-10",
                "verdict": "pass",
                "input_hashes": hashes,
                "inspected": ["synthetic inspection fixture"],
                "findings": ["This test exercises release state validation, not human visual review."],
            },
        )
    assert (
        export_manufacturing(board, root / "manufacturing")["status"]
        == "exported_requires_manufacturing_review"
    )
    assert len(list((root / "manufacturing").glob("*.gbr"))) == 7
    assert release(
        root,
        sch,
        root / "intent.json",
        root / "visual.json",
        board=board,
        physical_path=root / "physical.json",
    )["files"]
    import sexpdata as sx

    data = sx.loads(board.read_text())
    assert any(children(z, "filled_polygon") for z in children(data, "zone"))
    # Remove one physical conductor; zone is OUT-isolated and cannot conceal the open.
    segment = next(s for s in children(data, "segment") if child(s, "net")[-1] == "/IN")
    data.remove(segment)
    dump(board, data)
    with pytest.raises(ValueError, match="inputs changed"):
        release(
            root,
            sch,
            root / "intent.json",
            root / "visual.json",
            board=board,
            physical_path=root / "physical.json",
        )
    broken = verify_board(board, intent, sch, root / "broken", refill=True)
    assert broken["status"] == "fail" and broken["drc"]["unconnected_items"]
    # A wrong independent pad map fails even if schematic and PCB agree with each other.
    intent.pad_map["R1"] = {"1": "2", "2": "1"}
    assert verify_board(board, intent, sch, root / "wrong-pad")["pad_map_issues"]
    # Native export detects a geometry-only wire deletion against original intent.
    schematic = sx.loads(sch.read_text())
    schematic.remove(children(schematic, "wire")[0])
    dump(sch, schematic)
    assert verify_schematic(sch, intent, root / "broken-sch")["connectivity"]["status"] == "fail"
    with pytest.raises(ValueError, match="already exists"):
        generate_checked(spec, intent, root)


def test_unknown_schematic_fields_fail_closed():
    value = json.loads((FIXTURE / "schematic-spec.json").read_text())
    value["autoroute_everything"] = True
    with pytest.raises(ValueError):
        SchematicSpec.model_validate(value)


@pytest.mark.skipif(not installed(), reason="Native KiCad installation required")
def test_power_flags_do_not_become_physical_bom_components(tmp_path):
    spec = SchematicSpec.model_validate_json((FIXTURE / "schematic-spec.json").read_text())
    spec.components.append(
        Component(ref="#FLG01", symbol="power:PWR_FLAG", value="PWR_FLAG", at=(55.88, 60.96))
    )
    intent = Intent.model_validate_json((FIXTURE / "intent.json").read_text())
    result = generate_checked(spec, intent, tmp_path / "flag")
    assert result["status"] == "pass"
    graph = Graph.from_xml(tmp_path / "flag/reports/netlist.xml", {})
    assert "#FLG01" not in graph.components

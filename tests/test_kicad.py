import pytest
from defusedxml.common import DefusedXmlException

from kicad_rag.kicad import Graph, schematic_hashes


def test_graph_keeps_pin_numbers_and_expands_neighbors(demo):
    root, p = demo
    graph = Graph.load(root, p)
    assert graph.pin_net("U1.3") == graph.pin_net("U2.3") == "GPIO_LINK"
    assert graph.path("U1", "U2") == ["U1", "GPIO_LINK", "U2"]
    assert set(graph.context("U1 voltage")["components"]) == {"U1", "U2", "U3"}
    assert graph.resolve("Can U10 work?") == []


def test_unknown_refs_are_not_guessed(demo):
    root, p = demo
    with pytest.raises(ValueError, match="Unknown component"):
        Graph.load(root, p).context("voltage", ["U99"])


def test_external_xml_entities_are_rejected(tmp_path):
    net = tmp_path / "evil.xml"
    net.write_text('<!DOCTYPE export [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><export>&xxe;</export>')
    with pytest.raises(DefusedXmlException):
        Graph.from_xml(net)


def test_missing_component_in_net_rejected(tmp_path):
    net = tmp_path / "invalid.xml"
    net.write_text('<export><nets><net name="X"><node ref="U404" pin="1"/></net></nets></export>')
    with pytest.raises(ValueError, match="missing component"):
        Graph.from_xml(net)


def test_hierarchical_sheet_change_detected(tmp_path):
    parent = tmp_path / "main.kicad_sch"
    child = tmp_path / "child.kicad_sch"
    parent.write_text('(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))')
    child.write_text('(kicad_sch)')
    before = schematic_hashes(parent)
    child.write_text('(kicad_sch (wire))')
    assert schematic_hashes(parent) != before
    assert len(before) == 2

import pytest

from kicad_rag.checks import calculate, evaluate, load_design
from kicad_rag.kicad import Graph
from kicad_rag.store import Store, index_project


def run_checks(root, p, design=None):
    with Store(root) as store:
        return evaluate(design or load_design(root, p), Graph.load(root, p), p, store)


def test_teaching_failures_have_correct_margins(demo):
    root, p = demo
    index_project(root, p)
    logic, thermal = run_checks(root, p)
    assert logic["status"] == thermal["status"] == "violation"
    assert logic["results"]["high_margin_V"] == pytest.approx(-1.1)
    assert logic["results"]["low_margin_V"] == pytest.approx(1.1)
    assert thermal["results"]["dissipation_W"] == pytest.approx(1.8)
    assert thermal["results"]["estimated_junction_degC"] == pytest.approx(220)


@pytest.mark.parametrize("change", ["unreviewed", "unit", "quote", "missing", "wrong_ref", "conditions", "different_net"])
def test_bad_evidence_cannot_become_a_pass(demo, change):
    root, p = demo
    index_project(root, p)
    design = load_design(root, p)
    if change == "unreviewed":
        design.facts["u1_voh"].reviewed = False
    elif change == "unit":
        design.facts["u1_voh"].unit = "A"
    elif change == "quote":
        design.facts["u1_voh"].quote = "Wrong quote entirely"
    elif change == "missing":
        del design.facts["u1_voh"]
    elif change == "wrong_ref":
        design.facts["u1_voh"].ref = "U2"
    elif change == "conditions":
        design.checks[0].conditions_reviewed = False
    elif change == "different_net":
        design.checks[0].endpoints = ["U1.1", "U2.3"]
    result = run_checks(root, p, design)[0]
    assert result["status"] == "unassessed"
    assert result["issues"]


def test_pin_voltage_distinguishes_recommended_from_absolute():
    result, issues = calculate("pin_voltage", {"applied_min": 3.0, "applied_max": 3.8,
        "recommended_min": 3.0, "recommended_max": 3.6, "absolute_min": -0.3, "absolute_max": 4.0})
    assert result["upper_operating_margin_V"] == pytest.approx(-0.2)
    assert len(issues) == 1 and "recommended" in issues[0]


def test_pullup_calculation_matches_published_equations():
    result, issues = calculate("i2c_pullup", {"vpu_max": 3.3, "vol_max": 0.4, "iol_max": 0.003,
        "bus_cap_max": 200e-12, "rise_time_max": 300e-9, "resistance_min": 1100, "resistance_max": 1600})
    assert result["required_resistance_min_ohm"] == pytest.approx(966.6666667)
    assert result["allowed_resistance_max_ohm"] == pytest.approx(1770.3293, rel=1e-5)
    assert not issues


def test_no_load_budget_headroom_is_flagged():
    _, issues = calculate("power_budget", {"load_max": 0.5, "supply_available": 0.5})
    assert issues


def test_unphysical_inputs_rejected():
    with pytest.raises(ValueError):
        calculate("power_budget", {"load_max": -1, "supply_available": 0.5})

import shutil
from pathlib import Path

import pytest

from kicad_rag.advanced import ocr_copy, source_diff
from kicad_rag.simulation import simulate


def test_revision_diff_keeps_originals(tmp_path):
    a, b = tmp_path / "old.txt", tmp_path / "new.txt"
    a.write_text("Maximum 3.3 V\fOther page")
    b.write_text("Maximum 3.0 V\fOther page")
    result = source_diff(a, b)
    assert [p["page"] for p in result["changed_pages"]] == [1]
    assert "-Maximum 3.3 V" in result["changed_pages"][0]["diff"]
    assert a.read_text().startswith("Maximum 3.3 V")


def test_ocr_unavailable_is_not_a_success(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="requires ocrmypdf"):
        ocr_copy(tmp_path / "in.pdf", tmp_path / "out.pdf")
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice required")
def test_actual_rc_simulation_matches_analytical_corner(tmp_path):
    import math

    source = Path(__file__).parents[1] / "examples/native/rc-ac.cir"
    result = simulate(source, tmp_path / "sim")
    assert result["status"] == "executed_requires_measurement_review"
    log = (tmp_path / "sim/simulation.log").read_text()
    rows = [line.split() for line in log.splitlines() if line.strip().startswith("0\t")]
    assert rows, log
    assert float(rows[0][2]) == pytest.approx(1 / math.sqrt(2), rel=1e-5)
    assert float(rows[0][3]) == pytest.approx(-math.pi / 4, rel=1e-5)
    bad = tmp_path / "unsafe.cir"
    bad.write_text("Title\n.control\nshell echo bad\n.endc\n.end\n")
    with pytest.raises(ValueError, match="control scripts"):
        simulate(bad, tmp_path / "bad")


def test_reviewed_model_includes_are_bound_and_cannot_run_control_commands(tmp_path):
    from kicad_rag.config import digest
    from kicad_rag.simulation_models import ModelManifest
    model = tmp_path / 'passives.inc'
    model.write_text('R1 in out 1k\nC1 out 0 100n\n')
    deck = tmp_path / 'included.cir'
    deck.write_text('Hash-bound include fixture\nV1 in 0 AC 1\n.include "passives.inc"\n.ac lin 1 1591.54943 1591.54943\n.print ac vm(out) vp(out)\n.end\n')
    manifest = ModelManifest(models=[{'path':'passives.inc','sha256':digest(model),
                                     'source_url':'synthetic-test-fixture',
                                     'review_note':'Synthetic passive include reviewed for the model loading test.'}])
    result = simulate(deck,tmp_path/'included',models=manifest)
    assert result['status'] == 'executed_requires_measurement_review'
    assert result['model_hashes'][str(model)] == digest(model)
    assert '.include' not in (tmp_path/'included/circuit.cir').read_text()
    model.write_text('.control\nshell echo forbidden\n.endc\n')
    with pytest.raises(ValueError,match='hash changed'):
        simulate(deck,tmp_path/'changed',models=manifest)
    manifest.models[0].sha256 = digest(model)
    with pytest.raises(ValueError,match='does not allow control scripts'):
        simulate(deck,tmp_path/'control',models=manifest)

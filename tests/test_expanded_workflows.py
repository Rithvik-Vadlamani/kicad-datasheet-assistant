import json
from pathlib import Path

import pytest
from test_evidence import setup

from kicad_rag.config import atomic_json
from kicad_rag.evidence import quantities, review_fact
from kicad_rag.parameter_extraction import propose_parameters
from kicad_rag.rag import ask


def test_extraction_never_claims_requested_conditions_are_verified(demo):
    root, project, request, fact = setup(demo)
    # Remove the manually-created fixture to test extraction alone.
    (root / "parameters.json").unlink()
    result = propose_parameters(root, project, request)
    proposals = result["results"][0]["proposals"]
    assert proposals and result["automatically_accepted"] == 0
    assert not (root / "parameters.json").exists()
    candidate = next(p["fact"] for p in proposals if p["fact"]["value"] == 3.3)
    assert candidate["conditions"] != request.obligations[0].conditions
    assert candidate["state"] == "needs_review" and candidate["review"] is None
    atomic_json(root / "parameters.json", [candidate])
    with pytest.raises(ValueError, match="replace_unreviewed_conditions"):
        review_fact(
            root, project, candidate["id"], "test", "Review fixture intentionally leaves unknown conditions."
        )
    request.candidates[0].revision = "wrong-revision"
    assert not propose_parameters(root, project, request)["results"][0]["proposals"]


def test_exact_provisional_scope_abstains_without_chat_call(demo):
    root, project, _, _ = setup(demo)

    class NeverGenerate:
        def generate(self, *args):
            raise AssertionError("No model should be asked to guess a missing exact part")

    result = ask(
        root,
        project,
        "What voltage does MISSING123 support?",
        candidate_parts=["MISSING123"],
        provider=NeverGenerate(),
    )
    assert result["mode"] == "abstained"
    assert not any(s["type"] == "topology" for s in result["sources"])
    assert not any(s.get("doc_id") for s in result["sources"])


def test_pdf_micro_symbols_and_negative_temperature_are_not_positive():
    assert quantities("–55 °C and −55 °C") == quantities("-55 °C")
    assert quantities("100 μA") == quantities("0.1 mA")
    assert quantities("5.3 mΩ") == quantities("0.0053 Ω")
    assert quantities("–55 °C") != quantities("55 °C")


def test_benchmark_rejects_family_leakage_before_scoring(demo):
    from kicad_rag.benchmark import evaluate_cases

    root, project = demo
    with pytest.raises(ValueError, match="crosses benchmark splits"):
        evaluate_cases(
            root,
            project,
            [
                {"id": "a", "families": ["LDO"], "split": "development", "type": "missing_source"},
                {"id": "b", "families": ["LDO"], "split": "holdout", "type": "missing_source"},
            ],
        )


def test_unknown_layer_and_impossible_via_rejected():
    from kicad_rag.board import BoardSpec

    data = json.loads((Path(__file__).parents[1] / "examples/native/multilayer-board-spec.json").read_text())
    data["copper_layers"] = 2
    with pytest.raises(ValueError, match="outside the declared stack"):
        BoardSpec.model_validate(data)
    data["copper_layers"] = 4
    data["vias"][0]["drill"] = data["vias"][0]["diameter"]
    with pytest.raises(ValueError, match="diameter must exceed"):
        BoardSpec.model_validate(data)

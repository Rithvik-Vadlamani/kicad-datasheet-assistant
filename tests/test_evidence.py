import json

import pytest

from kicad_rag.config import atomic_json, digest
from kicad_rag.evidence import (
    Candidate,
    EvidenceRequest,
    Obligation,
    ParameterFact,
    Source,
    evidence_packet,
    impact,
    quantity,
    review_fact,
    verify_packet,
)
from kicad_rag.rag import validate_answer
from kicad_rag.store import index_project


def setup(demo):
    root, project = demo
    doc = project.documents[0]
    text = "Recommended output maximum 3.3 V at ambient 25 degC and no load."
    (root / doc.path).write_text(text)
    doc.revision = "A"
    index_project(root, project)
    candidate = Candidate(ref="U1", part=doc.parts[0], package="test-package", revision="A")
    fact = ParameterFact(
        id="voltage",
        candidate=candidate,
        parameter="output_voltage",
        category="recommended",
        bound="max",
        value=3.3,
        unit="V",
        conditions={"temperature": "25 degC", "load": "0 A"},
        source=Source(document=doc.id, sha256=digest(root / doc.path), page=1, quote=text),
    )
    atomic_json(root / "parameters.json", [fact.model_dump()])
    request = EvidenceRequest(
        design_id="test",
        candidates=[candidate],
        obligations=[
            Obligation(
                id="output",
                ref="U1",
                parameter="output_voltage",
                category="recommended",
                bound="max",
                unit="V",
                conditions=fact.conditions,
                query="output maximum voltage",
            )
        ],
    )
    return root, project, request, fact


def test_unreviewed_value_cannot_complete_request(demo):
    root, project, request, _ = setup(demo)
    packet = evidence_packet(root, project, request)
    assert not packet["complete"]
    assert packet["obligations"][0]["state"] == "needs_review"


def test_review_conditions_revision_and_source_freshness(demo):
    root, project, request, fact = setup(demo)
    review_fact(
        root,
        project,
        fact.id,
        "test-reviewer",
        "Fixture original text inspected; exact units and conditions match.",
    )
    packet = evidence_packet(root, project, request)
    assert packet["complete"] and verify_packet(root, project, packet)["valid"]
    request.obligations[0].conditions["temperature"] = "85 degC"
    assert not evidence_packet(root, project, request)["complete"]
    (root / project.documents[0].path).write_text("Changed specification: output maximum 2.5 V")
    assert "source_hash_changed" in impact(root, project)[0]["issues"]
    assert not verify_packet(root, project, packet)["valid"]


def test_changed_fact_invalidates_review(demo):
    root, project, request, fact = setup(demo)
    review_fact(
        root,
        project,
        fact.id,
        "test-reviewer",
        "Fixture original text inspected; exact units and conditions match.",
    )
    path = root / "parameters.json"
    data = json.loads(path.read_text())
    data[0]["candidate"]["package"] = "different-package"
    atomic_json(path, data)
    result = evidence_packet(root, project, request)
    assert not result["complete"]
    assert "original_page_review_missing_or_stale" in result["obligations"][0]["unusable_facts"][0]["issues"]


def test_numerical_quote_mismatch_rejected(demo):
    root, project, _, fact = setup(demo)
    fact.value = 33
    atomic_json(root / "parameters.json", [fact.model_dump()])
    with pytest.raises(ValueError, match="numeric_value"):
        review_fact(
            root,
            project,
            fact.id,
            "test-reviewer",
            "This incorrect fact must not be accepted by the review command.",
        )


def test_3_3_v_cannot_be_cited_as_33_v():
    def raw(value):
        return json.dumps(
            {
                "claims": [
                    {
                        "kind": "documented",
                        "text": f"The limit is {value}.",
                        "citations": [{"source": "S1", "quote": "The limit is 3.3 V."}],
                    }
                ],
                "assumptions": [],
                "missing_information": [],
                "validation_steps": [],
            }
        )

    sources = [{"source_id": "S1", "text": "The limit is 3.3 V."}]
    with pytest.raises(ValueError, match="numerical"):
        validate_answer(raw("33 V"), sources)
    assert validate_answer(raw("3300 mV"), sources)
    assert quantity(1, "mA") != quantity(1, "A")


def test_packet_edit_detected(demo):
    root, project, request, fact = setup(demo)
    review_fact(
        root,
        project,
        fact.id,
        "test-reviewer",
        "Fixture original text inspected; exact units and conditions match.",
    )
    packet = evidence_packet(root, project, request)
    packet["obligations"][0]["facts"][0]["value"] = 33
    assert "packet_changed" in verify_packet(root, project, packet)["issues"]


def test_conflicting_accepted_facts_prevent_complete_packet(demo):
    root, project, request, first = setup(demo)
    doc = project.documents[0]
    (root / doc.path).write_text(first.source.quote + "\nAnother condition states maximum 3.0 V.")
    index_project(root, project)
    first.source.sha256 = digest(root / doc.path)
    second = first.model_copy(deep=True)
    second.id, second.value, second.source.quote = "other", 3, "Another condition states maximum 3.0 V."
    atomic_json(root / "parameters.json", [first.model_dump(), second.model_dump()])
    for f in (first, second):
        review_fact(
            root,
            project,
            f.id,
            "test-reviewer",
            "Deliberate contradictory accepted records for conflict regression.",
        )
    packet = evidence_packet(root, project, request)
    assert not packet["complete"] and packet["obligations"][0]["state"] == "conflicting"


def test_typical_value_cannot_satisfy_maximum_obligation(demo):
    root, project, request, fact = setup(demo)
    fact.bound = "typ"
    atomic_json(root / "parameters.json", [fact.model_dump()])
    review_fact(
        root,
        project,
        fact.id,
        "test-reviewer",
        "Synthetic typical value reviewed only for this regression fixture.",
    )
    assert not evidence_packet(root, project, request)["complete"]


def test_shared_cache_requires_local_registered_source(demo, tmp_path):
    from kicad_rag.config import Project

    root, project = demo
    project.settings.shared_extraction_cache = str(tmp_path / "shared")
    first = index_project(root, project)
    other = tmp_path / "other"
    other.mkdir()
    copy = Project.model_validate(project.model_dump())
    # A configured cache never makes documents from another project searchable by itself.
    copy.documents = []
    copy.netlist = None
    with pytest.raises(ValueError, match="No chunks"):
        index_project(other, copy)
    assert first["documents"] == 3


def test_index_cancellation_preserves_old_index(demo):
    import threading

    from kicad_rag.store import Store

    root, project = demo
    index_project(root, project)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        index_project(root, project, force=True, cancel=cancel)
    with Store(root) as store:
        store.require_fresh(root, project)
        assert len(store.chunks()) == 3

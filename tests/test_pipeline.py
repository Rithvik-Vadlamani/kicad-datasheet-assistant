import json

import numpy as np
import pytest

from kicad_rag.config import Document, Project, Settings, local_path
from kicad_rag.ingest import chunk_document, split_text
from kicad_rag.rag import ask, build_bundle, validate_answer
from kicad_rag.store import Store, index_project


class MockProvider:
    """Deterministic test double, never presented as a semantic model."""

    def __init__(self):
        self.inputs = []
        self.generations = 0

    def embed(self, texts):
        self.inputs.extend(texts)
        return np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (len(texts), 1))

    def generate(self, system, context):
        self.generations += 1
        bundle = json.loads(context)
        source = next(s for s in bundle["sources"] if s["type"] == "datasheet")
        return json.dumps(
            {
                "claims": [
                    {
                        "kind": "documented",
                        "text": "This is a fictional teaching source.",
                        "citations": [{"source": source["source_id"], "quote": source["text"][:70]}],
                    }
                ],
                "assumptions": [],
                "missing_information": [],
                "validation_steps": [],
            }
        )


def test_offline_evidence_has_both_parts_and_actual_pin_connection(demo):
    root, p = demo
    stats = index_project(root, p)
    assert stats["chunks"] == 3
    bundle = ask(root, p, "Can U1 drive U2 with compatible logic voltage?", evidence_only=True)
    assert {s.get("doc_id") for s in bundle["sources"]} >= {"driver", "receiver"}
    topology = next(s for s in bundle["sources"] if s["type"] == "topology")
    assert "GPIO_LINK" in topology["text"]
    assert "answer" not in bundle
    assert bundle["mode"] == "evidence_only"
    assert (root / "reports").exists()


def test_document_changes_block_stale_answers(demo):
    root, p = demo
    index_project(root, p)
    (root / p.documents[0].path).write_text("New revision has different limits")
    with pytest.raises(ValueError, match="stale"):
        build_bundle(root, p, "U1 voltage")


def test_netlist_changes_block_stale_answers(demo):
    root, p = demo
    index_project(root, p)
    path = root / p.netlist
    path.write_text(path.read_text().replace("GPIO_LINK", "CHANGED_NET"))
    with pytest.raises(ValueError, match="stale"):
        build_bundle(root, p, "U1 voltage")


def test_removed_document_is_not_retrievable(demo):
    root, p = demo
    index_project(root, p)
    p.documents = [d for d in p.documents if d.id != "receiver"]
    index_project(root, p)
    result = build_bundle(root, p, "U1 and U2 logic voltage")
    assert "receiver" not in {s.get("doc_id") for s in result["sources"]}
    assert any("demo-sensor-5" in w for w in result["warnings"])


def test_reindex_is_incremental_and_metadata_invalidates_embeddings(demo):
    root, p = demo
    p.settings = Settings(embedding_provider="ollama", embedding_model="mock")
    provider = MockProvider()
    assert index_project(root, p, provider=provider)["new_embeddings"] == 3
    assert index_project(root, p, provider=provider)["unchanged"]
    p.documents[0].title = "Changed title"
    assert index_project(root, p, provider=provider)["new_embeddings"] == 1
    assert len(provider.inputs) == 4


def test_failed_embedding_preserves_previous_index(demo):
    root, p = demo
    index_project(root, p)
    with Store(root) as store:
        previous = store.meta("fingerprint")
    p.settings = Settings(embedding_provider="ollama", embedding_model="mock")

    class Broken:
        def embed(self, texts):
            raise ValueError("service unavailable")

    with pytest.raises(ValueError, match="unavailable"):
        index_project(root, p, provider=Broken())
    with Store(root) as store:
        assert store.meta("fingerprint") == previous
        assert len(store.chunks()) == 3


def test_project_isolation_even_with_same_part_number(demo, tmp_path):
    root, p = demo
    index_project(root, p)
    other = tmp_path / "other"
    other.mkdir()
    (other / "secret.txt").write_text("Private cross-project marker GOLDFISH. DEMO-MCU-33 voltage.")
    op = Project(
        name="Other",
        documents=[Document(id="secret", path="secret.txt", title="Other", parts=["DEMO-MCU-33"])],
    )
    index_project(other, op)
    result = build_bundle(root, p, "U1 voltage GOLDFISH")
    assert "GOLDFISH" not in " ".join(s["text"] for s in result["sources"])


def test_full_generation_pipeline_with_test_double(demo):
    root, p = demo
    p.settings = Settings(
        embedding_provider="ollama", embedding_model="mock", chat_provider="ollama", chat_model="mock"
    )
    provider = MockProvider()
    index_project(root, p, provider=provider)
    result = ask(root, p, "U1 and U2 logic voltage", provider=provider)
    assert provider.generations == 1
    assert result["retrieval"] == "hybrid"
    assert result["answer"]["claims"][0]["citations"]


def test_unknown_question_abstains_without_generation(demo):
    root, p = demo
    index_project(root, p)
    provider = MockProvider()
    result = ask(root, p, "quasarzz gravitonz?", provider=provider)
    assert result["mode"] == "abstained"
    assert provider.generations == 0


@pytest.mark.parametrize(
    "source,quote", [("S9", "This is real evidence"), ("S1", "An invented quotation"), ("S1", "        ")]
)
def test_fabricated_citation_or_quote_is_rejected(source, quote):
    raw = json.dumps(
        {
            "claims": [
                {
                    "kind": "documented",
                    "text": "Some assertion",
                    "citations": [{"source": source, "quote": quote}],
                }
            ],
            "assumptions": [],
            "missing_information": [],
            "validation_steps": [],
        }
    )
    with pytest.raises(ValueError):
        validate_answer(raw, [{"source_id": "S1", "text": "This is real evidence"}])


def test_uncited_claim_rejected():
    raw = json.dumps(
        {
            "claims": [{"kind": "inference", "text": "It will work", "citations": []}],
            "assumptions": [],
            "missing_information": [],
            "validation_steps": [],
        }
    )
    with pytest.raises(ValueError, match="without source"):
        validate_answer(raw, [])


def test_chunks_keep_pdf_page_and_revision_and_long_table_progress():
    doc = Document(id="part", path="part.pdf", title="Part", revision="Rev C")
    pages = [{"page": 7, "text": "Voltage " * 800, "tables": [["X" * 2000, "Y" * 3000]]}]
    chunks = chunk_document(doc, pages, Settings(chunk_chars=500, overlap_chars=20), "sha")
    assert len(chunks) > 10
    assert all(c["page"] == 7 and c["revision"] == "Rev C" for c in chunks)
    assert split_text("abc", 500) == ["abc"]


def test_project_paths_cannot_escape(tmp_path):
    with pytest.raises(ValueError, match="inside"):
        local_path(tmp_path, "../secret.pdf")


def test_raw_query_does_not_execute_fts_operators(demo):
    root, p = demo
    index_project(root, p)
    build_bundle(root, p, 'U1 voltage " OR NOT ) -- *')


def test_restored_parent_pages_are_not_repeated_in_context(demo):
    root, project = demo
    doc = project.documents[0]
    (root / doc.path).write_text(
        ("VOH voltage operating driver specification. " * 80) + "\nEnd of parent page."
    )
    project.settings.chunk_chars = 500
    project.settings.overlap_chars = 30
    project.settings.restore_parent_context = True
    index_project(root, project)
    bundle = build_bundle(root, project, "U1 VOH voltage")
    pages = [(s["doc_id"], s["page"]) for s in bundle["sources"] if s.get("context_restored") == "same_page"]
    assert pages and len(pages) == len(set(pages))

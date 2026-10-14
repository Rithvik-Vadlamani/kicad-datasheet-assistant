from __future__ import annotations

import json
import re
from collections import defaultdict

import numpy as np

from .config import Project
from .providers import Provider
from .store import Store

STOP = set("a an the is are was were of for to in on at be it can could would will what which how do does if and or with between my happen happens this that they them all any about".split())
EXPANSIONS = {
    "compatible": "VIH VIL VOH VOL recommended operating absolute maximum",
    "interact": "VIH VIL VOH VOL supply startup current timing",
    "voltage": "recommended operating absolute maximum supply input output",
    "heat": "thermal junction temperature dissipation theta",
    "hot": "thermal junction temperature dissipation theta",
    "startup": "power sequencing reset enable inrush back power",
    "i2c": "open drain pull up capacitance rise time sink current",
    "stable": "stability ESR output capacitor transient",
}


def tokens(query: str) -> list[str]:
    # Parameterized FTS expression prevents question text from becoming query operators.
    return list(dict.fromkeys(t for t in re.findall(r"[\w]+", query.casefold()) if t not in STOP))[:70]


def retrieve(store: Store, project: Project, question: str, graph_context: dict,
             *, top_k: int | None = None, lexical_only: bool = False, provider=None) -> tuple[list[dict], list[str]]:
    provider = provider or Provider(project.settings)
    k = top_k or project.settings.top_k
    all_chunks = {c["id"]: c for c in store.chunks()}
    warnings = []
    components = graph_context["components"]
    parts = {c["part"].casefold() for c in components.values()}
    seed_parts = {components[r]["part"].casefold() for r in graph_context["seeds"]}
    missing = [p for p in parts if not any(p in [x.casefold() for x in d.parts] for d in project.documents)]
    warnings.extend(f"No document mapped to part {p}." for p in sorted(missing))
    allowed = {d.id for d in project.documents if not parts or d.kind == "design_note" or
               parts.intersection(p.casefold() for p in d.parts)}
    candidates = {key: c for key, c in all_chunks.items() if c["doc_id"] in allowed}
    if not candidates:
        return [], warnings + ["No source documents available for the selected component scope."]
    expanded = question + " " + " ".join(EXPANSIONS[t] for t in tokens(question) if t in EXPANSIONS)
    query_tokens = tokens(expanded)
    scores = defaultdict(float)
    ranks = []
    if query_tokens:
        match = " OR ".join('"' + t + '"' for t in query_tokens)
        # Filter before ranking so unrelated projects/components cannot consume the candidate pool.
        rows = store.db.execute("SELECT id, bm25(search) AS score FROM search WHERE search MATCH ? ORDER BY score", (match,))
        lexical = [row[0] for row in rows if row[0] in candidates][:max(100, k * 5)]
        ranks.append(lexical)
    if project.settings.embedding_provider != "none" and not lexical_only:
        query_vector = provider.embed([project.settings.embedding_query_prefix + question])[0]
        vector_rows = [(row[0], json.loads(row[1])) for row in store.db.execute(
            "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL") if row[0] in candidates]
        if vector_rows:
            matrix = np.asarray([r[1] for r in vector_rows], dtype=np.float32)
            if matrix.shape[1] != query_vector.shape[0]:
                raise ValueError("Embedding dimensions changed; run index --force with the current model")
            similarities = matrix @ query_vector
            order = np.argsort(-similarities, kind="stable")[:max(100, k * 5)]
            ranks.append([vector_rows[i][0] for i in order if similarities[i] > 0])
    else:
        warnings.append("Keyword retrieval only; semantic embeddings are disabled or bypassed.")
    for ranking in ranks:
        for rank, key in enumerate(ranking, 1):
            scores[key] += 1 / (60 + rank)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    if project.settings.reranker_model and ordered:
        from .retrieval_adapters import rerank
        head = ordered[:project.settings.reranker_candidates]
        ordered = rerank(project.settings.reranker_model, question, head, candidates) + ordered[len(head):]
    selected, page_counts = [], defaultdict(int)
    def add(key):
        item = candidates[key]
        page = (item["doc_id"], item["page"])
        # A restorable page supplies all of its child chunks. Reserve one slot per page.
        limit = 1 if project.settings.restore_parent_context and len(store.page(*page)) <= 8000 else 3
        if key not in selected and len(selected) < k and page_counts[page] < limit:
            selected.append(key)
            page_counts[page] += 1
            return True
        return False
    # Round-robin exact parts so an early part cannot take the budget for other prerequisites.
    scoped = {part: [key for key in ordered if part in [p.casefold() for p in candidates[key]["parts"]]]
              for part in sorted(seed_parts)}
    for _ in range(2):
        for keys in scoped.values():
            for key in keys:
                if add(key):
                    break
    for key in ordered:
        if len(selected) >= k:
            break
        add(key)
    results = [{**candidates[key], "retrieval_score": scores[key]} for key in selected]
    if project.settings.restore_parent_context:
        for result in results:
            parent = store.page(result['doc_id'], result['page'])
            # Preserve the scored child and its ID. Large parents remain available through the page command.
            if len(parent) <= 8000:
                result['retrieved_child_text'] = result['text']
                result['text'] = parent
                result['context_restored'] = 'same_page'
    if not results:
        warnings.append("No matching evidence. Rephrase, select components, or add the missing datasheet.")
    for part in sorted(seed_parts):
        if not any(part in [p.casefold() for p in c["parts"]] for c in results):
            warnings.append(f"No retrieved evidence for requested part {part}; do not infer its specifications.")
    return results, warnings

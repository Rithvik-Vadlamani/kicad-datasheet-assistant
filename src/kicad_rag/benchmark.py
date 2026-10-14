"""Gold labels stay in this evaluator, never in retrieval or context-packing inputs."""

import math
import random
import statistics
import time
from collections import defaultdict

from .config import canonical_hash, digest, local_path
from .ingest import normalized
from .rag import build_bundle
from .store import Store, fingerprint


def wilson(successes, count):
    if not count:
        return None
    z, p = 1.959964, successes / count
    denominator = 1 + z*z/count
    center = (p + z*z/(2*count)) / denominator
    half = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / denominator
    return [max(0, center-half), min(1, center+half)]


def evaluate_cases(root, project, cases, *, split=None, lexical_only=False):
    from pathlib import Path
    implementation = {name: digest(Path(__file__).with_name(name))
                      for name in ["retrieval.py", "retrieval_adapters.py", "rag.py", "store.py", "benchmark.py"]}
    if not cases:
        raise ValueError("Benchmark has no cases")
    ids, families = set(), {}
    for case in cases:
        if case["id"] in ids:
            raise ValueError("Duplicate benchmark case ID")
        ids.add(case["id"])
        for family in case["families"]:
            if family in families and families[family] != case["split"]:
                raise ValueError("Device family crosses benchmark splits")
            families[family] = case["split"]
        if case["type"] not in ("region", "missing_source"):
            raise ValueError("Unsupported benchmark case type")
        if case["type"] == "region" and not case.get("required"):
            raise ValueError("Answerable region case has no required evidence")
    chosen = [c for c in cases if split is None or c["split"] == split]
    if not chosen:
        raise ValueError("Requested split has no cases")
    results = []
    with Store(root) as store:
        store.require_fresh(root, project)
        initial = fingerprint(root, project)
        for case in chosen:
            for region in case.get("required", []):
                doc = next((d for d in project.documents if d.id == region["document"]), None)
                if doc is None or digest(local_path(root, doc.path)) != region["sha256"]:
                    raise ValueError(f"Stale or unavailable original in {case['id']}")
                if normalized(region["contains"]) not in normalized(store.page(doc.id, region["page"])):
                    raise ValueError(f"Invalid gold region in {case['id']}")
            start = time.perf_counter()
            # Only question and provisional part scope enter the production pipeline.
            bundle = build_bundle(root, project, case["question"], lexical_only=lexical_only,
                                  candidate_parts=case["parts"])
            elapsed = time.perf_counter() - start
            sources = [s for s in bundle["sources"] if "doc_id" in s]
            ranks = [next((i for i, s in enumerate(sources, 1) if s["doc_id"] == r["document"]
                           and s["page"] == r["page"] and normalized(r["contains"]) in normalized(s["text"])), None)
                     for r in case.get("required", [])]
            found = sum(r is not None for r in ranks)
            complete = not sources if case["type"] == "missing_source" else found == len(ranks)
            results.append({"id": case["id"], "type": case["type"], "slice": case["slice"],
                            "split": case["split"], "families": case["families"], "complete": complete,
                            "found": found, "required": len(ranks), "ranks": ranks, "seconds": elapsed,
                            "context_chars": sum(len(s["text"]) for s in sources),
                            "retrieved": [[s["doc_id"], s["page"]] for s in sources]})
        store.require_fresh(root, project)
        if initial != fingerprint(root, project):
            raise ValueError("Benchmark inputs changed during scoring")
    if any(digest(Path(__file__).with_name(name)) != sha for name, sha in implementation.items()):
        raise ValueError("Evaluation implementation changed during scoring; rerun on frozen code")
    answerable = [r for r in results if r["type"] == "region"]
    missing = [r for r in results if r["type"] == "missing_source"]
    success = sum(r["complete"] for r in answerable)
    clusters = defaultdict(list)
    for r in answerable:
        clusters["|".join(r["families"])].append(int(r["complete"]))
    rng, cluster_means = random.Random(17), []
    keys = sorted(clusters)
    if keys:
        for _ in range(2000):
            sample = [v for key in rng.choices(keys, k=len(keys)) for v in clusters[key]]
            cluster_means.append(statistics.mean(sample))
        cluster_means.sort()
    warm = sorted(r["seconds"] for r in results[1:])
    return {"schema_version": 1, "implementation_hashes": implementation, "benchmark_hash": canonical_hash(cases), "index_fingerprint": initial,
            "configuration": project.settings.model_dump(), "split": split or "all", "cases": results,
            "region_cases": len(answerable), "complete_region_cases": success,
            "complete_evidence_recall": success / len(answerable) if answerable else None,
            "required_region_recall": sum(r["found"] for r in answerable) / max(1, sum(r["required"] for r in answerable)),
            "case_wilson_95": wilson(success, len(answerable)),
            "family_cluster_bootstrap_95": [cluster_means[50], cluster_means[1949]] if cluster_means else None,
            "missing_source_cases": len(missing), "correct_empty_scope": sum(r["complete"] for r in missing),
            "cold_first_query_seconds": results[0]["seconds"],
            "warm_p50_seconds": statistics.median(warm) if warm else None,
            "warm_p95_seconds": warm[min(len(warm)-1, int(len(warm)*.95))] if warm else None,
            "slices": {s: {"cases": sum(r["slice"] == s for r in results),
                           "complete": sum(r["complete"] for r in results if r["slice"] == s)} for s in sorted({r["slice"] for r in results})},
            "scope": "Evidence-region retrieval and missing-source scope handling only. Template-generated anchors and correlated families are reported; no answer-model, automatic parameter acceptance or hardware accuracy score."}

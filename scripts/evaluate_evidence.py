"""Evaluate retrieved evidence regions against held-out labels; never feed labels into retrieval."""

import argparse
import json
import statistics
import time
from pathlib import Path

from kicad_rag.config import atomic_json, load_project
from kicad_rag.ingest import normalized
from kicad_rag.rag import build_bundle
from kicad_rag.store import Store

p = argparse.ArgumentParser()
p.add_argument("--project", type=Path, required=True)
p.add_argument("--cases", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--lexical-only", action="store_true")
a = p.parse_args()
root = a.project.resolve()
project = load_project(root)
cases = [json.loads(line) for line in a.cases.read_text().splitlines() if line.strip()]
results = []
with Store(root) as store:
    store.require_fresh(root, project)
    index = store.meta("fingerprint")
    for case in cases:
        required = case["required_regions"]
        for region in required:
            if normalized(region["contains"]) not in normalized(
                store.page(region["document"], region["page"])
            ):
                raise ValueError(f"Invalid gold region in case {case['id']}; do not score stale annotations")
        start = time.perf_counter()
        bundle = build_bundle(root, project, case["question"], case.get("refs"), lexical_only=a.lexical_only)
        elapsed = time.perf_counter() - start
        sources = [s for s in bundle["sources"] if "doc_id" in s]
        ranks = []
        for region in required:
            ranks.append(
                next(
                    (
                        i
                        for i, s in enumerate(sources, 1)
                        if s["doc_id"] == region["document"]
                        and s["page"] == region["page"]
                        and normalized(region["contains"]) in normalized(s["text"])
                    ),
                    None,
                )
            )
        found = sum(r is not None for r in ranks)
        results.append(
            {
                "id": case["id"],
                "slice": case["slice"],
                "complete": found == len(required),
                "required": len(required),
                "found": found,
                "ranks": ranks,
                "seconds": elapsed,
                "retrieved": [[s["doc_id"], s["page"], s["id"]] for s in sources],
            }
        )
latencies = sorted(r["seconds"] for r in results)
report = {
    "schema_version": 1,
    "index_fingerprint": index,
    "configuration": project.settings.model_dump(),
    "cases": results,
    "total": len(results),
    "complete": sum(r["complete"] for r in results),
    "region_recall": sum(r["found"] for r in results) / max(1, sum(r["required"] for r in results)),
    "p50_seconds": statistics.median(latencies),
    "p95_seconds": latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))],
    "slices": {
        s: {
            "cases": sum(r["slice"] == s for r in results),
            "complete": sum(r["complete"] for r in results if r["slice"] == s),
        }
        for s in sorted({r["slice"] for r in results})
    },
    "scope": "Text-reviewed pilot region retrieval only; no model-answer, parameter-acceptance, hardware or population accuracy claim.",
}
atomic_json(a.output, report)
print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2))

"""Evaluate expected document coverage, not answer correctness or confidence."""
import argparse
import json
from pathlib import Path

from kicad_rag.config import atomic_json, load_project
from kicad_rag.rag import build_bundle

p = argparse.ArgumentParser()
p.add_argument("--project", type=Path, default=Path("examples/demo"))
p.add_argument("--cases", type=Path)
p.add_argument("--output", type=Path)
p.add_argument("--lexical-only", action="store_true")
args = p.parse_args()
root = args.project.resolve()
project = load_project(root)
cases = args.cases or root / "eval.jsonl"
results = []
for line in cases.read_text().splitlines():
    if not line.strip():
        continue
    case = json.loads(line)
    bundle = build_bundle(root, project, case["question"], case.get("refs"), lexical_only=args.lexical_only)
    actual = {s["doc_id"] for s in bundle["sources"] if "doc_id" in s}
    expected = set(case["expected_documents"])
    success = not actual if case.get("expect_empty") else expected.issubset(actual)
    results.append({"question": case["question"], "expected": sorted(expected),
                    "retrieved": sorted(actual), "passed": success})
report = {"cases": results, "passed": sum(r["passed"] for r in results), "total": len(results),
          "scope": "Tiny project-specific retrieval fixture. Does not measure real-model reasoning or general circuit competence."}
if args.output:
    atomic_json(args.output, report)
print(json.dumps(report, indent=2))
raise SystemExit(0 if all(r["passed"] for r in results) else 1)

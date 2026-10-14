import argparse
import json
from pathlib import Path

from kicad_rag.benchmark import evaluate_cases
from kicad_rag.config import atomic_json, load_project

parser = argparse.ArgumentParser()
parser.add_argument("project", type=Path)
parser.add_argument("cases", type=Path)
parser.add_argument("--split", choices=["development", "calibration", "holdout"])
parser.add_argument("--lexical-only", action="store_true")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
result = evaluate_cases(
    args.project, load_project(args.project), cases, split=args.split, lexical_only=args.lexical_only
)
atomic_json(args.output, result)
print(json.dumps({k: v for k, v in result.items() if k != "cases"}, indent=2))

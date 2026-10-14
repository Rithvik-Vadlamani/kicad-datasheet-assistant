"""Read a real PDF and export a real schematic into an isolated integration workspace."""
import argparse
import json
import shutil
from pathlib import Path

from kicad_rag.config import Document, Project, save_project
from kicad_rag.kicad import Graph, export_schematic
from kicad_rag.store import index_project

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pdf", type=Path, required=True)
parser.add_argument("--schematic", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.output.resolve()
if (root / "project.json").exists():
    raise SystemExit("Use a fresh output folder for this integration test.")
root.mkdir(parents=True, exist_ok=True)
shutil.copyfile(args.pdf, root / "source.pdf")
project = Project(name="Independent PDF and KiCad ingestion fixtures", documents=[
    Document(id="source", path="source.pdf", title=args.pdf.stem)])
save_project(root, project)
export_schematic(root, project, args.schematic)
graph = Graph.load(root, project)
print(json.dumps({"components": len(graph.components), "nets": len(graph.nets),
                  "index": index_project(root, project)}, indent=2))

"""Recreate the pinned primary-source corpus. Updated PDF bytes require annotation review."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from kicad_rag.config import Document, Project, Settings, atomic_json, digest, save_project


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).parents[1] / "examples/evaluation/expanded-manifest.json",
    )
    args = parser.parse_args()
    if (args.output / "project.json").exists():
        raise ValueError("Use a new benchmark directory to preserve existing projects")
    sources = json.loads(args.manifest.read_text())["sources"]
    folder = args.output / "datasheets"
    folder.mkdir(parents=True, exist_ok=True)

    def fetch(entry):
        target = folder / f"{entry['id']}.pdf"
        try:
            if not target.exists():
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    response = client.get(entry["url"])
                    response.raise_for_status()
                    if not response.content.startswith(b"%PDF") or len(response.content) > 25_000_000:
                        raise ValueError("Response is not a bounded PDF")
                    # Fail before promoting a different source revision.
                    import hashlib

                    if hashlib.sha256(response.content).hexdigest() != entry["sha256"]:
                        raise ValueError("Manufacturer PDF revision changed; review new source and labels")
                    target.write_bytes(response.content)
            if digest(target) != entry["sha256"]:
                raise ValueError("Local source bytes differ from the pinned benchmark")
            return {
                **entry,
                "path": str(target.relative_to(args.output)),
                "status": "verified_original_bytes",
            }
        except Exception as error:
            return {**entry, "error": str(error)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, sources))
    atomic_json(args.output / "corpus-downloads.json", results)
    if any("error" in r for r in results):
        print(json.dumps({"failures": [r for r in results if "error" in r]}, indent=2))
        raise ValueError(
            "Corpus incomplete; no project registered. Keep original snapshots when sources change."
        )
    project = Project(
        name="Multi-manufacturer evidence benchmark",
        settings=Settings(restore_parent_context=True),
        documents=[
            Document(
                id=r["id"],
                path=r["path"],
                title=r["parts"][0] + " manufacturer datasheet",
                parts=r["parts"],
                revision="sha256:" + r["sha256"],
                source_url=r["url"],
            )
            for r in results
        ],
    )
    save_project(args.output, project)
    print(f"Registered {len(results)} hash-verified PDFs. Run index, then evaluate_benchmark.py.")


if __name__ == "__main__":
    main()

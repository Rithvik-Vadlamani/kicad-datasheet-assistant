"""Build reproducible section/row-location probes, clearly separated from engineering answer labels.
Review emitted anchors before freezing the JSONL. Never run this in the retrieval path.
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from kicad_rag.config import atomic_json, canonical_hash, load_project
from kicad_rag.store import Store

FAMILIES = {
    "ina240": ("current_sense", "calibration"),
    "lm76005": ("buck", "development"),
    "csd19531q5a": ("mosfet", "development"),
    "tlv9062": ("opamp", "development"),
    "tps7a20": ("linear_regulator", "calibration"),
    "tps62160": ("buck", "development"),
    "sn74lvc1g17": ("logic", "holdout"),
    "tmp117": ("temperature_sensor", "calibration"),
    "ads1115": ("adc", "holdout"),
    "tca9548a": ("i2c_switch", "holdout"),
    "irf540": ("mosfet", "development"),
    "1n4001": ("diode", "holdout"),
    "lm317": ("linear_regulator", "calibration"),
    "2n3904": ("bjt", "holdout"),
    "ap2112": ("linear_regulator", "calibration"),
    "bav99": ("diode", "holdout"),
    "lm324": ("opamp", "development"),
    "lm393": ("comparator", "holdout"),
    "tl431": ("shunt_reference", "holdout"),
    "2n7000": ("mosfet", "development"),
    "ap3211": ("buck", "development"),
    "pam8403": ("audio", "holdout"),
    "az1117": ("linear_regulator", "calibration"),
    "lm5155": ("boost", "holdout"),
    "iso7741": ("isolator", "holdout"),
}
# Questions name the engineering topic, not the expected numerical answer or page.
TOPICS = [
    (
        "thermal",
        r"junction.{0,12}ambient|thermal resistance|thermal impedance",
        "thermal resistance specifications and their conditions",
    ),
    ("limits", r"(absolute maximum ratings|maximum ratings)", "the absolute maximum rating table"),
    ("conditions", r"recommended operating conditions", "recommended operating limits"),
    ("electrical", r"electrical characteristics", "the electrical characteristics and their test conditions"),
    (
        "timing",
        r"(switching characteristics|propagation delay|reverse recovery time|rise time|switching frequency)",
        "switching timing or frequency specifications",
    ),
    (
        "pinout",
        r"(pin functions|pin description|pin assignments|pin configuration|pin connections|pin connection)",
        "the physical pin assignments",
    ),
    (
        "layout",
        r"(layout guidelines|layout recommendations|layout considerations|pcb layout)",
        "PCB layout recommendations",
    ),
    (
        "temperature",
        r"(operating junction|junction temperature|storage temperature|operating temperature)",
        "junction or operating temperature limits",
    ),
    (
        "package",
        r"(package dimensions|package outline|package information|mechanical data|mechanical dimensions)",
        "the package outline or mechanical details",
    ),
    (
        "voltage",
        r"(supply voltage|drain.source voltage|collector.emitter voltage|repetitive peak reverse voltage)",
        "supply or terminal voltage limits",
    ),
    (
        "current",
        r"(continuous drain current|output current|collector current|forward rectified current|forward current)",
        "output or terminal current ratings",
    ),
    (
        "capacitance",
        r"(input capacitance|output capacitance|total capacitance)",
        "capacitance specifications",
    ),
    (
        "threshold",
        r"(threshold voltage|input offset voltage|forward voltage|saturation voltage)",
        "threshold, offset, forward or saturation voltage specifications",
    ),
    (
        "power",
        r"(power dissipation|power consumption|quiescent current|supply current)",
        "power dissipation or supply-current specifications",
    ),
    (
        "application",
        r"(application information|application and implementation|applications information)",
        "application guidance",
    ),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    ROOT = args.project
    project = load_project(ROOT)
    docs = {d.id: d for d in project.documents}
    rows = []
    by_doc = defaultdict(list)
    audit = []
    with Store(ROOT) as store:
        store.require_fresh(ROOT, project)
        for doc in project.documents:
            family, split = FAMILIES[doc.id]
            selected = []
            anchors = set()
            pages = store.db.execute(
                "SELECT page,text FROM pages WHERE doc_id=? ORDER BY page", (doc.id,)
            ).fetchall()
            for slice_, pattern, topic in TOPICS:
                matches = []
                for page, text in pages:
                    for line in text.splitlines():
                        line = line.strip()
                        if (
                            re.search(pattern.replace(" ", r"\s*"), line, re.I)
                            and 10 <= len(line) <= 230
                            and not re.search(r"\.{3,}", line)
                        ):
                            matches.append((page, line))
                if not matches:
                    continue
                # Prefer the actual heading/table row over document table-of-contents references.
                if slice_ in {"limits", "conditions", "electrical", "package", "layout", "application"}:
                    heading = re.compile(
                        r"^(?:[0-9]+(?:\.[0-9]+)*\s*)?(?:" + pattern.replace(" ", r"\s*") + r")", re.I
                    )
                    matches = [(p, line) for p, line in matches if heading.search(line)]
                if slice_ == "timing":
                    matches = [
                        (p, line)
                        for p, line in matches
                        if re.match(r"^(?:[0-9]+(?:\.[0-9]+)*\s*)?switching\s*characteristics", line, re.I)
                        or (re.search(r"\d", line) and re.search(r"\d[.,]?\d*\s*(?:ns|µs|μs|us|MHz|kHz)\b", line))
                    ]
                if not matches:
                    continue

                def anchor_rank(item, slice_=slice_):
                    page, line = item
                    if slice_ == "thermal":
                        return (
                            not bool(re.search(r"junction.{0,12}ambient", line, re.I)),
                            not bool(re.search(r"\d", line)),
                            page,
                        )
                    return (
                        bool(re.search(r"[•]|independent|features|may affect|beyond those", line, re.I)),
                        page,
                    )

                page, anchor = min(matches, key=anchor_rank)
                if (page, anchor) in anchors:
                    continue
                anchors.add((page, anchor))
                case = {
                    "id": f"{doc.id}-{slice_}",
                    "type": "region",
                    "question": f"Locate the evidence in the {doc.parts[0]} datasheet for {topic}.",
                    "parts": doc.parts,
                    "families": [family],
                    "split": split,
                    "slice": slice_,
                    "annotation": "programmatic anchor; location-only; original context needs engineering review",
                    "required": [
                        {
                            "document": doc.id,
                            "page": page,
                            "contains": anchor,
                            "sha256": doc.revision.removeprefix("sha256:"),
                        }
                    ],
                }
                selected.append(case)
                audit.append(f"{case['id']} | p{page} | {anchor}")
                if len(selected) == 6:
                    break
            if len(selected) != 6:
                raise ValueError(f"{doc.id}: only {len(selected)} distinct topics")
            rows.extend(selected)
            by_doc[doc.id] = selected
    for doc in project.documents:
        family, split = FAMILIES[doc.id]
        peers = [d for d in project.documents if d.id != doc.id and FAMILIES[d.id][0] == family]
        a = by_doc[doc.id][0]
        if peers:
            peer = peers[0]
            b = by_doc[peer.id][1]
            parts = doc.parts + peer.parts
            q = f"Gather both sources for a comparison: {a['question']} Also {b['question']}"
        else:
            b = by_doc[doc.id][-1]
            parts = doc.parts
            q = f"Gather both prerequisites: {a['question']} Also {b['question']}"
        rows.append(
            {
                "id": doc.id + "-prerequisites",
                "type": "region",
                "question": q,
                "parts": parts,
                "families": [family],
                "split": split,
                "slice": "multiple_prerequisites",
                "required": a["required"] + b["required"],
                "annotation": "Location prerequisites only; not a comparison answer or numerical conclusion",
            }
        )
        absent = doc.parts[0] + "-UNREGISTERED-TEST"
        rows.append(
            {
                "id": doc.id + "-missing-source",
                "type": "missing_source",
                "question": f"What is the maximum operating voltage of {absent}?",
                "parts": [absent],
                "families": [family],
                "split": split,
                "slice": "missing_source",
                "annotation": "Synthetic nonexistent exact part, tests no substitution with a similar registered part",
                "required": [],
            }
        )
    output = Path("examples/evaluation/expanded-200.jsonl")
    output.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    Path("examples/evaluation/expanded-anchor-review.txt").write_text("\n".join(audit) + "\n")
    atomic_json(
        Path("examples/evaluation/expanded-manifest.json"),
        {
            "cases": len(rows),
            "documents": len(docs),
            "benchmark_hash": canonical_hash(rows),
            "splits": {
                s: sum(r["split"] == s for r in rows) for s in ["development", "calibration", "holdout"]
            },
            "label_method": "Deterministic row/section anchors. These are navigation probes, not independently human-labeled engineering answers.",
            "family_splits": FAMILIES,
            "sources": [
                {
                    "id": d.id,
                    "url": d.source_url,
                    "sha256": d.revision.removeprefix("sha256:"),
                    "parts": d.parts,
                }
                for d in docs.values()
            ],
        },
    )
    print(f"{len(rows)} cases; inspect expanded-anchor-review.txt before evaluation")


if __name__ == "__main__":
    main()

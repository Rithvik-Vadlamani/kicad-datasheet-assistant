"""Generate review candidates; never silently resolve applicability or accept a model guess."""

import re
from decimal import Decimal

from .config import canonical_hash
from .evidence import NUMBER, QUANTITY, UNITS, ParameterFact, Source, quantity
from .ingest import normalized
from .retrieval import retrieve
from .store import Store, fingerprint


def propose_parameters(root, project, request, *, lexical_only=False):
    results = []
    with Store(root) as store:
        store.require_fresh(root, project)
        before = fingerprint(root, project)
        for obligation in request.obligations:
            candidate = next(c for c in request.candidates if c.ref == obligation.ref)
            context = {"components": {candidate.ref: {"part": candidate.part}}, "seeds": [candidate.ref]}
            hits, warnings = retrieve(
                store, project, obligation.query, context, top_k=8, lexical_only=lexical_only
            )
            proposals, seen = [], set()
            for hit in hits:
                doc = next(d for d in project.documents if d.id == hit["doc_id"])
                if doc.revision != candidate.revision or candidate.part.casefold() not in {
                    p.casefold() for p in doc.parts
                }:
                    continue
                if (doc.id, hit["page"]) in seen:
                    continue
                seen.add((doc.id, hit["page"]))
                layout = store.page_layout(doc.id, hit["page"])

                # Numeric tokens alone do not establish a min/max/typ column. A candidate carries
                # that ambiguity explicitly and keeps conditions distinct from the request.
                def add(
                    value,
                    unit,
                    quote,
                    obligation=obligation,
                    doc=doc,
                    hit=hit,
                    candidate=candidate,
                    proposals=proposals,
                    **binding,
                ):
                    if len(quote) < 8 or quantity(value, unit)[0] != UNITS[obligation.unit][0]:
                        return
                    source = Source(
                        document=doc.id, sha256=hit["sha256"], page=hit["page"], quote=quote, **binding
                    )
                    identifier = canonical_hash([obligation.id, source.model_dump(), value, unit])[:24]
                    fact = ParameterFact(
                        id=f"proposal-{identifier}",
                        candidate=candidate,
                        parameter=obligation.parameter,
                        category=obligation.category,
                        bound=obligation.bound,
                        value=value,
                        unit=unit,
                        conditions={"source_context_unreviewed": quote},
                        source=source,
                        state="needs_review",
                    )
                    proposals.append(
                        {
                            "fact": fact.model_dump(),
                            "requested_conditions": obligation.conditions,
                            "unverified_fields": ["parameter", "category", "bound", "package", "conditions"],
                            "review_instruction": "Inspect the original row, column, section heading, footnotes and package. Replace conditions with the verified applicability before accepting.",
                        }
                    )

                for line in layout["text"].splitlines():
                    for match in QUANTITY.finditer(line):
                        add(
                            float(Decimal(match.group(1).replace("−", "-").replace("–", "-"))),
                            match.group(2),
                            line.strip(),
                        )
                for ti, table in enumerate(layout.get("table_regions", [])):
                    rows = table["rows"]
                    # Table proposals require an explicit unit cell and an explicit requested-bound
                    # header in one of the first four rows. Merged or ambiguous headers are deferred.
                    columns = set()
                    for header in rows[:4]:
                        columns.update(
                            i
                            for i, cell in enumerate(header)
                            if normalized(str(cell or "")).strip(".") == obligation.bound
                        )
                    if len(columns) != 1:
                        continue
                    column = next(iter(columns))
                    for ri, row in enumerate(rows):
                        if column >= len(row) or row[column] is None:
                            continue
                        units = [str(c).strip() for c in row if str(c).strip() in UNITS]
                        if len(set(units)) != 1:
                            continue
                        numbers = list(re.finditer(NUMBER, row[column]))
                        if len(numbers) != 1:
                            continue
                        quote = " | ".join(re.sub(r"\s+", " ", str(c or "")).strip() for c in row)
                        add(
                            float(Decimal(numbers[0].group().replace("−", "-").replace("–", "-"))),
                            units[0],
                            quote,
                            table=ti,
                            row=ri,
                            column=column,
                            bbox=table["bbox"],
                        )
            # Stable deduplication, bounded review workload. Truncation is visible.
            unique = {p["fact"]["id"]: p for p in proposals}
            results.append(
                {
                    "obligation": obligation.model_dump(),
                    "state": "needs_review" if unique else "missing",
                    "proposals": list(unique.values())[:40],
                    "total_candidates": len(unique),
                    "truncated": len(unique) > 40,
                    "warnings": warnings,
                }
            )
        store.require_fresh(root, project)
        if before != fingerprint(root, project):
            raise ValueError("Sources changed during extraction")
    return {
        "schema_version": 1,
        "index_fingerprint": before,
        "request": request.model_dump(),
        "results": results,
        "automatically_accepted": 0,
        "scope": "Source-bound numeric review candidates. Category/bound/package/conditions are not inferred truths; no engineering acceptance occurs.",
    }

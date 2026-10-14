"""Versioned, fail-closed evidence exchange. No model output is accepted as an engineering fact."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .config import StrictModel, atomic_json, canonical_hash, digest, local_path
from .ingest import normalized
from .retrieval import retrieve
from .store import Store, fingerprint

# Explicit case-sensitive SI units: mA is not MA; degrees C are not kelvin differences.
UNITS = {
    "V": ("voltage", 1),
    "mV": ("voltage", 0.001),
    "A": ("current", 1),
    "mA": ("current", 0.001),
    "uA": ("current", 0.000001),
    "µA": ("current", 0.000001),
    "μA": ("current", 0.000001),
    "nA": ("current", 1e-9),
    "pA": ("current", 1e-12),
    "W": ("power", 1),
    "mW": ("power", 0.001),
    "ohm": ("resistance", 1),
    "Ω": ("resistance", 1),
    "Ω": ("resistance", 1),
    "mΩ": ("resistance", .001),
    "mΩ": ("resistance", .001),
    "kohm": ("resistance", 1000),
    "kΩ": ("resistance", 1000),
    "Mohm": ("resistance", 1000000),
    "MΩ": ("resistance", 1000000),
    "F": ("capacitance", 1),
    "uF": ("capacitance", 0.000001),
    "µF": ("capacitance", 0.000001),
    "μF": ("capacitance", 0.000001),
    "nF": ("capacitance", 1e-9),
    "pF": ("capacitance", 1e-12),
    "H": ("inductance", 1),
    "uH": ("inductance", 1e-6),
    "µH": ("inductance", 1e-6),
    "μH": ("inductance", 1e-6),
    "nH": ("inductance", 1e-9),
    "nC": ("charge", 1e-9),
    "µC": ("charge", 1e-6),
    "μC": ("charge", 1e-6),
    "s": ("time", 1),
    "ms": ("time", 0.001),
    "us": ("time", 1e-6),
    "µs": ("time", 1e-6),
    "μs": ("time", 1e-6),
    "ps": ("time", 1e-12),
    "ns": ("time", 1e-9),
    "Hz": ("frequency", 1),
    "kHz": ("frequency", 1000),
    "MHz": ("frequency", 1e6),
    "GHz": ("frequency", 1e9),
    "degC": ("temperature_c", 1),
    "°C": ("temperature_c", 1),
    "K/W": ("thermal_resistance", 1),
    "°C/W": ("thermal_resistance", 1),
    "degC/W": ("thermal_resistance", 1),
    "%": ("percent", 1),
}
NUMBER = r"(?<![\w.])[-+−–]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
QUANTITY = re.compile(
    f"({NUMBER})\\s*({'|'.join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))})(?![A-Za-z/])"
)


def quantity(value, unit):
    if unit not in UNITS:
        raise ValueError(f"Unsupported unit {unit}; do not silently convert it")
    dimension, scale = UNITS[unit]
    return dimension, Decimal(str(value).replace("−", "-").replace("–", "-")) * Decimal(str(scale))


def quantities(text):
    return {quantity(m.group(1), m.group(2)) for m in QUANTITY.finditer(text)}


class Candidate(StrictModel):
    ref: str = Field(min_length=1)
    part: str = Field(min_length=1)
    package: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class Obligation(StrictModel):
    id: str = Field(min_length=1)
    ref: str
    parameter: str = Field(min_length=1)
    category: Literal["recommended", "absolute_maximum", "electrical", "timing", "thermal", "mechanical"]
    bound: Literal["min", "typ", "max", "nominal"]
    unit: str
    conditions: dict[str, str] = Field(min_length=1)
    query: str = Field(min_length=3)


class EvidenceRequest(StrictModel):
    schema_version: Literal[1] = 1
    design_id: str = Field(min_length=1)
    topology: Literal["provisional", "exported"] = "provisional"
    candidates: list[Candidate] = Field(min_length=1)
    obligations: list[Obligation] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        refs = [c.ref for c in self.candidates]
        ids = [o.id for o in self.obligations]
        if len(refs) != len(set(refs)) or len(ids) != len(set(ids)):
            raise ValueError("Duplicate candidate reference or obligation ID")
        for o in self.obligations:
            if o.ref not in refs or o.unit not in UNITS:
                raise ValueError("Obligation has unknown reference or unsupported unit")
        return self


class Source(StrictModel):
    document: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    quote: str = Field(min_length=8)
    bbox: tuple[float, float, float, float] | None = None
    table: int | None = Field(default=None, ge=0)
    row: int | None = Field(default=None, ge=0)
    column: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid(self):
        if self.bbox and not (0 <= self.bbox[0] < self.bbox[2] and 0 <= self.bbox[1] < self.bbox[3]):
            raise ValueError("Invalid page rectangle (PDF points, top-left origin)")
        cells = [self.table, self.row, self.column]
        if any(v is not None for v in cells) and not all(v is not None for v in cells):
            raise ValueError("A table binding requires table, row and column")
        return self


class Review(StrictModel):
    actor: str = Field(min_length=1)
    method: Literal["original_page_review"]
    rationale: str = Field(min_length=20)
    timestamp: str
    binding_hash: str


class ParameterFact(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1)
    candidate: Candidate
    parameter: str
    category: Literal["recommended", "absolute_maximum", "electrical", "timing", "thermal", "mechanical"]
    bound: Literal["min", "typ", "max", "nominal"]
    value: float
    unit: str
    conditions: dict[str, str] = Field(min_length=1)
    source: Source
    state: Literal["extracted", "needs_review", "accepted_for_use", "rejected", "superseded"] = "extracted"
    review: Review | None = None

    @model_validator(mode="after")
    def valid(self):
        quantity(self.value, self.unit)
        if self.state == "accepted_for_use" and self.review is None:
            raise ValueError("Accepted facts require a bound original-page review")
        return self

    def binding(self):
        return canonical_hash(self.model_dump(exclude={"state", "review"}))


def facts(root):
    path = root / "parameters.json"
    data = json.loads(path.read_text()) if path.exists() else []
    result = [ParameterFact.model_validate(f) for f in data]
    if len({f.id for f in result}) != len(result):
        raise ValueError("Duplicate parameter fact IDs")
    return result


def verify_fact(root, project, store, fact, *, require_review=True):
    issues = []
    doc = next((d for d in project.documents if d.id == fact.source.document), None)
    if not doc:
        return ["source_not_registered"]
    if digest(local_path(root, doc.path)) != fact.source.sha256:
        issues.append("source_hash_changed")
    if fact.candidate.part.casefold() not in [p.casefold() for p in doc.parts]:
        issues.append("part_not_mapped")
    if fact.candidate.revision != doc.revision or doc.revision == "unspecified":
        issues.append("revision_not_bound")
    try:
        page = store.page(doc.id, fact.source.page)
        if normalized(fact.source.quote) not in normalized(page):
            issues.append("quote_not_on_page")
        supported = quantity(fact.value, fact.unit) in quantities(fact.source.quote)
        if not supported and fact.source.table is not None:
            region = store.page_layout(doc.id, fact.source.page)
            try:
                table = region["table_regions"][fact.source.table]
                cell = table["rows"][fact.source.row][fact.source.column]
                # Table units/column semantics are explicitly signed off in original-page review.
                supported = cell is not None and any(
                    Decimal(m.group().replace("−", "-").replace("–", "-")) == Decimal(str(fact.value))
                    for m in re.finditer(NUMBER, cell)
                )
            except (KeyError, IndexError, TypeError):
                issues.append("table_cell_missing")
        if not supported:
            issues.append("numeric_value_not_bound_to_quote_or_cell")
        if fact.source.bbox:
            layout = store.page_layout(doc.id, fact.source.page)
            if (
                not layout.get("width")
                or fact.source.bbox[2] > layout["width"]
                or fact.source.bbox[3] > layout["height"]
            ):
                issues.append("region_outside_page")
    except ValueError:
        issues.append("source_page_missing")
    if require_review and (
        fact.state != "accepted_for_use" or fact.review is None or fact.review.binding_hash != fact.binding()
    ):
        issues.append("original_page_review_missing_or_stale")
    return issues


def review_fact(root, project, fact_id, actor, rationale):
    records = facts(root)
    fact = next((f for f in records if f.id == fact_id), None)
    if fact is None:
        raise ValueError(f"Unknown fact {fact_id}")
    with Store(root) as store:
        store.require_fresh(root, project)
        issues = verify_fact(root, project, store, fact, require_review=False)
    if any("unreviewed" in key or "unverified" in key for key in fact.conditions):
        issues.append("replace_unreviewed_conditions_after_original_source_review")
    if issues:
        raise ValueError("Cannot accept fact: " + ", ".join(issues))
    fact.review = Review(
        actor=actor,
        method="original_page_review",
        rationale=rationale,
        timestamp=datetime.now(timezone.utc).isoformat(),
        binding_hash=fact.binding(),
    )
    fact.state = "accepted_for_use"
    atomic_json(root / "parameters.json", [f.model_dump() for f in records])
    return fact.model_dump()


def evidence_packet(root, project, request, *, lexical_only=False):
    from .kicad import Graph

    candidate_map = {c.ref: c for c in request.candidates}
    if request.topology == "exported":
        if not project.netlist:
            raise ValueError("Exported topology requires an actual netlist")
        graph = Graph.load(root, project)
        for c in request.candidates:
            if (
                c.ref not in graph.components
                or graph.components[c.ref]["part"].casefold() != c.part.casefold()
            ):
                raise ValueError(f"Candidate {c.ref} does not match the exported netlist part")
    records, results = facts(root), []
    with Store(root) as store:
        store.require_fresh(root, project)
        for o in request.obligations:
            candidate = candidate_map[o.ref]
            context = {"components": {candidate.ref: {"part": candidate.part}}, "seeds": [candidate.ref]}
            hits, warnings = retrieve(store, project, o.query, context, top_k=6, lexical_only=lexical_only)
            matching, unusable = [], []
            for f in records:
                if f.candidate.ref != o.ref or f.parameter != o.parameter:
                    continue
                problems = verify_fact(root, project, store, f)
                applicable = (
                    f.candidate == candidate
                    and f.category == o.category
                    and f.bound == o.bound
                    and f.conditions == o.conditions
                    and UNITS[f.unit][0] == UNITS[o.unit][0]
                )
                if not applicable:
                    problems.append("not_applicable_to_exact_request")
                if problems:
                    unusable.append({"id": f.id, "issues": problems})
                else:
                    matching.append(f)
            values = {quantity(f.value, f.unit) for f in matching}
            state = (
                "conflicting"
                if len(values) > 1
                else "found"
                if matching
                else "needs_review"
                if hits or unusable
                else "missing"
            )
            results.append(
                {
                    "id": o.id,
                    "state": state,
                    "facts": [f.model_dump() for f in matching],
                    "unusable_facts": unusable,
                    "retrieved_candidates": hits,
                    "warnings": warnings,
                }
            )
        current = store.meta("fingerprint")
    packet = {
        "schema_version": 1,
        "request": request.model_dump(),
        "request_hash": canonical_hash(request.model_dump()),
        "index_fingerprint": current,
        "parameters_hash": canonical_hash([f.model_dump() for f in records]),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "obligations": results,
        "complete": all(r["state"] == "found" for r in results),
        "meaning": "Complete means every requested fact was accepted for these exact conditions; it is not design approval.",
    }
    if current != fingerprint(root, project) or packet["parameters_hash"] != canonical_hash(
        [f.model_dump() for f in facts(root)]
    ):
        raise ValueError("Sources or parameter reviews changed during evidence resolution")
    packet["packet_hash"] = canonical_hash(packet)
    return packet


def verify_packet(root, project, packet):
    payload = {k: v for k, v in packet.items() if k != "packet_hash"}
    problems = []
    if packet.get("packet_hash") != canonical_hash(payload):
        problems.append("packet_changed")
    request = EvidenceRequest.model_validate(packet["request"])
    if packet.get("request_hash") != canonical_hash(request.model_dump()):
        problems.append("request_changed")
    if packet.get("index_fingerprint") != fingerprint(root, project):
        problems.append("project_or_sources_changed")
    if packet.get("parameters_hash") != canonical_hash([f.model_dump() for f in facts(root)]):
        problems.append("parameter_reviews_changed")
    if not packet.get("complete") or any(r["state"] != "found" for r in packet["obligations"]):
        problems.append("evidence_incomplete")
    current_facts = {f.id: f for f in facts(root)}
    candidates = {c.ref: c for c in request.candidates}
    obligations = {o.id: o for o in request.obligations}
    results = packet["obligations"]
    if len(results) != len(obligations) or {r["id"] for r in results} != set(obligations):
        problems.append("obligation_set_changed")
    if problems:
        return {"valid": False, "issues": problems}
    with Store(root) as store:
        store.require_fresh(root, project)
        for result in results:
            obligation = obligations.get(result["id"])
            if not result.get("facts"):
                problems.append("obligation_has_no_facts")
            for data in result.get("facts", []):
                fact = ParameterFact.model_validate(data)
                if current_facts.get(fact.id) != fact or verify_fact(root, project, store, fact):
                    problems.append("fact_not_currently_accepted")
                if obligation is None or (
                    fact.candidate != candidates[obligation.ref]
                    or fact.parameter != obligation.parameter
                    or fact.bound != obligation.bound
                    or fact.category != obligation.category
                    or fact.conditions != obligation.conditions
                    or UNITS[fact.unit][0] != UNITS[obligation.unit][0]
                ):
                    problems.append("fact_does_not_satisfy_obligation")
        # A newly accepted contradictory fact cannot be concealed by removing it from the packet.
        for o in request.obligations:
            matches = [
                f
                for f in current_facts.values()
                if f.candidate == candidates[o.ref]
                and f.parameter == o.parameter
                and f.bound == o.bound
                and f.category == o.category
                and f.conditions == o.conditions
                and not verify_fact(root, project, store, f)
            ]
            if len({quantity(f.value, f.unit) for f in matches}) > 1:
                problems.append("conflicting_accepted_facts")
    return {
        "valid": not problems,
        "issues": problems,
        "note": "Hashes detect changes; they are not cryptographic reviewer identity or design approval.",
    }


def impact(root, project):
    with Store(root) as store:
        return [
            {"fact": f.id, "ref": f.candidate.ref, "issues": verify_fact(root, project, store, f)}
            for f in facts(root)
        ]


def calculate_packet(root, project, packet, design):
    """Reuse the existing calculator engine, with stricter source bindings at this interface."""
    from .checks import evaluate
    from .kicad import Graph

    verified = verify_packet(root, project, packet)
    if not verified["valid"]:
        raise ValueError("Cannot calculate from evidence: " + ", ".join(verified["issues"]))
    accepted = {f["id"]: ParameterFact.model_validate(f) for o in packet["obligations"] for f in o["facts"]}
    for identifier, fact in design.facts.items():
        if fact.basis == "scenario":
            if not fact.reviewed:
                raise ValueError(f"Scenario {identifier} has not been explicitly reviewed")
            continue
        binding = accepted.get(identifier)
        if binding is None:
            raise ValueError(f"Calculation input {identifier} is not an accepted packet fact")
        if (
            quantity(fact.value, fact.unit) != quantity(binding.value, binding.unit)
            or fact.document != binding.source.document
            or fact.page != binding.source.page
            or fact.quote != binding.source.quote
            or fact.ref != binding.candidate.ref
            or fact.condition != json.dumps(binding.conditions, sort_keys=True)
        ):
            raise ValueError(f"Calculation input {identifier} differs from accepted evidence")
    with Store(root) as store:
        store.require_fresh(root, project)
        reports = evaluate(design, Graph.load(root, project), project, store)
    return {
        "schema_version": 1,
        "packet_hash": packet["packet_hash"],
        "design_hash": canonical_hash(design.model_dump()),
        "checks": reports,
        "status": "within_stated_bounds"
        if reports and all(r["status"] == "within_stated_bounds" for r in reports)
        else "unassessed_or_violation",
        "scope": "Configured calculations only; no full-circuit approval or simulation is implied.",
    }

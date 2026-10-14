from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .config import Project, StrictModel, local_path
from .ingest import normalized
from .kicad import Graph
from .store import Store


class Fact(StrictModel):
    value: float
    unit: Literal["V", "A", "W", "degC", "K/W", "ohm", "F", "s"]
    condition: str = Field(min_length=10)
    reviewed: bool = False
    basis: Literal["datasheet", "scenario"]
    document: str | None = None
    page: int | None = Field(default=None, ge=1)
    quote: str | None = None
    ref: str | None = None

    @model_validator(mode="after")
    def provenance(self):
        if self.basis == "datasheet" and (not self.document or not self.page or not self.quote or not self.ref):
            raise ValueError("Datasheet facts need document, PDF page, quote and component ref")
        return self


class Check(StrictModel):
    id: str
    kind: Literal["logic", "pin_voltage", "ldo_thermal", "power_budget", "i2c_pullup"]
    inputs: dict[str, str]
    endpoints: list[str] = Field(default_factory=list)
    conditions: str = Field(min_length=10)
    conditions_reviewed: bool = False


class Design(StrictModel):
    assumptions: list[str] = Field(default_factory=list)
    facts: dict[str, Fact] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("Check IDs must be unique")
        return self


UNITS = {
    "logic": {"voh_min": "V", "vol_max": "V", "vih_min": "V", "vil_max": "V"},
    "pin_voltage": {"applied_min": "V", "applied_max": "V", "recommended_min": "V",
                    "recommended_max": "V", "absolute_min": "V", "absolute_max": "V"},
    "ldo_thermal": {"vin_max": "V", "vout_min": "V", "iout_max": "A", "iq_max": "A",
                    "theta_ja": "K/W", "ambient_max": "degC", "junction_limit": "degC"},
    "power_budget": {"load_max": "A", "supply_available": "A"},
    "i2c_pullup": {"vpu_max": "V", "vol_max": "V", "iol_max": "A", "bus_cap_max": "F",
                   "rise_time_max": "s", "resistance_min": "ohm", "resistance_max": "ohm"},
}


def load_design(root: Path, project: Project) -> Design:
    path = local_path(root, project.design)
    return Design.model_validate_json(path.read_text()) if path.exists() else Design()


def evaluate(design: Design, graph: Graph, project: Project, store: Store) -> list[dict]:
    reports = []
    documents = {d.id: d for d in project.documents}
    for check in design.checks:
        report = {"id": check.id, "kind": check.kind, "status": "unassessed",
                  "conditions": check.conditions, "inputs": {}, "results": {}, "issues": [],
                  "scope": "Only the stated calculation and operating conditions; not circuit approval."}
        issues, values = report["issues"], {}
        required = UNITS[check.kind]
        if set(check.inputs) != set(required):
            issues.append(f"Expected exactly these input names: {', '.join(required)}")
        if not check.conditions_reviewed:
            issues.append("Check conditions have not been reviewed for compatibility and worst-case coverage.")
        for name, unit in required.items():
            fact = design.facts.get(check.inputs.get(name, ""))
            if fact is None:
                issues.append(f"Missing fact for {name}")
                continue
            report["inputs"][name] = fact.model_dump()
            if fact.unit != unit:
                issues.append(f"{name}: expected {unit}, got {fact.unit}; convert explicitly.")
            if not fact.reviewed:
                issues.append(f"{name}: value/source/conditions not reviewed")
            if fact.basis == "datasheet":
                if fact.document not in documents:
                    issues.append(f"{name}: unknown document {fact.document}")
                else:
                    try:
                        page = store.page(fact.document, fact.page)
                        if len(normalized(fact.quote)) < 8 or normalized(fact.quote) not in normalized(page):
                            issues.append(f"{name}: source quote is not present on the cited page")
                    except ValueError as error:
                        issues.append(str(error))
                    component = graph.components.get(fact.ref)
                    if not component or component["part"].casefold() not in [p.casefold() for p in documents[fact.document].parts]:
                        issues.append(f"{name}: datasheet is not mapped to component {fact.ref}")
            values[name] = fact.value
        expected_endpoints = 2 if check.kind == "logic" else 1 if check.kind == "pin_voltage" else None
        if expected_endpoints is not None and len(check.endpoints) != expected_endpoints:
            issues.append(f"{check.kind} needs exactly {expected_endpoints} endpoint(s)")
        endpoint_nets = []
        for endpoint in check.endpoints:
            try:
                net = graph.pin_net(endpoint)
                if not net:
                    issues.append(f"{endpoint} is not connected to a net")
                endpoint_nets.append(net)
                if graph.components[endpoint.partition('.')[0]]["dnp"]:
                    issues.append(f"{endpoint}: component is DNP")
            except ValueError as error:
                issues.append(str(error))
        if check.kind == "logic" and len(endpoint_nets) == 2 and endpoint_nets[0] != endpoint_nets[1]:
            issues.append("Driver and receiver pins are not on the same net. Check intervening components separately.")
        # Bind published limits to the claimed driver/receiver, not merely any mapped part.
        roles = {}
        if check.kind == "logic" and len(check.endpoints) == 2:
            roles = {k: check.endpoints[0 if k in ("voh_min", "vol_max") else 1].partition('.')[0]
                     for k in required}
        elif check.kind == "pin_voltage" and len(check.endpoints) == 1:
            roles = {k: check.endpoints[0].partition('.')[0] for k in required if k.startswith(("recommended", "absolute"))}
        for name, ref in roles.items():
            fact = design.facts.get(check.inputs.get(name, ""))
            if fact and fact.basis == "datasheet" and fact.ref != ref:
                issues.append(f"{name}: fact belongs to {fact.ref}, expected {ref}")
        if not issues:
            try:
                result, problems = calculate(check.kind, values)
                report["results"] = result
                report["issues"] = problems
                report["status"] = "violation" if problems else "within_stated_bounds"
            except ValueError as error:
                issues.append(str(error))
        reports.append(report)
    return reports


def calculate(kind: str, x: dict[str, float]) -> tuple[dict, list[str]]:
    issues = []
    if kind == "logic":
        if x["vol_max"] > x["voh_min"] or x["vil_max"] > x["vih_min"]:
            raise ValueError("Inconsistent high/low voltage bounds")
        high = x["voh_min"] - x["vih_min"]
        low = x["vil_max"] - x["vol_max"]
        if high <= 0:
            issues.append("No positive guaranteed high-level noise margin.")
        if low <= 0:
            issues.append("No positive guaranteed low-level noise margin.")
        return {"high_margin_V": high, "low_margin_V": low,
                "equations": "NMH = VOH(min) - VIH(min); NML = VIL(max) - VOL(max)",
                "not_checked": "Input overvoltage, startup, timing, loading and powered-off behavior."}, issues
    if kind == "pin_voltage":
        if not x["absolute_min"] <= x["recommended_min"] <= x["recommended_max"] <= x["absolute_max"]:
            raise ValueError("Recommended limits must lie within absolute limits")
        if x["applied_min"] > x["applied_max"]:
            raise ValueError("Applied minimum exceeds maximum")
        if x["applied_min"] < x["absolute_min"] or x["applied_max"] > x["absolute_max"]:
            issues.append("Applied range exceeds absolute maximum ratings: potential damage.")
        if x["applied_min"] < x["recommended_min"] or x["applied_max"] > x["recommended_max"]:
            issues.append("Applied range is outside recommended operating conditions.")
        return {"lower_operating_margin_V": x["applied_min"] - x["recommended_min"],
                "upper_operating_margin_V": x["recommended_max"] - x["applied_max"],
                "not_checked": "Transients and powered-off pin constraints unless included in the supplied bounds."}, issues
    if kind == "ldo_thermal":
        if x["vin_max"] < x["vout_min"] or any(x[k] < 0 for k in ("vin_max", "vout_min", "iout_max", "iq_max")) or x["theta_ja"] <= 0:
            raise ValueError("Invalid LDO voltage/current/thermal-resistance inputs")
        power = (x["vin_max"] - x["vout_min"]) * x["iout_max"] + x["vin_max"] * x["iq_max"]
        junction = x["ambient_max"] + power * x["theta_ja"]
        if junction >= x["junction_limit"]:
            issues.append("Estimated junction temperature reaches/exceeds the specified limit.")
        return {"dissipation_W": power, "estimated_junction_degC": junction,
                "thermal_margin_degC": x["junction_limit"] - junction,
                "equations": "P = (Vin - Vout) * Iout + Vin * Iq; Tj = Ta + P * thetaJA",
                "not_checked": "Dropout, stability, SOA, current limit, transients; thetaJA depends on PCB and airflow."}, issues
    if kind == "power_budget":
        if x["load_max"] < 0 or x["supply_available"] <= 0:
            raise ValueError("Load must be nonnegative and available supply current positive")
        margin = x["supply_available"] - x["load_max"]
        if margin <= 0:
            issues.append("No positive current headroom.")
        return {"current_margin_A": margin, "utilization_percent": 100 * x["load_max"] / x["supply_available"],
                "not_checked": "Load transients, regulator thermal derating and current sharing unless included in inputs."}, issues
    if kind == "i2c_pullup":
        if any(x[k] <= 0 for k in ("iol_max", "bus_cap_max", "rise_time_max", "resistance_min", "resistance_max")) or x["vpu_max"] <= x["vol_max"] or x["resistance_min"] > x["resistance_max"]:
            raise ValueError("Invalid pull-up voltage/current/capacitance/resistance/time bounds")
        rmin = (x["vpu_max"] - x["vol_max"]) / x["iol_max"]
        rmax = x["rise_time_max"] / (0.8473 * x["bus_cap_max"])
        if x["resistance_min"] < rmin:
            issues.append("Pull-up minimum resistance demands too much guaranteed sink current.")
        if x["resistance_max"] > rmax:
            issues.append("Pull-up maximum resistance exceeds the first-order rise-time bound.")
        if rmin > rmax:
            issues.append("No feasible resistance interval under the stated assumptions.")
        return {"required_resistance_min_ohm": rmin, "allowed_resistance_max_ohm": rmax,
                "estimated_rise_time_s": 0.8473 * x["resistance_max"] * x["bus_cap_max"],
                "equations": "Rmin = (Vpu - VOL) / IOL; Rmax = tr / (0.8473 * Cbus)",
                "not_checked": "All devices' voltage tolerance, leakage, distributed effects, parallel pull-ups and bus buffers."}, issues
    raise ValueError(f"Unsupported check: {kind}")

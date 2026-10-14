"""Regenerate the readable design.json teaching fixture; no model or network needed."""
from pathlib import Path

from kicad_rag.checks import Check, Design, Fact
from kicad_rag.config import atomic_json

root = Path(__file__).resolve().parents[1] / "examples" / "demo"
facts = {}


def published(name, value, unit, doc, ref, quote, condition):
    facts[name] = Fact(value=value, unit=unit, basis="datasheet", document=doc, ref=ref,
                       page=1, quote=quote, condition=condition, reviewed=True)


def scenario(name, value, unit, condition):
    facts[name] = Fact(value=value, unit=unit, basis="scenario", condition=condition, reviewed=True)


published("u1_voh", 2.4, "V", "driver", "U1", "VOH minimum is 2.4 V at VDD = 3.0 V, IOH = -4 mA, TA = -40 to 85 degC.", "Fictional worst case: VDD 3.0 V, sourcing 4 mA, -40 to 85 degC.")
published("u1_vol", 0.4, "V", "driver", "U1", "VOL maximum is 0.4 V at VDD = 3.0 V, IOL = 4 mA, TA = -40 to 85 degC.", "Fictional worst case: VDD 3.0 V, sinking 4 mA, -40 to 85 degC.")
published("u2_vih", 3.5, "V", "receiver", "U2", "VIH minimum is 3.5 V at VDD = 5.0 V, TA = -40 to 85 degC.", "Fictional receiver exactly 5.0 V, -40 to 85 degC.")
published("u2_vil", 1.5, "V", "receiver", "U2", "VIL maximum is 1.5 V at VDD = 5.0 V, TA = -40 to 85 degC.", "Fictional receiver exactly 5.0 V, -40 to 85 degC.")
published("u3_iq", 0.005, "A", "regulator", "U3", "Ground current maximum is 0.005 A at the scenario's input and load conditions.", "Fictional 12 V input and 0.2 A output teaching scenario.")
published("u3_theta", 100, "K/W", "regulator", "U3", "Junction-to-ambient thermal resistance is 100 K/W for the declared teaching PCB.", "Fictional specified teaching PCB in steady state, fixed airflow.")
published("u3_tj", 125, "degC", "regulator", "U3", "Maximum recommended junction temperature is 125 degC.", "Fictional maximum recommended steady-state junction temperature.")
scenario("vin", 12, "V", "Teaching assumption: constant 12 V input, no tolerance or transients.")
scenario("vout", 3.3, "V", "Teaching assumption: constant 3.3 V output; dropout ignored.")
scenario("load", 0.2, "A", "Teaching assumption: 0.2 A total steady load on the LDO.")
scenario("ambient", 40, "degC", "Teaching assumption: 40 degC ambient and matching declared PCB.")
checks = [
    Check(id="U1-to-U2-logic", kind="logic", endpoints=["U1.3", "U2.3"],
          inputs={"voh_min": "u1_voh", "vol_max": "u1_vol", "vih_min": "u2_vih", "vil_max": "u2_vil"},
          conditions="Synthetic static interface at the listed supplies and temperatures. Receiver leakage is within the driver's stated load; capacitance/timing/power sequencing remain unassessed.", conditions_reviewed=True),
    Check(id="U3-thermal", kind="ldo_thermal", endpoints=["U3.1", "U3.3"],
          inputs={"vin_max": "vin", "vout_min": "vout", "iout_max": "load", "iq_max": "u3_iq", "theta_ja": "u3_theta", "ambient_max": "ambient", "junction_limit": "u3_tj"},
          conditions="Synthetic steady-state linear regulator, exact scenario voltages, matching teaching board thermal resistance. This is an estimate, not measurement or SPICE.", conditions_reviewed=True),
]
design = Design(assumptions=["All components and specifications in this demo are fictional.",
                            "Logic analysis and regulator thermal analysis are separate teaching scenarios; the 3.0 V logic corner is not asserted to be the exact 3.3 V thermal scenario."],
                facts=facts, checks=checks)
atomic_json(root / "design.json", design.model_dump())
print(root / "design.json")

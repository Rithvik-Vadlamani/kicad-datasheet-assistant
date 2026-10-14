# Configure reproducible engineering checks

Search and generated answers work without a `design.json`. Numerical checks are opt-in because operating conditions and guaranteed limits must be chosen deliberately.

## A reviewed fact

This example is copied from the fictional demo, not a specification for a commercial component:

```json
{
  "value": 2.4,
  "unit": "V",
  "condition": "Fictional VDD 3.0 V, sourcing 4 mA, -40 to 85 degC.",
  "reviewed": true,
  "basis": "datasheet",
  "document": "driver",
  "page": 1,
  "quote": "VOH minimum is 2.4 V at VDD = 3.0 V, IOH = -4 mA, TA = -40 to 85 degC.",
  "ref": "U1"
}
```

Put the object under a stable key in `facts`, such as `u1_voh`. For real work, verify the original PDF and leave `reviewed` false until you have checked the value, unit, applicable pins, device variant, min/typ/max classification, footnotes and test conditions.

The application checks that the source is registered, the quote appears on the indexed page, the source document maps to the component's exact part, and the unit matches the equation. It does not parse the quote to prove the number was transcribed correctly. A long correct quote can still accompany an incorrect numeric value. Your review is the boundary for that risk.

`page` is a one-based physical PDF page, not necessarily the number printed in the footer. For `.txt` and `.md`, pages are separated by form-feed characters; ordinary files have one logical page.

## An explicit scenario input

Supply voltage, load, ambient temperature or a proposed resistor value may come from your scenario rather than a published specification:

```json
{
  "value": 0.2,
  "unit": "A",
  "basis": "scenario",
  "condition": "Assumed total steady-state load including every enabled device on this rail.",
  "reviewed": true
}
```

Scenario values remain assumptions. They are not promoted to manufacturer guarantees. Convert units explicitly: 200 mA becomes `0.2 A`, 200 pF becomes `2e-10 F`, 300 ns becomes `3e-7 s`. The accepted units are `V`, `A`, `W`, `degC`, `K/W`, `ohm`, `F`, and `s`. No automatic unit guessing is performed.

## A complete check

```json
{
  "assumptions": ["These are separate static operating-corner checks."],
  "facts": {
    "u1_voh": {"value": 2.4, "unit": "V", "basis": "scenario", "condition": "Replace with a reviewed guaranteed driver specification.", "reviewed": false},
    "u1_vol": {"value": 0.4, "unit": "V", "basis": "scenario", "condition": "Replace with a reviewed guaranteed driver specification.", "reviewed": false},
    "u2_vih": {"value": 3.5, "unit": "V", "basis": "scenario", "condition": "Replace with a reviewed guaranteed receiver specification.", "reviewed": false},
    "u2_vil": {"value": 1.5, "unit": "V", "basis": "scenario", "condition": "Replace with a reviewed guaranteed receiver specification.", "reviewed": false}
  },
  "checks": [{
    "id": "u1-u2-static-interface",
    "kind": "logic",
    "endpoints": ["U1.3", "U2.3"],
    "inputs": {
      "voh_min": "u1_voh",
      "vol_max": "u1_vol",
      "vih_min": "u2_vih",
      "vil_max": "u2_vil"
    },
    "conditions": "Review supply, temperature, output mode and loading compatibility before enabling.",
    "conditions_reviewed": false
  }]
}
```

This skeleton intentionally reports `unassessed`. Replace the placeholders, add actual provenance, and review the conditions before enabling it. The shipped demo has fully populated fictional evidence so you can see a working example.

Endpoints use **reference.pin-number**, not pin names. For `logic`, the first endpoint is the driver and the second is the receiver, and both must share the same net. If a level translator or series network intervenes, create checks for the relevant stages rather than pretending the pins are directly connected. For `pin_voltage`, supply one destination endpoint. Other checks accept optional endpoints as existence/connectivity anchors; they do not infer the whole physical network from them.

The system binds logic datasheet facts to the declared driver or receiver reference, and voltage limits to the destination reference. Pin-specific applicability within a multi-function component still requires review. Other calculation types validate each fact's mapped component, but do not automatically prove the entire set describes one regulator, one rail or every device on a bus. State those relationships explicitly in the conditions.

## Input names for each check

Names must match exactly. Every input name maps to a key in `facts`.

| Type | Inputs and units |
|---|---|
| `logic` | `voh_min`, `vol_max`, `vih_min`, `vil_max`: V |
| `pin_voltage` | `applied_min`, `applied_max`, `recommended_min`, `recommended_max`, `absolute_min`, `absolute_max`: V |
| `ldo_thermal` | `vin_max`, `vout_min`: V; `iout_max`, `iq_max`: A; `theta_ja`: K/W; `ambient_max`, `junction_limit`: degC |
| `power_budget` | `load_max`, `supply_available`: A |
| `i2c_pullup` | `vpu_max`, `vol_max`: V; `iol_max`: A; `bus_cap_max`: F; `rise_time_max`: s; `resistance_min`, `resistance_max`: ohm |

`iol_max` in the pull-up calculation means the maximum load current for which the device guarantees the chosen VOL limit. It must not be an absolute-maximum stress current. `resistance_min` and `resistance_max` describe the effective pull-up including tolerance and parallel pull-ups. This check uses the familiar first-order 30–70% rise-time relation; other timing definitions need a different equation. See [TI SLVA689](https://www.ti.com/lit/pdf/slva689).

Use `load_max` for the full relevant simultaneous load, not just one IC's typical current. `supply_available` should reflect thermal, voltage and operating-mode derating. The software does not automatically sum unspecified loads.

For the thermal calculation, select a `theta_ja` appropriate to the intended board and environment. A characterization-board value may not represent your PCB. Include dropout, safe operating area, current limiting, stability and transient checks separately; a thermal margin alone is insufficient.

## Result states

- `unassessed`: missing values, unreviewed facts/conditions, wrong units, source mismatch, missing/disconnected endpoints, or inconsistent input bounds. No valid numerical conclusion is issued.
- `violation`: the specified calculation violates a bound or lacks positive headroom under its stated conditions.
- `within_stated_bounds`: this particular calculation did not violate its specified criterion. It is not a declaration that the circuit is safe, reliable or fully reviewed.

`kicad-rag check` writes `reports/checks.json`. It returns 2 if any configured check is unassessed or a violation, 1 for an application/input error, and 0 otherwise. An empty set of checks returns 0 with an explicit “nothing assessed” note; scripts must inspect the check count before treating a run as design coverage.

## Build an actual review workflow

1. Add the exact datasheet revisions, errata, application notes and board requirements.
2. Run `coverage`; resolve ambiguous MPNs and missing documents.
3. Run broad evidence searches for voltage, current, timing, startup and thermal constraints.
4. Open the original pages. Verify table columns and conditions.
5. Enter only the values required by a selected check and clearly distinguish assumptions.
6. Check that endpoints correspond to the intended pin functions and operating mode.
7. Confirm that the chosen min/max inputs can coexist or form an intentionally conservative bound.
8. Run checks, inspect violations and unknowns, then ask the model to explain the evidence.
9. Validate important behavior with ERC/DRC, an appropriate simulation and measurements.

## Scanned datasheets and diagrams

Version 1 flags low-text PDF pages and rejects fully unreadable PDFs. It does not install or run OCR automatically. If you use an OCR tool such as OCRmyPDF, create a separate searchable copy and register that copy; preserve the original. A typical command is `ocrmypdf --skip-text original.pdf searchable.pdf` after installing OCRmyPDF and its dependencies. Consult its [official documentation](https://ocrmypdf.readthedocs.io/en/latest/cookbook.html) for supported inputs and options.

Extracted table rows and OCR text are never automatically marked reviewed. Pay special attention to minus signs, inequalities, subscripts, micro/milli prefixes, min/typ/max columns, footnotes and package-specific thermal values. Use `page DOCUMENT NUMBER --render page.png` to compare the indexed text with the original page.

## Extending the rules

`checks.py` contains the permitted input names/units, validation logic and fixed equations. Add a named check type, explicit units, input validity rules and tests with independently calculated expected values. Avoid adding general-purpose expression evaluation with `eval` or executing instructions from a PDF. Unsupported calculations should remain a documented unknown until a reviewed rule or simulation is available.

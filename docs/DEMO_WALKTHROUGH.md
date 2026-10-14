# Run a complete session without an API key

All parts in this demo are fictional. The data is deliberately small so every retrieved statement and calculation can be inspected.

## 1. Open the application

Activate the environment and run `kicad-rag --project examples/demo gui`. The demo uses `none` for both model providers. Click **Build index** if the index has not already been built.

## 2. Inspect document coverage

Click **Coverage**. U1 maps to the fictional MCU datasheet, U2 to the sensor datasheet, and U3 to the LDO datasheet. This verifies registration, not the correctness or completeness of each datasheet.

## 3. Retrieve evidence

Enter “Can U1 drive U2? Compare VIH VIL VOH VOL.” and click **Find evidence**. The report includes the `GPIO_LINK` connection and text from both relevant documents. It explicitly says that no language-model answer was generated.

The driver excerpt supplies its guaranteed high/low levels for specified loads and conditions. The receiver excerpt supplies its high/low thresholds under its own conditions. The circuit graph establishes the direct pin connection.

## 4. Run numerical checks

Click **Run design checks**. The logic check reports a high-level margin of −1.1 V and a low-level margin of +1.1 V. The high-level guarantee is inadequate. The separate LDO teaching scenario reports 1.8 W of dissipation and a steady-state estimate above the fictional junction limit.

Inspect `examples/demo/design.json` to see exactly which reviewed source values and scenario inputs produced these results. The conditions explicitly separate the logic corner from the nominal regulator scenario.

## 5. See how missing evidence behaves

Ask “quasarzz gravitonz” using **Find evidence**. There are no matching document passages. This illustrates an empty retrieval result. In ordinary English, unrelated words may still match common language in a datasheet, so empty-result detection alone is not a reliable answerability classifier.

To see deterministic abstention, copy the demo to a new folder, change one fact's `reviewed` value to false, and run checks. The corresponding check becomes `unassessed`, not `within_stated_bounds`.

## 6. Enable a real model

Once you configure a backend and index with embeddings, **Generate answer** runs the full RAG pipeline. The expected output structure is a series of documented facts, inferences, recommendations or unknowns with short source quotations, followed by assumptions, missing information and suggested validation.

No prewritten response is substituted for the language model. If a backend is missing, generation reports a configuration error; if a generated citation fails validation, the application withholds that answer. The evidence-only and deterministic-check workflows remain available independently.

## 7. Move to your own board

Use **New project**, **Add datasheet**, **Sync KiCad** and **Build index**. The desktop settings dialog selects models; the CLI or `project.json` supports exact part overrides and additional options. Populate your own `design.json` only when you want reproducible numerical checks. Never copy the fictional component limits into a real design.

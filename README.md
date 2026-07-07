# General DXF Layout Pipeline

Config-driven pipeline that turns any retail-store DXF + one JSON config into
per-docket layout drawings and BOQ quantities. Every layer name, block name,
text keyword and coordinate comes from the config — **no drawing-specific
values live in code** (grep-provably zero).

## Run

```bash
py -3 app.py                 # web app  -> http://127.0.0.1:5000
py -3 run_pipeline.py <config.json> [input.dxf] [output_dir]   # CLI
py -3 -m unittest tests.test_pipeline                          # tests
```

## Architecture

- **`config_loader.py`** — loads/normalizes schema 3.1 (canonical + intake-form
  spellings), resolves `$ref` zones with cycle detection, validates with
  JSON-path errors before any DXF work. `DEFAULTS` holds the only constants.
- **`zone_service.py`** — `ZoneResolver`: polyline / block / balloon zone
  resolution (cached), mirrored-block-safe bboxes, open-polyline chaining,
  closed-room polygonization; cyclic-block-safe throughout.
- **`dockets/`** — one engine per layout, uniform
  `generate(doc, ctx, docket_cfg, out) -> DocketResult`:
  flooring, speakers, sprinklers, partition, electrical, finishes, skirting.
- **`run_pipeline.py`** — orchestrator: shared context once, each enabled
  docket → its own DXF + merged `boq_summary.json` + `run_log.json`.
- **`app.py`** — Flask: `/layers`, `/patterns`, `/generate`,
  `/download/<job>/<docket>`.
- **`flooring_app.html`** — the intake form (STEP 1–11).

`legacy/` holds the retired brand-specific scripts (reference only).

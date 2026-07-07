# Flooring Layout Pipeline — Full Detail

End-to-end pipeline that turns an architectural **DXF** + a **layer-name config**
into a tiled **flooring layout DXF**. Designed so a customer only supplies layer
names (ideally just one), and the system floors the store automatically while
honoring per-zone rules.

---

## 1. Architecture

```
┌─────────────────────────────────┐
│ flooring_geometry_config (1).json│   ← customer fills in LAYER NAMES only
│  (schema_version 2.0)            │
└───────────────┬─────────────────┘
                │  reads
                ▼
┌─────────────────────────────────────────────────────────────┐
│ run_flooring_pipeline.py   (ORCHESTRATOR — the glue)         │
│                                                               │
│  STEP 1  load config                                          │
│  STEP 2  extract geometry from DXF, apply zone rules,         │
│          assemble ctx, store to context.ctx                   │
│  STEP 3  run the flooring engine with that ctx                │
└───────────────┬─────────────────────────────────────────────┘
                │ calls
   ┌────────────┼───────────────────────────────┐
   ▼            ▼                                 ▼
extract_*    context.ctx (JSON)            flooring_layout.py
modules      ← stored & reloaded           (TILING ENGINE)
                                                  │
                                                  ▼
                                    <input_dir>/output/<name>_FLOORING.dxf
```

**Data contract between the two halves** is the **ctx** dict (see §6). The
orchestrator produces it; the engine consumes it. The ctx is written to
`context.ctx` and reloaded, so the geometry that gets tiled is exactly what was
stored on disk.

---

## 2. Files & roles

| File | Role |
|------|------|
| `flooring_geometry_config (1).json` | Customer-facing config: layer names + tile params + zone rules (schema 2.0). |
| `run_flooring_pipeline.py` | **Orchestrator.** Reads config → runs extractors → builds & stores ctx → runs engine. |
| `extract_layer_polygons_closed.py` | Extracts **closed** polygons (store outline, zones) from named layers. |
| `extract_polylines_opened.py` | Extracts **open** polylines / LINEs (façade, door, column segments). Used only when those layers are configured. |
| `extract_hatches.py` | Extracts HATCH boundaries (wall cross-sections). Used only if a hatch layer is configured. |
| `balloon_zone_extract.py` | Text-seeded room-zone detection (legacy heavy mode; not used by the minimal 2.0 flow). |
| `flooring_layout.py` | **Tiling engine.** Builds the net floor polygon, the rotated diamond tile grid, computes areas, writes the output DXF. |
| `context.ctx` | The assembled context, stored as JSON between extraction and tiling. |

---

## 3. Config reference (schema 2.0)

```jsonc
{
  "schema_version": "2.0",
  "units": "mm",

  "io": {
    "input_dxf":  "",            // optional; can be passed as CLI arg 2
    "output_dxf": "",            // optional; defaults to <input_dir>/output/<name>_FLOORING.dxf
    "ctx_file":   "context.ctx"  // where the assembled ctx is stored (relative to config)
  },

  "params": {
    "tile_size_mm":      600,    // tile edge length
    "tile_rotation_deg": 45,     // diamond rotation
    "wastage_pct":       10,     // added to order qty
    "min_door_width_mm": 600     // door-gap detection threshold (engine)
  },

  "flooring": {
    "docket": "flooring",

    // ── REQUIRED: the floor boundary ────────────────────────────
    "store_outline": {
      "dxf_types": ["LWPOLYLINE", "POLYLINE"],
      "layers": ["PLANO-CARPET"],   // <-- customer edits this
      "layer_pattern": null,        // optional wildcard, e.g. "PLANO-*"
      "role": "site_boundary",
      "required": true
    },

    // ── OPTIONAL: zones with rules ──────────────────────────────
    "zone_outlines": [
      {
        "id": "NO-FLOOR",
        "layers": ["PLANO-NOFLOOR"],
        "layer_pattern": null,
        "action": "skip",           // skip  -> leave empty (subtracted from floor)
        "start_point": null
      },
      {
        "id": "TILED-ZONE",
        "layers": ["PLANO-ZONE"],
        "action": "floor",          // floor -> tiled (default behaviour)
        "start_point": {            // optional tile-grid origin
          "dxf_types": ["INSERT", "POINT"],
          "layers": ["PLANO-START"],
          "scope": "global",
          "required": false
        }
      }
    ]
  }
}
```

### Field semantics

- **store_outline.layers** — one or more layers; the first closed polygon found
  becomes the floor boundary. **The only mandatory input.**
- **layer_pattern** — optional `fnmatch` wildcard (case-insensitive) resolved
  against the DXF layer table, e.g. `"ZONE-*"` matches `ZONE-A`, `ZONE-B`.
- **zone_outlines[].action**
  - `"skip"` → the zone polygon is **subtracted** from the floor (left empty).
  - `"floor"` (default) → no special handling; the area is tiled as part of the store.
  - If `action` is omitted, `role: "exclude_region"` ⇒ skip, otherwise ⇒ floor.
- **start_point** — optional. First `INSERT`/`POINT` found on its layers (search
  recurses into blocks) becomes the tile-grid origin. If absent, the engine
  auto-derives the origin (see §5).

**Rule of thumb:** *floor the whole store; carve out only what a `skip` zone
covers. No zones ⇒ complete flooring.*

---

## 4. STEP 2 — Extraction & ctx assembly (orchestrator)

Implemented in `run_flooring_pipeline.py` → `build_ctx()`.

1. **Open the DXF** once with ezdxf (used for layer resolution + start points).
2. **Resolve layers** — `_resolve_layers(doc, layers, pattern)`:
   - case-insensitive match of each configured name against the DXF layer table,
   - plus any layers matching `layer_pattern`,
   - returns only layers that actually exist (missing names are silently dropped).
3. **Store outline** — `_collect_closed(dxf, store_layers)` calls
   `extract_layer_polygons_closed` per layer and concatenates closed polygons.
   If none are found → hard error (the floor boundary is required).
4. **Zones** — for each `zone_outline`:
   - resolve its layers, extract closed polygons,
   - `action == "skip"` → add each polygon to `skip_zones` (keyed `id_n`),
   - `action == "floor"` → counted only (tiled as part of the store),
   - the first `start_point` found across zones sets the global tile origin.
5. **Assemble ctx** (see §6). Skip-zones are routed through the engine's
   exclusion slot (`BALLOON.TOILET.zones`). All other components are left empty;
   the engine's fallbacks handle their absence.
6. **Inject origin** — if a start point `(x, y)` was found, ctx gets a tiny
   2-point segment `store-maindoor = [[(x-1,y),(x+1,y)]]`; the engine reads the
   origin as the midpoint of that segment.
7. **Store & reload** — ctx is `json.dump`ed to `context.ctx`, then reloaded, so
   what the engine tiles is exactly the on-disk ctx.

### Extractor I/O summary

| Extractor | Input | Output shape |
|-----------|-------|--------------|
| `extract_layer_polygons_closed(dxf, {"priority":[L], L:key})` | layer→key map | `{key: [ [(x,y),…], … ]}` closed polygons |
| `extract_polylines_by_layer_opened(dxf, {key: layer})` | key→layer map | `{key: [ [(x,y,z),…], … ]}` open polylines/LINEs |
| `extract_hatch(dxf, [layers])` | layer list | `[ [(x,y),…], … ]` or `None` |

(The minimal 2.0 flow uses only the first; the others are wired for richer
configs that name façade/door/column/hatch layers.)

---

## 5. STEP 3 — Tiling engine (`flooring_layout.py`)

`FlooringLayout(dxf, ctx=…, tile params…)` then `.extract()` then
`.save_output(path)`.

### `extract()` flow (in order)

1. **Store polygon** ← `ctx["store-outline"][0]` (required).
2. **Hatches** ← `ctx["hatches"]` (wall cross-sections; empty in minimal mode).
3. **Façade line** ← `ctx["store-mainglass"]` if present; otherwise
   **synthesized** from the store polygon's **bottom edge** (min-Y), else its
   longest edge. The façade defines the diamond grid's orientation axes.
4. **Exclusions** gathered from ctx (all config-driven now):
   - skip-zones via `ctx["BALLOON"]["TOILET"]["zones"]` → `toilet_polygon` list,
   - `bulkhead` ← `ctx["bulkhead"][0]` (or None),
   - `lintel`   ← `ctx["lintel"]` (list),
   - `columns`  ← polygonized from `ctx["cols"]` segments (or None).
5. **Net polygon** = `store − (bulkhead ∪ skip-zones ∪ columns ∪ lintels)`
   (`_compute_net_polygon`, via Shapely `difference`). Raises if empty.
6. **Tile origin** — priority:
   1. **Façade door** — midpoint of `ctx["store-maindoor"][0]`, shifted past any
      bulkhead. *(This is how an injected start point arrives.)*
   2. **User-drawn line** — a LINE/LWPOLYLINE on a recognized start-line layer;
      origin snaps to its midpoint, re-centred on the store width.
   3. **Auto** — detected entry-door gap in the façade, re-centred, pushed past
      the bulkhead.
7. **Tile grid** (`_compute_tile_grid`) — a checkerboard of `tile_size` squares
   rotated by `tile_rotation_deg` about the origin, **clipped to the net
   polygon**. Each tile is tagged `grey` / `white` (alternating) or `start`.
8. **Areas** (`_compute_tile_area`) — net area, + wastage %, → order quantity,
   in sq ft and sq m.

### `save_output()` — what's written to the DXF

A fresh R2018 DXF with colored layers:

| Output layer | Contents |
|--------------|----------|
| `FLOOR-TILE-GREY` / `FLOOR-TILE-WHITE` | filled diamond tiles (alternating) |
| `FLOOR-TILE-START` | the highlighted start tile |
| `FLOOR-TILE-OUTLINE` | tile edges |
| `FLOOR-OUTLINE` | net floor exterior |
| `FLOOR-TOILET-CUT` | skip-zone / excluded-region outlines |
| `FLOORING_COLUMN_CUT` | column cut hatches |
| `FLOOR-TILE-ORIGIN` | origin marker |
| wall/annotation/dim layers | wall frame copied from source + annotations |

Drawing order: tiles first, then outlines/cuts overdraw on top. The original
wall frame (HATCH + outlines on wall/partition layers) is copied in for context,
and helper layers (FF&E, part-hatch, fittings) are stripped.

---

## 6. The ctx schema (contract)

```jsonc
{
  "store-outline":   [ [ [x,y], … ] ],        // REQUIRED; [0] is the floor boundary
  "BALLOON": {                                 // skip / excluded zones
    "TOILET": { "count": N, "zones": {
        "<id>": { "polygon": [ [x,y], … ] }
    }}
  },
  "store-mainglass": [ [x,y], … ],             // façade line (empty ⇒ synthesized)
  "store-maindoor":  [ [ [x,y], … ] ],         // [0] midpoint = tile origin (empty ⇒ auto)
  "bulkhead":        [ [ [x,y], … ] ],         // optional no-tile strip
  "lintel":          [ [ [x,y], … ], … ],      // optional no-tile regions
  "cols":            [ [ [x,y], [x,y] ], … ],  // optional column segments
  "hatches":         [ [ [x,y], … ], … ]       // optional wall hatches
}
```

In the **minimal 2.0 flow**, only `store-outline` (and optionally
`BALLOON.TOILET.zones` for skip zones + `store-maindoor` for a start point) are
populated; everything else is `[]` and the engine falls back gracefully.

---

## 7. Running it

```bash
# uses io.input_dxf / io.output_dxf from the config if args omitted
py -3 run_flooring_pipeline.py "flooring_geometry_config (1).json" "C:\path\input.dxf" [output.dxf]
```

Output defaults to `<input_dir>\output\<name>_FLOORING.dxf`; the ctx is written
next to the config as `context.ctx`.

**Verified run (LK_BOQ_.dxf):** store 836.71 sq ft → net 836.71 sq ft (no skip
zones drawn) → 268 tiles @ 600 mm / 45° → order qty 920.38 sq ft @ 10% wastage.

---

## 8. Customizing for a new drawing / customer

1. Set `store_outline.layers` to the layer that holds the **closed** floor
   boundary polygon. (That's the minimum — everything else is optional.)
2. To leave a room empty (toilet, staff, store): draw a **closed polygon**
   around it on a layer, and add a `zone_outline` with `"action": "skip"`
   pointing at that layer.
3. To fix the tile origin: drop a `POINT`/`INSERT` on a layer and name it in a
   zone's `start_point.layers`. Otherwise the origin is auto-derived.
4. Tune `params` for tile size / rotation / wastage.

No code changes are needed per drawing — only the config.

---

## 9. Changes made to the original code

- **`run_flooring_pipeline.py`** — new orchestrator built for schema 2.0
  (layer/pattern resolution, skip/floor rules, start-point origin, ctx assembly
  & storage). Strips Z from polyline coords (extractors emit `x,y,z`; the tiler
  expects `x,y`).
- **`flooring_layout.py`** — made fully **ctx-driven and tolerant**: every ctx
  access uses `.get()` with the engine's existing synthesis fallbacks (façade
  from store edge, etc.), so the engine runs from just a store outline. Only
  `store-outline` is hard-required. Exclusions are strictly config-driven (the
  old curtain auto-detection that returned `None` and crashed was removed).
- **`flooring_geometry_config (1).json`** — rewritten to schema 2.0 with a
  per-zone `action` rule and optional `start_point`.

---

## 10. Known limitations / next steps

- **Single tile pattern.** All `floor` zones share one diamond grid/origin.
  True per-zone independent grids (per-zone `start_point`, different tile sizes)
  are modeled in the schema but not yet implemented in the engine.
- **Columns** are only cut if a column layer is configured **and** holds closed
  footprints; LINE-only column markers polygonize to near-zero area.
- **Start point** must be an `INSERT`/`POINT`; if absent the engine auto-derives
  the origin, which can vary between drawings.
```

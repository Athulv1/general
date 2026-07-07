# balloon_zone_extract.py — Documentation

## Overview

`balloon_zone_extract.py` detects and outlines **zone polygons** inside an architectural DXF floor plan.

The core idea — called **balloon inflation** — works like a balloon being blown up inside a room: starting from a text label position (e.g. the word `CLINIC` placed on the drawing), a circular shape is repeatedly expanded outward until it hits walls, partitions, or other barriers. The final shape is the zone polygon for that label.

The script returns all results as a JSON dictionary and can optionally write the polygons back into the DXF file as new layers.

---

## Programmatic Usage

### Flat path (existing behaviour — unchanged)

```python
from balloon_zone_extract import detect_zones

# All labels found in the full DXF
result = detect_zones("plan.dxf")

# All labels found on specific annotation layers
result = detect_zones("plan.dxf", scan_layers=["ANNO-TEXT", "ROOM-TAG"])

# Explicit labels, seeds restricted to those layers, write output DXF
result = detect_zones(
    "plan.dxf",
    scan_layers=["ANNO-TEXT"],
    keywords=["CLINIC", "BOH"],
    output_dxf="plan_zones.dxf",
)
```

**Return value schema (flat path):**

```python
{
    "clinic": [
        [(x1, y1), (x2, y2), ...],   # polygon 1 exterior coords
        [(x1, y1), (x2, y2), ...],   # polygon 2 exterior coords
    ],
    "boh": [
        [(x1, y1), ...],
    ],
    ...
}
```

Keys are lowercased text label values. Values are lists of polygons; each polygon is a list of `(x, y)` float tuples from Shapely's `exterior.coords`.

---

### Grouped path (new — `balloon_groups`)

Pass `balloon_groups` to get a structured, enriched output grouped by logical zone type. Each group calls the existing inflation logic independently via a recursive self-call.

```python
result = detect_zones(
    "plan.dxf",
    balloon_groups={
        "CLINIC":   {"layers": ["LK-ROOM TAG"], "keywords": ["clinic"]},
        "STORE":    {"layers": ["LK-ROOM TAG"], "keywords": ["store"]},
        "TOILET":   {"layers": ["LK-ROOM TAG"], "keywords": ["toilet"]},
        "PICKUP":   {"layers": ["LK-ROOM TAG"], "keywords": ["pickup"]},
        "CORRIDOR": {"layers": ["LK-ROOM TAG"], "keywords": ["corridor"]},
    }
)
```

When `balloon_groups` is passed, `scan_layers`, `keywords`, and `output_dxf` are ignored — the groups dict is the sole input.

**Return value schema (grouped path):**

```python
{
    "CLINIC": {
        "count": 2,
        "zones": {
            "clinic_1": {
                "polygon":   [(x1, y1), (x2, y2), ...],  # exterior coords
                "area_mm2":  4500000.0,                   # Shapely polygon area in mm²
                "area_sqft": 48.44,                       # area_mm2 / 92903.04
                "seed_text": "clinic",                    # keyword that produced this zone
                "seed_xy":   (12345.67, 8901.23)          # centroid of the inflated polygon
            },
            "clinic_2": { ... }
        }
    },
    "STORE": {
        "count": 1,
        "zones": {
            "store_1": { ... }
        }
    },
    "TOILET": {
        "count": 0,
        "zones": {}          # always present, even when no polygon was found
    },
    ...
}
```

**Notes:**
- Every group key is always present in the output — `count: 0, zones: {}` when no polygons were found for that group.
- `seed_xy` is the **Shapely centroid of the inflated polygon**, not the original DXF text insertion point.
- Zone keys are numbered sequentially per group (`clinic_1`, `clinic_2`, …), restarting at 1 for each group.
- The `claimed_zone` anti-overlap logic is **not** shared across groups — each group's recursive call runs independently, which is intentional.

---

## Dependencies

| Library | Version | Purpose |
|---|---|---|
| `ezdxf` | ≥ 1.0 | Read and write DXF files, walk modelspace entities |
| `shapely` | ≥ 2.0 | 2D geometry — polygons, buffering, boolean ops |
| `json` | stdlib | Serialise results to stdout |
| `math` | stdlib | Euclidean distance calculation |
| `re` | stdlib | Strip MTEXT control codes; sanitise layer names |
| `sys` | stdlib | CLI argument access and exit codes |
| `typing` | stdlib | Type hints (`Optional`) |

Install the external dependencies:

```bash
pip install ezdxf shapely
```

---

## Hardcoded Constant — `BARRIER_LAYERS`

```python
BARRIER_LAYERS = [
    "PLANO-CARPET", "LK-PARTITION", "LK-PELMET",
    "LK-DOOR LINTEL", "A-WALL", "HATCH_OUTLINE",
    "BOH_WALL_VALID_OUTLINE", "I-LK CURTAIN",
]
```

This list defines which DXF layers act as **walls and physical barriers** during inflation. Any line geometry found on these layers will block the balloon from expanding through it. Edit this list directly in the script to add or remove barrier layer types for your project.

---

## Function Reference

### `_dist(a, b)`

**Why it exists:** A fast Euclidean distance helper used throughout the script whenever two (x, y) points need to be compared.

**How it works:** Applies the standard formula `sqrt((x2-x1)² + (y2-y1)²)` using Python's `math.sqrt`.

**Library:** `math`

---

### `_entity_to_linestrings(entity)`

**Why it exists:** DXF files store geometry as typed entities (`LINE`, `LWPOLYLINE`, `POLYLINE`). Shapely cannot work with them directly, so this function converts them into Shapely `LineString` objects that can be used in geometric operations.

**How it works:**
- Checks the entity type with `entity.dxftype()`
- For a `LINE`: extracts start/end coordinates; skips lines shorter than 1 mm
- For `LWPOLYLINE` / `POLYLINE`: extracts all vertex points; closes the ring if the entity is marked closed

**Libraries:** `ezdxf` (to read entity attributes), `shapely.geometry.LineString`

---

### `_collect_world_segments(msp, target_layers)`

**Why it exists:** DXF drawings often embed geometry inside **block references** (`INSERT` entities) rather than placing it directly in modelspace. This function recursively expands all block references so no geometry is missed, then filters by layer name.

**How it works:**
1. Walks every entity in modelspace
2. When it encounters an `INSERT`, calls `entity.virtual_entities()` to expand the block contents into world coordinates
3. Applies layer inheritance — if a sub-entity is on the generic layer `"0"`, it inherits the parent block's layer
4. Collects only entities on the `target_layers` set and converts them via `_entity_to_linestrings`

**Libraries:** `ezdxf` (modelspace iteration, `virtual_entities`), `shapely.geometry.LineString`

---

### `_snap_endpoints(segs, tol)`

**Why it exists:** In real DXF drawings, wall lines that visually touch often have tiny gaps between their endpoints (sub-millimetre mismatches from different CAD operations). These gaps would allow the balloon to leak through what appears to be a solid wall. Snapping closes those gaps.

**How it works:** Uses a **Union-Find (disjoint-set)** algorithm:
1. Collects every segment's start and end point into a flat list
2. Clusters any two points that are within `tol` mm of each other into the same group
3. Replaces all points in a cluster with the group's canonical coordinate
4. Rebuilds each `LineString` with snapped endpoints

The default tolerance used internally is **2 mm**.

**Libraries:** `math` (for `_dist`), `shapely.geometry.LineString`

---

### `_find_text_seeds(msp, keyword)`

**Why it exists:** The balloon needs a starting position. Text labels in the DXF (e.g. the word `CLINIC` placed in the middle of a clinic zone) are the seed points. This function locates all of them for a given keyword.

**How it works:**
- Scans every `TEXT` and `MTEXT` entity in modelspace
- For `TEXT`: reads `entity.dxf.text` and does a case-insensitive substring match
- For `MTEXT`: strips formatting control codes first (e.g. `{\Fstyle;...}`, `\P`, `{}`) using the `_MTEXT_STRIP` regex, then matches
- Returns the `(x, y)` insertion point of each matching entity

**Libraries:** `ezdxf` (entity access), `re` (MTEXT control code stripping)

---

### `_dedup_seeds(seeds, merge_dist)`

**Why it exists:** The same label text is sometimes placed multiple times very close together in a drawing (e.g. duplicated annotation layers). Without deduplication, each copy would generate a separate balloon inflating from almost the same spot, producing duplicate or overlapping zones.

**How it works:** Greedy pass — iterates seeds in order; only accepts a seed if it is at least `merge_dist` mm away from all previously accepted seeds. Default merge distance is **2000 mm** (2 metres).

**Libraries:** `math` (via `_dist`)

---

### `_inflate_balloon(seed_pt, barrier_region, constraint, exclusion, step, max_iter, stop_thresh, seed_radius, verbose)`

**Why it exists:** This is the core algorithm. Given a starting point, it grows a polygon to fill the available enclosed space — like filling a room with an inflating balloon.

**How it works:**
1. Creates an initial circle at `seed_pt` with radius `seed_radius` (default 200 mm)
2. Each iteration:
   - Expands the balloon outward by `step` mm using `polygon.buffer(step)`
   - Clips to the `constraint` bounding box: `candidate.intersection(constraint)`
   - Removes barrier geometry: `candidate.difference(barrier_region)`
   - Removes already-claimed zones: `candidate.difference(exclusion)`
   - If the balloon split into disconnected pieces (MultiPolygon), keeps only the piece that still contains the original seed point
   - Stops early if the fractional area growth drops below `stop_thresh` (converged)
3. Returns the final Shapely `Polygon` (may be empty if the seed was inside a wall)

**Libraries:** `shapely.geometry.Point`, `shapely.geometry.Polygon` (buffering, intersection, difference, contains, distance)

---

### `_discover_labels(msp, scan_layers)`

**Why it exists:** In Mode 2 and Mode 3, the user does not specify which text labels to look for — the script finds them automatically. This function collects all unique text values from the drawing.

**How it works:**
- Iterates every `TEXT` and `MTEXT` entity in modelspace
- If `scan_layers` is provided (a Python `set`), skips entities not on those layers
- Strips MTEXT control codes with `_MTEXT_STRIP`
- Returns a **sorted** list of unique non-empty strings

**Libraries:** `ezdxf` (entity iteration), `re` (MTEXT stripping)

---

### `_seeds_on_layers(msp, keyword, layers)`

**Why it exists:** When the user restricts the scan to specific layers (Modes 1 and 2), seed positions should also only come from entities on those layers — not from identically-named labels on unrelated annotation layers elsewhere in the drawing.

**How it works:** Same logic as `_find_text_seeds` but adds a layer filter: skips any entity whose `entity.dxf.layer` is not in the `layers` set before checking the keyword.

**Libraries:** `ezdxf`, `re`

---

### `_sanitize_layer_name(name)`

**Why it exists:** DXF layer names have strict character restrictions — they cannot contain `< > / \ " : ; ? * | = '`. Text labels in drawings sometimes contain these characters (e.g. `55"` for a 55-inch screen). Passing such a string directly to `doc.layers.new()` raises a `DXFValueError`. This function makes any label safe to use as a layer name.

**How it works:** Uses the `_INVALID_LAYER_CHARS` compiled regex to replace all forbidden characters with underscores. The result-dict key (used in the JSON output) is **not** affected — only the DXF layer name is sanitised.

**Libraries:** `re`

---

### `detect_zones(dxf_path, scan_layers, keywords, output_dxf, balloon_groups, ...)` — Public API

**Why it exists:** This is the single entry point that orchestrates everything above into a usable result. It supports two distinct calling modes — a flat path (existing behaviour) and a grouped path (new).

**How it works — grouped path** (when `balloon_groups` is not None):

| Step | What happens |
|---|---|
| 1 | Early-return block fires immediately, before any DXF I/O |
| 2 | For each group in `balloon_groups`: call `detect_zones` recursively with `balloon_groups=None` (prevents re-entry), forwarding all tuning params |
| 3 | For each keyword in the group: pull the raw coord lists from the recursive result, build a `Polygon`, compute area + centroid, store as a numbered zone entry |
| 4 | Collect all zones under the group name with a `count` key |
| 5 | Return the fully structured `balloon_context` dict — no DXF file is read or written at this level |

**How it works — flat path** (when `balloon_groups` is None, existing behaviour unchanged):

| Step | What happens |
|---|---|
| 1 | Load DXF with `ezdxf.readfile` |
| 2 | Resolve keyword list — use provided `keywords`, or auto-discover via `_discover_labels` |
| 3 | Collect barrier geometry from `BARRIER_LAYERS` via `_collect_world_segments`, then snap gaps with `_snap_endpoints` |
| 4 | Merge all barrier segments into one Shapely geometry with `unary_union` + buffering |
| 5 | Compute the bounding constraint box from the min/max extent of the barrier geometry |
| 6 | For each keyword: find seeds → deduplicate → inflate balloon → simplify → store |
| 7 | A single `claimed_zone` polygon accumulates across **all** keywords so zones of different types never bleed into each other |
| 8 | If `output_dxf` is given: write each polygon as an `LWPOLYLINE` to a sanitised layer name and save the file |
| 9 | Return the result dict |

**Libraries:** `ezdxf`, `shapely.geometry`, `shapely.ops.unary_union`

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dxf_path` | str | required | Path to the input DXF file |
| `scan_layers` | list\|None | None | Layers to scan for text labels/seeds. None = entire drawing |
| `keywords` | list\|None | None | Explicit label list. None = auto-discover from `scan_layers` |
| `output_dxf` | str\|None | None | If set, write zone polygons to this DXF path (flat path only) |
| `balloon_groups` | dict\|None | None | If set, runs the grouped path and returns enriched structured output (see Grouped path above) |
| `min_area` | float | 1e6 | Skip zones smaller than this area in mm² (1 m²) |
| `step` | float | 100.0 | Balloon growth per iteration in mm |
| `max_iter` | int | 600 | Maximum inflation iterations per seed |
| `stop_thresh` | float | 0.001 | Stop when area growth fraction drops below this |
| `barrier_buffer` | float | 3.0 | Buffer applied around barrier segments in mm |
| `seed_merge_dist` | float | 2000.0 | Merge seeds closer than this distance in mm |
| `seed_radius` | float | 200.0 | Initial balloon circle radius in mm |
| `bbox_margin` | float | 200.0 | Padding added to the bounding constraint box in mm |
| `simplify_tol` | float | 5.0 | Output polygon simplification tolerance in mm |
| `verbose` | bool | False | Print per-seed progress to stdout |

**Return value — flat path:**

```python
{
    "clinic": [
        [(x1, y1), (x2, y2), ...],   # polygon 1
        [(x1, y1), ...],             # polygon 2
    ],
    "boh": [
        [(x1, y1), ...],
    ],
}
```

Keys are lowercased label strings. Each value is a list of polygons; each polygon is a list of `(x, y)` float tuples (exterior ring from Shapely's `exterior.coords`).

**Return value — grouped path:** see the schema in the [Grouped path](#grouped-path-new----balloon_groups) section above.

---

## How to Run — CLI

### Syntax

```
python balloon_zone_extract.py <dxf_path> [LAYER ...] [TEXT ...] [--output out.dxf]
```

The script auto-detects which mode to use based on the arguments — no flags or switches are required for mode selection.

### Arguments

| Argument | Required | Description |
|---|---|---|
| `<dxf_path>` | Yes | Path to the input DXF file |
| `LAYER ...` | No | One or more DXF layer names to restrict the search to |
| `TEXT ...` | No | One or more specific text labels to extract (must follow a layer name) |
| `--output` / `-o` | No | Path to write the output DXF with zone polygons drawn in |

### Mode 1 — Specific layers AND specific text labels

Only extract zones for the named labels found on the named layers.

```bash
python balloon_zone_extract.py "DXF FILE\plan.dxf" "LK-ROOM TAG" "CLINIC" "BOH"
```

```bash
# With DXF output
python balloon_zone_extract.py "DXF FILE\plan.dxf" "LK-ROOM TAG" "CLINIC" "BOH" --output zones_out.dxf
```

You can pass multiple layers, each followed by its own text labels:

```bash
python balloon_zone_extract.py "DXF FILE\plan.dxf" "LK-ROOM TAG" "CLINIC" "LK-ANNO" "BOH" "STORAGE"
```

### Mode 2 — Specific layers only, auto-discover labels

Scan only the named layers for any text, then extract zones for everything found.

```bash
python balloon_zone_extract.py "DXF FILE\plan.dxf" "LK-ROOM TAG"
```

```bash
python balloon_zone_extract.py "DXF FILE\plan.dxf" "LK-ROOM TAG" "LK-ANNO" --output zones_out.dxf
```

### Mode 3 — Entire DXF, auto-discover all labels

Scan the entire drawing for any text label and extract a zone for each one found.

```bash
python balloon_zone_extract.py "DXF FILE\plan.dxf"
```

```bash
python balloon_zone_extract.py "DXF FILE\plan.dxf" --output zones_out.dxf
```

---

## Sample Tests

### Test 1 — Scan a single annotation layer (Mode 2)

```bash
python balloon_zone_extract.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LK-ROOM TAG"
```

Expected: JSON dict printed to stdout with all unique text labels found on the `LK-ROOM TAG` layer, each mapped to a list of zone polygons.

---

### Test 2 — Scan a single layer and write output DXF

```bash
python balloon_zone_extract.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LK-ROOM TAG" --output zones_out.dxf
```

Expected: Same JSON output to stdout, plus `zones_out.dxf` created in the current directory with each zone drawn as an LWPOLYLINE on a layer named after its label.

---

### Test 3 — Specific layer + specific labels (Mode 1)

```bash
python balloon_zone_extract.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LK-ROOM TAG" "CLINIC" "BOH"
```

Expected: JSON dict with only `"clinic"` and `"boh"` keys (if those labels exist on `LK-ROOM TAG`).

---

### Test 4 — Whole DXF scan, no output file (Mode 3)

```bash
python balloon_zone_extract.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf"
```

Expected: JSON dict covering every text label found across the entire drawing.

---

### Test 5 — Whole DXF scan with DXF output (Mode 3 + output)

```bash
python balloon_zone_extract.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" --output full_zones.dxf
```

Expected: Complete zone map saved to `full_zones.dxf`, JSON also printed to stdout.

---

## Output Format

All three modes print the result to **stdout** as pretty-printed JSON:

```json
{
  "clinic": [
    [
      [12345.0, 67890.0],
      [12400.0, 67890.0]
    ]
  ],
  "boh": [
    [
      [11000.0, 65000.0]
    ]
  ]
}
```

- Keys are the **lowercased** text label values from the DXF
- Each key maps to a **list of polygons** (one polygon per seed point found for that label)
- Each polygon is a list of `(x, y)` float tuples in millimetres (DXF drawing units), taken from Shapely's `exterior.coords`

When `--output` is provided, the same polygons are also written into the DXF as closed `LWPOLYLINE` entities, each on a layer named after its label. Characters that are illegal in DXF layer names (`< > / \ " : ; ? * | = '`) are automatically replaced with underscores.

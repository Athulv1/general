# extract_layer_polygons_closed.py — Function Reference

## Overview

This script reads any DXF file, finds all closed polygon shapes on the layers
you specify (or across the entire file), and returns their `(x, y)` vertex
coordinates in a form that can be passed directly into Shapely for area
calculations, hatch generation, or any other downstream BOQ pipeline step.

It is **fully self-contained** — the only external libraries it needs are
`ezdxf` (to read the DXF file) and `shapely` (to validate polygon geometry).

---

## Library Dependencies

| Library | Why it is needed |
|---|---|
| `ezdxf` | Reads `.dxf` files and exposes every entity (polyline, spline, layer table, modelspace) as a Python object |
| `shapely` | Creates geometric Polygon objects from vertex lists; validates their geometry and computes area, bounds |
| `math` | Provides `sqrt()` for the Euclidean gap calculation between two points |
| `sys` | Reads command-line arguments (`sys.argv`) and exits cleanly (`sys.exit`) |
| `pprint` | Pretty-prints the raw dictionary in the DEBUG section so nested structure is readable |

---

## Function-by-Function Reference

---

### `_geometric_gap(pts)`

**Purpose**
Measures the straight-line distance (in DXF drawing units, which are millimetres
in this project) between the **first vertex** and the **last vertex** of a shape.

**Why it exists**
Some polylines in DXF files are not flagged as "closed" by their entity flag, yet
their start and end points are so close together (< 1 mm) that they are
effectively closed. This function lets us apply a geometric fallback: if the gap
is less than 1 mm, we still treat the shape as a valid closed polygon.

**Library used**
`math.sqrt` — standard library, no external dependency.

**Inputs / Output**
```
pts  →  list of (x, y) tuples
returns →  float   (distance in mm)
```

---

### `_recover_layer_name(doc, target_lower)`

**Purpose**
Looks up the **exact, case-preserved** layer name from the DXF layer table,
given a lowercase search string.

**Why it exists**
DXF layer names are case-sensitive. A user might type `"plano-carpet"` but the
file stores it as `"PLANO-CARPET"`. This function searches `doc.layers` (the
authoritative list of layers inside the DXF) case-insensitively and returns the
original spelling so that later filtering works correctly.

**Library used**
`ezdxf` — specifically `doc.layers`, which is the layer table object exposed
by the ezdxf document.

**Inputs / Output**
```
doc           →  ezdxf Document object
target_lower  →  str  (user-supplied layer name, already lowercased)
returns       →  str  (canonical DXF name)  OR  None if not found
```

---

### `_try_lwpolyline(entity)`

**Purpose**
Extracts the `(x, y)` vertex list from an **LWPOLYLINE** entity (the most
common polyline type in modern DXF files).

**Why it exists**
LWPOLYLINE (Lightweight Polyline) is a compact DXF entity that stores all
its vertices in a single object. It has an `is_closed` flag, but that flag
is not always set even when the shape is geometrically closed. This function:
1. Reads all vertices using `entity.get_points("xy")`, converting each
   coordinate to a plain Python `float` to avoid numpy types in the output.
2. Checks the `entity.closed` flag first (the official way).
3. If not officially closed, calls `_geometric_gap` and accepts the shape
   if the gap is less than 1 mm (tolerance fallback).
4. If neither check passes, prints an alert with the start point, end point,
   and exact gap, then returns `None` to skip the shape.

**Library used**
`ezdxf` — `entity.get_points("xy")` and `entity.closed` are ezdxf API calls.

**Inputs / Output**
```
entity  →  ezdxf LWPOLYLINE entity object
returns →  list of (float, float) tuples   OR   None (shape skipped)
```

---

### `_try_polyline(entity)`

**Purpose**
Extracts the `(x, y)` vertex list from an older **POLYLINE** entity (used in
DXF files created by older CAD software or certain export paths).

**Why it exists**
The legacy POLYLINE entity stores its closure state as a **bit flag** inside
`entity.dxf.flags`. Bit `0` (value `1`) means the polyline is closed. This
function:
1. Reads the closure flag with `entity.dxf.flags & 1`.
2. Collects vertices by iterating `entity.vertices` and reading each vertex's
   `dxf.location.x` / `dxf.location.y` — the direct property access approach
   that works reliably across all ezdxf versions.
3. Applies the same 1 mm geometric gap fallback as `_try_lwpolyline` if the
   flag is not set.
4. Prints an alert and returns `None` if the shape cannot be confirmed closed.

> **Note on vertex access:** The earlier implementation used `entity.points()`,
> which is a higher-level iterator. It was replaced with
> `[(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]`
> to access the raw VERTEX sub-entity coordinates directly, avoiding potential
> issues with the `points()` method on some DXF dialects.

**Library used**
`ezdxf` — `entity.dxf.flags`, `entity.vertices`, and `v.dxf.location` are
ezdxf API calls.

**Inputs / Output**
```
entity  →  ezdxf POLYLINE entity object
returns →  list of (float, float) tuples   OR   None (shape skipped)
```

---

### `_try_spline(entity)`

**Purpose**
Extracts the `(x, y)` vertex list from a **SPLINE** entity (a smooth curved
boundary defined by control points).

**Why it exists**
Splines are mathematically defined curves — they do not have a simple vertex
list like polylines do. The control points that define the curve are **not**
the same as the boundary points. This function uses ezdxf's `entity.flattening()`
method, which approximates the spline as a sequence of short straight line
segments (with a maximum deviation of 0.01 DXF units from the true curve),
giving a dense and accurate polygon boundary. Closure is checked via
`entity.closed`, with the same 1 mm geometric fallback.

**Library used**
`ezdxf` — `entity.flattening(0.01)` and `entity.closed` are ezdxf API calls.

**Inputs / Output**
```
entity  →  ezdxf SPLINE entity object
returns →  list of (float, float) tuples   OR   None (shape skipped)
```

---

### `extract_layer_polygons(dxf_path, layer_names)`

**Purpose**
The **main public function** of this script. Given a DXF file path and a list
of layer names, it returns every closed polygon shape found on those layers.

**Why it exists**
This is the single entry point for the BOQ pipeline. Any downstream script
(flooring area, hatch generator, zone boundary) calls this one function and
gets back clean `(x, y)` coordinate lists ready to feed into Shapely.

**How it works — step by step**
1. Opens the DXF file with `ezdxf.readfile()`.
2. For each requested layer name, calls `_recover_layer_name` to confirm the
   layer exists and get its correct casing.
3. **Alert 1** — prints a warning for any layer name that does not exist in
   the DXF's layer table at all, and returns `[]` for that key.
4. Scans every entity in the modelspace (the main drawing area of the DXF).
5. For each entity on a target layer, dispatches to `_try_lwpolyline`,
   `_try_polyline`, or `_try_spline` depending on the entity type. Other
   entity types (lines, text, hatches, blocks) are silently ignored.
6. **Alert 2** — after the scan, prints a warning for any layer that existed
   in the layer table but produced zero closed shapes.
7. Returns the result dictionary.

**Library used**
`ezdxf` — file loading, modelspace iteration, entity type dispatch.

**Signature**
```python
def extract_layer_polygons(
    dxf_path: str,
    layer_names: list[str],
) -> dict[str, list[list[tuple[float, float]]]]:
```

**Return value structure**
```python
{
    "PLANO-CARPET":     [ [(x1,y1), (x2,y2), ...] ],          # one shape
    "PLANO-TRADE AREA": [ [(x1,y1), ...], [(x1,y1), ...] ],   # two shapes
    "PLANO-NONEXISTENT": [],                                   # not found
}
```

---

### `validate_polygons(raw)`

**Purpose**
Converts the raw vertex lists returned by `extract_layer_polygons` (or
`_extract_all_from_doc`) into **Shapely Polygon objects**, keeping only
geometrically valid ones.

**Why it exists**
A list of `(x, y)` coordinates is not yet a proper geometric object — it is
just numbers. To compute area in m², check for self-intersections, get
bounding boxes, or use the shape for spatial operations (overlap, containment,
etc.), you need a Shapely Polygon. This function bridges that gap. It also
acts as a quality gate: self-intersecting or degenerate shapes are caught here
and reported with a plain-English explanation before they can cause silent
errors downstream.

**How it works**
1. For each shape, constructs a `ShapelyPolygon(pts)`.
2. Checks `poly.is_valid` (Shapely's built-in geometry validity test).
3. If valid → adds the Shapely object to the output list.
4. If invalid → calls `explain_validity(poly)` (from `shapely.validation`)
   and prints a human-readable description of the problem (e.g., "Self-intersection
   at coordinate (x, y)"). The invalid polygon is excluded from the output.

**Library used**
`shapely.geometry.Polygon` — creates the Polygon object.
`shapely.validation.explain_validity` — describes why a polygon is invalid.

**Signature**
```python
def validate_polygons(
    raw: dict[str, list[list[tuple[float, float]]]]
) -> dict[str, list]:   # values are lists of ShapelyPolygon
```

**Typical downstream usage**
```python
raw       = extract_layer_polygons(path, ["PLANO-CARPET"])
validated = validate_polygons(raw)

carpet    = validated["PLANO-CARPET"][0]
area_sqm  = carpet.area / 1_000_000   # DXF units are mm → convert to m²
```

---

### `_fmt_vertices(shapes)`

**Purpose**
Formats the vertex-count summary string shown in the Section 1 scan result table.

**Why it exists**
A layer can contain multiple separate closed shapes. This helper produces a
readable summary like `"26"` for one shape or `"8 / 10"` for two shapes (8
vertices and 10 vertices respectively), which fits neatly into the table column.

**Library used**
None (pure Python string formatting).

**Inputs / Output**
```
shapes  →  list of shape-lists (the value from the raw dict)
returns →  str   e.g. "26"  or  "8 / 10"  or  "—" (if empty)
```

---

### `_load_doc_safe(dxf_path)`

**Purpose**
Loads a DXF file with `ezdxf.readfile()` and exits cleanly with an error
message if the file cannot be opened.

**Why it exists**
`main()` needs the document object before calling `extract_layer_polygons`
or `_extract_all_from_doc`. This helper centralises the error-handling so the
same safe loading pattern is not repeated inline.

**Library used**
`ezdxf.readfile` — parses the DXF file from disk into a document object.
`sys.exit` — terminates the process if loading fails.

**Inputs / Output**
```
dxf_path  →  str
returns   →  ezdxf Document object  (or exits the process on failure)
```

---

### `_extract_all_from_doc(doc)`

**Purpose**
Scans the **entire DXF** — every entity on every layer — and collects all
closed shapes into a dictionary keyed by layer name.

**Why it exists**
When no layer names are supplied on the command line, the user wants to see
every closed polygon in the whole file without knowing layer names in advance.
This function provides that "scan everything" mode. Unlike `extract_layer_polygons`,
it does not check the layer table for specific names and does not emit Alert 1
or Alert 2 warnings — it simply collects whatever is closed and groups it by
the layer attribute on each entity.

**Key design decisions**
- Reuses the three existing `_try_lwpolyline`, `_try_polyline`, `_try_spline`
  extractors — no duplication of closure logic.
- Only layers that have **at least one closed shape** appear in the result —
  empty layers produce no noise.
- The already-loaded `doc` object is passed in directly, so the DXF file is
  **not loaded a second time**.

**Library used**
`ezdxf` — `doc.modelspace()`, `entity.dxf.layer`, `entity.dxftype()`.

**Inputs / Output**
```
doc     →  ezdxf Document object (already loaded)
returns →  dict[layer_name, list of shapes]   (only layers with ≥ 1 shape)
```

---

### `_print_sections(raw, validated, display_order, doc_layers_lower)`

**Purpose**
Prints the four structured output sections (Scan Result table, All Vertices,
Shapely Validation, Next Step, and the DEBUG raw dict) to the terminal.

**Why it exists**
Both code paths in `main()` — the full-scan path and the named-layer path —
need to produce identical formatted output. Extracting the printing logic into
this single helper avoids duplicating ~100 lines of formatting code and ensures
the output format stays consistent regardless of how the shapes were collected.

**How it works**

| Section | Content |
|---|---|
| Section 1 — Scan Result | Table: layer, shape count, vertex count, status |
| Section 2 — All Vertices | Every `(x, y)` coordinate numbered to 4 decimal places |
| Section 3 — Shapely Validation | Area m², bounding box, valid/invalid per shape |
| Section 4 — Next Step | One-line recommendation based on valid shape count |
| DEBUG | `pprint(raw)` — the full raw dictionary for inspection |

**`doc_layers_lower` controls the status column in Section 1:**
- `None` (full-scan mode) — every displayed layer already has shapes, so status
  shows `✓ N/N valid` or `⚠ N/N valid` based on Shapely validation only.
- A `set[str]` (named-layer mode) — status can also show `⚠ not in DXF` (Alert 1)
  or `⚠ no shapes` (Alert 2) for layers that were requested but came up empty.

**Library used**
`shapely` (via `ShapelyPolygon` and `explain_validity`) — for Section 3.
`pprint` — for the DEBUG raw dict dump.

**Inputs**
```
raw              →  dict[layer_name, list of shapes]
validated        →  dict[layer_name, list of ShapelyPolygon]
display_order    →  list[str]  (controls row order in output)
doc_layers_lower →  set[str] | None
```

---

### `main()`

**Purpose**
The command-line interface. Parses arguments, drives the extraction and
validation pipeline, and prints all output sections to the terminal.

**Why it exists**
Allows the script to be run directly from the terminal without writing any
Python code, making it usable for quick inspection of any DXF file.

**How it works — two modes**

| Mode | Trigger | What happens |
|---|---|---|
| **Full scan** | No layer names given | Calls `_extract_all_from_doc(doc)` → scans every entity in the DXF → prints all 5 output sections for every layer that has closed shapes |
| **Named-layer** | One or more layer names given | Calls `extract_layer_polygons(dxf_path, layer_args)` → scans only the requested layers → prints all 5 output sections with Alert 1/2 statuses |

Both modes share `_print_sections()` for identical output formatting.

**Library used**
`sys.argv` — reads the DXF path and layer names from the command line.
`ezdxf` (via `_load_doc_safe`) — loads the document before branching.
`shapely` (via `validate_polygons`) — for geometry validation in Section 3.

**Command syntax**
```bash
# Full scan — no layer names: extracts everything from the entire DXF
python extract_layer_polygons_closed.py "path/to/file.dxf"

# Named-layer mode — targeted extraction
python extract_layer_polygons_closed.py "path/to/file.dxf" "PLANO-CARPET" "PLANO-TRADE AREA"
```

---

## Data Flow Diagram

```
DXF file on disk
      │
      ▼
_load_doc_safe()              ← ezdxf.readfile()  (loaded once in main)
      │
      ├─── No layer args ─────────────────────────────────┐
      │                                                    ▼
      │                                      _extract_all_from_doc(doc)
      │                                      (scans ALL entities, all layers)
      │                                                    │
      └─── Layer args given ──────────────────────────────┐│
                │                                          ││
                ▼                                          ││
      extract_layer_polygons(dxf_path, layer_args)         ││
      (loads DXF again internally, filters by layer)       ││
                │                                          ││
      _recover_layer_name()  ← doc.layers                  ││
      Alert 1 / Alert 2 checks                             ││
                │                                          ││
                └──────────────────────────────────────────┘│
                                    │                        │
                          Both paths produce raw dict        │
                                    │◄───────────────────────┘
                                    ▼
                         validate_polygons(raw)
                         ← shapely.Polygon + explain_validity
                                    │
                                    ▼
                         _print_sections(raw, validated, ...)
                         ← pprint for DEBUG section
                                    │
                                    ▼
                    5 sections printed to terminal
                    (Scan Result, Vertices, Validation,
                     Next Step, DEBUG raw dict)
```

---

## Alert Reference

| Alert | Meaning | When triggered |
|---|---|---|
| `⚠ WARNING: layer '...' does not exist in this DXF — skipping` | **Alert 1** — layer name is completely absent from the DXF layer table | Named-layer mode only: immediately after loading, before the entity scan |
| `⚠ WARNING: layer '...' found in layer table but no closed shapes extracted — skipping` | **Alert 2** — layer exists but all its polylines were open or had no polyline entities | Named-layer mode only: after the full modelspace scan |
| `⚠ LWPOLYLINE not closed  start=...  end=...  gap=... mm — SKIPPED` | An open polyline was found whose gap was ≥ 1 mm (cannot be treated as closed) | During entity scan, inside `_try_lwpolyline` |
| `⚠ POLYLINE not closed  start=...  end=...  gap=... mm — SKIPPED` | Same as above but for legacy POLYLINE entity | During entity scan, inside `_try_polyline` |
| `⚠ SPLINE not closed  start=...  end=...  gap=... mm — SKIPPED` | Same as above but for SPLINE entity | During entity scan, inside `_try_spline` |
| `⚠ INVALID polygon — layer '...' shape #N: ...` | Shapely found the polygon geometry is self-intersecting or otherwise invalid | Inside `validate_polygons` |

---

## Changelog

| Change | What was updated |
|---|---|
| `_try_polyline` vertex access | Switched from `entity.points()` to `[(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]` for reliable cross-version POLYLINE support |
| Added `_extract_all_from_doc(doc)` | Full-DXF scan mode when no layer names are passed |
| Added `_print_sections(...)` | Extracted 4-section printing from `main()` so both scan modes share one formatter |
| `main()` no-args behaviour | Changed from listing PLANO-* layers to running a full scan and printing all 5 sections |
| Added DEBUG section | `pprint(raw)` printed after Section 4 in every run, using `pprint` standard library |

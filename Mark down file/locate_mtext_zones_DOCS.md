# locate_mtext_zones.py — Full Documentation

**Script:** `locate_mtext_zones.py`
**DXF File:** `DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf`
**Purpose:** For every MTEXT label in a DXF file, find the nearest polyline boundary and return its full vertex list (bbox). Used in the Lenskart BOQ pipeline to associate zone labels with their enclosing floor area polygons.

---

## Table of Contents

1. [Dependencies](#1-dependencies)
2. [Constants](#2-constants)
3. [Functions — Quick Reference](#3-functions--quick-reference)
4. [Function Details](#4-function-details)
   - [_strip_mtext](#_strip_mtext)
   - [_gap](#_gap)
   - [_extract_vertices](#_extract_vertices)
   - [_collect_polys](#_collect_polys)
   - [_nearest_vertex_dist](#_nearest_vertex_dist)
   - [_find_zone](#_find_zone)
   - [_matches_pattern](#_matches_pattern)
   - [locate_mtext_zones](#locate_mtext_zones)
   - [_discovery](#_discovery)
   - [_print_report](#_print_report)
   - [_clean_for_print](#_clean_for_print)
   - [_cli](#_cli)
5. [How to Run](#5-how-to-run)
   - [Mode 3 — Full DXF Scan](#mode-3--full-dxf-scan)
   - [Mode 2 — Layer Scan](#mode-2--layer-scan)
   - [Mode 1 — Targeted Search](#mode-1--targeted-search)
   - [Output Flags](#output-flags)
   - [Python API Usage](#python-api-usage)
6. [Output Dictionary Structure](#6-output-dictionary-structure)
7. [Common Pitfalls](#7-common-pitfalls)

---

## 1. Dependencies

| Library | Used For | Install |
|---|---|---|
| `ezdxf` | Reading DXF files, iterating modelspace entities (MTEXT, LWPOLYLINE, POLYLINE, SPLINE) | `pip install ezdxf` |
| `shapely` | Building Shapely Polygon objects from vertex lists, polygon validity check | `pip install shapely` |
| `re` | Regular expression pattern matching for MTEXT format-code stripping | Built-in |
| `math` | `math.hypot()` for Euclidean distance calculation between points | Built-in |
| `fnmatch` | Wildcard text pattern matching (supports `*`, `?`, `[seq]`) | Built-in |
| `sys` | Reading CLI arguments (`sys.argv`) | Built-in |
| `pprint` | Pretty-printing the result dictionary in `--dict` / `--dict-full` mode | Built-in |
| `typing` | Type hints (`List`, `Dict`, `Optional`, `Tuple`) | Built-in |

---

## 2. Constants

```python
CLOSURE_GAP_TOLERANCE = 1.0   # mm -- do not change
DIST_WARN_MM          = 5000.0  # flag hits whose nearest-vertex dist exceeds this
```

| Constant | Value | Meaning |
|---|---|---|
| `CLOSURE_GAP_TOLERANCE` | `1.0` mm | If the first and last vertices of a polyline are within 1 mm of each other, the polyline is treated as geometrically closed even if the close flag is not set in the DXF |
| `DIST_WARN_MM` | `5000.0` mm | Any hit whose nearest-vertex distance exceeds 5000 mm is flagged `[!] CHECK DISTANCE` in the report |

---

## 3. Functions — Quick Reference

| Function | Type | Purpose |
|---|---|---|
| `_strip_mtext(raw)` | Private | Clean MTEXT entity text by removing DXF inline formatting codes |
| `_gap(p1, p2)` | Private | Euclidean distance between two (x, y) points |
| `_extract_vertices(e)` | Private | Extract vertex list from any polyline entity (LWPOLYLINE / POLYLINE / SPLINE) |
| `_collect_polys(msp)` | Private | Scan entire modelspace and collect all polylines from all layers |
| `_nearest_vertex_dist(point, vertices)` | Private | Find minimum distance from a point to any vertex in a list |
| `_find_zone(point, polys)` | Private | Find the polyline whose nearest vertex is closest to a given point |
| `_matches_pattern(clean, pattern)` | Private | Test if a cleaned text string matches a search pattern |
| `locate_mtext_zones(...)` | **Public API** | Main function — match MTEXT labels to nearest polylines |
| `_discovery(doc)` | Private | Print all layers, MTEXT layers, and closed polyline layers in the DXF |
| `_print_report(result, ...)` | Private | Format and print the 3-section terminal report |
| `_clean_for_print(result, compact)` | Private | Produce a clean copy of the result dict safe for `pprint` |
| `_cli()` | Private | Parse command-line arguments and dispatch to the correct mode |

---

## 4. Function Details

---

### `_strip_mtext`

```python
def _strip_mtext(raw: str) -> str
```

**Why it exists:**
DXF MTEXT entities store their text with embedded formatting codes — things like `\pxql;Mirror`, `\H2.5;CARPET`, `\fArial|b1|i0;TEXT`. If you try to search or match against the raw string, the codes will cause false negatives. This function strips all codes and returns the clean human-readable text.

**How it works:**
1. Applies a compiled regex (`_MTEXT_CODE_RE`) that matches all known MTEXT inline codes in a single pass:
   - `\H`, `\W`, `\Q`, `\A`, `\F`, `\C` etc. with arguments: `\H2.5;`
   - `\pxql;`, `\pxqc,t8;` — paragraph alignment codes with parameters
   - Bare `\P`, `\p`, `\N`, `\n` — paragraph/newline breaks
   - `{` and `}` — grouping braces
   - `%%c`, `%%d`, `%%p` — degree symbol, diameter symbol, plus/minus
   - `\\~` — non-breaking space
   - `\\U+XXXX` — Unicode escape codes
2. Replaces DXF paragraph breaks (`^J`, `^M`, `\r\n`, `\r`, `\n`) with a single space
3. Collapses multiple consecutive spaces into one
4. Strips leading/trailing whitespace

**Libraries used:** `re` (built-in)

**Input/Output example:**
```
Raw:   '\pxql;JJ-EYE'
Clean: 'JJ-EYE'

Raw:   '\fArial|b1|i0|c0|p34;CABLE TREY LEGEND'
Clean: 'CABLE TREY LEGEND'
```

---

### `_gap`

```python
def _gap(p1: tuple, p2: tuple) -> float
```

**Why it exists:**
Used in two places: (1) to check whether a polyline is geometrically closed (first vertex close to last vertex), and (2) inside `_extract_vertices` to compute the closure gap for reporting.

**How it works:**
Computes the straight-line Euclidean distance between two `(x, y)` coordinate tuples using `math.hypot(dx, dy)`. Returns the distance in mm (DXF units).

**Libraries used:** `math` (built-in)

---

### `_extract_vertices`

```python
def _extract_vertices(e) -> Optional[Tuple[List[tuple], str, str]]
```

**Why it exists:**
Different DXF entity types store their geometry differently. An `LWPOLYLINE` uses `.get_points("xy")`, a `POLYLINE` stores vertices in sub-entities, and a `SPLINE` must be flattened into line segments. This function normalises all three into a plain Python list of `(x, y)` tuples.

**How it works:**
Checks the entity type with `e.dxftype()` then extracts vertices accordingly:

| Entity Type | How vertices are read | Library call |
|---|---|---|
| `LWPOLYLINE` | `e.get_points("xy")` — returns (x, y, bulge, start_width, end_width); we take only x, y | `ezdxf` |
| `POLYLINE` | Iterates `e.vertices`, reads `v.dxf.location.x` and `.y` from each vertex sub-entity | `ezdxf` |
| `SPLINE` | `e.flattening(0.01)` — converts the spline curve into straight-line approximation points with 0.01 mm tolerance | `ezdxf` |

For each type:
- Checks if the entity has a `closed` flag (`is_closed`, `flags & 1`, or `e.closed`)
- If closed and the first/last vertex don't match, appends first vertex to close the ring
- Computes the geometric gap between first and last vertex
- Returns a `closure` string: `"flag"`, `"geometric_gap=X.Xmm"`, or `"open_gap=X.Xmm"`

Returns `None` if the entity type is not supported or extraction fails.

**Libraries used:** `ezdxf` (external), `math` (built-in)

---

### `_collect_polys`

```python
def _collect_polys(msp, poly_layer: str = "") -> List[Dict]
```

**Why it exists:**
Builds a global index of every polyline in the DXF. The `poly_layer` parameter is kept for API compatibility but is intentionally **ignored** — the scan always covers all layers. This ensures that `_find_zone` can find the nearest polyline anywhere in the file, not just on a specific layer.

**How it works:**
1. Iterates every entity in `msp` (the DXF modelspace)
2. Calls `_extract_vertices(e)` for each entity; skips entities that return `None`
3. Skips entities with fewer than 2 vertices
4. For closed shapes with 4+ vertices, attempts to build a `ShapelyPolygon`:
   - If the polygon is invalid, tries `poly.buffer(0)` to repair it
   - Stores the Shapely object for potential future containment checks
5. Stores each polyline as a dict:

```python
{
    "vertices":    [(x1,y1), (x2,y2), ...],  # list of (x, y) tuples
    "entity_type": "LWPOLYLINE",              # LWPOLYLINE / POLYLINE / SPLINE
    "closure":     "flag",                   # flag / geometric_gap=Xmm / open_gap=Xmm
    "layer":       "LK-FF&E WALL",           # DXF layer name (canonical)
    "shapely":     <ShapelyPolygon or None>  # None for open polylines
}
```

**Libraries used:** `ezdxf` (external), `shapely` (external)

---

### `_nearest_vertex_dist`

```python
def _nearest_vertex_dist(point: tuple, vertices: List[tuple]) -> float
```

**Why it exists:**
Core distance measurement used by `_find_zone`. For a given MTEXT insertion point, this checks every vertex of a polyline and returns the distance to the closest one.

**How it works:**
Loops through all `(vx, vy)` in `vertices`, computes `math.hypot(px - vx, py - vy)` for each, and keeps the running minimum. Returns that minimum distance in mm.

**Libraries used:** `math` (built-in)

---

### `_find_zone`

```python
def _find_zone(point: tuple, polys: List[Dict]) -> tuple
```

**Why it exists:**
This is the spatial matching engine. For each MTEXT label, it answers: "which polyline in the entire DXF is geometrically closest to this label's position?"

**How it works:**
1. If `polys` is empty → returns `(None, "no_polygon", None)`
2. Loops over every poly dict in `polys`
3. Calls `_nearest_vertex_dist(point, poly["vertices"])` for each polyline
4. Tracks the polyline with the smallest minimum vertex distance
5. Returns that polyline dict, the method string `"nearest_vertex"`, and the distance in mm

This is a **global scan** — no layer filter, no containment check. Every MTEXT independently finds its own closest polyline.

**Libraries used:** `math` (built-in, via `_nearest_vertex_dist`)

---

### `_matches_pattern`

```python
def _matches_pattern(clean: str, pattern: str) -> bool
```

**Why it exists:**
Provides flexible text search so users can find MTEXT labels without needing the exact full text. Supports wildcards (`*`, `?`) and plain substring search.

**How it works:**
1. Converts both `clean` and `pattern` to lowercase
2. First tries `fnmatch(clean, "*pattern*")` — this allows wildcards:
   - `"Mirror"` matches `"Mirror"`, `"Large Mirror"`, `"Mirror Panel"`
   - `"PICKUP*"` matches `"PICKUP TABLE"`, `"PICKUP STORAGE-1200"`
   - `"*AREA*"` matches `"TRADE AREA"`, `"CARPET AREA"`
3. If `fnmatch` returns False, falls back to plain `in` substring check

**Libraries used:** `fnmatch` (built-in)

---

### `locate_mtext_zones`

```python
def locate_mtext_zones(
    dxf_path:    str,
    mtext_layer: str,
    poly_layer:  str = "",     # kept for API compatibility; ignored internally
    texts:       Optional[List[str]] = None,
) -> Dict[str, List[Dict]]
```

**Why it exists:**
This is the **main public function** — the entry point for programmatic use. It ties together all the helper functions into a complete pipeline: read DXF → collect all polylines → collect MTEXT on the requested layer → match each MTEXT to its nearest polyline → return structured results.

**How it works (step by step):**

```
1. ezdxf.readfile(dxf_path)         → load the DXF
2. _collect_polys(msp)               → scan ALL layers, build poly index
3. Loop MTEXT entities on mtext_layer
     → _strip_mtext(entity.text)     → clean the text
     → record (clean_text, layer, insertion_point)
4. For each text pattern in `texts` (or "__ALL__" if texts=None):
     → _matches_pattern(clean, pattern)  → filter candidates
     → _find_zone(position, polys)       → nearest-vertex match
     → build hit dict
5. Return dict[pattern -> list[hit_dict]]
```

**Parameter details:**

| Parameter | Type | Description |
|---|---|---|
| `dxf_path` | `str` | Path to the `.dxf` file |
| `mtext_layer` | `str` | Exact DXF layer name where MTEXT labels live (case-insensitive match) |
| `poly_layer` | `str` | Ignored — kept for backwards compatibility only |
| `texts` | `list[str] or None` | List of search patterns. `None` = return all MTEXT on the layer |

**Libraries used:** `ezdxf` (external), `shapely` (external, via `_collect_polys`), `math` (built-in, via `_find_zone`)

**Returns:** `dict[str, list[dict]]`

Each hit dict contains:

```python
{
    "position":                  (x, y),         # MTEXT insertion point in mm
    "raw_text":                  "Mirror",        # cleaned text after _strip_mtext()
    "layer":                     "LN-TEXT",       # MTEXT source layer (canonical)
    "match_method":              "nearest_vertex",# always nearest_vertex
    "bbox":                      [(x1,y1), ...],  # full vertex list of matched polyline
    "_nearest_vertex_dist_mm":   30.11,           # distance in mm to closest vertex
    "_poly_layer":               "LK-FF&E WALL",  # layer of the matched polyline
    "_entity_type":              "LWPOLYLINE",    # LWPOLYLINE / POLYLINE / SPLINE
    "_closure":                  "flag",          # closure method
}
```

---

### `_discovery`

```python
def _discovery(doc) -> None
```

**Why it exists:**
When you don't know the DXF layer structure, this function prints a full map of every layer, every layer that has MTEXT entities, and every layer that has closed polylines. Run this first on an unknown DXF to discover the correct layer names before running a targeted search.

**How it works:**
Iterates `doc.layers` for the full layer list, then iterates the modelspace twice: once counting MTEXT entities per layer (with sample text), once counting closed LWPOLYLINE and POLYLINE entities per layer.

**Libraries used:** `ezdxf` (external)

**When it runs:** Automatically in Mode 3 (full DXF scan). You can also call it directly in Python.

---

### `_print_report`

```python
def _print_report(result: Dict, mode_label: str, has_poly: bool) -> None
```

**Why it exists:**
Formats the raw result dictionary into a structured 3-section human-readable terminal report every time a search is run.

**How it works:**

**Section 1 — Match Summary table**
Prints one row per hit. Columns: Key, Hits, Method, Layer, Poly Layer, Dist(mm), Position. Appends `[!] CHECK` to any row whose `_nearest_vertex_dist_mm` exceeds `DIST_WARN_MM` (5000 mm).

**Section 2 — BBOX Vertices per match**
For every hit, prints all metadata fields (`raw_text`, `layer`, `position`, `match_method`, `nearest_vertex_dist`, `poly_layer`, `entity_type`, `closure`) plus the first 3 bbox vertices and a count of remaining vertices.

**Section 3 — Next Step / Action**
Prints counts (total hits, nearest_vertex count, no_polygon count, flagged hits), a distance summary table for every hit, and a one-sentence recommendation on what to do next.

**Libraries used:** None (pure formatting with `str.format()`)

---

### `_clean_for_print`

```python
def _clean_for_print(result: Dict, compact: bool = True) -> Dict
```

**Why it exists:**
The raw result dict from `locate_mtext_zones()` contains long bbox vertex lists that would flood the terminal if printed with `pprint`. This function produces a clean copy suitable for `pprint` output.

**How it works:**
- `compact=True` (`--dict` flag): replaces `bbox` with `bbox_pts` (integer vertex count)
- `compact=False` (`--dict-full` flag): keeps full `bbox` vertex list
- Always includes: `raw_text`, `layer`, `position`, `match_method`, `_poly_layer`, `_nearest_vertex_dist_mm`, `_entity_type`, `_closure`
- Strips no other private keys — all diagnostic fields are preserved

**Libraries used:** None (pure Python dict manipulation)

---

### `_cli`

```python
def _cli() -> None
```

**Why it exists:**
The command-line interface entry point. Reads `sys.argv`, strips flag arguments (`--dict`, `--dict-full`), parses the remaining tokens as layer names and text patterns, and dispatches to the correct mode.

**How it works — Mode detection:**

```
Remaining tokens after dxf_path are walked left-to-right:
  - Token found in doc.layers (case-insensitive) → starts a new layer group
  - Token NOT found in doc.layers → text pattern for the current layer group

Mode 3:  no tokens at all → full DXF scan
Mode 2:  layer tokens only, no text patterns → layer scan with spatial lookup
Mode 1:  at least one text pattern found → targeted search
```

**Mode 2 change (important):** Mode 2 now calls `locate_mtext_zones(dxf_path, layer, "", None)` instead of bypassing spatial lookup. This means every Mode 2 hit gets a populated bbox and a real `_nearest_vertex_dist_mm` value.

**Libraries used:** `sys` (built-in), `ezdxf` (external)

---

## 5. How to Run

### Prerequisites

```
pip install ezdxf shapely
```

Open a terminal in the project folder:
```
cd "c:\Users\rashe\Downloads\dxf_12_11_Surrya\DOC_NEW_INPUT_DXF"
```

Switch terminal to UTF-8 (Windows — run once per session):
```
chcp 65001
```

---

### Mode 3 — Full DXF Scan

**Use when:** You don't know the layer names. This prints all layers, all MTEXT layers, and all closed polyline layers, then lists every MTEXT in the file.

```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf"
```

What you see:
- Full layer list
- All MTEXT layers with entity counts and sample text
- All closed LWPOLYLINE layers with counts
- Section 1/2/3 report for all MTEXT (bbox is empty in Mode 3 — no spatial lookup)

---

### Mode 2 — Layer Scan

**Use when:** You know the layer name but want all MTEXT on that layer with their nearest polylines.

```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT"
```

Multiple layers at once:
```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" "PLANO-TXT"
```

What you see:
- All MTEXT on the named layer(s)
- Every hit has `bbox` populated and `_nearest_vertex_dist_mm` filled
- Section 1/2/3 report with distance summary table

---

### Mode 1 — Targeted Search

**Use when:** You know exactly which text labels you are looking for.

```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" "Mirror"
```

Wildcard patterns:
```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" "PICKUP*"
```

Multiple patterns on one layer:
```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" "Mirror" "PICKUP*" "DINING*"
```

Multiple layers with their own patterns:
```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" "Mirror" "PLANO-TXT" "VC-EYE"
```

---

### Output Flags

Add these anywhere after the DXF path:

| Flag | What it prints |
|---|---|
| *(no flag)* | 3-section formatted report only |
| `--dict` | Report + compact dictionary (bbox shown as `bbox_pts` count) |
| `--dict-full` | Report + full dictionary with complete bbox vertex lists |

Examples:
```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" --dict
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" "Mirror" --dict-full
```

Save output to a file:
```
python locate_mtext_zones.py "DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "LN-TEXT" --dict-full > output.txt
```

---

### Python API Usage

```python
from locate_mtext_zones import locate_mtext_zones
from shapely.geometry import Polygon as ShapelyPolygon

DXF = r"DXF FILE/BOQ_DEV_STD_SAMPLE _2025.05.20.dxf"

# Get all MTEXT on LN-TEXT layer
result = locate_mtext_zones(
    dxf_path    = DXF,
    mtext_layer = "LN-TEXT",
    poly_layer  = "",          # ignored -- global scan always used
    texts       = None,        # None = return everything; or pass ["Mirror", "PICKUP*"]
)

for pattern, hits in result.items():
    for hit in hits:
        print("Text  :", hit["raw_text"])
        print("Layer :", hit["layer"])
        print("Method:", hit["match_method"])
        print("Dist  :", hit["_nearest_vertex_dist_mm"], "mm")
        print("PolyL :", hit["_poly_layer"])
        print("BBox  :", len(hit["bbox"]), "vertices")

        # Compute area if bbox is a closed polygon
        if len(hit["bbox"]) >= 4:
            zone    = ShapelyPolygon(hit["bbox"])
            area_m2 = zone.area / 1_000_000   # DXF units are mm; convert to m2
            print("Area  :", round(area_m2, 3), "m2")
        print()
```

---

## 6. Output Dictionary Structure

```python
{
    "Mirror": [                          # key = pattern searched (or "__ALL__")
        {
            "position":                  (1259.54, -890704.98),  # MTEXT insertion point (mm)
            "raw_text":                  "Mirror",               # cleaned text string
            "layer":                     "LN-TEXT",              # MTEXT source layer
            "match_method":              "nearest_vertex",       # always nearest_vertex
            "bbox":                      [(x1,y1), (x2,y2), ...], # polyline vertices
            "_nearest_vertex_dist_mm":   1483.29,                # dist to closest vertex (mm)
            "_poly_layer":               "PLANO-TRADE AREA",     # layer of matched polyline
            "_entity_type":              "LWPOLYLINE",           # entity type
            "_closure":                  "flag",                 # closure method
        },
        ...
    ]
}
```

---

## 7. Common Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `bbox` is empty | Mode 3 does not run spatial lookup | Use Mode 2 or Mode 1, or call the Python API |
| Text shows as `-EYE` instead of `JJ-EYE` | Old `_strip_mtext` had `[\r\n\^J]` which matched literal `J` | Fixed — current version uses `\^J` as two-char sequence |
| Text shows `xql;TEXT` (code prefix not stripped) | Old regex missed `\pxql;` style codes with arguments | Fixed — current regex includes `\\p[^;]*;` |
| `_poly_layer` is None | Hit came from Mode 3 bypass (no spatial lookup ran) | Re-run in Mode 2 or via Python API |
| Very large distances (>5000 mm) | MTEXT label is far from any polyline — likely a legend/title block entity | Check visually in DXF; exclude from BOQ if it is a legend label |
| Wrong area for a zone | `bbox` is an open polyline (e.g. `open_gap=2494mm`) | Open polylines cannot form valid areas; check DXF and close the boundary in CAD |
| Layer name not recognised | Typo or wrong casing in the CLI argument | Run Mode 3 first to print exact canonical layer names |
| `ezdxf` import error | Library not installed | `pip install ezdxf` |
| `shapely` import error | Library not installed | `pip install shapely` |

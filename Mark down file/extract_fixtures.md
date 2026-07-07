# `extract_fixtures.py` — Documentation

High-level reference for every function and building block in
[`extract_fixtures.py`](extract_fixtures.py). It explains **what** each piece
does, **why** it exists, **how** it works, and **which library** it depends on.

---

## Purpose of the script

Read a **DXF** CAD drawing and pull out every **`INSERT`** (block reference) as a
fixture, compute each one's **world-space (WCS) bounding box**, group the results
by block name, and draw those boxes back onto a copy of the drawing. The result
is a plain Python dictionary that downstream BIM/geometry pipelines can consume
directly, plus an annotated `_bbox.dxf` you can open in any CAD viewer.

- **Input:** a DXF file path, plus optional layer names and block-name filters.
- **Output (stdout):** a JSON dictionary keyed by fixture (block) name, followed
  by a one-line-per-fixture summary.
- **Output (file):** a copy of the DXF with bounding-box outlines drawn on a new
  `BBOX_OUTPUT` layer, saved as `<stem>_bbox.dxf` next to the input (the original
  is never overwritten).

The three invocation **modes** are selected purely from `sys.argv` — no
`argparse`/`click` — and resolved against the document's actual layer table.

---

## Library dependencies

| Library | Type | Why it is used |
|---------|------|----------------|
| **`ezdxf`** | Third-party (`pip install ezdxf`) | The core DXF engine. Reads the file, exposes the modelspace, queries `INSERT` entities, resolves block definitions, gives the `matrix44()` block→WCS transform, and writes the annotated drawing back out. |
| **`ezdxf.bbox`** | Part of `ezdxf` | `extents()` computes the local (block-coordinate) bounding box of a whole block definition in one call. |
| **`ezdxf.math.Vec3`** | Part of `ezdxf` | 3D vector used to build the 8 box corners that get transformed into world space. |
| **`json`** | Standard library | Serializes the result dict to stdout as `json.dumps(result, indent=2)` — the machine-readable contract. |
| **`logging`** | Standard library | Emits diagnostics (INFO) and alerts (WARNING/ERROR) without using `print`, so stdout stays clean for JSON and the caller controls verbosity. |
| **`os`** | Standard library | Builds the `<stem>_bbox.dxf` output path next to the input file. |
| **`sys`** | Standard library | Raw `sys.argv` access (the entire CLI grammar) and the process exit code. |
| **`typing`** | Standard library | Type hints (`Dict`, `List`, `Optional`, `Sequence`, `Tuple`) that document the data shapes and enable static checking. |
| **`pprint`** | Standard library | Pretty-prints each block record in the standalone debug loop at the end of `main`. |
| **`__future__.annotations`** | Standard library | Lets type hints be written as strings (PEP 563), so `ezdxf` types in annotations don't need to resolve at runtime. |

---

## The three invocation modes

| Mode | Invocation | Meaning |
|------|------------|---------|
| **1 — Filtered** | `py extract_fixtures.py file.dxf LAYER_1 BLK_A BLK_B LAYER_2 BLK_C` | Keep only the **named blocks** under each named layer. |
| **2 — Layer-wide** | `py extract_fixtures.py file.dxf LAYER_1 LAYER_2` | Keep **all inserts** found on each named layer. |
| **3 — Full scan** | `py extract_fixtures.py file.dxf` | Keep **all inserts across all layers**. |

**How a mode is detected:** the tokens after the DXF path are matched against the
document's layer table. A token that *names a layer* opens a layer group; a
following token that *does not* name a layer is a block-name filter for the most
recent layer. No tokens after the path → Mode 3. All tokens match layers →
Mode 2. At least one token is a non-layer (block) filter → Mode 1.

---

## Module-level building blocks

### `logger`
```python
logger = logging.getLogger(__name__)
```
- **Why:** a single named logger for the module. Handlers/levels are *not*
  configured here — that is left to the caller (or the `__main__` block) so the
  file behaves well when imported as a library.
- **Library:** `logging`.

### Type aliases
```python
Coord3 = Tuple[float, float, float]              # one (x, y, z) point
Coord2 = Tuple[float, float]                     # one (x, y) point
LayerBlockMap = Dict[str, Optional[List[str]]]   # {layer: None | [block_name, ...]}
```
- **Why:** name the data shapes once so signatures stay readable. In a
  `LayerBlockMap`, a value of `None` means **all blocks** on that layer (Mode 2 /
  Mode 3) and a `list` means **only these block names** (Mode 1).
- **Library:** `typing`.

### `BBOX_LAYER`
```python
BBOX_LAYER = "BBOX_OUTPUT"
```
- **Why:** the single, dedicated layer that receives every drawn bounding-box
  outline in the output DXF. Auto-created if missing.

### `USAGE`
- **Why:** the help text printed for `-h`/`--help` and on a missing path. It
  documents all three modes and the layer/block grammar; the `BBOX_OUTPUT` layer
  name is interpolated in so docs and behavior never drift apart.

---

## Functions

### 1. `parse_args(argv, doc) -> LayerBlockMap`

- **Why it is used:** turns the raw `sys.argv` tokens into a
  `{layer: block-filter}` map, resolving the mode from the data rather than from
  flags.
- **How it works:**
  - Builds a **case-insensitive** lookup `lowercased name → actual layer name`
    from `doc.layers`.
  - Walks `argv[2:]` (skipping the program name and the DXF path):
    - A token that **matches a layer** opens (or re-opens) that layer group with
      a default value of `None` (= "all blocks").
    - A non-layer token **before any layer** is meaningless → **WARNING** + skip.
    - A non-layer token **after a layer** is a block filter: the group's value is
      promoted from `None` to a list and the token is appended.
  - Map keys use the layer's **actual original-case** name.
- **Library dependency:** `ezdxf` (reads `doc.layers`), `logging` (warnings).
- **Returns:** a possibly-empty `LayerBlockMap`. **Empty = Mode 3** (full scan).

---

### 2. `get_insert_bbox(insert, doc) -> (extmin, extmax) | None`

- **Why it is used:** computes the **world-space** axis-aligned bounding box of a
  single `INSERT`, accounting for its placement.
- **How it works:**
  1. Resolves the block definition via `doc.blocks.get(insert.dxf.name)`. A
     missing definition (orphan insert) → **WARNING** + `None`.
  2. Computes the **local** extents of the whole block with
     `ezdxf.bbox.extents(block, fast=True)`. No measurable geometry → **WARNING**
     + `None`.
  3. Builds the **8 corners** of that local box as `Vec3` points.
  4. Transforms all 8 corners into WCS with `insert.matrix44()` — which already
     folds in the insertion point, `xscale`/`yscale`/`zscale`, rotation, and the
     block base point.
  5. Returns the min/max of the transformed corners as `extmin`/`extmax`
     **plain float tuples** (not `Vec3`).
- **Library dependency:** `ezdxf`, `ezdxf.bbox`, `ezdxf.math.Vec3`, `logging`.
- **Returns:** `((x_min, y_min, z_min), (x_max, y_max, z_max))`, or `None` if the
  insert is an orphan or its block is empty.

---

### 3. `compute_center(extmin, extmax) -> Coord3`

- **Why it is used:** derives the geometric center of a bounding box for
  placement/snapping downstream.
- **How it works:** returns the per-axis midpoint
  `((xmin+xmax)/2, (ymin+ymax)/2, (zmin+zmax)/2)`.
- **Library dependency:** none (pure arithmetic).
- **Returns:** a `(cx, cy, cz)` float tuple.

---

### 4. `draw_bbox_on_dxf(msp, extmin, extmax, color) -> None`

- **Why it is used:** writes a visible bounding-box outline back into the drawing
  so the result can be inspected in a CAD viewer.
- **How it works:** builds the 4-corner **XY footprint** of the box and adds a
  **closed `LWPOLYLINE`** on the `BBOX_OUTPUT` layer with the given ACI `color`
  (`close=True` closes the loop implicitly).
- **Library dependency:** `ezdxf` (`msp.add_lwpolyline`).
- **Returns:** nothing (mutates the modelspace).

---

### 5. `extract_fixtures(doc, layer_block_map) -> Dict[str, dict]`

The main aggregation step. Scans the document and returns geometry grouped by
fixture (block) name. **Identical return schema for all three modes.**

- **Why it is used:** turns the matched `INSERT` entities into a simple,
  name-keyed dictionary of bounding boxes that other code can process.
- **How it works (step by step):**
  1. Gets the modelspace and **ensures the `BBOX_OUTPUT` layer exists**
     (`doc.layers.add` if missing).
  2. `full_scan = not layer_block_map` — an empty map means Mode 3.
  3. Iterates `msp.query("INSERT")`. For each, reads its layer
     (`insert.dxf.layer`) and block name (`insert.dxf.name`).
  4. **Applies the filter** (skipped entirely in full-scan mode):
     - Layer not in the map → skip.
     - Layer's filter is a list and the block name isn't in it → skip.
  5. Calls `get_insert_bbox()`; if it returns `None` (orphan/empty) → skip.
  6. Computes the `center` and the closed 5-point `outline`.
  7. Colors the box with `hash(block_name) % 256` and calls
     `draw_bbox_on_dxf()`.
  8. **Aggregates** with the `setdefault` pattern: `result.setdefault(block_name,
     {"count": 0, "blocks": []})`, increments `count`, and appends a record. The
     record's first field is **`fixture_name` (= `insert.dxf.name`)**, so each
     entry is self-describing even outside its parent key.
  9. Logs **INFO** if nothing matched and returns the dict.
- **Library dependency:** `ezdxf` (query + entity access), `logging`.
- **Returns:** the result dict (see schema below); `{}` if no inserts matched.

---

### 6. `_output_path(dxf_path) -> str`

- **Why it is used:** computes the safe output path so the original DXF is never
  overwritten.
- **How it works:** joins the input's directory with `<stem>_bbox.dxf` using
  `os.path`.
- **Library dependency:** `os`.

---

### 7. `main(argv) -> int`

- **Why it is used:** the orchestrator and process exit-code source.
- **How it works:**
  1. Handles `-h`/`--help` → prints `USAGE`, returns `0`.
  2. Missing path → logs **ERROR**, prints usage to stderr, returns `2`.
  3. `ezdxf.readfile(dxf_path)`; `IOError` → ERROR + `1`, `DXFStructureError` →
     ERROR + `1`.
  4. Calls `parse_args()` and logs which **mode** was resolved.
  5. Calls `extract_fixtures()`, prints `json.dumps(result, indent=2)` to stdout,
     then a `NAME: N instances found` summary line per fixture.
  6. Saves the annotated DXF via `doc.saveas(_output_path(...))`.
  7. A `pprint` debug loop prints each block record grouped by fixture.
- **Library dependency:** `ezdxf`, `json`, `logging`, `pprint`, `sys`.
- **Returns:** process exit code (`0` ok, `1` file error, `2` usage error).

---

### 8. `__main__` block (command-line entry point)

- **Why it is used:** lets the file run as a standalone CLI tool, not just an
  importable library.
- **How it works:** configures `logging` at **INFO** to **stderr** (so stdout
  carries only JSON + summary), then `sys.exit(main(sys.argv))`.
- **Note:** logging writes to **stderr**. In PowerShell, append `2>$null` to hide
  the INFO/WARNING lines (PowerShell renders stderr in red as if it were an
  error, even though it isn't).

---

## Return schema

Identical for all three modes:

```python
{
  "FIXTURE_NAME": {                          # = insert.dxf.name
    "count": 2,
    "blocks": [
      {
        "fixture_name": "FIXTURE_NAME",      # same value as the outer key
        "layer": "LAYER_NAME",
        "bounding_box": {
          "extmin": (x_min, y_min, z_min),
          "extmax": (x_max, y_max, z_max),
          "outline": [(x1, y1), (x2, y2), (x3, y3), (x4, y4), (x1, y1)]
        },
        "center": (cx, cy, cz)
      }
      # ... one entry per instance of this block
    ]
  }
  # ... one bucket per distinct block name
}
```

> **Note on `fixture_name`:** the fixture name and the insert/block name are the
> **same value** (`insert.dxf.name`). It appears both as the outer dictionary key
> and inside each block record, so an individual entry is identifiable on its own.

---

## Usage

```powershell
# Mode 3 — all inserts across all layers, quiet logging:
py extract_fixtures.py "DXF FILE\BOQ_DEV_STD_standardized.dxf" 2>$null

# Mode 2 — every insert on the given layers:
py extract_fixtures.py "DXF FILE\BOQ_DEV_STD_standardized.dxf" "CLINIC_2" 2>$null

# Mode 1 — only the named blocks under a layer (case-insensitive layer match):
py extract_fixtures.py "DXF FILE\BOQ_DEV_STD_standardized.dxf" "CLINIC_2" "PHOROPTER" "DOCTOR STOOL" 2>$null

# Help:
py extract_fixtures.py --help
```

```python
# As a library:
import ezdxf
from extract_fixtures import parse_args, extract_fixtures

doc = ezdxf.readfile("drawing.dxf")
layer_block_map = parse_args(["prog", "drawing.dxf", "CLINIC_2"], doc)  # Mode 2
fixtures = extract_fixtures(doc, layer_block_map)
for name, info in fixtures.items():
    print(name, info["count"], info["blocks"][0]["center"])
```

---

## Behavior summary

| Situation | Handling | In result? |
|-----------|----------|------------|
| INSERT matches the filter | bbox computed, box drawn, aggregated | ✅ yes |
| Orphan INSERT (block def missing) | WARNING, skip | ❌ no |
| Block exists but has no geometry | WARNING, skip | ❌ no |
| Non-layer token before any layer | WARNING, skip token | n/a |
| Layer requested but absent / no matches | (silently produces no entries) | ❌ no |
| No inserts matched at all | INFO log, returns `{}` | — |
| Missing / unreadable file | ERROR, exit code `1` | — |
| Missing path argument | ERROR + usage, exit code `2` | — |
| `-h` / `--help` | print usage, exit code `0` | — |

---

## Outputs at a glance

| Channel | Content |
|---------|---------|
| **stdout** | `json.dumps(result, indent=2)`, then `NAME: N instances found` per fixture, then the `pprint` debug dump. |
| **stderr** | `logging` mode banner, warnings, and the saved-file path. |
| **file** | `<stem>_bbox.dxf` next to the input — a copy with bounding boxes drawn on layer `BBOX_OUTPUT`. The original is never overwritten. |

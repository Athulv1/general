# `extract_polylines_opened.py` — Documentation

High-level reference for every function and building block in
[`extract_polylines_opened.py`](extract_polylines_opened.py). It explains **what** each piece
does, **why** it exists, **how** it works, and **which library** it depends on.

---

## Purpose of the script

Read a **DXF** CAD drawing and pull out the vertex geometry of its **open**
polylines and lines, organized by layer. The result is a plain Python
dictionary that downstream BIM/geometry pipelines can consume directly.

- **Input:** a DXF file path, plus an optional list of layer names to restrict to.
- **Output:** `{ "layer_name": [ {"points": [(x, y, z), ...]}, ... ] }`
- **Rule:** only **open** polylines/lines are kept. **Closed** polylines are
  alerted and skipped.

---

## Library dependencies

| Library | Type | Why it is used |
|---------|------|----------------|
| **`ezdxf`** | Third-party (`pip install ezdxf`) | The core DXF parser. Reads the file, exposes the modelspace, iterates entities, and gives typed access to entity attributes (layer, vertices, elevation, flags). Everything CAD-specific comes from here. |
| **`logging`** | Standard library | Emits diagnostics (DEBUG) and alerts (WARNING) without using `print`, so the calling application controls verbosity and output destination. |
| **`typing`** | Standard library | Type hints (`Dict`, `List`, `Tuple`, `Optional`, `Iterable`) that document the data shapes and enable static checking. |
| **`argparse`** | Standard library | Parses command-line arguments when the script is run directly (file path + optional layer names). |
| **`pprint`** | Standard library | Pretty-prints the final dictionary in the standalone demo block. |
| **`__future__.annotations`** | Standard library | Lets type hints be written as strings (PEP 563), so e.g. `ezdxf` types in annotations don't need to be resolved at runtime. |

---

## Module-level building blocks

### `logger`
```python
logger = logging.getLogger(__name__)
```
- **Why:** a single named logger for the module. Handlers/levels are *not*
  configured here — that is left to the caller (or the `__main__` block) so
  this file behaves well when imported as a library.
- **Library:** `logging`.

### Type aliases
```python
Point = Tuple[float, float, float]          # one (x, y, z) vertex
Polyline = Dict[str, List[Point]]           # {"points": [Point, ...]}
LayerGeometry = Dict[str, List[Polyline]]   # {layer_name: [Polyline, ...]}
```
- **Why:** name the return schema once so signatures stay readable and the
  intended structure is self-documenting.
- **Library:** `typing`.

---

## Functions

### 1. `is_closed_polyline(entity) -> bool`

- **Why it is used:** decides whether an `LWPOLYLINE` forms a closed loop. The
  main function uses this to drop closed shapes and keep only open ones.
- **How it works:**
  - DXF stores the closed state as a **bit flag** inside the integer
    `entity.dxf.flags`. The "closed" bit is `ezdxf.const.LWPOLYLINE_CLOSED`
    (value `1`).
  - It uses a **bitwise AND** mask (`flags & LWPOLYLINE_CLOSED`) instead of
    `flags == 1`, so other flag bits never produce a wrong answer.
  - `getattr(entity.dxf, "flags", 0)` guards entities that have no `flags`
    attribute — those are treated as open.
- **Library dependency:** `ezdxf` (for `ezdxf.const.LWPOLYLINE_CLOSED` and the
  `entity.dxf` attribute access).
- **Returns:** `True` if closed, `False` if open.

---

### 2. `extract_polylines_by_layer(dxf_path, layers=None) -> LayerGeometry`

The main entry point. Parses the file and returns geometry grouped by layer.

- **Why it is used:** turns an opaque DXF drawing into a simple, layer-keyed
  Python dictionary of open-polyline vertices that other code can process.

- **Inputs:**
  - `dxf_path` *(str)* — path to the DXF file.
  - `layers` *(optional iterable of str)* — restrict extraction to these layer
    names. **Case-insensitive.** `None`/empty means "all layers".

- **How it works (step by step):**
  1. **Open the file** — `ezdxf.readfile(dxf_path)` parses the DXF into a
     document; `doc.modelspace()` returns the modelspace (the main drawing area)
     as an iterable of entities. *(library: `ezdxf`)*
  2. **Normalize the filter** — requested layer names are lowercased into a
     `set` named `wanted` for fast, case-insensitive matching; `None` when no
     filter is given.
  3. **Iterate every entity** — one pass over all entities on all layers.
     For each: read its type (`entity.dxftype()`) and layer
     (`entity.dxf.layer.lower()`). *(library: `ezdxf`)*
  4. **Apply the layer filter** — if a filter is active and this entity's layer
     isn't requested, skip it immediately.
  5. **Branch by entity type:**
     - **`LWPOLYLINE`** — call `is_closed_polyline()`. If closed, log a
       **WARNING** ("segment is closed, skipping") and skip. If open, read the
       Z height from `dxf.elevation` (default `0.0`), then build `(x, y, z)`
       tuples from `entity.get_points("xy")`. *(library: `ezdxf`)*
     - **`LINE`** — treated as a 2-point open polyline using `dxf.start` and
       `dxf.end` (each a 3D `Vec3` with `.x/.y/.z`). *(library: `ezdxf`)*
     - **anything else** (MTEXT, INSERT, CIRCLE, …) — skipped silently with a
       DEBUG log only.
  6. **Store the result** — `result.setdefault(layer_name, []).append({"points": points})`
     lazily creates the per-layer list and appends, which naturally supports
     **multiple polylines per layer**.
  7. **Warn on empty requested layers** — for any explicitly requested layer
     that produced no geometry (missing, or only closed/unsupported entities),
     log a **WARNING**. Such layers are intentionally omitted from the result.
  8. **Return** the dictionary.

- **Library dependencies:** `ezdxf` (parsing + entity access), `logging`
  (warnings/debug), `typing` (annotations).

- **Errors:** `ezdxf.readfile` raises on a missing or malformed file
  (`FileNotFoundError`, `DXFStructureError`); these propagate to the caller.

---

### 3. `__main__` block (command-line entry point)

- **Why it is used:** lets the file run as a standalone CLI tool, not just an
  importable library.
- **How it works:**
  - Configures `logging` at DEBUG so messages are visible during a manual run.
  - Uses `argparse` to read a required `dxf_path` and zero or more optional
    `layers` (`nargs="*"`). An empty layer list is converted to `None` so the
    "all layers" path is used.
  - Calls `extract_polylines_by_layer(...)` and prints a per-layer summary, then
    pretty-prints the full dictionary with `pprint`.
- **Library dependencies:** `argparse`, `logging`, `pprint` (all standard library).
- **Note:** logging writes to **stderr**. In PowerShell, append `2>$null` to
  hide the DEBUG/WARNING lines (PowerShell renders stderr in red as if it were
  an error, even though it isn't).

---

## Usage

```powershell
# All layers:
py extract_polylines_opened.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" 2>$null

# Specific layers (case-insensitive), quiet logging:
py extract_polylines_opened.py "DXF FILE\BOQ_DEV_STD_SAMPLE _2025.05.20.dxf" "PLANO-DOOR" "PLANO-GLASS" 2>$null
```

```python
# As a library:
from extract_polylines import extract_polylines_by_layer

geometry = extract_polylines_by_layer("drawing.dxf", layers=["A-WALL"])
for layer, polylines in geometry.items():
    for poly in polylines:
        print(layer, poly["points"])
```

---

## Behavior summary

| Entity type | Open/closed handling | Kept? | Z source |
|-------------|----------------------|-------|----------|
| `LWPOLYLINE` (open)   | n/a | ✅ yes | `dxf.elevation` (default `0.0`) |
| `LWPOLYLINE` (closed) | WARNING + skip | ❌ no | — |
| `LINE`                | always open (2 points) | ✅ yes | true 3D `start.z` / `end.z` |
| any other type        | DEBUG log, skip | ❌ no | — |

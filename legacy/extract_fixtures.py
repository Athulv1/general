"""Extract INSERT (block reference) fixtures from a DXF, with world-space bounding boxes.

A single-file CLI built on ``ezdxf`` that scans a DXF for ``INSERT`` entities,
computes each one's world-space (WCS) bounding box, aggregates the results by
block name, draws the boxes back onto a copy of the drawing, and prints the
aggregate as JSON.

Three invocation modes are selected purely from ``sys.argv`` (no argparse/click):

  MODE 1 -- Filtered (layer + block names)
      python extract_fixtures.py file.dxf LAYER_1 BLK_A BLK_B LAYER_2 BLK_C
      Keep only the named blocks under each named layer.

  MODE 2 -- Layer-wide (layers only)
      python extract_fixtures.py file.dxf LAYER_1 LAYER_2
      Keep ALL inserts found on each named layer.

  MODE 3 -- Full DXF scan (path only)
      python extract_fixtures.py file.dxf
      Keep ALL inserts across ALL layers.

The argument grammar is resolved against the actual layer table of the loaded
document: a token that names a layer starts a new layer group; a following token
that does NOT name a layer is a block-name filter for the most recent layer.

Requires: ezdxf (pip install ezdxf), Python 3.9+.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple, Any

import ezdxf
import ezdxf.bbox
from ezdxf.math import Vec3

# Module-level logger. Configuration (handlers/level) is left to the caller;
# the __main__ block configures it for standalone runs.
logger = logging.getLogger(__name__)

# Type aliases describing the data shapes used throughout.
Coord3 = Tuple[float, float, float]
Coord2 = Tuple[float, float]
# Maps an actual layer name -> block-name filter. ``None`` means "all blocks on
# this layer" (Mode 2 / Mode 3); a list means "only these block names" (Mode 1).
LayerBlockMap = Dict[str, Optional[List[str]]]

# Layer that receives the drawn bounding-box outlines in the output DXF.
BBOX_LAYER = "BBOX_OUTPUT"

USAGE = """\
Extract INSERT fixtures from a DXF with world-space bounding boxes.

Usage:
  python extract_fixtures.py <file.dxf>                          (Mode 3: all inserts, all layers)
  python extract_fixtures.py <file.dxf> LAYER [LAYER ...]        (Mode 2: all inserts on the given layers)
  python extract_fixtures.py <file.dxf> LAYER BLK [BLK ...] ...  (Mode 1: only the named blocks per layer)

Notes:
  * Tokens that name a layer in the DXF start a layer group; tokens that do not
    are treated as block-name filters for the most recently named layer.
  * Results are printed as JSON to stdout; a copy of the DXF with the bounding
    boxes drawn on layer "%s" is saved next to the input as <stem>_bbox.dxf.

Options:
  -h, --help   Show this message and exit.
""" % BBOX_LAYER


def parse_args(argv: Sequence[str], doc: "ezdxf.document.Drawing") -> LayerBlockMap:
    """Resolve CLI tokens into a layer -> block-filter map against the document.

    The first element of ``argv`` is the program name and the second is the DXF
    path; both are ignored here (the document is already loaded). Remaining
    tokens are interpreted relative to the document's layer table:

      * A token that names an existing layer opens a new layer group whose
        filter starts as ``None`` (meaning "all blocks").
      * A token that does NOT name a layer is appended as a block-name filter to
        the most recently opened layer group, switching that group's value from
        ``None`` to a concrete list.
      * A non-layer token appearing before any layer has been named has no
        parent group; it is logged as a warning and skipped.

    Matching is case-insensitive; keys in the returned map use the layer's
    actual (original-case) name as stored in the DXF.

    Args:
        argv: The full argument vector (``sys.argv``).
        doc: The already-loaded DXF document, used to resolve layer names.

    Returns:
        A possibly-empty ``LayerBlockMap``. An empty map signals Mode 3 (scan
        every insert on every layer); the caller treats it accordingly.
    """
    # Build a case-insensitive lookup from lowercased name -> actual layer name.
    layer_lookup = {layer.dxf.name.lower(): layer.dxf.name for layer in doc.layers}

    layer_block_map: LayerBlockMap = {}
    current_layer: Optional[str] = None

    # Tokens after the program name and the DXF path are the layer/block grammar.
    for token in argv:
        actual = layer_lookup.get(token.lower())
        if actual is not None:
            # A recognized layer opens (or re-opens) a group defaulting to "all".
            current_layer = actual
            layer_block_map.setdefault(current_layer, None)
        elif current_layer is None:
            # A block filter with no preceding layer is meaningless; warn + skip.
            logger.warning(
                "Token '%s' is not a layer in the DXF and has no preceding "
                "layer to filter; skipping",
                token,
            )
        else:
            # A non-layer token filters the most recently named layer. Promote
            # the group's value from None ("all") to a concrete filter list.
            if layer_block_map[current_layer] is None:
                layer_block_map[current_layer] = []
            layer_block_map[current_layer].append(token)

    return layer_block_map


def get_insert_bbox(
    insert: "ezdxf.entities.Insert", doc: "ezdxf.document.Drawing"
) -> Optional[Tuple[Coord3, Coord3]]:
    """Compute the world-space (WCS) bounding box of a single INSERT.

    The block definition is measured in its own coordinate system, then the 8
    corners of that local box are transformed into WCS by the insert's
    ``matrix44()`` -- which already incorporates the insertion point, the
    ``xscale``/``yscale``/``zscale`` factors, the rotation, and the block base
    point. The axis-aligned extents of the transformed corners are returned.

    Args:
        insert: The INSERT entity to measure.
        doc: The document the insert belongs to, used to resolve its block def.

    Returns:
        ``(extmin, extmax)`` as ``(x, y, z)`` float tuples, or ``None`` if the
        referenced block definition is missing (orphan insert) or empty.
    """
    block = doc.blocks.get(insert.dxf.name)
    if block is None:
        # Orphan insert: references a block definition that does not exist.
        logger.warning(
            "INSERT references missing block '%s'; skipping", insert.dxf.name
        )
        return None

    # Local (block-coordinate) extents of the whole block definition. ``fast``
    # uses primitive bounding boxes -- ample for an enclosing box.
    local = ezdxf.bbox.extents(block, fast=True)
    if not local.has_data:
        # Block exists but contains no measurable geometry (e.g. only text defs).
        logger.warning(
            "Block '%s' has no measurable geometry; skipping", insert.dxf.name
        )
        return None

    lo, hi = local.extmin, local.extmax
    # 8 corners of the local axis-aligned box.
    corners = [
        Vec3(x, y, z)
        for x in (lo.x, hi.x)
        for y in (lo.y, hi.y)
        for z in (lo.z, hi.z)
    ]
    # matrix44() maps block coordinates -> WCS (location, scale, rotation, base).
    wcs = list(insert.matrix44().transform_vertices(corners))

    xs = [p.x for p in wcs]
    ys = [p.y for p in wcs]
    zs = [p.z for p in wcs]
    extmin: Coord3 = (float(min(xs)), float(min(ys)), float(min(zs)))
    extmax: Coord3 = (float(max(xs)), float(max(ys)), float(max(zs)))
    return extmin, extmax


def compute_center(extmin: Coord3, extmax: Coord3) -> Coord3:
    """Return the geometric center of an axis-aligned bounding box.

    Args:
        extmin: The ``(x, y, z)`` minimum corner.
        extmax: The ``(x, y, z)`` maximum corner.

    Returns:
        The midpoint ``((xmin+xmax)/2, (ymin+ymax)/2, (zmin+zmax)/2)``.
    """
    return (
        (extmin[0] + extmax[0]) / 2.0,
        (extmin[1] + extmax[1]) / 2.0,
        (extmin[2] + extmax[2]) / 2.0,
    )


def draw_bbox_on_dxf(
    msp: "ezdxf.layouts.Modelspace",
    extmin: Coord3,
    extmax: Coord3,
    color: int,
) -> None:
    """Draw a closed bounding-box outline onto the modelspace.

    Adds a closed LWPOLYLINE tracing the XY footprint of the box on the
    dedicated ``BBOX_OUTPUT`` layer, using the supplied ACI color so different
    block types are visually distinguishable.

    Args:
        msp: The modelspace to draw into.
        extmin: The ``(x, y, z)`` minimum corner of the box.
        extmax: The ``(x, y, z)`` maximum corner of the box.
        color: The ACI color index (0-255) for the outline.
    """
    xmin, ymin = extmin[0], extmin[1]
    xmax, ymax = extmax[0], extmax[1]
    footprint: List[Coord2] = [
        (xmin, ymin),
        (xmax, ymin),
        (xmax, ymax),
        (xmin, ymax),
    ]
    # close=True repeats the first vertex implicitly to close the loop.
    msp.add_lwpolyline(
        footprint,
        close=True,
        dxfattribs={"layer": BBOX_LAYER, "color": color},
    )

def point_in_polygon(point, polygon):
    """Ray casting algorithm. polygon is a list of (x, y) tuples."""
    x, y = point[0], point[1]
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def extract_fixtures(
    doc: "ezdxf.document.Drawing", layer_block_map: LayerBlockMap, store_outline: List[Tuple[float, float]]
) -> Dict[str, dict]:
    """Scan the document for matching INSERTs and aggregate them by block name.

    For every INSERT that passes the layer/block filter, compute its WCS
    bounding box, derive the center and footprint outline, draw the box onto the
    ``BBOX_OUTPUT`` layer, and accumulate the record under its block name.

    Filtering rules:
      * An empty ``layer_block_map`` means Mode 3 -- every insert on every layer.
      * Otherwise an insert is kept only if its layer is a key in the map AND
        (the layer's filter is ``None`` -> any block) OR (the block name is in
        the layer's filter list).

    Args:
        doc: The loaded DXF document (mutated: bbox outlines are added to it).
        layer_block_map: The layer -> block-filter map from :func:`parse_args`.

    Returns:
        The aggregate result dict keyed by block (fixture) name; ``{}`` if no
        inserts matched.
    """
    msp = doc.modelspace()

    # Ensure the output layer exists before drawing onto it.
    if BBOX_LAYER not in doc.layers:
        doc.layers.add(BBOX_LAYER)

    full_scan = not layer_block_map
    result: Dict[str, dict] = {}

    for insert in msp.query("INSERT"):
        layer_name = insert.dxf.layer
        block_name = insert.dxf.name
        
        if not full_scan:
            if layer_name not in layer_block_map:
                # Layer not requested -- ignore this insert.
                continue
            block_filter = layer_block_map[layer_name]
            if block_filter is not None and block_name not in block_filter:
                # Layer is requested but this specific block was not.
                continue

        bbox = get_insert_bbox(insert, doc)
        if bbox is None:
            # Orphan / empty block already logged inside get_insert_bbox.
            continue
        extmin, extmax = bbox
        center = compute_center(extmin, extmax)[:2]
        # print(block_name,center)
        if not point_in_polygon(center, store_outline):
            # print("FLAG", block_name)
            continue
        outline: List[Coord2] = [
            (extmin[0], extmin[1]),
            (extmax[0], extmin[1]),
            (extmax[0], extmax[1]),
            (extmin[0], extmax[1]),
            (extmin[0], extmin[1]),
        ]

        # Stable-per-run color so each distinct block type is drawn consistently.
        color = hash(block_name) % 256
        draw_bbox_on_dxf(msp, extmin, extmax, color)

        # Aggregate by fixture (block) name; lazily create the bucket.
        bucket = result.setdefault(block_name, {"count": 0, "blocks": []})
        bucket["count"] += 1
        bucket["blocks"].append(
            {
                "fixture_name": block_name,
                "layer": layer_name,
                "bounding_box": {
                    "extmin": extmin,
                    "extmax": extmax,
                    "outline": outline,
                },
                "center":   center,
                "height":   extmax[1] - extmin[1],
                "width":    extmax[0] - extmin[0],
                "pos":      (insert.dxf.insert.x, insert.dxf.insert.y),
                "rotation": getattr(insert.dxf, 'rotation', 0.0)
            }
        )

    if not result:
        logger.info("No matching INSERT entities found.")

    return result


def _output_path(dxf_path: str) -> str:
    """Return ``<stem>_bbox.dxf`` next to the input, never overwriting it."""
    directory = os.path.dirname(os.path.abspath(dxf_path))
    stem, _ext = os.path.splitext(os.path.basename(dxf_path))
    return os.path.join(directory, f"{stem}_bbox.dxf")


def extract_inserts(dxf_path: str, layers: List[str], store_outline: List[Tuple[float, float]]):
    try:
        doc = ezdxf.readfile(dxf_path)
    except IOError:
        logger.error("Cannot open DXF file: %s", dxf_path)
        return 1
    except ezdxf.DXFStructureError as exc:
        logger.error("Invalid or corrupt DXF '%s': %s", dxf_path, exc)
        return 1
    
    layer_block_map = parse_args(layers, doc)

    return extract_fixtures(doc, layer_block_map, store_outline)


def main(argv: Sequence[str]) -> int:
    """CLI entry point. Returns a process exit code."""
    # Help is handled before touching the filesystem so it always works.
    if any(token in ("-h", "--help") for token in argv[1:]):
        print(USAGE)
        return 0

    if len(argv) < 2:
        logger.error("Missing required DXF path argument.\n")
        print(USAGE, file=sys.stderr)
        return 2

    dxf_path = argv[1]

    # Let ezdxf raise on missing/malformed files; surface it cleanly to the user.
    try:
        doc = ezdxf.readfile(dxf_path)
    except IOError:
        logger.error("Cannot open DXF file: %s", dxf_path)
        return 1
    except ezdxf.DXFStructureError as exc:
        logger.error("Invalid or corrupt DXF '%s': %s", dxf_path, exc)
        return 1

    layer_block_map = parse_args(argv[2:], doc)

    # Report which mode we resolved to, for operator visibility.
    if not layer_block_map:
        logger.info("Mode 3: scanning ALL inserts across ALL layers.")
    elif all(value is None for value in layer_block_map.values()):
        logger.info("Mode 2: all inserts on layers %s.", sorted(layer_block_map))
    else:
        logger.info("Mode 1: filtered blocks per layer %s.", dict(layer_block_map))

    result = extract_fixtures(doc, layer_block_map)

    # JSON to stdout is the machine-readable contract; keep it clean.
    print(json.dumps(result, indent=2))

    # Human-readable summary (also stdout, after the JSON).
    for fixture_name, info in result.items():
        print(f"{fixture_name}: {info['count']} instances found")

    # Persist the annotated drawing alongside the original, never overwriting it.
    out_path = _output_path(dxf_path)
    doc.saveas(out_path)
    logger.info("Saved annotated DXF to %s", out_path)
    # print("extract...",result)
    from pprint import pprint

    for block_name, data in result.items():
        print(f"\n{block_name} ({data['count']} found)")
        
        for i, block in enumerate(data["blocks"], 1):
            print(f"\n[{i}]")
            pprint(block, sort_dicts=False)
    

    return 0


if __name__ == "__main__":
    # Configure logging for standalone runs; library users configure their own.
    # Logs go to stderr so stdout carries only the JSON + summary.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    sys.exit(main(sys.argv))

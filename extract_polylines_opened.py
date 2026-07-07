"""Extract OPEN polyline geometry from a DXF file, grouped by a user-supplied context key.

Iterates every entity in the modelspace, handling LWPOLYLINE and LINE
entities, and returns their vertices organized by caller-defined context keys.
Closed LWPOLYLINEs are skipped (with a warning); only open segments are kept.

Requires: ezdxf (pip install ezdxf), Python 3.9+.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import ezdxf

# Module-level logger. Configuration (handlers/level) is left to the caller;
# we only emit messages here so library consumers control output.
logger = logging.getLogger(__name__)

# Type aliases describing the return schema for clarity.
Point = Tuple[float, float, float]
ContextGeometry = Dict[str, List[List[Point]]]


def is_closed_polyline(entity: "ezdxf.entities.DXFEntity") -> bool:
    """Return True if the given LWPOLYLINE entity is closed.

    The "closed" state of an LWPOLYLINE is encoded as a bit flag in
    ``dxf.flags``. We mask it against ``LWPOLYLINE_CLOSED`` (value 1) rather
    than comparing equality, so other flag bits do not affect the result.
    Entities without a ``flags`` attribute are treated as open.
    """
    # getattr guards against entity types that lack the flags attribute.
    flags = getattr(entity.dxf, "flags", 0)
    return bool(flags & ezdxf.const.LWPOLYLINE_CLOSED)


def extract_polylines_by_layer_opened(
    dxf_path: str,
    layers: Optional[Dict[str, str]] = None
) -> ContextGeometry:
    """Parse a DXF file and return polyline vertices grouped by context key.

    Iterates ALL entities across ALL layers in the modelspace. Supported
    entity types:

      * LWPOLYLINE -- only OPEN polylines are kept; Z comes from
        ``dxf.elevation``. Closed polylines are skipped with a warning.
      * LINE       -- always kept; a 2-point open polyline (start -> end).

    Any other entity type is skipped silently (logged at DEBUG level).

    Args:
        dxf_path: Path to the DXF file to read.
        layers: Dict mapping context_key -> layer_name, e.g.
            {"store-maindoor": "plano-door", "store-mainglass": "plano-glass"}.
            Matching against DXF layer names is case-insensitive.
            When ``None``, every layer is returned with the layer name as key.

    Returns:
        A dictionary keyed by context_key. Each value is a list of polylines;
        each polyline is a list of (x, y, z) float tuples.
        Example: {"store-maindoor": [[(x,y,z), ...], [(x,y,z), ...]], ...}
    """
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    # Build reverse lookup: lowercased_layer_name -> context_key.
    # When layers is None, context_key == layer_name (no remapping).
    if layers:
        layer_to_key: Dict[str, str] = {v.lower(): k for k, v in layers.items()}
        wanted = set(layer_to_key.keys())
    else:
        layer_to_key = {}
        wanted = None

    result: ContextGeometry = {}

    # print(layer_to_key)
    # print(wanted)
    collect_entities(msp, doc, wanted, layer_to_key, result)

    if wanted is not None:
        found_layers = set(layer_to_key.keys()) & {entity.dxf.layer.lower() for entity in msp}
        for layer_lower, key in layer_to_key.items():
            if key not in result:
                logger.warning("No polyline/line geometry found for layer '%s' (key '%s')", layer_lower, key)

    return result

def collect_entities(entities, doc, wanted, layer_to_key, result, xref_transform=None):
    for entity in entities:
        dxftype = entity.dxftype()

        if dxftype == "INSERT":
            try:
                block = doc.blocks[entity.dxf.name]
                m = entity.matrix44()
                combined = m if xref_transform is None else xref_transform @ m
                collect_entities(block, doc, wanted, layer_to_key, result, combined)
            except Exception:
                pass
            continue

        try:
            layer_name = entity.dxf.layer.lower()
        except Exception:
            layer_name = "0"

        if wanted is not None and layer_name not in wanted:
            continue

        context_key = layer_to_key.get(layer_name, layer_name)
        points = None

        if dxftype == "LWPOLYLINE":
            if is_closed_polyline(entity):
                logger.warning(
                    "Closed polyline on layer '%s' -- segment is closed, skipping",
                    layer_name,
                )
                continue
            elevation = getattr(entity.dxf, "elevation", 0.0) or 0.0
            points = [
                (float(x), float(y), float(elevation))
                for x, y in entity.get_points("xy")
            ]
            # print(entity.dxf.layer)
            if xref_transform is not None:
                points = [tuple(xref_transform.transform((x, y, z))) for x, y, z in points]
            logger.debug("Open LWPOLYLINE on layer '%s': %d vertices", layer_name, len(points))

        elif dxftype == "LINE":
            start = entity.dxf.start
            end = entity.dxf.end
            points = [
                (float(start.x), float(start.y), float(start.z)),
                (float(end.x), float(end.y), float(end.z)),
            ]
            if xref_transform is not None:
                points = [tuple(xref_transform.transform(p)) for p in points]
            logger.debug("LINE on layer '%s': start=%s end=%s", layer_name, start, end)

        else:
            logger.debug(
                "Skipping unsupported entity '%s' on layer '%s'", dxftype, layer_name
            )
            continue

        if points is not None:
            result.setdefault(context_key, []).append(points)

# ---------------------------------------------------------------------------
# Usage example
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    # Configure logging for standalone runs; library users would do this
    # themselves rather than rely on this block.
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    # Accept the DXF path as a command-line argument so the file can be
    # chosen at runtime without editing the source.
    parser = argparse.ArgumentParser(description="Extract DXF polylines by layer.")
    parser.add_argument("dxf_path", help="Path to the DXF file to parse.")
    parser.add_argument(
        "layers",
        nargs="*",
        help="Optional layer name(s) to extract. Omit to extract all layers.",
    )
    cli_args = parser.parse_args()

    # Accept "key=layername" pairs or plain layer names (key == layer name).
    layers_map = None
    if cli_args.layers:
        layers_map = {}
        for token in cli_args.layers:
            if "=" in token:
                k, v = token.split("=", 1)
                layers_map[k] = v
            else:
                layers_map[token] = token

    geometry = extract_polylines_by_layer_opened(cli_args.dxf_path, layers_map)

    for key, polylines in geometry.items():
        print(f"Key '{key}': {len(polylines)} polyline(s)")
        for i, pts in enumerate(polylines):
            print(f"  [{i}] {len(pts)} points -> {pts[:2]}...")

    from pprint import pprint
    pprint(geometry)

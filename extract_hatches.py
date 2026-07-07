import os
import sys
import math
from typing import Any, Dict, List, Optional, Tuple

import ezdxf
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union


MM2_TO_SQFT = 1.0 / (304.8 ** 2)
MM2_TO_SQM = 1.0 / (1000.0 ** 2)


def _iter_hatches_in_block(doc, entities, layers, transform=None):
    """Recursively yield (hatch_entity, transform) pairs from entities and nested INSERTs."""
    layer_set = {l.upper() for l in layers}
    for entity in entities:
        etype = entity.dxftype()
        if etype == "HATCH":
            try:
                if entity.dxf.layer.upper() in layer_set:
                    yield entity, transform
            except Exception:
                pass
        elif etype == "INSERT":
            try:
                block_name = entity.dxf.name
                if block_name not in doc.blocks:
                    continue
                child_m = entity.matrix44()
                composed = transform @ child_m if transform is not None else child_m
                yield from _iter_hatches_in_block(doc, doc.blocks[block_name], layers, composed)
            except Exception:
                pass


def extract_hatch(
        dxf_path: str, layers: List[str]
) -> None:
        """Scan the source DXF for HATCH entities on HATCH_LAYERS and
        populate:

          self.hatch_outer_polygon  – the outer (largest) boundary polygon
          self.hatch_inner_polygons – inner hole polygons (cutouts in the hatch)
          self.hatch_wall_polygon   – outer minus inner = wall material area

        The outer boundary is the store perimeter drawn by the hatch fill.
        The inner holes are enclosed regions where no flooring goes (toilet,
        column pockets, doorways, etc.) as encoded in the source HATCH entity.
        The wall polygon = outer − union(inner) reproduces the cross-hatched
        wall section that appears in the original drawing.
        """

        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()

        found_layer = ""

        for entity, transform in _iter_hatches_in_block(doc, msp, layers):
            all_path_polys: List[Polygon] = []

            try:
                if not hasattr(entity, "paths"):
                    continue
                for path in entity.paths:
                    pts = _hatch_path_to_vertices(path, transform)
                    if not pts or len(pts) < 3:
                        continue
                    try:
                        poly = Polygon(pts)
                        if not poly.is_valid:
                            poly = poly.buffer(0)
                        if not poly.is_empty and poly.area > 0:
                            all_path_polys.append(poly)
                    except Exception:
                        continue
            except Exception:
                continue

            return [list(p.exterior.coords) for p in all_path_polys]

        if not found_layer:
            print(f"  ⚠ No HATCH entities found on {layers}")

        return None


def _hatch_path_to_vertices(path, transform=None) -> Optional[List[Tuple[float, float]]]:
        """Convert a HATCH boundary path (PolylinePath or EdgePath) to a
        flat list of (x, y) tuples suitable for Shapely Polygon construction.

        PolylinePath vertices carry an optional bulge value (arc segments).
        We ignore bulge here — the approximation is good enough for wall
        outlines whose curvature is either zero or very slight.

        EdgePath arcs are approximated by sampling at ≤10° intervals so
        curved door-frames render cleanly without excessive vertex counts.
        """
        def apply(pts):
            if transform is None or not pts:
                return pts
            transformed = list(transform.transform_vertices([(x, y, 0) for x, y in pts]))
            return [(p[0], p[1]) for p in transformed]

        try:
            if hasattr(path, "vertices") and path.vertices:
                pts = [(float(v[0]), float(v[1])) for v in path.vertices]
                pts = apply(pts)
                return pts if len(pts) >= 3 else None

            if hasattr(path, "edges") and path.edges:
                pts: List[Tuple[float, float]] = []
                for edge in path.edges:
                    etype = getattr(edge, "EDGE_TYPE", "")
                    if etype == "LineEdge":
                        s, e = edge.start, edge.end
                        if not pts:
                            pts.append((float(s[0]), float(s[1])))
                        pts.append((float(e[0]), float(e[1])))
                    elif etype == "ArcEdge":
                        cx, cy = float(edge.center[0]), float(edge.center[1])
                        r = float(edge.radius)
                        a0 = math.radians(float(edge.start_angle))
                        a1 = math.radians(float(edge.end_angle))
                        ccw = getattr(edge, "ccw", True)
                        if ccw and a1 < a0:
                            a1 += 2.0 * math.pi
                        elif not ccw and a1 > a0:
                            a1 -= 2.0 * math.pi
                        n = max(8, int(abs(a1 - a0) / math.radians(10)))
                        for i in range(n + 1):
                            t = a0 + (a1 - a0) * i / n
                            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
                    # SplineEdge / EllipseEdge: skip (rare in floor plans)
                pts = apply(pts)
                return pts if len(pts) >= 3 else None
        except Exception:
            pass
        return None

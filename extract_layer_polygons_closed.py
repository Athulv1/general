import logging
import sys
import math
import ezdxf
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.validation import explain_validity
from pprint import pprint

from typing import Optional, List, Dict
import json

_log = logging.getLogger(__name__)

def _reconfigure_stdout_for_cli() -> None:
    """CLI-only: make Unicode output work on Windows terminals.

    Deliberately NOT run at import time — library consumers must not have
    their stdout reconfigured as an import side effect.
    """
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ── internal helpers ──────────────────────────────────────────────────────────

def _geometric_gap(pts: list) -> float:
    dx = pts[-1][0] - pts[0][0]
    dy = pts[-1][1] - pts[0][1]
    return math.sqrt(dx * dx + dy * dy)


def _recover_layer_name(doc, target_lower: str):
    """Return canonical (case-preserved) layer name from doc.layers, or None."""
    for lt in doc.layers:
        try:
            if lt.dxf.name.strip().lower() == target_lower:
                return lt.dxf.name
        except Exception:
            pass
    return None


def _try_lwpolyline(entity):
    """Extract (x,y) points from a closed LWPOLYLINE. Returns list or None."""
    try:
        pts = [(float(x), float(y)) for x, y in entity.get_points("xy")]
        if len(pts) < 3:
            return None
        if not entity.closed:
            gap = _geometric_gap(pts)
            if gap >= 1.0:
                _log.warning(
                    "LWPOLYLINE not closed  start=%s end=%s gap=%.3f mm — skipped",
                    pts[0], pts[-1], gap)
                return None
        return pts
    except Exception as e:
        _log.warning("_try_lwpolyline: %s", e)
        return None


def _try_polyline(entity):
    """Extract (x,y) points from a closed POLYLINE. Returns list or None."""
    try:
        is_closed = bool(entity.dxf.flags & 1)
        pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
        if len(pts) < 3:
            return None
        if not is_closed:
            gap = _geometric_gap(pts)
            if gap >= 1.0:
                _log.warning(
                    "POLYLINE not closed  start=%s end=%s gap=%.3f mm — skipped",
                    pts[0], pts[-1], gap)
                return None
        return pts
    except Exception as e:
        _log.warning("_try_polyline: %s", e)
        return None


def _try_spline(entity):
    """Extract (x,y) points from a closed SPLINE via flattening. Returns list or None."""
    try:
        pts = [(p.x, p.y) for p in entity.flattening(0.01)]
        if len(pts) < 3:
            return None
        if not entity.closed:
            gap = _geometric_gap(pts)
            if gap >= 1.0:
                _log.warning(
                    "SPLINE not closed  start=%s end=%s gap=%.3f mm — skipped",
                    pts[0], pts[-1], gap)
                return None
        return pts
    except Exception as e:
        _log.warning("_try_spline: %s", e)
        return None


# Shapes are validated against the store-outline boundary ONLY when a
# store-outline reference is already present in the result dict (see
# collect_polyline_points).  Per-layer extraction passes no such reference,
# so validation is naturally skipped — no layer-name exemption list needed.


# ── public API ────────────────────────────────────────────────────────────────

def extract_layer_polygons_closed(
    dxf_path: str,
    layer_names: Dict[str, List],
) -> dict[str, list[list[tuple[float, float]]]]:
    """
    Extract all closed polygon boundaries from the named layers in a DXF file.

    Returns a dict mapping each requested layer name to a list of shapes,
    where each shape is a list of (x, y) tuples ready for ShapelyPolygon().

    Alert 1: layer name not in doc.layers → warns and returns [] for that key.
    Alert 2: layer in doc.layers but zero closed shapes found → warns after scan.
    """
    try:
        doc = ezdxf.readfile(dxf_path)
    except FileNotFoundError:
        _log.error("file not found: %s", dxf_path)
        return {key: [] for key in layer_names["priority"]}
    except ezdxf.DXFStructureError as e:
        _log.error("DXF parse error: %s", e)
        return {key: [] for key in layer_names["priority"]}
    except Exception as e:
        _log.error("error loading DXF: %s", e)
        return {key: [] for key in layer_names["priority"]}

    # Resolve each requested name against the layer table (case-insensitive).
    canonical: dict[str, str | None] = {
        key: _recover_layer_name(doc, key.strip().lower())
        for key in layer_names["priority"]
    }

    # Alert 1 — layer absent from the layer table entirely.
    for name, canon in canonical.items():
        if canon is None:
            _log.warning("layer %r does not exist in this DXF — skipping", name)

    result: dict[str, list[list[tuple[float, float]]]] = {layer_names[key]: [] for key in layer_names["priority"]}
    # print(json.dumps(result, indent=2))

    active_lowers = [
        name.strip().lower()
        for name, canon in canonical.items()
        if canon is not None
    ]

    if active_lowers:
        msp = doc.modelspace()
        for layer in active_lowers:
            req_name = next(
                (key for key, _ in layer_names.items() if key.strip().lower() == layer), None
            )
            # print(layer)
            lines = collect_polyline_points(msp, doc, layer, layer_names, result)
            result[layer_names[req_name]] = lines

    # Alert 2 — layer exists in table but nothing closed was found.
    for name, canon in canonical.items():
        if canon is not None and len(result[layer_names[name]]) == 0:
            _log.warning("layer %r found in layer table but no closed "
                         "shapes extracted — skipping", name)

    return result

def collect_polyline_points(entities, doc, active_layer, layer_names, result, xref_transform=None, recurse=False):
    results = []
    debug = False # active_layer.startswith("lk-door")

    for entity in entities:
        etype = entity.dxftype()

        if etype == "INSERT":
            try:
                block = doc.blocks[entity.dxf.name]
                m = entity.matrix44()
                combined = m if xref_transform is None else xref_transform @ m
                sub = collect_polyline_points(block, doc, active_layer, layer_names, result, combined, recurse=True)
                results.extend(sub)
            except Exception:
                pass
            continue

        try:
            elayer = entity.dxf.layer if entity.dxf.hasattr("layer") else "0"
        except Exception:
            elayer = "0"

        if elayer.strip().lower() != active_layer:
            continue

        # Validate a shape lies inside the store boundary ONLY when a
        # store-outline reference is already available in `result`. When this
        # function is called per-layer (no store reference yet — e.g. the store
        # outline itself, or a standalone zone layer) validation is skipped so
        # the extractor stays layer-name-agnostic instead of crashing on an
        # empty/absent "store-outline" key.
        store_ref = result.get("store-outline") or []
        validate = len(store_ref) > 0
        pts = None
        if debug: print(active_layer)
        if etype == "LWPOLYLINE":
            if debug:
                print("lwp")
            pts = _try_lwpolyline(entity)
        elif etype == "POLYLINE":
            if debug:
                print("pl")
            pts = _try_polyline(entity)
        elif etype == "SPLINE":
            if debug:
                print("sp")
            pts = _try_spline(entity)

        if pts is not None and xref_transform is not None:
            if debug: print("pts before transform", pts)
            pts = [list(xref_transform.transform(p))[:2] for p in pts]
            if debug: print("pts after transform", pts)

        if pts is not None:
            if validate:
                if debug:
                    print("pnp", polygon_inside_polygon(pts, store_ref[0]))
                if polygon_inside_polygon(pts, store_ref[0]):
                    if debug: print("added")
                    results.append(pts)
            else:
                results.append(pts)
        if debug: print()

    return results

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

def polygon_inside_polygon(inner, outer):
    """Returns True if all points of inner lie inside outer."""
    return all(point_in_polygon(p, outer) for p in inner) or polygon_overlaps_polygon(inner, outer, threshold=0.4)

def polygon_overlaps_polygon(inner, outer, threshold=0.5):
    """Returns True if inner overlaps outer by more than threshold (default 50%) of inner's area."""
    poly_inner = ShapelyPolygon([(p[0], p[1]) for p in inner])
    poly_outer = ShapelyPolygon([(p[0], p[1]) for p in outer])
    if not poly_inner.is_valid or not poly_outer.is_valid or poly_inner.area == 0:
        return False
    # print("area overlap", poly_inner.intersection(poly_outer).area/ poly_inner.area)
    return poly_inner.intersection(poly_outer).area / poly_inner.area > threshold

def validate_polygons(
    raw: dict[str, list[list[tuple[float, float]]]]
) -> dict[str, list]:
    """
    Convert raw vertex lists to validated Shapely Polygon objects.

    Returns dict[layer_name, list[ShapelyPolygon]] containing only valid polygons.
    Prints explain_validity() for any invalid polygon encountered.
    """
    out: dict[str, list] = {}
    for layer, shapes in raw.items():
        polys = []
        for i, pts in enumerate(shapes):
            poly = ShapelyPolygon(pts)
            if poly.is_valid:
                polys.append(poly)
            else:
                reason = explain_validity(poly)
                _log.warning("invalid polygon — layer %r shape #%d: %s",
                             layer, i + 1, reason)
        out[layer] = polys
    return out


# ── CLI helpers ───────────────────────────────────────────────────────────────

def _fmt_vertices(shapes: list) -> str:
    if not shapes:
        return "—"
    return " / ".join(str(len(s)) for s in shapes)


def _load_doc_safe(dxf_path: str):
    try:
        return ezdxf.readfile(dxf_path)
    except Exception as e:
        print(f"ERROR loading DXF: {e}")
        sys.exit(1)


def _extract_all_from_doc(doc) -> dict[str, list[list[tuple[float, float]]]]:
    """Scan every entity in modelspace; return closed shapes grouped by layer name.
    Only layers that contain at least one closed shape appear in the result.
    """
    result: dict[str, list] = {}
    for entity in doc.modelspace():
        try:
            layer = entity.dxf.layer if entity.dxf.hasattr("layer") else "0"
        except Exception:
            layer = "0"

        etype = entity.dxftype()
        pts = None
        if etype == "LWPOLYLINE":
            pts = _try_lwpolyline(entity)
        elif etype == "POLYLINE":
            pts = _try_polyline(entity)
        elif etype == "SPLINE":
            pts = _try_spline(entity)

        if pts and len(pts) >= 3:
            result.setdefault(layer, []).append(pts)

    return result


def _print_sections(
    raw: dict,
    validated: dict,
    display_order: list,
    doc_layers_lower,          # set[str] for named-layer mode, None for full-scan mode
):
    col_l, col_s, col_v = 32, 12, 16

    # ── SECTION 1: SCAN RESULT ────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("SECTION 1 — SCAN RESULT")
    print("=" * 72)

    header = (
        f"{'Layer':<{col_l}} │ {'Shapes found':>{col_s}} │ "
        f"{'Vertices':<{col_v}} │ Status"
    )
    sep = (
        "─" * col_l + "─┼─"
        + "─" * col_s + "─┼─"
        + "─" * col_v + "─┼─"
        + "─" * 20
    )
    print(header)
    print(sep)

    for name in display_order:
        shapes = raw.get(name, [])
        n_shapes = len(shapes)
        verts = _fmt_vertices(shapes)
        n_valid = len(validated.get(name, []))

        if doc_layers_lower is None:
            # Full-scan mode — every row in display_order already has shapes
            status = f"✓ {n_valid}/{n_shapes} valid" if n_valid == n_shapes else f"⚠ {n_valid}/{n_shapes} valid"
        else:
            if name.strip().lower() not in doc_layers_lower:
                status = "⚠ not in DXF"
            elif n_shapes == 0:
                status = "⚠ no shapes"
            else:
                status = f"✓ {n_valid}/{n_shapes} valid"

        print(
            f"{name:<{col_l}} │ {n_shapes:>{col_s}} │ "
            f"{verts:<{col_v}} │ {status}"
        )

    # ── SECTION 2: ALL VERTICES ───────────────────────────────────────────────
    print()
    print("=" * 72)
    print("SECTION 2 — ALL VERTICES (x, y) per shape")
    print("=" * 72)

    for name in display_order:
        shapes = raw.get(name, [])
        if not shapes:
            print(f"  {name!r}: []")
            continue
        print(f"  {name!r}:")
        for i, pts in enumerate(shapes):
            collapsed = len(set(pts)) < 3
            flag = "  ⚠ COLLAPSED (< 3 unique points)" if collapsed else ""
            print(f"    shape #{i + 1} — {len(pts)} vertices:{flag}")
            for j, (x, y) in enumerate(pts, 1):
                print(f"      [{j:>3}]  x={x:.4f}  y={y:.4f}")

    # ── SECTION 3: SHAPELY VALIDATION ─────────────────────────────────────────
    print()
    print("=" * 72)
    print("SECTION 3 — SHAPELY VALIDATION")
    print("=" * 72)

    any_shape = False
    for name in display_order:
        for i, pts in enumerate(raw.get(name, [])):
            any_shape = True
            poly = ShapelyPolygon(pts)
            area_m2 = poly.area / 1_000_000
            bounds = tuple(round(b, 2) for b in poly.bounds)
            if poly.is_valid:
                valid_str = "✓ valid"
            else:
                valid_str = f"✗ INVALID — {explain_validity(poly)}"
            print(
                f"  {name!r} shape #{i + 1}:  "
                f"area={area_m2:.4f} m²  bounds={bounds}  {valid_str}"
            )
    if not any_shape:
        print("  (no shapes extracted)")

    # ── SECTION 4: NEXT STEP / ACTION ─────────────────────────────────────────
    print()
    print("=" * 72)
    print("SECTION 4 — NEXT STEP / ACTION")
    print("=" * 72)

    total_shapes = sum(len(raw.get(n, [])) for n in display_order)
    total_valid = sum(len(validated.get(n, [])) for n in display_order)

    if total_shapes == 0:
        print("  Fix layer names or entity closure issues above, then re-run.")
    elif total_valid < total_shapes:
        print(
            f"  {total_valid}/{total_shapes} shapes valid — "
            f"repair invalid polygons (see Section 3), then pass to validate_polygons()."
        )
    else:
        print(
            f"  All {total_valid} shape(s) valid — "
            f"pass validated polygons into the BOQ pipeline for area calculations."
        )
    print()

    # ── DEBUG: RAW DICT STRUCTURE ─────────────────────────────────────────────
    print("=" * 72)
    print("DEBUG — RAW DICT STRUCTURE")
    print("=" * 72)
    pprint(raw)
    print()


def extract_closed_polygons(dxf_path: str, scan_layers: list):
    raw = extract_layer_polygons_closed(dxf_path, scan_layers)
    valid = validate_polygons(raw)
    res = {}
    for key, val in valid.items():
        res[key] = [list(poly.exterior.coords) for poly in val]
    return res

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    _reconfigure_stdout_for_cli()
    if len(sys.argv) < 2:
        print("Usage: python extract_layer_polygons_closed.py <dxf_path> [layer1 layer2 ...]")
        sys.exit(1)

    dxf_path = sys.argv[1]
    layer_args = sys.argv[2:]

    doc = _load_doc_safe(dxf_path)

    # No layer names given → full DXF scan across all layers.
    if not layer_args:
        print(f"\nFULL DXF SCAN — no layer filter, collecting all closed shapes")
        print(f"Loading: {dxf_path}")
        raw = _extract_all_from_doc(doc)
        validated = validate_polygons(raw)
        _print_sections(raw, validated, sorted(raw.keys()), doc_layers_lower=None)
        sys.exit(0)

    doc_layers_lower = {lt.dxf.name.strip().lower() for lt in doc.layers}

    print(f"\nLoading: {dxf_path}")
    raw = extract_layer_polygons_closed(dxf_path, layer_args)
    validated = validate_polygons(raw)
    _print_sections(raw, validated, layer_args, doc_layers_lower=doc_layers_lower)


if __name__ == "__main__":
    main()

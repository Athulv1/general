"""
locate_mtext_zones.py
MTEXT Zone Locator -- 3-mode CLI + Python API

CLI Modes (auto-detected from arguments):
  Mode 3  python locate_mtext_zones.py "file.dxf"
  Mode 2  python locate_mtext_zones.py "file.dxf" "LAYER_A" "LAYER_B"
  Mode 1  python locate_mtext_zones.py "file.dxf" "LAYER_A" "pat1" "pat2" "LAYER_B" "pat3"

Python API:
  from locate_mtext_zones import locate_mtext_zones
  result = locate_mtext_zones(dxf_path, mtext_layer, poly_layer, texts)
"""

from __future__ import annotations

import re
import sys
import math
from fnmatch import fnmatch
from typing import List, Optional, Tuple, Dict

import ezdxf
from shapely.geometry import Polygon as ShapelyPolygon, Point
from shapely.validation import explain_validity

# ── constants ─────────────────────────────────────────────────────────────────
CLOSURE_GAP_TOLERANCE = 1.0  # mm -- do not change

# ── MTEXT format-code stripping ───────────────────────────────────────────────
_MTEXT_CODE_RE = re.compile(
    r"\\[HWQAFCfLlOoKkTI][^;]*;"   # \X<val>; codes (height, width, font, colour …)
    r"|\\p[^;]*;"                   # \pxql; \pxqc,t8; paragraph-format codes with args
    r"|\\[PpNn]"                    # bare \P \p \N \n paragraph/newline codes
    r"|\{|\}"                       # grouping braces
    r"|%%[cCdDpP]"                  # special chars (degree, diameter, plus-minus)
    r"|\\~"                         # non-breaking space
    r"|\\U\+[0-9A-Fa-f]{4}"        # unicode escape \U+XXXX
    r"|\\\\"                        # escaped backslash
)


def _strip_mtext(raw: str) -> str:
    """Remove MTEXT inline formatting codes and return clean text."""
    if not raw:
        return ""
    text = _MTEXT_CODE_RE.sub("", raw)
    # ^J is a two-char paragraph break in DXF MTEXT (caret + J); replace the pair
    text = re.sub(r"\^J|\^M|\r\n|\r|\n", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── polygon collection ────────────────────────────────────────────────────────
def _gap(p1: tuple, p2: tuple) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _extract_vertices(e) -> Optional[Tuple[List[tuple], str, str]]:
    """
    Extract vertices from LWPOLYLINE / POLYLINE / SPLINE regardless of
    whether they are closed or open.

    Returns (vertices, entity_type, closure_note)  or  None if extraction fails.
    Accepts both open and closed polylines — caller decides what to do with them.
    """
    etype = e.dxftype()

    if etype == "LWPOLYLINE":
        pts = [(float(x), float(y)) for x, y in e.get_points("xy")]
        if not pts:
            return None
        closed_flag = bool(e.is_closed)
        gap = _gap(pts[0], pts[-1]) if len(pts) >= 2 else 0.0
        closure = "flag" if closed_flag else (
            "geometric_gap={:.3f}mm".format(gap) if gap < CLOSURE_GAP_TOLERANCE
            else "open_gap={:.3f}mm".format(gap)
        )
        vertices = pts
        if closed_flag and vertices[0] != vertices[-1]:
            vertices = vertices + [vertices[0]]
        return vertices, etype, closure

    elif etype == "POLYLINE":
        pts = [(v.dxf.location.x, v.dxf.location.y)
               for v in e.vertices
               if hasattr(v.dxf, "location")]
        if not pts:
            return None
        closed_flag = bool(e.dxf.flags & 1)
        gap = _gap(pts[0], pts[-1]) if len(pts) >= 2 else 0.0
        closure = "flag" if closed_flag else (
            "geometric_gap={:.3f}mm".format(gap) if gap < CLOSURE_GAP_TOLERANCE
            else "open_gap={:.3f}mm".format(gap)
        )
        vertices = pts
        if closed_flag and vertices[0] != vertices[-1]:
            vertices = vertices + [vertices[0]]
        return vertices, etype, closure

    elif etype == "SPLINE":
        try:
            pts = [(p[0], p[1]) for p in e.flattening(0.01)]
        except Exception:
            return None
        if not pts:
            return None
        closed_flag = bool(getattr(e, "closed", False))
        gap = _gap(pts[0], pts[-1]) if len(pts) >= 2 else 0.0
        closure = "flag" if closed_flag else (
            "geometric_gap={:.3f}mm".format(gap) if gap < CLOSURE_GAP_TOLERANCE
            else "open_gap={:.3f}mm".format(gap)
        )
        vertices = pts
        if closed_flag and vertices[0] != vertices[-1]:
            vertices = vertices + [vertices[0]]
        return vertices, etype, closure

    return None


def _collect_polys(msp, poly_layer: str = "") -> List[Dict]:
    """
    Collect ALL polyline entities from ALL layers in modelspace.

    poly_layer parameter is kept for API compatibility but is now IGNORED —
    we always do a global scan so that _find_zone() can find the nearest
    polyline anywhere in the DXF regardless of which layer it lives on.

    Returns list of dicts:
      { vertices, entity_type, closure, layer, shapely (optional) }
    """
    polys = []

    for e in msp:
        result = _extract_vertices(e)
        if result is None:
            continue
        vertices, etype, closure = result

        if len(vertices) < 2:
            continue

        # Build Shapely polygon only for closed shapes (used for contains check)
        shapely_poly = None
        if len(vertices) >= 4 and vertices[0] == vertices[-1]:
            try:
                p = ShapelyPolygon(vertices)
                if not p.is_valid:
                    p = p.buffer(0)
                if p.is_valid and not p.is_empty:
                    shapely_poly = p
            except Exception:
                pass

        polys.append({
            "vertices":    vertices,
            "entity_type": etype,
            "closure":     closure,
            "layer":       e.dxf.layer,
            "shapely":     shapely_poly,   # None for open polylines
        })

    return polys


# ── spatial lookup ────────────────────────────────────────────────────────────
def _nearest_vertex_dist(point: tuple, vertices: List[tuple]) -> float:
    """Return the distance from point to the nearest vertex in the list."""
    px, py = point
    best = float("inf")
    for vx, vy in vertices:
        d = math.hypot(px - vx, py - vy)
        if d < best:
            best = d
    return best


def _find_zone(point: tuple, polys: List[Dict]) -> tuple:
    """
    Find the nearest polyline to `point` using nearest-vertex distance.

    Strategy (global scan — no layer filter):
      For every polyline in the DXF, measure the distance from `point` to
      the closest vertex on that polyline. Return the polyline whose minimum
      vertex distance is smallest.

    Returns (matched_poly_dict | None, method_str, distance_mm | None)
      method is always "nearest_vertex" (or "no_polygon" if polys is empty).
    """
    if not polys:
        return None, "no_polygon", None

    best_poly = None
    best_dist = float("inf")

    for p in polys:
        d = _nearest_vertex_dist(point, p["vertices"])
        if d < best_dist and point_in_polygon(point, p["vertices"]):
            best_dist = d
            best_poly = p

    return best_poly, "nearest_vertex", best_dist


# ── text matching ─────────────────────────────────────────────────────────────
def _matches_pattern(clean: str, pattern: str) -> bool:
    """fnmatch with *pattern* wrapping (case-insensitive), then substring."""
    cl = clean.lower()
    pt = pattern.lower()
    if fnmatch(cl, "*{}*".format(pt)):
        return True
    return pt in cl

def extract_text(
        dxf_path: str,
        layer_text_dict: Dict[str, List[str]],
        store_outline: List[Tuple[float, float]]
) -> Dict[str, List[Dict]]:
    
    combined_result: Dict[str, List[Dict]] = {}
    for key, val in layer_text_dict.items():
        r = locate_mtext_zones(dxf_path=dxf_path, mtext_layer=key, poly_layer=key, store_outline=store_outline, texts=val)
    
        for pat, hits in r.items():
            combined_result.setdefault(pat, []).extend(hits)
    
    return combined_result

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

# ── public API ────────────────────────────────────────────────────────────────
def locate_mtext_zones(
    dxf_path:    str,
    mtext_layer: str,
    poly_layer:  str,
    store_outline: List[Tuple[float, float]],
    texts:       Optional[List[str]] = None,
) -> Dict[str, List[Dict]]:
    """
    Locate MTEXT labels and match each to an enclosing zone polyline.

    Returns dict keyed by each pattern in texts (or "__ALL__" when texts=None).
    Each value is a list of hit dicts:
      {position, raw_text, layer, match_method, bbox}
    """
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    # Global scan — all polylines across all layers
    polys = _collect_polys(msp, "")   # poly_layer arg ignored; global scan
    mtext_layer_lower = mtext_layer.lower()

    candidates = []
    for e in msp:
        if e.dxftype() != "MTEXT":
            continue
        if e.dxf.layer.lower() != mtext_layer_lower:
            continue
        raw = e.text or ""
        clean = _strip_mtext(raw)
        bbox = ezdxf.bbox.extents([e])
        center = bbox.center
        if not point_in_polygon((center.x, center.y), store_outline):
            continue
        
        candidates.append({
            "raw_text": clean,
            "layer":    e.dxf.layer,
            "position": (center.x, center.y),
        })

    result: Dict[str, List[Dict]] = {}

    if texts is None:
        keys = ["__ALL__"]
        pattern_map = {"__ALL__": None}
    else:
        keys = texts
        pattern_map = {t: t for t in texts}

    for key in keys:
        pattern = pattern_map[key]
        hits = []
        for cand in candidates:
            if pattern is None or _matches_pattern(cand["raw_text"], pattern):
                poly_dict, method, dist = _find_zone(cand["position"], polys)
                hit = {
                    "position":                  cand["position"],
                    "raw_text":                  cand["raw_text"],
                    "layer":                     cand["layer"],
                    "match_method":              method,
                    "bbox":                      poly_dict["vertices"] if poly_dict else [],
                    "_nearest_vertex_dist_mm":   dist,
                    "_entity_type":              poly_dict["entity_type"] if poly_dict else None,
                    "_closure":                  poly_dict["closure"] if poly_dict else None,
                    "_poly_layer":               poly_dict["layer"] if poly_dict else None,
                }
                hits.append(hit)
        result[key] = hits

    return result


# ── discovery helper ──────────────────────────────────────────────────────────
def _discovery(doc) -> None:
    msp = doc.modelspace()

    print("-- ALL LAYERS --")
    for lyr in sorted(doc.layers, key=lambda l: l.dxf.name):
        print("  {!r}".format(lyr.dxf.name))

    print("\n-- LAYERS WITH MTEXT --")
    mtext_layers: Dict[str, list] = {}
    for e in msp:
        if e.dxftype() == "MTEXT":
            lyr = e.dxf.layer
            mtext_layers.setdefault(lyr, []).append(e)
    for lyr, ents in sorted(mtext_layers.items()):
        samples = [_strip_mtext(e.text or "")[:40] for e in ents[:3]]
        print("  {!r}  ({} entities)  samples: {}".format(lyr, len(ents), samples))

    print("\n-- LAYERS WITH CLOSED LWPOLYLINES --")
    poly_layers: Dict[str, int] = {}
    for e in msp:
        if e.dxftype() == "LWPOLYLINE":
            pts = list(e.get_points("xy"))
            closed = e.is_closed or (
                len(pts) >= 2 and _gap(pts[0], pts[-1]) < CLOSURE_GAP_TOLERANCE
            )
            if closed:
                lyr = e.dxf.layer
                poly_layers[lyr] = poly_layers.get(lyr, 0) + 1
    for lyr, cnt in sorted(poly_layers.items()):
        print("  {!r}  ({} closed LWPOLYLINE)".format(lyr, cnt))

    print("\n-- LAYERS WITH CLOSED POLYLINES (3D) --")
    poly3d: Dict[str, int] = {}
    for e in msp:
        if e.dxftype() == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y)
                   for v in e.vertices
                   if hasattr(v.dxf, "location")]
            closed = bool(e.dxf.flags & 1) or (
                len(pts) >= 2 and _gap(pts[0], pts[-1]) < CLOSURE_GAP_TOLERANCE
            )
            if closed:
                lyr = e.dxf.layer
                poly3d[lyr] = poly3d.get(lyr, 0) + 1
    for lyr, cnt in sorted(poly3d.items()):
        print("  {!r}  ({} closed POLYLINE)".format(lyr, cnt))


# ── CLI output formatting ─────────────────────────────────────────────────────
DIST_WARN_MM = 5000.0   # flag hits whose nearest-vertex dist exceeds this


def _print_report(result: Dict, mode_label: str, has_poly: bool) -> None:
    SEP = "-" * 150

    # -- Section 1 --
    print("\n" + "=" * 90)
    print("  SECTION 1 -- MATCH SUMMARY  [{}]".format(mode_label))
    print("=" * 90)
    print("{:<28} | {:>4} | {:<16} | {:<22} | {:<22} | {:>10} | Position".format(
        "Key", "Hits", "Method", "Layer", "Poly Layer", "Dist(mm)"))
    print(SEP)

    for key, hits in result.items():
        if not hits:
            print("{:<28} |    0 | {:<16} | {:<22} | {:<22} | {:>10} | -".format(
                key, "-", "-", "-", "-"))
            continue
        for i, h in enumerate(hits):
            k_col  = key if i == 0 else ""
            n_col  = str(len(hits)) if i == 0 else ""
            m_col  = h["match_method"]
            l_col  = h["layer"]
            pl_col = h.get("_poly_layer") or "-"
            dist   = h.get("_nearest_vertex_dist_mm")
            d_col  = "{:.2f}".format(dist) if dist is not None else "-"
            x, y   = h["position"]
            p_col  = "x={:.2f}  y={:.2f}".format(x, y)
            flag   = "  [!] CHECK" if (dist is not None and dist > DIST_WARN_MM) else ""
            print("{:<28} | {:>4} | {:<16} | {:<22} | {:<22} | {:>10} | {}{}".format(
                k_col, n_col, m_col, l_col, pl_col, d_col, p_col, flag))

    # -- Section 2 --
    print("\n" + "=" * 90)
    print("  SECTION 2 -- BBOX VERTICES PER MATCH")
    print("=" * 90)

    for key, hits in result.items():
        if not hits:
            print("\n  Key '{}' -- NO HITS".format(key))
            continue
        for idx, h in enumerate(hits, 1):
            dist = h.get("_nearest_vertex_dist_mm")
            flag = "  [!] CHECK DISTANCE" if (dist is not None and dist > DIST_WARN_MM) else ""
            print("\n  Key '{}' hit #{}:{}".format(key, idx, flag))
            print("    raw_text            : {!r}".format(h["raw_text"]))
            print("    layer               : {!r}".format(h["layer"]))
            x, y = h["position"]
            print("    position            : ({:.4f}, {:.4f})".format(x, y))
            print("    match_method        : {}".format(h["match_method"]))
            if dist is not None:
                print("    nearest_vertex_dist : {:.2f} mm{}".format(dist, flag))
            poly_layer = h.get("_poly_layer")
            if poly_layer:
                print("    poly_layer          : {}".format(poly_layer))
            if h.get("_entity_type"):
                print("    entity_type         : {}".format(h["_entity_type"]))
            if h.get("_closure"):
                print("    closure             : {}".format(h["_closure"]))
            bbox = h["bbox"]
            if not bbox:
                print("    bbox                : EMPTY -- no spatial lookup (Mode 2/3 bypass)")
            else:
                n = len(bbox)
                print("    bbox vertices       : {} pts".format(n))
                for vi, (vx, vy) in enumerate(bbox[:3], 1):
                    print("      [{:>3}]  x={:.4f}  y={:.4f}".format(vi, vx, vy))
                if n > 3:
                    print("       ... ({} more) ...".format(n - 3))

    # -- Section 3 --
    print("\n" + "=" * 90)
    print("  SECTION 3 -- NEXT STEP / ACTION")
    print("=" * 90)

    total = sum(len(v) for v in result.values())
    nearest_v = sum(
        1 for hits in result.values()
        for h in hits if h["match_method"] == "nearest_vertex"
    )
    no_poly_count = sum(
        1 for hits in result.values()
        for h in hits if h["match_method"] == "no_polygon"
    )
    no_hit    = sum(1 for hits in result.values() if len(hits) == 0)
    far_hits  = sum(
        1 for hits in result.values()
        for h in hits
        if h.get("_nearest_vertex_dist_mm") is not None
        and h["_nearest_vertex_dist_mm"] > DIST_WARN_MM
    )

    print("  Total hits          : {}".format(total))
    print("  Nearest vertex match: {}".format(nearest_v))
    print("  No polygon (bypass) : {}".format(no_poly_count))
    print("  Patterns with 0 hits: {}".format(no_hit))
    print("  Dist > {}mm (flag): {}".format(int(DIST_WARN_MM), far_hits))

    # Distance summary table
    all_hits = [h for hits in result.values() for h in hits
                if h.get("_nearest_vertex_dist_mm") is not None]
    if all_hits:
        print("\n  Distance summary:")
        print("  {:<28} | {:<22} | {:>10} | Note".format(
            "raw_text", "poly_layer", "dist(mm)"))
        print("  " + "-" * 80)
        for h in all_hits:
            dist = h["_nearest_vertex_dist_mm"]
            note = "[!] CHECK DISTANCE" if dist > DIST_WARN_MM else "ok"
            print("  {:<28} | {:<22} | {:>10.2f} | {}".format(
                h["raw_text"][:28],
                (h.get("_poly_layer") or "-")[:22],
                dist, note))

    if nearest_v > 0:
        print("\n  [OK] {} hit(s) matched via nearest-vertex global scan.".format(nearest_v))
    if far_hits > 0:
        print("  [!] {} hit(s) exceed {}mm -- verify in DXF.".format(
            far_hits, int(DIST_WARN_MM)))
    if no_poly_count > 0:
        print("\n  [i] {} hit(s) have empty bbox -- Mode 2/3 bypassed spatial lookup.".format(
            no_poly_count))
        print("      Re-run with: python locate_mtext_zones.py \"file.dxf\" \"LAYER\" --dict-full")

    if total > 0 and nearest_v == total:
        print("\n  --> All {} bbox(es) populated -- check distances then use for BOQ.".format(total))


def _clean_for_print(result: Dict, compact: bool = True) -> Dict:
    """Return a copy of result safe to pprint.
    compact=True  -> bbox replaced by vertex count
    compact=False -> full bbox included
    Always includes _poly_layer and _nearest_vertex_dist_mm."""
    out = {}
    for key, hits in result.items():
        clean_hits = []
        for h in hits:
            row = {
                "raw_text":                h["raw_text"],
                "layer":                   h["layer"],
                "position":                h["position"],
                "match_method":            h["match_method"],
                "_poly_layer":             h.get("_poly_layer"),
                "_nearest_vertex_dist_mm": h.get("_nearest_vertex_dist_mm"),
                "_entity_type":            h.get("_entity_type"),
                "_closure":                h.get("_closure"),
            }
            if compact:
                row["bbox_pts"] = len(h["bbox"])
            else:
                row["bbox"] = h["bbox"]
            clean_hits.append(row)
        out[key] = clean_hits
    return out


# ── CLI entry point ───────────────────────────────────────────────────────────
def _cli() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python locate_mtext_zones.py <dxf_path> [--dict] [LAYER [pattern ...]] ...")
        sys.exit(1)

    # pull out --dict / --dict-full flags before other parsing
    show_dict    = "--dict"      in args
    show_full    = "--dict-full" in args
    args = [a for a in args if a not in ("--dict", "--dict-full")]

    dxf_path = args[0]
    rest     = args[1:]

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    # canonical layer name lookup (case-insensitive)
    layer_names = {lyr.dxf.name.lower(): lyr.dxf.name for lyr in doc.layers}

    # -- Mode 3: no extra args ---------------------------------------------------
    if not rest:
        print("\n[Mode 3 -- full DXF scan]  {}".format(dxf_path))
        _discovery(doc)

        all_result: Dict[str, List[Dict]] = {"__ALL_MTEXT__": []}
        for e in msp:
            if e.dxftype() != "MTEXT":
                continue
            raw   = e.text or ""
            clean = _strip_mtext(raw)
            ins   = e.dxf.insert
            all_result["__ALL_MTEXT__"].append({
                "raw_text":          clean,
                "layer":             e.dxf.layer,
                "position":          (ins.x, ins.y),
                "match_method":      "no_polygon",
                "bbox":              [],
                "_centroid_dist_mm": None,
                "_entity_type":      None,
                "_closure":          None,
            })

        _print_report(all_result, "Mode 3 -- full DXF", has_poly=False)
        if show_dict or show_full:
            import pprint
            print("\n-- RESULT DICT --")
            pprint.pprint(_clean_for_print(all_result, compact=not show_full))
        return

    # -- Parse layer/pattern groups ---------------------------------------------
    groups = []
    cur_layer = None
    cur_texts: List[str] = []

    for tok in rest:
        canon = layer_names.get(tok.lower())
        if canon:
            if cur_layer is not None:
                groups.append({"layer": cur_layer, "texts": cur_texts})
            cur_layer = canon
            cur_texts = []
        else:
            if cur_layer is None:
                print("  [WARN] Token {!r} not found in doc.layers -- "
                      "treating as layer name anyway".format(tok))
                cur_layer = tok
                cur_texts = []
            else:
                cur_texts.append(tok)

    if cur_layer is not None:
        groups.append({"layer": cur_layer, "texts": cur_texts})

    has_any_texts = any(bool(g["texts"]) for g in groups)

    if not groups:
        print("No valid layer groups parsed. Run Mode 3 to discover layers.")
        sys.exit(1)

    # -- Mode 2: layers only, no patterns -- uses full spatial lookup --------------
    if not has_any_texts:
        print("\n[Mode 2 -- layer scan]  layers: {}".format(
            [g["layer"] for g in groups]))
        combined: Dict[str, List[Dict]] = {}
        for g in groups:
            # Call locate_mtext_zones so spatial lookup (nearest-vertex) runs
            r = locate_mtext_zones(dxf_path, g["layer"], "", None)
            for pat, hits in r.items():
                combined.setdefault(g["layer"], []).extend(hits)
        _print_report(combined, "Mode 2 -- layer scan", has_poly=True)
        if show_dict or show_full:
            import pprint
            print("\n-- RESULT DICT --")
            pprint.pprint(_clean_for_print(combined, compact=not show_full))
        return

    # -- Mode 1: targeted -- layer(s) + text patterns ----------------------------
    print("\n[Mode 1 -- targeted]  groups: {}".format(
        [(g["layer"], g["texts"]) for g in groups]))

    combined_result: Dict[str, List[Dict]] = {}
    for g in groups:
        layer  = g["layer"]
        texts  = g["texts"] if g["texts"] else None
        r = locate_mtext_zones(dxf_path, layer, layer, texts)
        
        for pat, hits in r.items():
            combined_result.setdefault(pat, []).extend(hits)

    _print_report(combined_result, "Mode 1 -- targeted", has_poly=True)
    if show_dict or show_full:
        import pprint
        print("\n-- RESULT DICT --")
        pprint.pprint(_clean_for_print(combined_result, compact=not show_full))


if __name__ == "__main__":
    _cli()
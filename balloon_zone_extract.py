"""
balloon_zone_extract.py — Multi-label zone detection via balloon inflation for DXF floor plans.

Programmatic usage:
    from balloon_zone_extract import detect_zones

    # All labels found in the full DXF
    result = detect_zones("plan.dxf")

    # All labels found on specific annotation layers
    result = detect_zones("plan.dxf", scan_layers=["ANNO-TEXT", "ROOM-TAG"])

    # Explicit labels, seeds restricted to those layers, write output DXF
    result = detect_zones(
        "plan.dxf",
        scan_layers=["ANNO-TEXT"],
        keywords=["ROOM-A", "ROOM-B"],
        output_dxf="plan_zones.dxf",
    )

CLI usage:
    python balloon_zone_extract.py plan.dxf [--output out.dxf]                        # Mode 3
    python balloon_zone_extract.py plan.dxf LAYER_1 LAYER_2 [--output out.dxf]        # Mode 2
    python balloon_zone_extract.py plan.dxf LAYER_1 TEXT_A TEXT_B [--output out.dxf]  # Mode 1

Return value schema:
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
Keys are lowercased text label values. Values are lists of polygons; each polygon
is a list of (x, y) float tuples from Shapely's exterior.coords.
"""

import json
import math
import re
import sys
from typing import Optional

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

# Internal default barrier layers, used ONLY when the caller passes none.
# Caller-supplied barrier layers fully define the barrier set: the pipeline's
# ZoneResolver always passes the config's store-outline + wall layers, so this
# default stays empty — barriers are drawing facts and must come from config.
BARRIER_LAYERS: list = []

# MTEXT control-code stripper — removes {\Fstyle;...}, \P, {}, etc.
_MTEXT_STRIP = re.compile(r'\\[A-Za-z][^;]*;|\\[A-Za-z]|[{}]')


# ── Low-level geometry helpers ────────────────────────────────────────────────

def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _entity_to_linestrings(entity):
    """Convert a DXF LINE / LWPOLYLINE / POLYLINE entity to Shapely LineStrings."""
    result = []
    try:
        t = entity.dxftype()
        if t == 'LINE':
            s, e = entity.dxf.start, entity.dxf.end
            if _dist((s.x, s.y), (e.x, e.y)) > 1:
                result.append(LineString([(s.x, s.y), (e.x, e.y)]))
        elif t == 'LWPOLYLINE':
            pts = [(p[0], p[1]) for p in entity.get_points()]
            if len(pts) >= 2:
                if entity.is_closed and pts[0] != pts[-1]:
                    pts.append(pts[0])
                result.append(LineString(pts))
        elif t == 'POLYLINE':
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            if len(pts) >= 2:
                if entity.is_closed and pts[0] != pts[-1]:
                    pts.append(pts[0])
                result.append(LineString(pts))
    except Exception:
        pass
    return result


def _collect_world_segments(msp, target_layers):
    """Walk modelspace + expand INSERTs to collect world-space geometry on target layers."""
    segments = []

    def _visit(entity, parent_layer=None):
        try:
            raw_layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else '0'
            eff_layer = parent_layer if (raw_layer == '0' and parent_layer) else raw_layer
            if entity.dxftype() == 'INSERT':
                try:
                    for sub in entity.virtual_entities():
                        _visit(sub, parent_layer=eff_layer)
                except Exception:
                    pass
            elif eff_layer in target_layers:
                segments.extend(_entity_to_linestrings(entity))
        except Exception:
            pass

    for e in msp:
        _visit(e)
    return segments


def _snap_endpoints(segs, tol):
    """Union-Find endpoint snapping: cluster segment tips within tol and snap to one coord."""
    endpoints = []
    for ls in segs:
        c = list(ls.coords)
        endpoints.append(c[0])
        endpoints.append(c[-1])

    parent = list(range(len(endpoints)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(endpoints)):
        for j in range(i + 1, len(endpoints)):
            if _dist(endpoints[i], endpoints[j]) < tol:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    canonical = {find(i): endpoints[find(i)] for i in range(len(endpoints))}
    snapped_coords = [canonical[find(i)] for i in range(len(endpoints))]

    snapped = []
    for idx, ls in enumerate(segs):
        c = list(ls.coords)
        new_start = snapped_coords[idx * 2]
        new_end = snapped_coords[idx * 2 + 1]
        snapped.append(LineString([new_start] + c[1:-1] + [new_end]))
    return snapped


def _find_text_seeds(msp, keyword):
    """Return (x, y) insertion points of TEXT/MTEXT entities containing keyword."""
    kw = re.sub(r'\s+', '', keyword.upper())
    seeds = []
    for entity in msp:
        try:
            t = entity.dxftype()
            if t == 'TEXT':
                val = re.sub(r'\s+', '', (entity.dxf.text if hasattr(entity.dxf, 'text') else '').upper())
                if kw in val:
                    pt = entity.dxf.insert
                    seeds.append((pt[0], pt[1]))
            elif t == 'MTEXT':
                raw = entity.text if hasattr(entity, 'text') else ''
                clean = re.sub(r'\s+', '', _MTEXT_STRIP.sub('', raw).upper())
                if kw in clean:
                    pt = entity.dxf.insert
                    seeds.append((pt[0], pt[1]))
        except Exception:
            pass
    return seeds


def _dedup_seeds(seeds, merge_dist):
    """Remove seeds closer than merge_dist mm to an already-accepted seed."""
    accepted = []
    for s in seeds:
        if all(_dist(s, a) >= merge_dist for a in accepted):
            accepted.append(s)
    return accepted


def _inflate_balloon(seed_pt, barrier_region, constraint, exclusion,
                     step, max_iter, stop_thresh, seed_radius, verbose):
    """
    Iteratively grow a circular balloon from seed_pt until it fills available space.

    Expansion is blocked by barrier_region and exclusion (already-claimed zones),
    and capped to constraint. Returns the final Shapely Polygon (may be empty).
    """
    balloon = seed_pt.buffer(seed_radius)

    if verbose:
        print(f"    Inflating from ({seed_pt.x:.0f}, {seed_pt.y:.0f})  step={step:.0f}mm ...")

    for i in range(max_iter):
        prev_area = balloon.area
        candidate = balloon.buffer(step)
        candidate = candidate.intersection(constraint)
        candidate = candidate.difference(barrier_region)
        if not exclusion.is_empty:
            candidate = candidate.difference(exclusion)

        # If the balloon split, keep the piece that still contains the seed
        if candidate.geom_type == 'MultiPolygon':
            best = None
            for geom in candidate.geoms:
                if geom.contains(seed_pt) or geom.distance(seed_pt) < 1:
                    best = geom
                    break
            if best is None:
                near = [g for g in candidate.geoms if g.distance(seed_pt) < step]
                best = (max(near, key=lambda g: g.area) if near
                        else max(candidate.geoms, key=lambda g: g.area))
            candidate = best

        if candidate.is_empty:
            if verbose:
                print(f"    Balloon empty at iteration {i} — stopping")
            break

        balloon = candidate
        growth = (balloon.area - prev_area) / max(prev_area, 1)
        if growth < stop_thresh:
            if verbose:
                print(f"    Converged after {i + 1} iterations  "
                      f"(area={balloon.area / 1e6:.2f} m²)")
            break

    return balloon


# ── detect_zones helpers ──────────────────────────────────────────────────────

def _discover_labels(msp, scan_layers):
    """
    Collect unique TEXT/MTEXT string values from modelspace.

    scan_layers — set of layer names to restrict to, or None for all layers.
    Returns a sorted list of unique non-empty strings.
    """
    seen = set()
    for ent in msp:
        if ent.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        if scan_layers is not None and ent.dxf.layer not in scan_layers:
            continue
        try:
            if ent.dxftype() == 'TEXT':
                raw = ent.dxf.text if hasattr(ent.dxf, 'text') else ''
            else:
                raw = ent.text if hasattr(ent, 'text') else ''
            text = _MTEXT_STRIP.sub('', raw).strip()
            if text:
                seen.add(text)
        except Exception:
            pass
    return sorted(seen)


def _seeds_on_layers(msp, keyword, layers):
    """
    Find (x, y) seed positions for keyword, restricted to entities on layers.

    Applies the same control-code stripping and case-insensitive matching as
    _find_text_seeds, but with an additional layer filter.
    """
    kw = re.sub(r'\s+', '', keyword.upper())
    layers_lower = {l.lower() for l in layers}
    seeds = []
    for ent in msp:
        if ent.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        if ent.dxf.layer.lower() not in layers_lower:
            continue
        try:
            if ent.dxftype() == 'TEXT':
                val = re.sub(r'\s+', '', (ent.dxf.text if hasattr(ent.dxf, 'text') else '').upper())
            else:
                raw = ent.text if hasattr(ent, 'text') else ''
                val = re.sub(r'\s+', '', _MTEXT_STRIP.sub('', raw).upper())
            if kw in val:
                pt = ent.dxf.insert
                seeds.append((pt[0], pt[1]))
        except Exception:
            pass
    return seeds


_INVALID_LAYER_CHARS = re.compile(r'[<>/\\\":;?*|=\']')


def _sanitize_layer_name(name: str) -> str:
    """Replace DXF-illegal layer-name characters with underscores."""
    return _INVALID_LAYER_CHARS.sub('_', name) or '_'


# ── Public API ────────────────────────────────────────────────────────────────

def detect_zones(
    dxf_path: str,
    scan_layers: Optional[list] = None,
    keywords: Optional[list] = None,
    output_dxf: Optional[str] = None,
    balloon_groups: Optional[dict] = None,
    min_area: float = 1e6,
    step: float = 100.0,
    max_iter: int = 600,
    stop_thresh: float = 0.001,
    barrier_buffer: float = 3.0,
    seed_merge_dist: float = 2000.0,
    seed_radius: float = 200.0,
    bbox_margin: float = 200.0,
    simplify_tol: float = 5.0,
    verbose: bool = False,
    barrier_layers: Optional[list] = None,
) -> dict:
    """
    Detect inflated zone polygons for every text label found in a DXF file.

    Parameters
    ----------
    dxf_path : str
        Path to the input DXF file.
    scan_layers : list[str] | None
        Layer names to scan when auto-discovering labels or locating seeds.
        None → search the entire modelspace (Mode 3 behaviour).
    keywords : list[str] | None
        Explicit list of text labels to process. When None, labels are
        auto-discovered from scan_layers (or all layers if that is None too).
    output_dxf : str | None
        If provided, each polygon is written as an LWPOLYLINE to a layer named
        after its label, and the result is saved to this path.
    min_area : float
        Polygons with area < min_area mm² are discarded (default 1 m² = 1e6 mm²).
    step : float
        Balloon growth per iteration in mm (default 100 mm).
    max_iter : int
        Maximum inflation iterations per seed (default 600).
    stop_thresh : float
        Fractional area-growth threshold for convergence (default 0.001).
    barrier_buffer : float
        Buffer applied to barrier segments in mm (default 3 mm).
    seed_merge_dist : float
        Seeds closer than this distance in mm are deduplicated (default 2000 mm).
    seed_radius : float
        Initial balloon radius in mm (default 200 mm).
    bbox_margin : float
        Expansion of the bounding constraint box in mm (default 200 mm).
    simplify_tol : float
        Polygon simplification tolerance in mm (default 5 mm).
    verbose : bool
        Print per-seed inflation progress to stdout.

    Returns
    -------
    dict[str, list[list[tuple[float, float]]]]
        Keys are lowercased label strings. Each value is a list of polygons;
        each polygon is a list of (x, y) float tuples (exterior ring).
    """
    if balloon_groups is not None:
        balloon_context = {}
        for group_name, cfg in balloon_groups.items():
            # recursive self-call with balloon_groups=None prevents re-entry
            raw = detect_zones(
                dxf_path=dxf_path,
                scan_layers=cfg["layers"],
                keywords=cfg["keywords"],
                output_dxf=None,
                balloon_groups=None,
                min_area=min_area,
                step=step,
                max_iter=max_iter,
                stop_thresh=stop_thresh,
                barrier_buffer=barrier_buffer,
                seed_merge_dist=seed_merge_dist,
                seed_radius=seed_radius,
                bbox_margin=bbox_margin,
                simplify_tol=simplify_tol,
                verbose=verbose,
                barrier_layers=cfg.get("barrier_layers") or barrier_layers,
            )
            # raw format: {"clinic": [[(x,y),...], ...], ...}
            zones = {}
            n = 1
            for kw in cfg["keywords"]:
                coords_list = raw.get(kw.lower(), [])
                for coords in coords_list:
                    poly = Polygon(coords)
                    area_mm2 = poly.area
                    centroid = poly.centroid
                    zone_key = f"{group_name.lower()}_{n}"
                    zones[zone_key] = {
                        "polygon": coords,
                        "area_mm2": round(area_mm2, 2),
                        "area_sqft": round(area_mm2 / 92903.04, 2),
                        "seed_text": kw,
                        "seed_xy": (round(centroid.x, 2), round(centroid.y, 2))
                    }
                    n += 1
            balloon_context[group_name] = {
                "count": len(zones),
                "zones": zones
            }
        return balloon_context
    # --- existing logic continues unchanged below ---

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    layer_set = set(scan_layers) if scan_layers is not None else None

    # ── 1. Resolve the keyword list ───────────────────────────────────────────
    if keywords is not None:
        active_keywords = list(keywords)
    else:
        active_keywords = _discover_labels(msp, layer_set)

    if not active_keywords:
        return {}

    # ── 2. Collect and snap barrier geometry ──────────────────────────────────
    # Caller-supplied barrier layers fully define the barrier set; the builtin
    # default (empty) applies only when the caller passes none.  Barrier layers
    # are drawing facts and therefore always come from the caller's config.
    active_barriers = set(barrier_layers) if barrier_layers else set(BARRIER_LAYERS)
    barrier_segs = _collect_world_segments(msp, active_barriers)
    barrier_segs = _snap_endpoints(barrier_segs, 2.0)

    if not barrier_segs:
        return {}

    barrier_region = unary_union([s.buffer(barrier_buffer) for s in barrier_segs])

    # ── 3. Build bounding constraint box from barrier extents ─────────────────
    all_pts = [pt for seg in barrier_segs for pt in seg.coords]
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    constraint = Polygon([
        (min(xs) - bbox_margin, min(ys) - bbox_margin),
        (max(xs) + bbox_margin, min(ys) - bbox_margin),
        (max(xs) + bbox_margin, max(ys) + bbox_margin),
        (min(xs) - bbox_margin, max(ys) + bbox_margin),
    ])

    # ── 4. Inflate balloons; claimed_zone is shared across ALL labels ──────────
    result: dict = {}
    claimed_zone = Polygon()  # prevents zones of different labels from bleeding

    for kw in active_keywords:
        # Seed search — layer-restricted when scan_layers was supplied
        if layer_set is not None:
            seeds = _seeds_on_layers(msp, kw, layer_set)
        else:
            seeds = _find_text_seeds(msp, kw)

        seeds = _dedup_seeds(seeds, seed_merge_dist)

        if not seeds:
            continue

        polys_for_kw = []
        for seed_xy in seeds:
            poly = _inflate_balloon(
                Point(seed_xy),
                barrier_region,
                constraint,
                claimed_zone,
                step,
                max_iter,
                stop_thresh,
                seed_radius,
                verbose,
            )
            if poly is None or poly.is_empty or poly.area < min_area:
                continue
            poly = poly.simplify(simplify_tol)
            polys_for_kw.append(list(poly.exterior.coords))
            claimed_zone = claimed_zone.union(poly)

        if polys_for_kw:
            result[kw.lower()] = polys_for_kw

    # ── 5. Optionally write polygons back to DXF ──────────────────────────────
    if output_dxf:
        for label, polys in result.items():
            layer_name = _sanitize_layer_name(label)  # e.g. 55" → 55_
            if layer_name not in doc.layers:
                doc.layers.new(layer_name)
            for coords in polys:
                msp.add_lwpolyline(coords, close=True, dxfattribs={'layer': layer_name})
        doc.saveas(output_dxf)

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Strip --output/-o before mode detection so positional tokens stay clean
    raw = sys.argv[1:]
    output_dxf_arg = None
    filtered = []
    i = 0
    while i < len(raw):
        if raw[i] in ('--output', '-o') and i + 1 < len(raw):
            output_dxf_arg = raw[i + 1]
            i += 2
        else:
            filtered.append(raw[i])
            i += 1
    args = filtered

    if not args:
        print(
            "Usage:\n"
            "  balloon_zone_extract.py <dxf> [--output out.dxf]                         # Mode 3\n"
            "  balloon_zone_extract.py <dxf> LAYER_1 LAYER_2 [--output out.dxf]         # Mode 2\n"
            "  balloon_zone_extract.py <dxf> LAYER_1 TEXT_A TEXT_B [--output out.dxf]   # Mode 1\n"
            "\n"
            "  -o / --output  Write zone polygons as LWPOLYLINES to this DXF file.",
            file=sys.stderr,
        )
        sys.exit(1)

    dxf_path = args[0]
    rest = args[1:]

    if not rest:
        # Mode 3 — scan entire DXF for all labels
        result = detect_zones(dxf_path, output_dxf=output_dxf_arg)
    else:
        # Load DXF once to determine which tokens are actual layer names
        doc = ezdxf.readfile(dxf_path)
        dxf_layer_names = {layer.dxf.name for layer in doc.layers}

        # Walk remaining tokens and group text names under their preceding layer
        layer_text_map: dict = {}
        current_layer = None
        all_tokens_are_layers = True

        for token in rest:
            if token in dxf_layer_names:
                current_layer = token
                layer_text_map[current_layer] = []
            else:
                all_tokens_are_layers = False
                if current_layer is None:
                    print(
                        f"Warning: '{token}' is not a recognised DXF layer; skipping.",
                        file=sys.stderr,
                    )
                    continue
                layer_text_map[current_layer].append(token)

        scan_layers = list(layer_text_map.keys())

        if all_tokens_are_layers:
            # Mode 2 — specific layers, auto-discover labels
            result = detect_zones(dxf_path, scan_layers=scan_layers, output_dxf=output_dxf_arg)
        else:
            # Mode 1 — specific layers AND specific text labels
            keywords = [t for texts in layer_text_map.values() for t in texts]
            result = detect_zones(
                dxf_path,
                scan_layers=scan_layers,
                keywords=keywords,
                output_dxf=output_dxf_arg,
            )

    print(json.dumps(result, indent=2))

"""Flooring docket — tile / hatch / skip per zone + default type remainder.

One mode only (schema 3.1): every zone assignment carries a flooring type;
whatever remains of the store gets ``default_flooring_type``.

Origin priority (all config-driven):
  1. explicit start-reference layers (``flooring.start_reference.layers``)
  2. façade door center (``flooring.facade.door_layers``)
  3. door-gap detection between façade glass segments
     (``flooring.facade.glass_layers``)
  4. store-bottom midpoint (geometric fallback)

Geometric assumptions (each overridable by the config field named):
  * Fallback 4 assumes the store entry is at the minimum-Y edge of the store
    outline.  Configure ``flooring.start_reference.layers`` or
    ``flooring.facade`` to override.
  * ``shift_origin_past`` exclusions move the origin up (+Y) just past the
    exclusion's bounding box, keeping the X of the original origin.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

from dockets.base import (Ctx, DocketResult, Out, add_polygon, add_text,
                          iter_polys, mm2_to_m2, m2_to_sqft, union_polys)
import config_loader

_MAX_TILE_CELLS = 250_000  # runaway-grid guard (engineering constant)


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("flooring")
    store = ctx.store_region
    if store is None:
        return result.fail("store outline not found — check "
                           "store_outline.layers in the config")

    types = {t["name"]: t for t in ctx.cfg.get("flooring_types") or []}
    params = docket_cfg.get("params") or {}
    dflt = config_loader.DEFAULTS["flooring"]
    wastage_pct = float(params.get("wastage_pct", dflt["wastage_pct"]))

    # ── exclusions ─────────────────────────────────────────────────────────
    exclusion_geoms = []
    shift_past_geoms = []
    for exc in docket_cfg.get("exclusions") or []:
        polys = ctx.resolver.resolve(exc.get("zone"))
        if not polys:
            result.warn(f"exclusion '{exc.get('name')}' resolved to no "
                        f"geometry — skipped")
            continue
        geom = union_polys(polys)
        exclusion_geoms.append(geom)
        if exc.get("shift_origin_past"):
            shift_past_geoms.append(geom)
    exclusions = union_polys([g for g in exclusion_geoms]) if \
        exclusion_geoms else None

    # ── zone assignments ───────────────────────────────────────────────────
    remainder = store
    regions: List[Tuple[str, Any]] = []   # (type_name, region)
    for i, entry in enumerate(docket_cfg.get("zones") or []):
        polys = ctx.resolver.resolve(entry.get("zone"))
        if not polys:
            result.warn(f"flooring.zones[{i}] resolved to no geometry — "
                        f"skipped")
            continue
        # claim only still-unclaimed floor: earlier assignments win, so a
        # skip zone can never be painted over by a later, larger zone
        region = union_polys(polys).intersection(remainder)
        remainder = remainder.difference(region)
        tname = entry.get("flooring_type")
        ftype = types.get(tname)
        if ftype is None or ftype.get("kind") == "skip":
            continue  # carved out, nothing drawn
        regions.append((tname, region))

    dft_name = docket_cfg.get("default_flooring_type")
    if dft_name and types.get(dft_name, {}).get("kind") != "skip" \
            and not remainder.is_empty:
        regions.append((dft_name, remainder))
    elif not dft_name and not remainder.is_empty:
        result.warn("no default_flooring_type set — the unassigned floor "
                    "area is left empty")

    if exclusions is not None:
        regions = [(n, r.difference(exclusions)) for n, r in regions]

    # ── origin ─────────────────────────────────────────────────────────────
    origin = _find_origin(ctx, docket_cfg, store, result)
    origin = _shift_origin_past(origin, shift_past_geoms)

    # ── draw each region with its type ─────────────────────────────────────
    boq_types: Dict[str, Dict[str, Any]] = {}
    for tname, region in regions:
        ftype = types.get(tname) or {}
        for poly in iter_polys(region):
            if poly.area < 1.0:
                continue
            entry = boq_types.setdefault(tname, {
                "kind": ftype.get("kind"), "area_m2": 0.0,
                "full_tiles": 0, "cut_tiles": 0})
            entry["area_m2"] += poly.area / 1e6
            if ftype.get("kind") == "tile":
                full, cut = _draw_tiles(out, poly, ftype, origin, result)
                entry["full_tiles"] += full
                entry["cut_tiles"] += cut
            elif ftype.get("kind") == "hatch":
                _draw_hatch(out, poly, ftype)
            add_polygon(out, poly, out.layer("floor_outline"))

    # ── BOQ ────────────────────────────────────────────────────────────────
    for tname, entry in boq_types.items():
        entry["area_m2"] = round(entry["area_m2"], 3)
        entry["area_sqft"] = m2_to_sqft(entry["area_m2"])
        if entry["kind"] == "tile":
            total = entry["full_tiles"] + entry["cut_tiles"]
            entry["tiles_total"] = total
            entry["wastage_pct"] = wastage_pct
            entry["order_qty"] = math.ceil(total * (1 + wastage_pct / 100.0))
    result.boq = {
        "types": boq_types,
        "net_area_m2": round(sum(e["area_m2"] for e in boq_types.values()), 3),
        "origin": [round(origin[0], 1), round(origin[1], 1)] if origin else None,
    }

    return result


# ---------------------------------------------------------------------------
# origin
# ---------------------------------------------------------------------------

def _find_origin(ctx: Ctx, docket_cfg: Dict[str, Any], store: Polygon,
                 result: DocketResult) -> Tuple[float, float]:
    # 1. explicit start-reference layers
    sr_layers = (docket_cfg.get("start_reference") or {}).get("layers") or []
    if sr_layers:
        pt = _first_point_on_layers(ctx, sr_layers)
        if pt is not None:
            return pt
        result.warn("start_reference layers configured but no usable entity "
                    "found on them")

    facade = docket_cfg.get("facade") or {}
    # 2. façade door center
    door_layers = facade.get("door_layers") or []
    if door_layers:
        segs = ctx.resolver.segments_on_layers(door_layers)
        if segs:
            longest = max(segs, key=lambda s: s.length)
            mid = longest.interpolate(0.5, normalized=True)
            return (mid.x, mid.y)
        result.warn("facade.door_layers configured but no segments found")

    # 3. door-gap detection between façade glass segments
    glass_layers = facade.get("glass_layers") or []
    if glass_layers:
        segs = ctx.resolver.segments_on_layers(glass_layers)
        gap_mid = _largest_gap_midpoint(segs)
        if gap_mid is not None:
            return gap_mid
        result.warn("facade.glass_layers configured but no door gap detected")

    # 4. geometric fallback: assume the entry is at the store's minimum-Y
    #    edge (override with flooring.start_reference or flooring.facade).
    minx, miny, maxx, _ = store.bounds
    return ((minx + maxx) / 2.0, miny)


def _first_point_on_layers(ctx: Ctx, layers: List[str]
                           ) -> Optional[Tuple[float, float]]:
    wanted = {l.strip().lower() for l in ctx.resolver.match_layers(layers)}
    if not wanted:
        return None
    for e in ctx.doc.modelspace():
        try:
            layer = (e.dxf.layer or "").strip().lower()
        except Exception:
            continue
        if layer not in wanted:
            continue
        t = e.dxftype()
        try:
            if t == "POINT":
                return (e.dxf.location.x, e.dxf.location.y)
            if t == "INSERT":
                return (e.dxf.insert.x, e.dxf.insert.y)
            if t == "LINE":
                return ((e.dxf.start.x + e.dxf.end.x) / 2,
                        (e.dxf.start.y + e.dxf.end.y) / 2)
            if t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
                if pts:
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    return (sum(xs) / len(xs), sum(ys) / len(ys))
        except Exception:
            continue
    return None


def _largest_gap_midpoint(segs: List[LineString]
                          ) -> Optional[Tuple[float, float]]:
    """Door-gap detection: project façade glass segments onto their dominant
    axis and return the midpoint of the largest gap between them.

    Geometric assumption: the façade runs along one dominant axis; the door
    is the widest un-glazed stretch.  Override via
    ``flooring.facade.door_layers`` or ``flooring.start_reference``.
    """
    if len(segs) < 2:
        return None
    total_dx = sum(abs(s.coords[-1][0] - s.coords[0][0]) for s in segs)
    total_dy = sum(abs(s.coords[-1][1] - s.coords[0][1]) for s in segs)
    horizontal = total_dx >= total_dy
    axis = 0 if horizontal else 1
    other = 1 - axis
    intervals = []
    for s in segs:
        c0, c1 = s.coords[0], s.coords[-1]
        lo, hi = sorted((c0[axis], c1[axis]))
        intervals.append((lo, hi, (c0[other] + c1[other]) / 2))
    intervals.sort()
    best_gap, best_mid = 0.0, None
    for (lo1, hi1, o1), (lo2, hi2, o2) in zip(intervals, intervals[1:]):
        gap = lo2 - hi1
        if gap > best_gap:
            best_gap = gap
            mid_axis = (hi1 + lo2) / 2
            mid_other = (o1 + o2) / 2
            best_mid = ((mid_axis, mid_other) if horizontal
                        else (mid_other, mid_axis))
    return best_mid


def _shift_origin_past(origin: Tuple[float, float],
                       geoms: List) -> Tuple[float, float]:
    """Move the origin up (+Y) just past any shift-past exclusion covering it."""
    ox, oy = origin
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        if geom.buffer(1.0).contains(Point(ox, oy)):
            oy = geom.bounds[3] + 1.0
    return (ox, oy)


# ---------------------------------------------------------------------------
# tile grid
# ---------------------------------------------------------------------------

def _draw_tiles(out: Out, region: Polygon, ftype: Dict[str, Any],
                origin: Tuple[float, float], result: DocketResult
                ) -> Tuple[int, int]:
    """Checkerboard tile grid, rotated about the origin, clipped to region.

    Grid is generated in a frame rotated by −rotation about the origin so the
    cells are axis-aligned, then each kept cell is rotated back by +rotation.
    Returns (full_tiles, cut_tiles).
    """
    dflt = config_loader.DEFAULTS["flooring"]
    length = float(ftype.get("length_mm") or dflt["tile_length_mm"])
    width = float(ftype.get("width_mm") or dflt["tile_width_mm"])
    rot = float(ftype.get("rotation_deg", dflt["tile_rotation_deg"]) or 0.0)
    ox, oy = origin

    local = (affinity.rotate(region, -rot, origin=(ox, oy))
             if rot else region)
    minx, miny, maxx, maxy = local.bounds
    i0 = math.floor((minx - ox) / length)
    i1 = math.ceil((maxx - ox) / length)
    j0 = math.floor((miny - oy) / width)
    j1 = math.ceil((maxy - oy) / width)
    n_cells = max(0, (i1 - i0)) * max(0, (j1 - j0))
    if n_cells > _MAX_TILE_CELLS:
        result.warn(f"tile grid of {n_cells} cells exceeds the "
                    f"{_MAX_TILE_CELLS} guard — tiles not drawn for one "
                    f"region (area still counted)")
        est = int(local.area // (length * width))
        return est, 0

    prepared = prep(local)
    layer_a = out.layer("tile_a")
    layer_b = out.layer("tile_b")
    layer_cut = out.layer("tile_cut")
    full = cut = 0
    cell_area = length * width
    for i in range(i0, i1):
        x0 = ox + i * length
        for j in range(j0, j1):
            y0 = oy + j * width
            cell = box(x0, y0, x0 + length, y0 + width)
            if not prepared.intersects(cell):
                continue
            if prepared.contains(cell):
                piece = cell
                is_full = True
            else:
                piece = cell.intersection(local)
                if piece.is_empty or piece.area < cell_area * 0.005:
                    continue
                is_full = False
            layer = ((layer_a if (i + j) % 2 == 0 else layer_b)
                     if is_full else layer_cut)
            for poly in iter_polys(piece):
                world = (affinity.rotate(poly, rot, origin=(ox, oy))
                         if rot else poly)
                add_polygon(out, world, layer)
            if is_full:
                full += 1
            else:
                cut += 1
    return full, cut


def _draw_hatch(out: Out, region: Polygon, ftype: Dict[str, Any]) -> None:
    layer = out.layer("floor_hatch")
    color = int(ftype.get("color", 8) or 8)
    pattern = ftype.get("pattern") or "SOLID"
    hatch = out.msp.add_hatch(color=color, dxfattribs={"layer": layer})
    if pattern.upper() != "SOLID":
        try:
            hatch.set_pattern_fill(pattern,
                                   scale=float(ftype.get("scale", 1) or 1),
                                   angle=float(ftype.get("angle", 0) or 0),
                                   color=color)
        except Exception:
            hatch.set_solid_fill(color=color)
    paths = hatch.paths
    paths.add_polyline_path(list(region.exterior.coords), is_closed=True)
    for ring in region.interiors:
        paths.add_polyline_path(list(ring.coords), is_closed=True)


# ---------------------------------------------------------------------------
# wall frame import
# ---------------------------------------------------------------------------


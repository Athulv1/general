#!/usr/bin/env python3
"""
Zone-based flooring renderer (schema 3.0).

Each ZONE (a region in the DXF) is given a FLOORING TYPE:
    - kind "tile"  -> individual length x width rectangles, rotated, clipped to
                      the zone, alternating grey/white (countable for BOQ).
    - kind "hatch" -> an ezdxf HATCH pattern fill (172 patterns + SOLID).
    - kind "skip"  -> no flooring (carved out, e.g. a toilet).

Zone regions come from:
    - "polyline" : closed polygons on a layer
    - "block"    : the bounding box of a named block's inserts
    - "balloon"  : a text label + balloon inflation (room with a name, no boundary)

Rules
-----
- If floor zones exist  -> each floor zone is tiled/hatched with its own type
  (minus any skip zones), clipped to the store outline.
- If NO floor zones     -> the whole store outline is floored with
  `default_flooring_type` (minus skip zones).

Flooring is drawn INTO the source drawing and saved as the output, so the
original walls / context are preserved automatically.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import ezdxf
from ezdxf import bbox
from shapely.affinity import rotate as shp_rotate
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from extract_layer_polygons_closed import extract_layer_polygons_closed

try:
    from balloon_zone_extract import detect_zones
except Exception:
    detect_zones = None

# ── output layers / colours ────────────────────────────────────────────────
L_TILE_GREY = "FLOOR-TILE-GREY"
L_TILE_WHITE = "FLOOR-TILE-WHITE"
L_OUTLINE = "FLOOR-TILE-OUTLINE"
L_NET = "FLOOR-OUTLINE"
L_SKIP = "FLOOR-NO-TILE-ZONE"
L_HATCH = "FLOOR-HATCH"
ACI_GREY, ACI_WHITE, ACI_OUTLINE, ACI_NET, ACI_SKIP, ACI_HATCH = 8, 254, 7, 3, 1, 9
MM2_SQFT = 1.0 / 92903.04


# ── geometry helpers ───────────────────────────────────────────────────────
def _iter_polys(geom):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        for g in geom.geoms:
            if g.geom_type == "Polygon" and not g.is_empty:
                yield g


def _polys_from_layers(dxf: str, layers: Optional[List[str]]) -> List[Polygon]:
    out: List[Polygon] = []
    for lay in layers or []:
        res = extract_layer_polygons_closed(dxf, {"priority": [lay], lay: "z"})
        for ring in res.get("z") or []:
            try:
                p = Polygon(ring)
                if not p.is_valid:
                    p = p.buffer(0)
                if p.is_valid and p.area > 0:
                    out.append(p)
            except Exception:
                pass
    return out


def _polys_from_block(doc, blockname: Optional[str]) -> List[Polygon]:
    out: List[Polygon] = []
    if not blockname:
        return out
    for ins in doc.modelspace().query("INSERT"):
        if ins.dxf.name.lower() != blockname.lower():
            continue
        try:
            b = bbox.extents([ins])
            if b.has_data:
                out.append(box(b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y))
        except Exception:
            pass
    return out


def _polys_from_balloon(dxf: str, text_layers, keywords, barrier_layers=None) -> List[Polygon]:
    if detect_zones is None or not keywords:
        return []
    try:
        res = detect_zones(dxf_path=dxf, balloon_groups={
            "Z": {"layers": text_layers or None, "keywords": keywords,
                  "barrier_layers": barrier_layers}}) or {}
        zones = (res.get("Z") or {}).get("zones", {})
        return [Polygon(v["polygon"]) for v in zones.values()]
    except Exception as exc:
        print(f"  ! balloon detection failed: {exc}")
        return []


def _resolve_zone(doc, dxf: str, z: Dict[str, Any], barrier_layers=None) -> List[Polygon]:
    ident = (z.get("identifier") or "polyline").lower()
    if ident == "polyline":
        layers = z.get("layers") or ([z["layer"]] if z.get("layer") else [])
        return _polys_from_layers(dxf, layers)
    if ident == "block":
        return _polys_from_block(doc, z.get("blockname") or z.get("block"))
    if ident == "balloon":
        tl = z.get("text_layers") or ([z["text_layer"]] if z.get("text_layer") else None)
        kw = z.get("keywords") or ([z["keyword"]] if z.get("keyword") else [])
        bl = z.get("barrier_layers") or barrier_layers
        return _polys_from_balloon(dxf, tl, kw, bl)
    return []


# ── drawing ────────────────────────────────────────────────────────────────
def _strip_to_layers(doc, keep_layers) -> int:
    """Keep ONLY the given source layers (plus all flooring layers); delete
    everything else, including geometry nested inside block definitions (walls
    often live inside the plan's INSERT). Gives a clean walls+flooring output."""
    keep = {l.strip().lower() for l in keep_layers}
    keep |= {l.lower() for l in (L_TILE_GREY, L_TILE_WHITE, L_OUTLINE, L_NET, L_SKIP, L_HATCH)}
    removed = 0

    def _strip(container, allow_inserts):
        nonlocal removed
        doomed = []
        for e in container:
            if e.dxftype() == "INSERT" and allow_inserts:
                continue  # keep INSERTs; their contents are filtered in blocks
            if (e.dxf.layer or "").lower() not in keep:
                doomed.append(e)
        for e in doomed:
            try:
                container.delete_entity(e); removed += 1
            except Exception:
                pass

    # block definitions first (walls/furniture live here)
    for block in doc.blocks:
        if block.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        _strip(block, allow_inserts=True)
    # then modelspace
    _strip(doc.modelspace(), allow_inserts=True)
    return removed


def _ensure_layers(doc):
    for name, aci in [(L_TILE_GREY, ACI_GREY), (L_TILE_WHITE, ACI_WHITE),
                      (L_OUTLINE, ACI_OUTLINE), (L_NET, ACI_NET),
                      (L_SKIP, ACI_SKIP), (L_HATCH, ACI_HATCH)]:
        if name not in doc.layers:
            doc.layers.add(name, color=aci)


def _draw_tiles(msp, region, ft: Dict[str, Any], origin: Tuple[float, float]) -> Tuple[int, int]:
    L = float(ft.get("length_mm", 600)); W = float(ft.get("width_mm", 600))
    rot = float(ft.get("rotation_deg", 0)); ox, oy = origin
    pr = shp_rotate(region, -rot, origin=(ox, oy))   # into axis-aligned tile frame
    minx, miny, maxx, maxy = pr.bounds
    i0 = math.floor((minx - ox) / L); i1 = math.ceil((maxx - ox) / L)
    j0 = math.floor((miny - oy) / W); j1 = math.ceil((maxy - oy) / W)
    full = partial = 0
    for i in range(i0, i1):
        for j in range(j0, j1):
            cell = box(ox + i * L, oy + j * W, ox + (i + 1) * L, oy + (j + 1) * W)
            inter = cell.intersection(pr)
            if inter.is_empty or inter.area < 1.0:
                continue
            grey = (i + j) % 2 == 0
            lay = L_TILE_GREY if grey else L_TILE_WHITE
            aci = ACI_GREY if grey else ACI_WHITE
            for piece in _iter_polys(inter):
                coords = list(shp_rotate(piece, rot, origin=(ox, oy)).exterior.coords)
                msp.add_lwpolyline(coords, close=True, dxfattribs={"layer": L_OUTLINE})
                h = msp.add_hatch(dxfattribs={"layer": lay})
                h.set_solid_fill(color=aci)
                h.paths.add_polyline_path(coords, is_closed=True)
            if abs(inter.area - L * W) < 1.0:
                full += 1
            else:
                partial += 1
    return full, partial


def _draw_hatch(msp, region, ft: Dict[str, Any]):
    pat = (ft.get("pattern") or "SOLID"); scale = float(ft.get("scale", 1.0))
    angle = float(ft.get("angle", 0.0)); color = int(ft.get("color", ACI_HATCH))
    for poly in _iter_polys(region):
        h = msp.add_hatch(color=color, dxfattribs={"layer": L_HATCH})
        if pat.upper() == "SOLID":
            h.set_solid_fill(color=color)
        else:
            try:
                h.set_pattern_fill(pat, scale=scale, angle=angle)
            except Exception as exc:
                print(f"  ! hatch pattern '{pat}' failed ({exc}); using SOLID")
                h.set_solid_fill(color=color)
        h.paths.add_polyline_path(list(poly.exterior.coords), is_closed=True)
        for ring in poly.interiors:
            h.paths.add_polyline_path(list(ring.coords), is_closed=True)


# ── main ───────────────────────────────────────────────────────────────────
def generate(dxf: str, cfg: Dict[str, Any], output: str) -> Optional[str]:
    doc = ezdxf.readfile(dxf)
    msp = doc.modelspace()
    flooring = cfg.get("flooring", {})

    print("=" * 70)
    print("ZONE-BASED FLOORING")
    print("=" * 70)

    # store outline
    so = flooring.get("store_outline", {})
    store_polys = _polys_from_layers(dxf, so.get("layers"))
    if not store_polys:
        raise ValueError(f"No store outline found on {so.get('layers')}")
    store = unary_union(store_polys)
    store_poly = max(_iter_polys(store), key=lambda p: p.area)
    print(f"  store outline: {store_poly.area * MM2_SQFT:.2f} sq ft")

    ftypes = {f["name"]: f for f in cfg.get("flooring_types", [])}

    # classify zones
    floor_zones: List[Tuple[Dict, Dict, Any]] = []
    skip_geoms = []
    for z in flooring.get("zones", []) or []:
        polys = _resolve_zone(doc, dxf, z)
        if not polys:
            print(f"  zone '{z.get('name')}' -> 0 regions found"); continue
        region = unary_union(polys)
        ftname = z.get("flooring_type")
        ft = ftypes.get(ftname)
        kind = (ft.get("kind") if ft else "skip").lower()
        if kind == "skip":
            skip_geoms.append(region)
            print(f"  zone '{z.get('name')}' [skip] -> left empty")
        else:
            floor_zones.append((z, ft, region))
            print(f"  zone '{z.get('name')}' -> '{ftname}' ({kind})")
    skip_union = unary_union(skip_geoms) if skip_geoms else None

    _ensure_layers(doc)
    origin = (store_poly.centroid.x, store_poly.centroid.y)

    total_full = 0
    floored_area = 0.0
    if floor_zones:
        for z, ft, region in floor_zones:
            region = region.intersection(store_poly)
            if skip_union:
                region = region.difference(skip_union)
            if region.is_empty:
                continue
            floored_area += region.area
            if (ft.get("kind") or "").lower() == "tile":
                f, _ = _draw_tiles(msp, region, ft, origin); total_full += f
            else:
                _draw_hatch(msp, region, ft)
    else:
        dft = ftypes.get(cfg.get("default_flooring_type")) or {
            "kind": "tile", "length_mm": 600, "width_mm": 600, "rotation_deg": 45}
        region = store_poly
        if skip_union:
            region = region.difference(skip_union)
        floored_area = region.area
        print(f"  no floor zones -> whole store with default '{cfg.get('default_flooring_type','(builtin tile)')}'")
        if (dft.get("kind") or "tile").lower() == "tile":
            total_full, _ = _draw_tiles(msp, region, dft, origin)
        else:
            _draw_hatch(msp, region, dft)

    # outlines
    msp.add_lwpolyline(list(store_poly.exterior.coords), close=True, dxfattribs={"layer": L_NET})
    if skip_union:
        for p in _iter_polys(skip_union):
            msp.add_lwpolyline(list(p.exterior.coords), close=True, dxfattribs={"layer": L_SKIP})

    # Optional clean-up: keep only the named source layers (e.g. walls /
    # partitions) + the flooring; strip furniture, fixtures, dimensions, etc.
    keep = (cfg.get("output") or {}).get("keep_layers") or cfg.get("keep_layers")
    if keep:
        n = _strip_to_layers(doc, keep)
        print(f"  ✓ clean output: kept {keep} + flooring, stripped {n} other entities")

    # Send all flooring entities BEHIND the original drawing so the walls,
    # partitions and context stay visible on top of the tile / hatch fills.
    # We assign a FULL redraw order (every entity) so flooring always sorts
    # before the original geometry, regardless of the source's handle values.
    floor_layers = {L_TILE_GREY, L_TILE_WHITE, L_OUTLINE, L_NET, L_SKIP, L_HATCH}
    flooring_ents, other_ents = [], []
    for e in msp:
        (flooring_ents if e.dxf.layer in floor_layers else other_ents).append(e)
    ordered = flooring_ents + other_ents          # flooring first => drawn behind
    order = {e.dxf.handle: format(i + 1, "X") for i, e in enumerate(ordered)}
    try:
        msp.set_redraw_order(order)
        print(f"  ✓ flooring sent behind walls/context "
              f"({len(flooring_ents)} flooring under {len(other_ents)} original entities)")
    except Exception as exc:
        print(f"  ! could not set draw order: {exc}")

    print(f"\n  floored area: {floored_area * MM2_SQFT:.2f} sq ft")
    if total_full:
        print(f"  full tiles:   {total_full}")
    if skip_union:
        print(f"  skipped area: {skip_union.area * MM2_SQFT:.2f} sq ft")

    import os
    out_dir = os.path.dirname(os.path.abspath(output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    doc.saveas(output)
    print(f"  ✓ saved: {output}")
    return output

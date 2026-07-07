#!/usr/bin/env python3
"""
Generalised flooring pipeline (schema 2.0).

Idea
----
The customer fills in only layer names in the config. The pipeline then:

    1. reads the store outline  -> the whole floor region
    2. reads each zone outline  -> applies its rule:
           action "skip"  -> leave that region empty (no tiles)
           action "floor" -> tile it (default; part of the store)
       If no zone rules are present, the COMPLETE store is floored.
    3. (optional) reads a start point (INSERT/POINT) -> tile-grid origin
    4. assembles the context (ctx), stores it to a .ctx file
    5. runs flooring_layout.py with that ctx -> final flooring DXF

Only two layer types are mandatory for the customer:
    * store_outline.layers      (the floor boundary)
    * zone_outlines[].layers    (regions with rules; optional)

Usage
-----
    py -3 run_flooring_pipeline.py [config.json] [input.dxf] [output.dxf]

Args fall back to io.input_dxf / io.output_dxf / io.ctx_file in the config.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import ezdxf

from extract_layer_polygons_closed import extract_layer_polygons_closed

try:
    from balloon_zone_extract import detect_zones  # text + balloon room detection
except Exception:
    detect_zones = None

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# config + layer helpers
# ---------------------------------------------------------------------------
def _load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_layers(doc, layers: Optional[List[str]], pattern: Optional[str]) -> List[str]:
    """Resolve explicit layer names + an optional wildcard pattern against the
    DXF layer table (case-insensitive). Returns the layer names that exist."""
    table = {l.dxf.name for l in doc.layers}
    table_lower = {n.lower(): n for n in table}
    out: List[str] = []
    for name in layers or []:
        canon = table_lower.get(name.strip().lower())
        if canon and canon not in out:
            out.append(canon)
    if pattern:
        pat = pattern.strip().lower()
        for low, canon in table_lower.items():
            if fnmatch.fnmatch(low, pat) and canon not in out:
                out.append(canon)
    return out


def _collect_closed(dxf: str, layer_list: List[str], ctx_key: str = "shapes") -> List[list]:
    """Collect closed polygons across one or more layers into a single list."""
    out: List[list] = []
    for layer in layer_list:
        res = extract_layer_polygons_closed(dxf, {"priority": [layer], layer: ctx_key})
        polys = res.get(ctx_key) or []
        if polys:
            out.extend(polys)
    return out


def _find_start_point(doc, layer_list: List[str]) -> Optional[Tuple[float, float]]:
    """Return the first INSERT/POINT location found on the given layers
    (recursing into blocks), or None."""
    msp = doc.modelspace()
    want = {l.lower() for l in layer_list}

    def scan(container, xform=None):
        for e in container:
            t = e.dxftype()
            if t == "INSERT":
                if e.dxf.layer.lower() in want:
                    p = e.dxf.insert
                    return (float(p[0]), float(p[1]))
                try:
                    blk = doc.blocks[e.dxf.name]
                    r = scan(blk)
                    if r:
                        return r
                except Exception:
                    pass
            elif t == "POINT" and e.dxf.layer.lower() in want:
                p = e.dxf.location
                return (float(p[0]), float(p[1]))
        return None

    return scan(msp)


def _to_plain(obj):
    if isinstance(obj, (list, tuple)):
        return [_to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# context assembly
# ---------------------------------------------------------------------------
def build_ctx(dxf: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    doc = ezdxf.readfile(dxf)
    flooring = cfg["flooring"]

    print("=" * 70)
    print("STEP 2 — EXTRACTING GEOMETRY (schema 2.0)")
    print("=" * 70)
    print(f"  Input DXF: {dxf}")

    # -- store outline (required) -----------------------------------------
    so = flooring["store_outline"]
    so_layers = _resolve_layers(doc, so.get("layers"), so.get("layer_pattern"))
    store_outline = _collect_closed(dxf, so_layers, "store-outline")
    print(f"  + store_outline: {len(store_outline)} polygon(s) from {so_layers or so.get('layers')}")
    if not store_outline:
        raise ValueError(
            f"No closed store outline found on {so.get('layers')} "
            f"(pattern={so.get('layer_pattern')}). Check store_outline.layers in the config."
        )

    # -- zones (rules) -----------------------------------------------------
    skip_zones: Dict[str, dict] = {}
    floor_zones = 0
    start_point: Optional[Tuple[float, float]] = None
    n = 1
    for zone in flooring.get("zone_outlines", []) or []:
        zid = zone.get("id", f"zone_{n}")
        action = (zone.get("action") or ("skip" if zone.get("role") == "exclude_region" else "floor")).lower()
        z_layers = _resolve_layers(doc, zone.get("layers"), zone.get("layer_pattern"))
        polys = _collect_closed(dxf, z_layers, "zone") if z_layers else []

        if action == "skip":
            for poly in polys:
                skip_zones[f"{zid.lower()}_{n}"] = {"polygon": poly}
                n += 1
            print(f"  + zone '{zid}' [skip]: {len(polys)} region(s) left empty")
        else:
            floor_zones += len(polys)
            print(f"  + zone '{zid}' [floor]: {len(polys)} region(s) tiled")

        # optional per-zone / global start point -> first one wins
        sp = zone.get("start_point")
        if start_point is None and sp:
            sp_layers = _resolve_layers(doc, sp.get("layers"), sp.get("layer_pattern"))
            if sp_layers:
                start_point = _find_start_point(doc, sp_layers)
                if start_point:
                    print(f"  + tiling origin from start point {sp_layers}: {start_point}")

    # Room-by-name exclusions (text label + balloon inflation) — for rooms that
    # carry a label (e.g. "TOILET") but have NO closed boundary polygon. The
    # balloon method seeds from the matching MTEXT/TEXT and inflates until it
    # hits surrounding walls, recovering the room outline automatically.
    er = flooring.get("exclude_rooms") or {}
    if er.get("keywords") and detect_zones is not None:
        try:
            balloon = detect_zones(
                dxf_path=dxf,
                balloon_groups={"ROOMS": {
                    "layers": er.get("text_layers") or None,
                    "keywords": er["keywords"],
                }},
            ) or {}
            rooms = (balloon.get("ROOMS") or {}).get("zones", {})
            for k, v in rooms.items():
                skip_zones[k] = {"polygon": v["polygon"]}
            print(f"  + exclude_rooms {er['keywords']}: {len(rooms)} room(s) removed by name")
        except Exception as exc:
            print(f"  ! room-by-name exclusion failed ({exc}); continuing")
    elif er.get("keywords") and detect_zones is None:
        print("  ! exclude_rooms set but balloon_zone_extract unavailable; skipping")

    # global start point (top-level) as a fallback when no zone supplied one
    if start_point is None:
        gsp = flooring.get("start_point")
        if gsp:
            sp_layers = _resolve_layers(doc, gsp.get("layers"), gsp.get("layer_pattern"))
            if sp_layers:
                start_point = _find_start_point(doc, sp_layers)
                if start_point:
                    print(f"  + tiling origin from start point {sp_layers}: {start_point}")

    # -- assemble ctx ------------------------------------------------------
    ctx: Dict[str, Any] = {
        "store-outline": store_outline,
        # skip-zones are routed through the engine's exclusion (no-tile) slot
        "BALLOON": {"TOILET": {"count": len(skip_zones), "zones": skip_zones}},
        # everything else is optional — left empty so the engine uses its
        # synthesis fallbacks (façade from store bottom edge, etc.)
        "hatches": [],
        "bulkhead": [],
        "lintel": [],
        "cols": [],
        "store-mainglass": [],
        "store-maindoor": [],
    }
    # If a start point was given, inject it as the tile-grid origin via a tiny
    # 2-point segment whose midpoint is the start point (the engine reads the
    # door/origin as midpoint(store-maindoor[0])).
    if start_point:
        x, y = start_point
        ctx["store-maindoor"] = [[(x - 1.0, y), (x + 1.0, y)]]

    print(f"\n  zones: {len(skip_zones)} skipped, {floor_zones} explicit-floor")
    return _to_plain(ctx)


# ---------------------------------------------------------------------------
# flooring run
# ---------------------------------------------------------------------------
def run_flooring(dxf: str, output: str, ctx: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    import flooring_layout

    print("=" * 70)
    print("STEP 3 — GENERATING FLOORING LAYOUT")
    print("=" * 70)

    p = cfg.get("params", {})
    layout = flooring_layout.FlooringLayout(
        dxf,
        granite_width=0.0,
        min_door_width=float(p.get("min_door_width_mm", 600.0)),
        tile_size=float(p.get("tile_size_mm", 600.0)),
        wastage_factor=float(p.get("wastage_pct", 10.0)) / 100.0,
        tile_rotation_deg=float(p.get("tile_rotation_deg", 45.0)),
        ctx=ctx,
    )

    # Wall cross-section layers are config-driven (no hardcoded layer names).
    # The engine copies these layers verbatim from the source DXF in
    # save_output(); overriding the instance attributes redirects that copy.
    wf = cfg.get("flooring", {}).get("wall_frame") or {}
    if wf.get("mirror_layers") is not None:
        layout.MIRROR_WALL_LAYERS = list(wf["mirror_layers"])
    if wf.get("hatch_layers") is not None:
        layout.HATCH_SOURCE_LAYERS = list(wf["hatch_layers"])
        layout.BOH_OUTLINE_LAYERS = list(wf["hatch_layers"])
    print(f"  wall frame layers: {layout.MIRROR_WALL_LAYERS} + hatch {layout.HATCH_SOURCE_LAYERS}")

    layout.extract()
    out_dir = os.path.dirname(os.path.abspath(output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    return layout.save_output(output)


# ---------------------------------------------------------------------------
# zone-based mode (schema 3.0) — runs the OLD engine with per-zone flooring
# ---------------------------------------------------------------------------
def _run_zonal_old_engine(cfg: Dict[str, Any], dxf: str, output: str) -> Optional[str]:
    import ezdxf
    import zone_flooring
    import flooring_layout
    from shapely.ops import unary_union

    print("=" * 70)
    print("STEP 2/3 — ZONE-BASED FLOORING (old engine)")
    print("=" * 70)

    doc = ezdxf.readfile(dxf)
    flooring = cfg["flooring"]
    ftypes = {f["name"]: f for f in cfg.get("flooring_types", [])}

    so = flooring.get("store_outline", {})
    store_polys = zone_flooring._polys_from_layers(dxf, so.get("layers"))
    if not store_polys:
        print(f"ERROR: no closed store outline on {so.get('layers')}")
        return None
    store_poly = max(zone_flooring._iter_polys(unary_union(store_polys)), key=lambda p: p.area)

    # Wall + store layers act as natural barriers for balloon (text) zones,
    # so the customer never configures barriers separately.
    wall_layers = list((flooring.get("wall_frame") or {}).get("mirror_layers") or [])
    barrier_layers = wall_layers + list(so.get("layers") or [])

    floor_zones: List[Dict[str, Any]] = []
    skip_polys = []
    for z in flooring.get("zones", []) or []:
        polys = zone_flooring._resolve_zone(doc, dxf, z, barrier_layers=barrier_layers)
        if not polys:
            print(f"  zone '{z.get('name')}' -> 0 regions found")
            continue
        region = unary_union(polys)
        ft = ftypes.get(z.get("flooring_type"))
        kind = (ft.get("kind") if ft else "skip").lower()
        if kind == "skip":
            skip_polys.extend(zone_flooring._iter_polys(region))
            print(f"  zone '{z.get('name')}' [skip] -> left empty")
        else:
            for p in zone_flooring._iter_polys(region):
                floor_zones.append({"polygon": list(p.exterior.coords), "flooring_type": ft})
            print(f"  zone '{z.get('name')}' -> '{z.get('flooring_type')}' ({kind})")

    ctx = {
        "store-outline": [list(store_poly.exterior.coords)],
        "hatches": [], "bulkhead": [], "lintel": [], "cols": [],
        "store-mainglass": [], "store-maindoor": [],
        "BALLOON": {"TOILET": {"zones": {
            f"skip_{i}": {"polygon": list(p.exterior.coords)} for i, p in enumerate(skip_polys)
        }}},
        "zones": floor_zones,
        "default_flooring_type": ftypes.get(cfg.get("default_flooring_type")),
    }

    dft = ftypes.get(cfg.get("default_flooring_type")) or {}
    tile_size = float(dft.get("length_mm", 600) or 600)
    rot = float(dft.get("rotation_deg", 45)) if (dft.get("kind") == "tile") else 45.0

    layout = flooring_layout.FlooringLayout(
        dxf, ctx=ctx, granite_width=0.0, tile_size=tile_size, tile_rotation_deg=rot,
    )
    wf = flooring.get("wall_frame") or {}
    if wf.get("mirror_layers") is not None:
        layout.MIRROR_WALL_LAYERS = list(wf["mirror_layers"])
    if wf.get("hatch_layers") is not None:
        layout.HATCH_SOURCE_LAYERS = list(wf["hatch_layers"])
        layout.BOH_OUTLINE_LAYERS = list(wf["hatch_layers"])
    print(f"  wall frame layers: {layout.MIRROR_WALL_LAYERS}")

    layout.extract()
    out_dir = os.path.dirname(os.path.abspath(output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    return layout.save_output(output)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: List[str]) -> int:
    config_path = argv[1] if len(argv) > 1 else os.path.join(HERE, "flooring_geometry_config (1).json")
    cfg = _load_config(config_path)
    io = cfg.get("io", {})

    dxf = argv[2] if len(argv) > 2 else io.get("input_dxf", "")
    output = argv[3] if len(argv) > 3 else io.get("output_dxf", "")
    ctx_file = io.get("ctx_file", "context.ctx")

    print("=" * 70)
    print("STEP 1 — CONFIG")
    print("=" * 70)
    print(f"  Config: {config_path}")

    if not dxf or not os.path.exists(dxf):
        print(f"\nERROR: input DXF not found: '{dxf}'")
        print("Set io.input_dxf in the config, or pass it as the 2nd argument:")
        print('  py -3 run_flooring_pipeline.py "<config>" "<input.dxf>" [output.dxf]')
        return 2

    if not output:
        base = os.path.splitext(os.path.basename(dxf))[0]
        output = os.path.join(os.path.dirname(os.path.abspath(dxf)), "output", f"{base}_FLOORING.dxf")

    if not os.path.isabs(ctx_file):
        ctx_file = os.path.join(os.path.dirname(os.path.abspath(config_path)), ctx_file)

    # ── Zone-based mode (schema 3.0): per-zone tile/hatch flooring ──────────
    # Triggered when the config defines reusable flooring_types. Each zone is
    # rendered with its own type; no zones -> whole store with the default type.
    if cfg.get("flooring_types"):
        result = _run_zonal_old_engine(cfg, dxf, output)
        if result:
            print(f"\n  DONE. Flooring DXF -> {result}")
            return 0
        print("\nERROR: flooring generation failed.")
        return 1

    # 2) extract + assemble ctx, store it, reload it
    ctx = build_ctx(dxf, cfg)
    with open(ctx_file, "w", encoding="utf-8") as fh:
        json.dump(ctx, fh, indent=2)
    print(f"\n  ctx stored -> {ctx_file}")
    with open(ctx_file, "r", encoding="utf-8") as fh:
        ctx = json.load(fh)

    # 3) generate
    result = run_flooring(dxf, output, ctx, cfg)
    if result:
        print(f"\n  DONE. Flooring DXF -> {result}")
        return 0
    print("\nERROR: flooring generation failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

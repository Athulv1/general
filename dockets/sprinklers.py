"""Sprinkler docket — 100% coverage grid over the store.

Spacing:
  * ``square``    → s = min(R·√2, max_spacing)   (circle covers its grid cell)
  * ``staggered`` → s = min(R·√3, max_spacing)   (triangular lattice,
    circumradius s/√3 ≤ R), rows s·√3/2 apart, offset s/2.

After grid placement the region is coverage-verified by sampling: any sample
farther than R from every head raises a warning, and with
``strict_coverage`` a head is inserted at the worst gap (repeated until
covered, bounded).  ``rooms_min_one`` guarantees ≥1 head per listed room —
or, when the list is blank, per closed room found by polygonizing the
configured wall layers.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from ezdxf.enums import TextEntityAlignment
from shapely.geometry import Point

from dockets.base import (Ctx, DocketResult, Out, add_text, coverage_gaps,
                          grid_points, iter_polys, union_polys)
import config_loader

_MAX_GAP_FILL_ITER = 50   # strict-coverage insertion bound
_SYMBOL_RADIUS = 60.0     # head glyph radius (drawing symbol, not a fact)


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("sprinklers")
    dflt = config_loader.DEFAULTS["sprinklers"]
    radius = float(docket_cfg.get("coverage_radius_mm")
                   or dflt["coverage_radius_mm"])
    max_spacing = float(docket_cfg.get("max_spacing_mm")
                        or dflt["max_spacing_mm"])
    pattern = docket_cfg.get("pattern") or dflt["pattern"]
    strict = docket_cfg.get("strict_coverage", dflt["strict_coverage"])

    store = ctx.store_region
    if store is None:
        return result.fail("store outline not found — check "
                           "store_outline.layers in the config")

    region = store
    for i, exc in enumerate(docket_cfg.get("exclusions") or []):
        polys = ctx.resolver.resolve(exc)
        if polys:
            region = region.difference(union_polys(polys))
        else:
            result.warn(f"sprinklers.exclusions[{i}] resolved to no geometry")

    factor = math.sqrt(3) if pattern == "staggered" else math.sqrt(2)
    spacing = min(radius * factor, max_spacing)
    heads: List[Tuple[float, float]] = []
    for poly in iter_polys(region):
        heads.extend(grid_points(poly, spacing, pattern))

    # ── coverage verification ──────────────────────────────────────────────
    gaps = coverage_gaps(region, heads, radius)
    inserted = 0
    if gaps and strict:
        for _ in range(_MAX_GAP_FILL_ITER):
            if not gaps:
                break
            # insert at the gap farthest from every existing head
            worst = max(gaps, key=lambda g: _min_dist2(g, heads))
            heads.append(worst)
            inserted += 1
            gaps = coverage_gaps(region, heads, radius)
    if gaps:
        result.warn(f"{len(gaps)} sampled points remain uncovered "
                    f"(radius {radius:.0f} mm) — check coverage_radius_mm")

    # ── at least one head per closed room ──────────────────────────────────
    room_boq: List[Dict[str, Any]] = []
    if docket_cfg.get("rooms_min_one", dflt["rooms_min_one"]):
        rooms = []
        listed = docket_cfg.get("rooms") or []
        if listed:
            for i, rdef in enumerate(listed):
                polys = ctx.resolver.resolve(rdef)
                if not polys:
                    result.warn(f"sprinklers.rooms[{i}] resolved to no "
                                f"geometry — skipped")
                for p in polys:
                    rooms.append((_caption(rdef, i), p))
        else:
            found = ctx.resolver.closed_rooms(dflt["min_room_area_mm2"])
            rooms = [(f"room_{i+1}", p) for i, p in enumerate(found)]
        for name, poly in rooms:
            inside = sum(1 for h in heads if poly.contains(Point(h)))
            if inside == 0:
                rp = poly.representative_point()
                heads.append((rp.x, rp.y))
                inside = 1
                inserted += 1
            room_boq.append({"room": name, "heads": inside})

    # ── draw ───────────────────────────────────────────────────────────────
    layer = out.layer("sprinkler")
    cov_layer = out.layer("sprinkler_coverage")
    dashed = out.ensure_linetype("DASHED_COV", [800.0, 500.0, -300.0])
    show_cov = bool(docket_cfg.get("show_coverage"))
    r = _SYMBOL_RADIUS
    for x, y in heads:
        out.msp.add_circle((x, y), r, dxfattribs={"layer": layer})
        out.msp.add_line((x - r, y), (x + r, y), dxfattribs={"layer": layer})
        out.msp.add_line((x, y - r), (x, y + r), dxfattribs={"layer": layer})
        if show_cov:
            out.msp.add_circle((x, y), radius,
                               dxfattribs={"layer": cov_layer,
                                           "linetype": dashed,
                                           "ltscale": 10.0})

    # count table at the right of the store
    bx = store.bounds
    add_text(out, (bx[2] + 1000.0, bx[3]),
             f"SPRINKLER HEADS: {len(heads)}", 300.0, layer,
             align=TextEntityAlignment.MIDDLE_LEFT)

    result.boq = {
        "total_heads": len(heads),
        "grid_spacing_mm": round(spacing, 1),
        "pattern": pattern,
        "coverage_radius_mm": radius,
        "heads_inserted_for_coverage": inserted,
        "rooms": room_boq,
    }
    return result


def _min_dist2(g: Tuple[float, float],
               heads: List[Tuple[float, float]]) -> float:
    if not heads:
        return float("inf")
    return min((g[0] - hx) ** 2 + (g[1] - hy) ** 2 for hx, hy in heads)


def _caption(zone_def: Any, i: int) -> str:
    if isinstance(zone_def, dict):
        return zone_def.get("zone_name") or f"room_{i+1}"
    return str(zone_def)

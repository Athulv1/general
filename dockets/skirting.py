"""Skirting docket — base strip along the walls of chosen rooms.

Path per room: the room-boundary portions on configured walls
(``boundary_on_walls``: door openings drop out where walls have gaps, and
segments on the configured door layers are subtracted), minus any exclusion
zones / layer geometry.  Measured in running metres; rooms list blank →
docket produces nothing (per the intake form's note).
"""
from __future__ import annotations

from typing import Any, Dict, List

from shapely.ops import unary_union

from dockets.base import (Ctx, DocketResult, Out, add_line_geom, add_text,
                          geom_length, iter_polys, union_polys)
import config_loader

_EXCLUDE_BUFFER_MM = 100.0  # tolerance around excluded geometry


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("skirting")
    height = float(docket_cfg.get("height_mm")
                   or config_loader.DEFAULTS["skirting"]["height_mm"])
    rooms = docket_cfg.get("rooms") or []
    if not rooms:
        result.warn("skirting.rooms is empty — nothing to skirt")
        result.boq = {"rooms": [], "total_length_m": 0.0,
                      "height_mm": height}
        return result

    # ── exclusion geometry (zones, layers or blocks) ───────────────────────
    exclude_geoms = []
    for i, exc in enumerate(docket_cfg.get("exclude") or []):
        if isinstance(exc, dict) or exc == "store":
            polys = ctx.resolver.resolve(exc)
            exclude_geoms += [p.buffer(_EXCLUDE_BUFFER_MM) for p in polys]
        else:
            name = str(exc)
            if ctx.resolver.has_layer(name):
                segs = ctx.resolver.segments_on_layers([name])
                exclude_geoms += [s.buffer(_EXCLUDE_BUFFER_MM, cap_style=2)
                                  for s in segs]
            else:
                polys = ctx.resolver.resolve_name(name)
                exclude_geoms += [p.buffer(_EXCLUDE_BUFFER_MM) for p in polys]
        if not exclude_geoms:
            result.warn(f"skirting.exclude[{i}] resolved to no geometry")
    exclusion = unary_union(exclude_geoms) if exclude_geoms else None

    layer = out.layer("skirting")
    rooms_boq: List[Dict[str, Any]] = []
    total_mm = 0.0
    from dockets.base import boundary_on_walls  # local import avoids cycle
    for i, room_def in enumerate(rooms):
        polys = ctx.resolver.resolve(room_def)
        if not polys:
            result.warn(f"skirting.rooms[{i}] resolved to no geometry — "
                        f"skipped")
            continue
        name = _caption(room_def, i)
        room_mm = 0.0
        tag_pos = None
        for poly in iter_polys(union_polys(polys)):
            path = boundary_on_walls(ctx, poly, result,
                                     extra_subtract=exclusion)
            if path.is_empty:
                continue
            room_mm += geom_length(path)
            add_line_geom(out, path, layer, {"const_width": 40.0})
            if tag_pos is None:
                first = path.geoms[0] if hasattr(path, "geoms") else path
                mid = first.interpolate(0.5, normalized=True)
                tag_pos = (mid.x, mid.y + 120.0)
        if room_mm <= 0:
            result.warn(f"skirting.rooms[{i}] ('{name}') has no wall length "
                        f"to skirt")
            continue
        if tag_pos is not None:
            add_text(out, tag_pos, f"SKIRTING H={height:.0f}", 100.0, layer)
        total_mm += room_mm
        rooms_boq.append({"room": name,
                          "length_m": round(room_mm / 1000.0, 3)})

    result.boq = {
        "rooms": rooms_boq,
        "total_length_m": round(total_mm / 1000.0, 3),
        "height_mm": height,
        "measure": "length",
    }
    return result


def _caption(zone_def: Any, i: int) -> str:
    if isinstance(zone_def, dict):
        return zone_def.get("zone_name") or f"room_{i+1}"
    return str(zone_def)

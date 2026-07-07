"""Skirting docket — base strip around each room's perimeter.

Path per room: the ENTIRE room-outline perimeter (the outline is the wall
line) minus door openings (segments on the configured door layers) and minus
any excluded zones / layer / block geometry.  Measured in running metres.
Rooms list blank → skirt every zone in the shared catalog.
"""
from __future__ import annotations

from typing import Any, Dict, List

from shapely.geometry import LineString
from shapely.ops import unary_union

from dockets.base import (Ctx, DocketResult, Out, add_line_geom, add_text,
                          geom_length, iter_polys, union_polys)
import config_loader

_EXCLUDE_BUFFER_MM = 100.0  # tolerance around excluded geometry
_DOOR_BUFFER_MM = 100.0     # tolerance around door/opening segments


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("skirting")
    height = float(docket_cfg.get("height_mm")
                   or config_loader.DEFAULTS["skirting"]["height_mm"])
    rooms = docket_cfg.get("rooms") or []
    if not rooms:
        # blank rooms list = skirt every ROOM-like zone from the shared
        # catalog (polyline outlines + balloon rooms).  Block zones are
        # fixtures/objects, not rooms, so they are not auto-skirted; list a
        # block zone explicitly if you really want a skirt around it.
        rooms = [z for z in (ctx.cfg.get("zones") or {}).values()
                 if isinstance(z, dict) and z.get("kind") in ("polyline",
                                                              "balloon")]
        if not rooms:
            result.warn("no zones defined — nothing to skirt")
            result.boq = {"rooms": [], "total_length_m": 0.0,
                          "height_mm": height}
            return result

    # zones named in the exclude list get NO skirting at all (and their
    # geometry is also subtracted from every other room's path below)
    excluded_names = {e.get("zone_name")
                      for e in docket_cfg.get("exclude") or []
                      if isinstance(e, dict) and e.get("zone_name")}
    if excluded_names:
        rooms = [r for r in rooms
                 if not (isinstance(r, dict)
                         and r.get("zone_name") in excluded_names)]

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

    # Door openings: buffers around segments on the configured door layers,
    # so skirting drops out wherever a door/opening crosses a room wall.
    door_segs = ctx.door_segments
    door_geom = (unary_union([s.buffer(_DOOR_BUFFER_MM, cap_style=2)
                              for s in door_segs]) if door_segs else None)
    if door_segs:
        result.warn(f"door openings: {len(door_segs)} segment(s) on the "
                    f"configured door layers will be skipped")

    layer = out.layer("skirting")
    rooms_boq: List[Dict[str, Any]] = []
    total_mm = 0.0
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
            # Skirting runs along the ENTIRE room perimeter (the room outline
            # IS the wall line) minus door openings and excluded areas.  No
            # wall-layer intersection — that dropped edges when the outline
            # didn't sit exactly on a separate wall layer.
            path = LineString(poly.exterior.coords)
            for ring in poly.interiors:
                path = path.union(LineString(ring.coords))
            if door_geom is not None:
                path = path.difference(door_geom)
            if exclusion is not None:
                path = path.difference(exclusion)
            if path.is_empty:
                continue
            room_mm += geom_length(path)
            add_line_geom(out, path, layer, {"const_width": 40.0})
            if tag_pos is None:
                first = path.geoms[0] if hasattr(path, "geoms") else path
                mid = first.interpolate(0.5, normalized=True)
                tag_pos = (mid.x, mid.y + 120.0)
        if room_mm <= 0:
            result.warn(f"skirting.rooms[{i}] ('{name}') has no perimeter "
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

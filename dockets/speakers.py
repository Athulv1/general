"""Speaker docket — coverage-driven or positional speaker placement.

Per placement:
  * ``count`` blank & no position → auto-fit: square grid of pitch R·√2
    (circle-covers-square condition) inside the zone, after an inward buffer
    of speaker_size/2.
  * explicit ``position`` (corners/center/front/back/left/right) → positional
    points (``front`` assumes the min-Y side of the zone is the shopfront;
    override by choosing a different position).
  * count N (no position) → N points evenly spaced along the zone's principal
    axis (minimum rotated rectangle centerline).

Amplifier boxes: W×H rectangle at the zone's positional point.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from shapely.geometry import Polygon

from dockets.base import (Ctx, DocketResult, Out, add_text, axis_points,
                          grid_points, iter_polys, positional_points,
                          union_polys)
import config_loader


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("speakers")
    dflt = config_loader.DEFAULTS["speakers"]
    size = float(docket_cfg.get("speaker_size_mm")
                 or dflt["speaker_size_mm"])
    radius = float(docket_cfg.get("coverage_radius_mm")
                   or dflt["coverage_radius_mm"])

    speaker_layer = out.layer("speaker")
    coverage_layer = out.layer("speaker_coverage")
    box_layer = out.layer("speaker_box")
    dashed = out.ensure_linetype("DASHED_COV", [800.0, 500.0, -300.0])

    placements_boq: List[Dict[str, Any]] = []
    total = 0
    for i, placement in enumerate(docket_cfg.get("placements") or []):
        zone_def = placement.get("zone")
        polys = ctx.resolver.resolve(zone_def)
        if not polys:
            result.warn(f"speakers.placements[{i}] resolved to no geometry — "
                        f"skipped")
            continue
        region = union_polys(polys)
        zone_name = _zone_caption(zone_def)
        points: List[Tuple[float, float]] = []
        for poly in iter_polys(region):
            points.extend(_points_for(poly, placement, size, radius))
        if not points:
            result.warn(f"speakers.placements[{i}] ('{zone_name}') produced "
                        f"no points")
            continue
        for x, y in points:
            total += 1
            out.msp.add_circle((x, y), size / 2,
                               dxfattribs={"layer": speaker_layer})
            out.msp.add_circle((x, y), radius,
                               dxfattribs={"layer": coverage_layer,
                                           "linetype": dashed,
                                           "ltscale": 10.0})
            add_text(out, (x, y), f"S{total}", size * 0.4, speaker_layer)
        placements_boq.append({"zone": zone_name, "count": len(points)})

    boxes_boq: List[Dict[str, Any]] = []
    for i, entry in enumerate(docket_cfg.get("boxes") or []):
        polys = ctx.resolver.resolve(entry.get("zone"))
        if not polys:
            result.warn(f"speakers.boxes[{i}] resolved to no geometry — "
                        f"skipped")
            continue
        region = max(iter_polys(union_polys(polys)), key=lambda p: p.area)
        w = float(entry.get("width_mm") or dflt["box_width_mm"])
        h = float(entry.get("height_mm") or dflt["box_height_mm"])
        pts = positional_points(region, entry.get("position", "center"),
                                int(entry.get("count") or 1),
                                inset=max(w, h) / 2)
        for x, y in pts:
            out.msp.add_lwpolyline(
                [(x - w / 2, y - h / 2), (x + w / 2, y - h / 2),
                 (x + w / 2, y + h / 2), (x - w / 2, y + h / 2)],
                close=True, dxfattribs={"layer": box_layer})
            add_text(out, (x, y), "AMP", h * 0.4, box_layer)
        boxes_boq.append({"zone": _zone_caption(entry.get("zone")),
                          "count": len(pts)})

    result.boq = {
        "total_speakers": total,
        "coverage_radius_mm": radius,
        "placements": placements_boq,
        "boxes": boxes_boq,
        "total_boxes": sum(b["count"] for b in boxes_boq),
    }
    return result


def _points_for(poly: Polygon, placement: Dict[str, Any], size: float,
                radius: float) -> List[Tuple[float, float]]:
    count = placement.get("count")
    position = placement.get("position")
    if position:
        return positional_points(poly, position, count, inset=size / 2)
    if count:
        return axis_points(poly, int(count), inset=size / 2)
    # auto-fit by coverage: pitch R·√2 covers the plane with radius-R circles
    pitch = radius * math.sqrt(2)
    pts = grid_points(poly, pitch, "square", inset=size / 2)
    if not pts:  # zone smaller than one pitch — one speaker at the centroid
        pts = positional_points(poly, "center", 1, inset=size / 2)
    return pts


def _zone_caption(zone_def: Any) -> str:
    if isinstance(zone_def, dict):
        return zone_def.get("zone_name") or zone_def.get("kind") or "zone"
    return str(zone_def)

"""Finishes docket — wall-finish marking + BOQ (area = length × height).

Targets per material:
* zone targets → the zone-boundary portions that sit on configured walls
  (door openings excluded two ways — see ``boundary_on_walls``);
* layer targets → total polyline/edge length of the matched geometry;
* block targets → perimeter of the matched blocks' bounding boxes.

Draw: the counted lines in the material's color (const-width band) plus a
``{material} H={height}`` tag; BOQ per material: running length (m),
area (m²), color code.
"""
from __future__ import annotations

from typing import Any, Dict, List

from shapely.geometry import LineString

from dockets.base import (Ctx, DocketResult, Out, add_line_geom, add_text,
                          boundary_on_walls, geom_length, iter_polys,
                          union_polys)
import config_loader


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("finishes")
    layer = out.layer("finish")
    dflt = config_loader.DEFAULTS["finishes"]
    text_h = 120.0  # tag text height (drawing symbol size, not a fact)

    items_boq: List[Dict[str, Any]] = []
    for i, item in enumerate(docket_cfg.get("items") or []):
        material = item.get("material") or f"finish_{i+1}"
        height_mm = float(item.get("height_mm") or dflt["height_mm"])
        color = int(item.get("color", dflt["color"]))
        total_mm = 0.0
        drawn = 0
        tag_pos = None

        for j, target in enumerate(item.get("targets") or []):
            geoms = _target_lines(ctx, target, result,
                                  f"finishes[{i}].targets[{j}]")
            for geom in geoms:
                length = geom_length(geom)
                if length <= 1.0:
                    continue
                total_mm += length
                drawn += add_line_geom(
                    out, geom, layer,
                    {"color": color, "const_width": 75.0})
                if tag_pos is None:
                    first = (geom.geoms[0] if hasattr(geom, "geoms")
                             else geom)
                    mid = first.interpolate(0.5, normalized=True)
                    tag_pos = (mid.x, mid.y + 150.0)

        if tag_pos is not None:
            add_text(out, tag_pos, f"{material} H={height_mm:.0f}",
                     text_h, layer)
        if total_mm <= 0:
            result.warn(f"finishes[{i}] ('{material}') matched no wall "
                        f"length")
        length_m = round(total_mm / 1000.0, 3)
        items_boq.append({
            "material": material,
            "length_m": length_m,
            "height_mm": height_mm,
            "area_m2": round(length_m * height_mm / 1000.0, 3),
            "color": color,
            "segments_drawn": drawn,
        })

    result.boq = {
        "items": items_boq,
        "total_area_m2": round(sum(x["area_m2"] for x in items_boq), 3),
    }
    return result


def _target_lines(ctx: Ctx, target: Any, result: DocketResult,
                  path: str) -> List:
    """Resolve one finish target to countable line geometry."""
    # zone-ish target (resolved dict or the "store" sentinel)
    if isinstance(target, dict) or target == "store":
        polys = ctx.resolver.resolve(target)
        if not polys:
            result.warn(f"{path}: zone resolved to no geometry")
            return []
        out = []
        for poly in iter_polys(union_polys(polys)):
            kept = boundary_on_walls(ctx, poly, result)
            if not kept.is_empty:
                out.append(kept)
        return out

    # bare string: a layer of lines, else block bboxes
    name = str(target)
    if ctx.resolver.has_layer(name):
        segs = ctx.resolver.segments_on_layers([name])
        if segs:
            return segs
    polys = ctx.resolver.resolve_name(name)
    return [LineString(p.exterior.coords) for p in polys]

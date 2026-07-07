"""Electrical docket — WP / CP / FP / CDP (and custom) point placement.

Point groups match fixtures by block name (INSERTs, recursive with full
transforms and mirrored-block-safe bboxes) and/or by layer (any entity's
representative point).  Placement per kind:

* WP  → the fixture point projected onto the nearest configured wall segment
        (warned and left in place when no wall layers are configured).
* CP / FP / custom → at the fixture point.
* CDP → at the fixture point with a 2:1 conduit-box symbol.

Symbols are parametric blocks created in the output doc (one per kind) so
placed points are countable INSERTs.  A legend table (label — note — count)
is written to the right of the store.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ezdxf.enums import TextEntityAlignment
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union

from dockets.base import Ctx, DocketResult, Out, add_text
import config_loader

_KIND_LAYER = {"WP": "electrical_wp", "CP": "electrical_cp",
               "FP": "electrical_fp", "CDP": "electrical_cdp"}


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("electrical")
    dflt = config_loader.DEFAULTS["electrical"]
    sym_r = float(dflt["symbol_radius_mm"])
    text_h = float(dflt["text_height_mm"])

    walls = ctx.wall_segments
    wall_union = unary_union(walls) if walls else None

    counts: Dict[str, int] = {}
    legend: List[Dict[str, Any]] = []
    for i, group in enumerate(docket_cfg.get("point_groups") or []):
        kind = str(group.get("place_as") or "FP")
        label = group.get("label") or kind
        match = group.get("match") or {}
        points = _fixture_points(ctx, match, result, i)
        if not points:
            result.warn(f"electrical.point_groups[{i}] ('{label}') matched "
                        f"no fixtures")
            legend.append({"label": label, "note": group.get("note") or "",
                           "count": 0})
            continue

        layer_key = _KIND_LAYER.get(kind.upper(), "electrical_other")
        layer = out.layer(layer_key)
        block_name = _ensure_symbol_block(out, kind, sym_r)

        n = 0
        for (x, y) in points:
            px, py = x, y
            if kind.upper() == "WP":
                if wall_union is not None:
                    q, _ = nearest_points(wall_union, Point(x, y))
                    px, py = q.x, q.y
                else:
                    result.warn(f"'{label}': no wall layers configured — "
                                f"wall point left at the fixture position")
            out.msp.add_blockref(block_name, (px, py),
                                 dxfattribs={"layer": layer})
            add_text(out, (px, py + sym_r * 1.8), label, text_h, layer)
            n += 1
        counts[label] = counts.get(label, 0) + n
        legend.append({"label": label, "note": group.get("note") or "",
                       "count": n})

    _draw_legend(ctx, out, legend, text_h, result)
    result.boq = {"counts": counts, "legend": legend,
                  "total_points": sum(counts.values())}
    return result


# ---------------------------------------------------------------------------
# fixture matching
# ---------------------------------------------------------------------------

def _fixture_points(ctx: Ctx, match: Dict[str, Any], result: DocketResult,
                    group_index: int) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    block_names = match.get("block_names") or []
    layer_names = match.get("layers") or []

    if block_names:
        polys = ctx.resolver.resolve(
            {"kind": "block", "block_names": block_names,
             "zone_name": f"@elec_blocks_{group_index}"})
        points += [(p.centroid.x, p.centroid.y) for p in polys]

    if layer_names:
        # only layers that actually exist — the mixed form list also carries
        # block names, which must not produce absent-layer warnings here
        existing = [l for l in layer_names if ctx.resolver.has_layer(l)]
        for seg in ctx.resolver.segments_on_layers(existing):
            mid = seg.interpolate(0.5, normalized=True)
            points.append((mid.x, mid.y))

    return _dedupe(points, tol=50.0)


def _dedupe(points: List[Tuple[float, float]], tol: float
            ) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    t2 = tol * tol
    for x, y in points:
        if not any((x - ox) ** 2 + (y - oy) ** 2 < t2 for ox, oy in out):
            out.append((x, y))
    return out


# ---------------------------------------------------------------------------
# symbols + legend
# ---------------------------------------------------------------------------

def _ensure_symbol_block(out: Out, kind: str, radius: float) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", kind.upper()) or "PT"
    name = f"ELEC_{safe}"
    if name in out.doc.blocks:
        return name
    block = out.doc.blocks.new(name)
    if kind.upper() == "CDP":
        w, h = radius * 4, radius * 2
        block.add_lwpolyline([(-w / 2, -h / 2), (w / 2, -h / 2),
                              (w / 2, h / 2), (-w / 2, h / 2)], close=True)
    else:
        block.add_circle((0, 0), radius)
        block.add_line((-radius, 0), (radius, 0))
        block.add_line((0, -radius), (0, radius))
    return name


def _draw_legend(ctx: Ctx, out: Out, legend: List[Dict[str, Any]],
                 text_h: float, result: DocketResult) -> None:
    store = ctx.store_region
    if store is None or not legend:
        return
    layer = out.layer("electrical_legend")
    row_h = float(config_loader.DEFAULTS["electrical"]["legend_row_height_mm"])
    x = store.bounds[2] + 1000.0
    y = store.bounds[3]
    add_text(out, (x, y), "ELECTRICAL LEGEND", text_h * 1.4, layer,
             align=TextEntityAlignment.MIDDLE_LEFT)
    for i, row in enumerate(legend, start=1):
        note = f" — {row['note']}" if row["note"] else ""
        add_text(out, (x, y - i * row_h),
                 f"{row['label']}{note}  (x{row['count']})",
                 text_h, layer, align=TextEntityAlignment.MIDDLE_LEFT)

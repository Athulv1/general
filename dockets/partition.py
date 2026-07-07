"""Partition / civil marking plan docket.

Whitelist copy: ONLY entities on the four configured keep groups (outer
boundary, walls & structure, internal dividers, room labels) survive — the
store-outline layers are always appended to the boundary group.  An INSERT
survives only when it sits on a walls-&-structure layer.  Entities inside
imported block definitions are stripped by the same whitelist.

Zone styling: per-zone outline color (ACI) + lineweight, optional area label
``{zone} = {area:.2f} m²`` at the zone centroid on the configured label layer.
"""
from __future__ import annotations

from typing import Any, Dict, List

from dockets.base import (Ctx, DocketResult, Out, add_text, iter_polys,
                          union_polys)
import config_loader


def generate(doc, ctx: Ctx, docket_cfg: Dict[str, Any], out: Out
             ) -> DocketResult:
    result = DocketResult("partition_plan")
    keep = docket_cfg.get("keep_layers") or {}
    boundary_layers = list(keep.get("outer_boundary") or [])
    boundary_layers += (ctx.cfg.get("store_outline") or {}).get("layers") or []
    walls_layers = list(keep.get("walls") or [])
    whitelist_names = (boundary_layers + walls_layers
                       + list(keep.get("dividers") or [])
                       + list(keep.get("labels") or []))
    whitelist = {l.strip().lower()
                 for l in ctx.resolver.match_layers(whitelist_names)}
    walls_set = {l.strip().lower()
                 for l in ctx.resolver.match_layers(walls_layers)}
    if not whitelist:
        return result.fail("partition_plan.keep_layers matched no layers in "
                           "this DXF")

    def keep_entity(e) -> bool:
        try:
            layer = (e.dxf.layer or "").strip().lower()
        except Exception:
            return False
        if layer not in whitelist:
            return False
        if e.dxftype() == "INSERT" and layer not in walls_set:
            return False
        return True

    entities = [e for e in doc.modelspace() if keep_entity(e)]
    kept = len(entities)
    if entities:
        try:
            from ezdxf.addons import Importer
            importer = Importer(doc, out.doc)
            importer.import_entities(entities, out.msp)
            importer.finalize()
        except Exception as exc:
            result.warn(f"entity import failed: {exc}")
    else:
        result.warn("no entities matched the keep layers")

    # strip non-whitelisted entities inside imported block definitions
    stripped_in_blocks = 0
    for block in out.doc.blocks:
        if block.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        for e in list(block):
            try:
                layer = (e.dxf.layer or "").strip().lower()
            except Exception:
                continue
            drop = layer not in whitelist
            if e.dxftype() == "INSERT" and layer not in walls_set:
                drop = True
            if drop:
                try:
                    block.delete_entity(e)
                    stripped_in_blocks += 1
                except Exception:
                    pass

    # ── zone styling + area labels ─────────────────────────────────────────
    style_layer = out.layer("partition_style")
    label_layer_name = docket_cfg.get("area_label_layer") or \
        config_loader.DEFAULTS["partition_plan"]["area_label_layer"]
    if label_layer_name not in out.doc.layers:
        out.doc.layers.add(label_layer_name, color=7)
    text_h = config_loader.DEFAULTS["partition_plan"]["label_text_height_mm"]

    styled: List[Dict[str, Any]] = []
    for i, style in enumerate(docket_cfg.get("zone_styles") or []):
        polys = ctx.resolver.resolve(style.get("zone"))
        if not polys:
            result.warn(f"partition_plan.zone_styles[{i}] resolved to no "
                        f"geometry — skipped")
            continue
        zone_name = _caption(style.get("zone"), i)
        color = int(style.get("color", 7))
        lineweight = int(style.get("lineweight", 50))
        total_area = 0.0
        for poly in iter_polys(union_polys(polys)):
            out.msp.add_lwpolyline(
                list(poly.exterior.coords), close=True,
                dxfattribs={"layer": style_layer, "color": color,
                            "lineweight": lineweight})
            total_area += poly.area
            if style.get("area_label"):
                c = poly.centroid
                out.msp.add_mtext(
                    f"{zone_name} = {poly.area / 1e6:.2f} m²",
                    dxfattribs={"layer": label_layer_name,
                                "char_height": text_h,
                                "insert": (c.x, c.y),
                                "attachment_point": 5})  # middle-center
        styled.append({"zone": zone_name, "area_m2": round(total_area / 1e6, 3),
                       "color": color, "lineweight": lineweight})

    result.boq = {
        "entities_kept": kept,
        "entities_stripped_in_blocks": stripped_in_blocks,
        "styled_zones": styled,
    }
    return result


def _caption(zone_def: Any, i: int) -> str:
    if isinstance(zone_def, dict):
        return zone_def.get("zone_name") or f"zone_{i+1}"
    return str(zone_def)

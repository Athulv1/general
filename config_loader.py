"""Config layer for the DXF layout pipeline (schema 3.1).

Responsibilities
----------------
* ``load_config(path)`` — read JSON, normalize legacy/form spellings into the
  canonical schema, resolve ``$ref`` zone references, validate, and return a
  fully materialized config dict.
* ``$ref`` semantics: JSON-pointer style (``{"$ref": "#/zones/<name>"}``).
  A ref may appear anywhere a zone object is expected.  Ref-to-ref chains are
  resolved iteratively with a visited set; cycles are rejected with a clear
  error naming the chain.
* Validation errors carry JSON paths
  (``flooring.zones[2].zone: $ref '#/zones/foo' not found``).  Loading fails
  fast, before any DXF work.
* ``DEFAULTS`` is the single home for engineering defaults (tile size,
  radii, heights ...).  These are engineering constants, not drawing facts —
  they are the only constants allowed anywhere in the pipeline.

The loader accepts BOTH spellings of schema 3.1:

* the canonical contract (``store_outline`` top level, ``zones`` catalog with
  ``kind``/``layers``/``keywords`` plurals, ``speakers``/``sprinklers``/
  ``partition_plan``/``electrical.point_groups``/``finishes`` list /
  ``skirting.rooms``), and
* the intake-form output (``flooring.store_outline``, zone ``identifier`` +
  singular ``layer``/``keyword``, ``speaker``/``sprinkler``/``partition``/
  ``electrical.groups``/``finishes.materials``/``skirting.zones``).

Everything is normalized to the canonical shape so the engines see one schema.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = "3.1"

# ---------------------------------------------------------------------------
# Engineering defaults — the ONLY constants allowed in the pipeline.
# Drawing facts (layer names, block names, keywords, coordinates) must come
# from the config; anything listed here is a pure engineering default that a
# config value overrides.
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    "flooring": {
        "tile_length_mm": 600.0,
        "tile_width_mm": 600.0,
        "tile_rotation_deg": 45.0,
        "wastage_pct": 10.0,
        "min_door_width_mm": 600.0,
    },
    "speakers": {
        "speaker_size_mm": 300.0,
        "coverage_radius_mm": 4000.0,
        "box_width_mm": 400.0,
        "box_height_mm": 200.0,
    },
    "sprinklers": {
        "coverage_radius_mm": 2300.0,
        "max_spacing_mm": 3700.0,
        "pattern": "square",
        "rooms_min_one": True,
        "strict_coverage": True,
        "min_room_area_mm2": 1_000_000.0,  # rooms smaller than 1 m² are ignored
    },
    "skirting": {
        "height_mm": 100.0,
        "min_door_width_mm": 600.0,
    },
    "finishes": {
        "height_mm": 2400.0,
        "color": 7,
    },
    "partition_plan": {
        "area_label_layer": "SYSTEM_LABELS",
        "label_text_height_mm": 150.0,
    },
    "electrical": {
        "symbol_radius_mm": 100.0,
        "text_height_mm": 80.0,
        "legend_row_height_mm": 300.0,
    },
    "balloon": {
        "min_area_mm2": 1_000_000.0,
        "step_mm": 100.0,
    },
    "walls": {
        "wall_buffer_mm": 150.0,  # tolerance when matching zone edges to walls
    },
    # Default output layer names + ACI colors; overridable via config
    # ``output.layers`` ({key: {"name": ..., "color": ...}}).
    "output_layers": {
        "tile_a":            {"name": "FLOOR-TILE-A",        "color": 9},
        "tile_b":            {"name": "FLOOR-TILE-B",        "color": 254},
        "tile_cut":          {"name": "FLOOR-TILE-CUT",      "color": 1},
        "floor_hatch":       {"name": "FLOOR-HATCH",         "color": 8},
        "floor_outline":     {"name": "FLOOR-OUTLINE",       "color": 7},
        "speaker":           {"name": "AUDIO-SPEAKER",       "color": 3},
        "speaker_coverage":  {"name": "AUDIO-COVERAGE",      "color": 8},
        "speaker_box":       {"name": "AUDIO-AMP-BOX",       "color": 5},
        "sprinkler":         {"name": "FIRE-SPRINKLER",      "color": 1},
        "sprinkler_coverage":{"name": "FIRE-COVERAGE",       "color": 8},
        "partition_style":   {"name": "MARKING-ZONE",        "color": 7},
        "area_label":        {"name": "SYSTEM_LABELS",       "color": 7},
        "electrical_wp":     {"name": "ELEC-WALL-POINT",     "color": 1},
        "electrical_cp":     {"name": "ELEC-CEIL-POINT",     "color": 6},
        "electrical_fp":     {"name": "ELEC-FLOOR-POINT",    "color": 4},
        "electrical_cdp":    {"name": "ELEC-DATA-POINT",     "color": 6},
        "electrical_other":  {"name": "ELEC-POINT",          "color": 2},
        "electrical_legend": {"name": "ELEC-LEGEND",         "color": 7},
        "finish":            {"name": "FINISH-MARK",         "color": 7},
        "skirting":          {"name": "SKIRTING",            "color": 30},
    },
}

DOCKET_KEYS = (
    "flooring", "speakers", "sprinklers", "partition_plan",
    "electrical", "finishes", "skirting",
)

_ZONE_KINDS = ("polyline", "block", "balloon")


class ConfigError(ValueError):
    """Raised when a config fails validation.  ``errors`` is the full list."""

    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("invalid config:\n  " + "\n  ".join(self.errors))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> List:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def _is_ref(node: Any) -> bool:
    return isinstance(node, dict) and "$ref" in node


# ---------------------------------------------------------------------------
# normalization: accept both the canonical contract and the form output
# ---------------------------------------------------------------------------

def _norm_zone_def(zone: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one zone definition to the canonical shape.

    Canonical:
      polyline: {kind, layers[], layer_pattern?}
      block:    {kind, block_names[]}
      balloon:  {kind, keywords[], text_layers[]|None, barrier_layers[]}
    """
    if _is_ref(zone):
        return dict(zone)
    z = dict(zone)
    kind = z.pop("kind", None) or z.pop("identifier", None)
    out: Dict[str, Any] = {"kind": kind}
    if kind == "polyline":
        out["layers"] = _as_list(z.get("layers") or z.get("layer"))
        if z.get("layer_pattern"):
            out["layer_pattern"] = z["layer_pattern"]
    elif kind == "block":
        out["block_names"] = _as_list(z.get("block_names") or z.get("blockname")
                                      or z.get("block_name"))
    elif kind == "balloon":
        out["keywords"] = _as_list(z.get("keywords") or z.get("keyword"))
        tl = _as_list(z.get("text_layers") or z.get("text_layer"))
        out["text_layers"] = tl or None
        out["barrier_layers"] = _as_list(z.get("barrier_layers")
                                         or z.get("boundaries"))
    else:
        out.update(z)  # unknown kind: keep fields; validation will flag it
    return out


def _norm_zone_or_ref(node: Any, zone_names=frozenset()) -> Any:
    if _is_ref(node) or node is None:
        return node
    if isinstance(node, str):
        if node == "store":
            return "store"
        # a bare zone name becomes a ref; any other string stays a raw
        # layer/block target (finishes, skirting excludes)
        if node in zone_names:
            return {"$ref": f"#/zones/{node}"}
        return node
    if isinstance(node, dict):
        return _norm_zone_def(node)
    return node


def _norm_place_kind(place: str) -> str:
    mapping = {"wall": "WP", "ceiling": "CP", "floor": "FP", "data": "CDP"}
    return mapping.get(place, place)


def normalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Map every accepted spelling into the canonical schema 3.1 shape."""
    cfg = copy.deepcopy(cfg)
    out: Dict[str, Any] = {"schema": str(cfg.get("schema")
                                         or cfg.get("schema_version")
                                         or SCHEMA_VERSION)}
    out["io"] = dict(cfg.get("io") or {})
    if "output_dir" not in out["io"] and out["io"].get("output_dxf"):
        # legacy io: derive output_dir from the old single-file field
        import os
        out["io"]["output_dir"] = os.path.dirname(out["io"]["output_dxf"]) or "."

    flooring_in = dict(cfg.get("flooring") or {})
    zone_names = frozenset((cfg.get("zones") or {}).keys())

    # ── store outline ──────────────────────────────────────────────────────
    so = cfg.get("store_outline") or flooring_in.get("store_outline") or {}
    out["store_outline"] = {
        "layers": _as_list(so.get("layers") or so.get("layer")),
        "layer_pattern": so.get("layer_pattern"),
    }

    # ── walls ──────────────────────────────────────────────────────────────
    walls = dict(cfg.get("walls") or {})
    wf = flooring_in.get("wall_frame") or {}
    if not walls.get("layers"):
        walls["layers"] = _as_list(wf.get("mirror_layers"))
    walls["layers"] = _as_list(walls.get("layers"))
    walls["door_layers"] = _as_list(walls.get("door_layers"))
    out["walls"] = walls

    # ── zones catalog ──────────────────────────────────────────────────────
    out["zones"] = {name: _norm_zone_def(z)
                    for name, z in (cfg.get("zones") or {}).items()}

    # ── flooring types ─────────────────────────────────────────────────────
    out["flooring_types"] = list(cfg.get("flooring_types") or [])

    # ── flooring docket ────────────────────────────────────────────────────
    if flooring_in or cfg.get("default_flooring_type"):
        fl: Dict[str, Any] = {}
        fl["enabled"] = flooring_in.get("enabled", True)
        fl["zones"] = [
            {"zone": _norm_zone_or_ref(e.get("zone"), zone_names),
             "flooring_type": e.get("flooring_type")}
            for e in (flooring_in.get("zones") or [])
        ]
        fl["default_flooring_type"] = (flooring_in.get("default_flooring_type")
                                       or cfg.get("default_flooring_type"))
        fl["params"] = dict(flooring_in.get("params") or cfg.get("params") or {})
        fl["exclusions"] = [
            {"name": e.get("name") or f"exclusion_{i+1}",
             "zone": _norm_zone_or_ref(e.get("zone"), zone_names),
             "shift_origin_past": bool(e.get("shift_origin_past"))}
            for i, e in enumerate(flooring_in.get("exclusions") or [])
        ]
        fac = flooring_in.get("facade") or {}
        fl["facade"] = {"glass_layers": _as_list(fac.get("glass_layers")),
                        "door_layers": _as_list(fac.get("door_layers"))}
        sr = flooring_in.get("start_reference") or {}
        fl["start_reference"] = {"layers": _as_list(sr.get("layers"))}
        fl["wall_frame"] = {
            "mirror_layers": _as_list(wf.get("mirror_layers")),
            "hatch_layers": _as_list(wf.get("hatch_layers")),
            "exclude_insert_layers": _as_list(wf.get("exclude_insert_layers")),
            "exclude_block_names": _as_list(wf.get("exclude_block_names")),
            "hatch_scale": wf.get("hatch_scale"),
        }
        out["flooring"] = fl

    # ── speakers ───────────────────────────────────────────────────────────
    sp_in = cfg.get("speakers") or cfg.get("speaker")
    if sp_in:
        sp = dict(sp_in)
        model = sp.pop("model", {}) or {}
        spk: Dict[str, Any] = {
            "enabled": sp.get("enabled", True),
            "speaker_size_mm": sp.get("speaker_size_mm",
                                      model.get("speaker_size_mm")),
            "coverage_radius_mm": sp.get("coverage_radius_mm",
                                         model.get("coverage_radius_mm")),
            "box_width_mm": sp.get("box_width_mm", model.get("box_width_mm")),
            "box_height_mm": sp.get("box_height_mm", model.get("box_height_mm")),
        }
        placements = []
        mode = sp.get("mode") or "placement"
        if mode == "coverage":
            cov = sp.get("coverage") or {}
            targets = cov.get("targets") or [cov.get("target") or "store"]
            for t in targets:
                placements.append({"zone": _norm_zone_or_ref(t, zone_names), "count": None,
                                   "position": None})
        for p in sp.get("placements") or []:
            placements.append({
                "zone": _norm_zone_or_ref(p.get("zone") or p.get("target"), zone_names),
                "count": p.get("count"),
                "position": p.get("position"),
            })
        spk["placements"] = placements
        boxes = []
        for b in sp.get("boxes") or []:
            boxes.append({
                "zone": _norm_zone_or_ref(b.get("zone") or b.get("target"), zone_names),
                "width_mm": b.get("width_mm", spk.get("box_width_mm")),
                "height_mm": b.get("height_mm", spk.get("box_height_mm")),
                "count": b.get("count", 1),
                "position": b.get("position", "center"),
            })
        spk["boxes"] = boxes
        out["speakers"] = spk

    # ── sprinklers ─────────────────────────────────────────────────────────
    sk_in = cfg.get("sprinklers") or cfg.get("sprinkler")
    if sk_in:
        sk = dict(sk_in)
        model = sk.pop("model", {}) or {}
        out["sprinklers"] = {
            "enabled": sk.get("enabled", True),
            "coverage_radius_mm": sk.get("coverage_radius_mm",
                                         model.get("coverage_radius_mm")),
            "max_spacing_mm": sk.get("max_spacing_mm",
                                     model.get("max_spacing_mm")),
            "pattern": sk.get("pattern", model.get("pattern")),
            "rooms_min_one": sk.get("rooms_min_one",
                                    sk.get("min_one_per_room", True)),
            "rooms": [_norm_zone_or_ref(r, zone_names) for r in _as_list(sk.get("rooms"))],
            "exclusions": [_norm_zone_or_ref(r, zone_names)
                           for r in _as_list(sk.get("exclude")
                                             or sk.get("exclusions"))],
            "strict_coverage": sk.get("strict_coverage", True),
        }

    # ── partition / marking plan ───────────────────────────────────────────
    pt_in = cfg.get("partition_plan") or cfg.get("partition")
    if pt_in:
        pt = dict(pt_in)
        keep = pt.get("keep_layers") or pt.get("keep") or {}
        styles = []
        for s in pt.get("zone_styles") or pt.get("style") or []:
            styles.append({
                "zone": _norm_zone_or_ref(s.get("zone"), zone_names),
                "color": s.get("color", s.get("color_aci", 7)),
                "lineweight": s.get("lineweight", 50),
                "area_label": bool(s.get("area_label", s.get("label_area"))),
            })
        out["partition_plan"] = {
            "enabled": pt.get("enabled", True),
            "keep_layers": {
                "outer_boundary": _as_list(keep.get("outer_boundary")
                                           or keep.get("boundary")),
                "walls": _as_list(keep.get("walls") or keep.get("structural")),
                "dividers": _as_list(keep.get("dividers")
                                     or keep.get("partitions")),
                "labels": _as_list(keep.get("labels") or keep.get("text")),
            },
            "zone_styles": styles,
            "area_label_layer": (pt.get("area_label_layer")
                                 or pt.get("label_layer")
                                 or DEFAULTS["partition_plan"]["area_label_layer"]),
        }

    # ── electrical ─────────────────────────────────────────────────────────
    el_in = cfg.get("electrical")
    if el_in:
        groups = []
        for g in el_in.get("point_groups") or el_in.get("groups") or []:
            match = g.get("match")
            if isinstance(match, list):
                # the form emits a mixed list of block/layer names — match either
                match = {"block_names": list(match), "layers": list(match)}
            match = match or {}
            groups.append({
                "match": {"block_names": _as_list(match.get("block_names")),
                          "layers": _as_list(match.get("layers"))},
                "place_as": _norm_place_kind(g.get("place_as")
                                             or g.get("place") or "FP"),
                "label": g.get("label") or "",
                "note": g.get("note") or "",
            })
        out["electrical"] = {"enabled": el_in.get("enabled", True),
                             "point_groups": groups}

    # ── finishes ───────────────────────────────────────────────────────────
    fn_in = cfg.get("finishes")
    if fn_in:
        items = fn_in if isinstance(fn_in, list) else (fn_in.get("materials") or [])
        enabled = True if isinstance(fn_in, list) else fn_in.get("enabled", True)
        finishes = []
        for m in items:
            finishes.append({
                "material": m.get("material") or m.get("name") or "",
                "targets": [_norm_zone_or_ref(t, zone_names)
                            for t in _as_list(m.get("targets")
                                              or m.get("applies_to"))],
                "height_mm": m.get("height_mm",
                                   DEFAULTS["finishes"]["height_mm"]),
                "color": m.get("color", m.get("color_aci",
                                              DEFAULTS["finishes"]["color"])),
            })
        out["finishes"] = {"enabled": enabled, "items": finishes}

    # ── skirting ───────────────────────────────────────────────────────────
    skr_in = cfg.get("skirting")
    if skr_in:
        out["skirting"] = {
            "enabled": skr_in.get("enabled", True),
            "rooms": [_norm_zone_or_ref(r, zone_names)
                      for r in _as_list(skr_in.get("rooms")
                                        or skr_in.get("zones"))],
            "exclude": [_norm_zone_or_ref(r, zone_names)
                        for r in _as_list(skr_in.get("exclude"))],
            "height_mm": skr_in.get("height_mm",
                                    DEFAULTS["skirting"]["height_mm"]),
        }

    # ── output ─────────────────────────────────────────────────────────────
    o = cfg.get("output") or {}
    out["output"] = {"strip_layers": _as_list(o.get("strip_layers")),
                     "layers": dict(o.get("layers") or {})}
    return out


# ---------------------------------------------------------------------------
# $ref resolution
# ---------------------------------------------------------------------------

def _resolve_ref(ref: str, catalog: Dict[str, Any], path: str,
                 errors: List[str]) -> Optional[Dict[str, Any]]:
    """Resolve one ``#/zones/<name>`` ref through the catalog.

    Ref-to-ref chains are followed iteratively with a visited set; cycles and
    missing targets are reported as errors (with the JSON path) and yield None.
    """
    visited: List[str] = []
    current = ref
    while True:
        if not (isinstance(current, str) and current.startswith("#/zones/")):
            errors.append(f"{path}: unsupported $ref '{current}' "
                          f"(expected '#/zones/<name>')")
            return None
        name = current[len("#/zones/"):]
        if name in visited:
            chain = " -> ".join(visited + [name])
            errors.append(f"{path}: $ref cycle detected ({chain})")
            return None
        visited.append(name)
        if name not in catalog:
            errors.append(f"{path}: $ref '#/zones/{name}' not found in zones "
                          f"catalog")
            return None
        target = catalog[name]
        if _is_ref(target):
            current = target["$ref"]
            continue
        resolved = copy.deepcopy(target)
        resolved["zone_name"] = visited[-1]
        return resolved


def resolve_refs(cfg: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
    """Walk the whole config and materialize every ``$ref`` in place.

    Sibling keys next to ``$ref`` override the referenced zone's fields.
    """
    catalog = cfg.get("zones") or {}

    def walk(node: Any, path: str) -> Any:
        if _is_ref(node):
            resolved = _resolve_ref(node["$ref"], catalog, path, errors)
            if resolved is None:
                return None
            overrides = {k: v for k, v in node.items() if k != "$ref"}
            resolved.update(overrides)
            return resolved
        if isinstance(node, dict):
            return {k: walk(v, f"{path}.{k}" if path else k)
                    for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
        return node

    out = dict(cfg)
    for key in list(out.keys()):
        if key == "zones":
            continue  # the catalog itself is not resolved into itself
        out[key] = walk(out[key], key)
    # also give catalog zones their own names (engines use them for captions)
    for name, z in catalog.items():
        if isinstance(z, dict) and not _is_ref(z):
            z.setdefault("zone_name", name)
    return out


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _validate_zone_def(z: Any, path: str, errors: List[str]) -> None:
    if z is None:
        errors.append(f"{path}: zone is missing or unresolved")
        return
    if z == "store":
        return
    if not isinstance(z, dict):
        errors.append(f"{path}: zone must be an object, got {type(z).__name__}")
        return
    kind = z.get("kind")
    if kind not in _ZONE_KINDS:
        errors.append(f"{path}.kind: expected one of {_ZONE_KINDS}, got {kind!r}")
        return
    if kind == "polyline" and not (z.get("layers") or z.get("layer_pattern")):
        errors.append(f"{path}: polyline zone needs 'layers' or 'layer_pattern'")
    if kind == "block" and not z.get("block_names"):
        errors.append(f"{path}: block zone needs 'block_names'")
    if kind == "balloon" and not z.get("keywords"):
        errors.append(f"{path}: balloon zone needs 'keywords'")


def validate(cfg: Dict[str, Any]) -> List[str]:
    """Return the list of validation errors (empty = valid)."""
    errors: List[str] = []

    so = cfg.get("store_outline") or {}
    if not (so.get("layers") or so.get("layer_pattern")):
        errors.append("store_outline: 'layers' (or 'layer_pattern') is required")

    for name, z in (cfg.get("zones") or {}).items():
        _validate_zone_def(z, f"zones.{name}", errors)

    ft_names = set()
    for i, ft in enumerate(cfg.get("flooring_types") or []):
        p = f"flooring_types[{i}]"
        if not ft.get("name"):
            errors.append(f"{p}.name: required")
        else:
            ft_names.add(ft["name"])
        if ft.get("kind") not in ("tile", "hatch", "skip"):
            errors.append(f"{p}.kind: expected tile|hatch|skip, got "
                          f"{ft.get('kind')!r}")

    fl = cfg.get("flooring")
    if fl and fl.get("enabled", True):
        for i, e in enumerate(fl.get("zones") or []):
            p = f"flooring.zones[{i}]"
            _validate_zone_def(e.get("zone"), f"{p}.zone", errors)
            ftype = e.get("flooring_type")
            if ftype and ft_names and ftype not in ft_names:
                errors.append(f"{p}.flooring_type: '{ftype}' is not a defined "
                              f"flooring type")
        dft = fl.get("default_flooring_type")
        if dft and ft_names and dft not in ft_names:
            errors.append(f"flooring.default_flooring_type: '{dft}' is not a "
                          f"defined flooring type")
        for i, e in enumerate(fl.get("exclusions") or []):
            _validate_zone_def(e.get("zone"),
                               f"flooring.exclusions[{i}].zone", errors)

    sp = cfg.get("speakers")
    if sp and sp.get("enabled", True):
        for i, e in enumerate(sp.get("placements") or []):
            _validate_zone_def(e.get("zone"),
                               f"speakers.placements[{i}].zone", errors)
        for i, e in enumerate(sp.get("boxes") or []):
            _validate_zone_def(e.get("zone"),
                               f"speakers.boxes[{i}].zone", errors)

    sk = cfg.get("sprinklers")
    if sk and sk.get("enabled", True):
        for i, e in enumerate(sk.get("rooms") or []):
            _validate_zone_def(e, f"sprinklers.rooms[{i}]", errors)
        for i, e in enumerate(sk.get("exclusions") or []):
            _validate_zone_def(e, f"sprinklers.exclusions[{i}]", errors)
        if sk.get("pattern") not in (None, "square", "staggered"):
            errors.append(f"sprinklers.pattern: expected square|staggered, got "
                          f"{sk.get('pattern')!r}")

    pt = cfg.get("partition_plan")
    if pt and pt.get("enabled", True):
        kl = pt.get("keep_layers") or {}
        if not any(_as_list(kl.get(k)) for k in
                   ("outer_boundary", "walls", "dividers", "labels")):
            errors.append("partition_plan.keep_layers: at least one keep group "
                          "must name a layer")
        for i, s in enumerate(pt.get("zone_styles") or []):
            _validate_zone_def(s.get("zone"),
                               f"partition_plan.zone_styles[{i}].zone", errors)

    el = cfg.get("electrical")
    if el and el.get("enabled", True):
        for i, g in enumerate(el.get("point_groups") or []):
            p = f"electrical.point_groups[{i}]"
            m = g.get("match") or {}
            if not (m.get("block_names") or m.get("layers")):
                errors.append(f"{p}.match: needs block_names or layers")
            if not g.get("label"):
                errors.append(f"{p}.label: required")

    fn = cfg.get("finishes")
    if fn and fn.get("enabled", True):
        for i, m in enumerate(fn.get("items") or []):
            p = f"finishes[{i}]"
            if not m.get("material"):
                errors.append(f"{p}.material: required")
            if not m.get("targets"):
                errors.append(f"{p}.targets: at least one target required")
            for j, t in enumerate(m.get("targets") or []):
                if isinstance(t, dict):
                    _validate_zone_def(t, f"{p}.targets[{j}]", errors)

    skr = cfg.get("skirting")
    if skr and skr.get("enabled", True):
        for i, r in enumerate(skr.get("rooms") or []):
            if isinstance(r, dict):
                _validate_zone_def(r, f"skirting.rooms[{i}]", errors)

    return errors


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    """Read, normalize, resolve and validate a config file.

    Raises ``ConfigError`` with the full list of path-annotated messages when
    anything is wrong.  Never touches the DXF.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return load_config_dict(raw)


def load_config_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Same as :func:`load_config` but from an in-memory dict."""
    cfg = normalize(raw)
    errors: List[str] = []
    cfg = resolve_refs(cfg, errors)
    errors += validate(cfg)
    if errors:
        raise ConfigError(errors)
    return cfg


def enabled_dockets(cfg: Dict[str, Any]) -> List[str]:
    """Dockets to run: section present and ``enabled`` is not false."""
    return [k for k in DOCKET_KEYS
            if cfg.get(k) and cfg[k].get("enabled", True)]


def output_layer(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Output layer spec for ``key`` — config override or default."""
    override = (cfg.get("output") or {}).get("layers", {}).get(key) or {}
    base = dict(DEFAULTS["output_layers"].get(
        key, {"name": key.upper(), "color": 7}))
    base.update({k: v for k, v in override.items() if v is not None})
    return base

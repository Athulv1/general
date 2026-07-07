"""Shared zone resolution service.

``ZoneResolver`` turns config zone definitions into shapely polygons, one
implementation for every docket.  Three identifier kinds (mirroring the form):

* ``polyline`` — closed polygons on named layers (recursing into INSERTs with
  full matrix transforms, via ``extract_layer_polygons_closed``).
* ``block`` — world-space bounding boxes of matching INSERTs.  Boxes are
  computed with ``ezdxf.bbox.extents([insert])`` so mirrored inserts
  (xscale = −1) produce correct geometry; nested inserts are handled by
  composing parent transforms.
* ``balloon`` — text-seeded balloon inflation (``balloon_zone_extract``).
  Barrier layers come ONLY from config: the zone's own ``barrier_layers``
  plus the store-outline layers and wall layers (both themselves config
  values) which the resolver auto-appends.

Results are cached by zone name (or a stable hash of ad-hoc definitions) so
ten dockets referencing ``#/zones/x`` cost one extraction.

Layer matching is case-insensitive against ``doc.layers`` with an optional
fnmatch pattern.  A named layer that is absent produces a structured warning,
never a crash.  Every INSERT recursion carries a visited set of block names
to terminate on cyclic block definitions.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import ezdxf
from ezdxf import bbox as _bbox
from ezdxf.math import Matrix44
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize, unary_union

from extract_layer_polygons_closed import extract_layer_polygons_closed

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# small geometry helpers
# ---------------------------------------------------------------------------

def to_polygon(points: Sequence[Tuple[float, float]]) -> Optional[Polygon]:
    """Points → valid Polygon (buffer(0)-repaired), or None."""
    try:
        if len(points) < 3:
            return None
        poly = Polygon([(float(p[0]), float(p[1])) for p in points])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return None
        if isinstance(poly, MultiPolygon):
            poly = max(poly.geoms, key=lambda g: g.area)
        return poly
    except Exception:
        return None


def iter_polys(geom) -> List[Polygon]:
    """Flatten any shapely geometry into its list of Polygons."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        out: List[Polygon] = []
        for g in geom.geoms:
            out.extend(iter_polys(g))
        return out
    return []


CHAIN_SNAP_MM = 5.0  # endpoint-snap tolerance when chaining open polylines


def chain_rings(point_lists: List[List[Tuple[float, float]]],
                snap_tol: float = CHAIN_SNAP_MM,
                min_area_mm2: float = 10_000.0) -> List[Polygon]:
    """Chain open polylines/lines into closed rings.

    Geometric assumption: a room outline may be drawn as several OPEN
    polylines (or individual lines) whose endpoints meet within ``snap_tol``
    millimetres.  Endpoints are clustered and snapped, the linework is noded,
    and closed rings are recovered with ``polygonize``.  Rings smaller than
    ``min_area_mm2`` are discarded.
    """
    import math as _math
    lines: List[List[Tuple[float, float]]] = [
        [(float(p[0]), float(p[1])) for p in pts]
        for pts in point_lists if len(pts) >= 2
    ]
    if not lines:
        return []
    # cluster endpoints within snap_tol; snap to the FIRST point seen so all
    # members of a cluster share EXACTLY the same coordinate (polygonize
    # requires exact noding — a moving cluster center would break it)
    clusters: List[Tuple[float, float]] = []

    def snap(pt: Tuple[float, float]) -> Tuple[float, float]:
        for cx, cy in clusters:
            if _math.hypot(pt[0] - cx, pt[1] - cy) <= snap_tol:
                return (cx, cy)
        clusters.append((pt[0], pt[1]))
        return pt

    snapped = []
    for pts in lines:
        pts = list(pts)
        pts[0] = snap(pts[0])
        pts[-1] = snap(pts[-1])
        try:
            ls = LineString(pts)
            if ls.length > 0:
                snapped.append(ls)
        except Exception:
            continue
    if not snapped:
        return []
    merged = unary_union(snapped)
    rings: List[Polygon] = []
    for poly in polygonize(merged):
        if poly.area >= min_area_mm2:
            fixed = poly if poly.is_valid else poly.buffer(0)
            if fixed is not None and not fixed.is_empty:
                rings.extend(iter_polys(fixed))
    return rings


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------

class ZoneResolver:
    """Resolve config zone definitions to shapely polygons (with caching)."""

    def __init__(self, doc, dxf_path: str, cfg: Dict[str, Any]):
        self.doc = doc
        self.dxf_path = dxf_path
        self.cfg = cfg
        self.warnings: List[str] = []
        self._cache: Dict[str, List[Polygon]] = {}
        self._layer_table = {l.dxf.name.strip().lower(): l.dxf.name
                             for l in doc.layers}

    # ── layer helpers ──────────────────────────────────────────────────────
    def match_layers(self, names: Iterable[str],
                     pattern: Optional[str] = None) -> List[str]:
        """Case-insensitive layer resolution + optional fnmatch pattern.

        Warns (structured) for every named layer absent from the DXF.
        """
        out: List[str] = []
        for name in names or []:
            canon = self._layer_table.get(str(name).strip().lower())
            if canon is None:
                self.warn(f"layer '{name}' not found in DXF — skipped")
            elif canon not in out:
                out.append(canon)
        if pattern:
            pat = pattern.strip().lower()
            for low, canon in self._layer_table.items():
                if fnmatch.fnmatch(low, pat) and canon not in out:
                    out.append(canon)
        return out

    def warn(self, message: str) -> None:
        log.warning(message)
        self.warnings.append(message)

    def has_layer(self, name: str) -> bool:
        return str(name).strip().lower() in self._layer_table

    # ── public API ─────────────────────────────────────────────────────────
    def resolve(self, zone_def: Any) -> List[Polygon]:
        """Zone definition (or the ``"store"`` sentinel) → polygons."""
        if zone_def in (None, ""):
            return []
        if zone_def == "store":
            return self.store_polygons()
        if isinstance(zone_def, str):
            # a bare layer/block name used as an ad-hoc target
            return self.resolve_name(zone_def)
        key = zone_def.get("zone_name") or json.dumps(zone_def, sort_keys=True)
        if key in self._cache:
            return self._cache[key]
        kind = zone_def.get("kind")
        if kind == "polyline":
            polys = self._resolve_polyline(zone_def)
        elif kind == "block":
            polys = self._resolve_block(zone_def)
        elif kind == "balloon":
            polys = self._resolve_balloon(zone_def)
        else:
            self.warn(f"zone '{key}': unknown kind {kind!r} — skipped")
            polys = []
        if not polys:
            self.warn(f"zone '{key}': no geometry found in DXF")
        self._cache[key] = polys
        return polys

    def resolve_name(self, name: str) -> List[Polygon]:
        """Bare string target: try a layer of closed polygons, then a block.

        Used by dockets whose targets may be a raw layer or block name
        (finishes, skirting excludes).
        """
        key = f"@name:{name}"
        if key in self._cache:
            return self._cache[key]
        polys = self._resolve_polyline({"layers": [name]}) if \
            self._layer_table.get(name.strip().lower()) else []
        if not polys:
            polys = self._resolve_block({"block_names": [name]})
        if not polys:
            self.warn(f"target '{name}': no closed layer shapes and no "
                      f"matching blocks found")
        self._cache[key] = polys
        return polys

    def store_polygons(self) -> List[Polygon]:
        """The store outline polygons (config ``store_outline``)."""
        key = "@store"
        if key in self._cache:
            return self._cache[key]
        so = self.cfg.get("store_outline") or {}
        polys = self._resolve_polyline({"layers": so.get("layers"),
                                        "layer_pattern": so.get("layer_pattern")})
        self._cache[key] = polys
        return polys

    def store_region(self) -> Optional[Polygon]:
        """Union of the store outline polygons (None when absent)."""
        polys = self.store_polygons()
        if not polys:
            return None
        merged = unary_union(polys)
        ps = iter_polys(merged)
        return max(ps, key=lambda p: p.area) if ps else None

    # ── kind: polyline ─────────────────────────────────────────────────────
    def _resolve_polyline(self, zone_def: Dict[str, Any]) -> List[Polygon]:
        layers = self.match_layers(zone_def.get("layers") or [],
                                   zone_def.get("layer_pattern"))
        polys: List[Polygon] = []
        for layer in layers:
            res = extract_layer_polygons_closed(
                self.dxf_path, {"priority": [layer], layer: "shapes"})
            layer_polys = [p for p in
                           (to_polygon(pts) for pts in res.get("shapes") or [])
                           if p is not None]
            if not layer_polys:
                # fallback: the outline may be drawn as several OPEN
                # polylines/lines that chain end-to-end — snap + polygonize
                segs = self.segments_on_layers([layer])
                rings = chain_rings([list(s.coords) for s in segs])
                if rings:
                    self.warn(f"layer '{layer}': no closed shapes — chained "
                              f"{len(segs)} open polyline(s) into "
                              f"{len(rings)} closed ring(s)")
                    layer_polys = rings
            polys.extend(layer_polys)
        return polys

    # ── kind: block ────────────────────────────────────────────────────────
    def _resolve_block(self, zone_def: Dict[str, Any]) -> List[Polygon]:
        patterns = [str(p).strip().lower()
                    for p in zone_def.get("block_names") or [] if p]
        if not patterns:
            return []

        def matches(name: str) -> bool:
            low = name.strip().lower()
            return any(fnmatch.fnmatch(low, p) or low.startswith(p.rstrip("*"))
                       for p in patterns)

        polys: List[Polygon] = []

        def scan(container, transform: Optional[Matrix44],
                 visited: Set[str]) -> None:
            for e in container:
                if e.dxftype() != "INSERT":
                    continue
                name = e.dxf.name
                if matches(name):
                    box = self._insert_bbox_polygon(e, transform)
                    if box is not None:
                        polys.append(box)
                    continue
                if name in visited:
                    self.warn(f"cyclic block reference detected at "
                              f"'{name}' — recursion stopped")
                    continue
                try:
                    block = self.doc.blocks[name]
                except Exception:
                    continue
                m = e.matrix44()
                combined = m if transform is None else transform @ m
                scan(block, combined, visited | {name})

        scan(self.doc.modelspace(), None, set())
        return polys

    def _insert_bbox_polygon(self, insert,
                             parent: Optional[Matrix44]) -> Optional[Polygon]:
        """World-space bbox polygon of one INSERT.

        ``ezdxf.bbox.extents([insert])`` resolves the insert's own transform
        (including negative scales / mirroring); a parent transform from
        nested recursion is applied to the resulting corners.  When extents
        itself fails (e.g. a cyclic block definition overflows its internal
        recursion) the bbox falls back to the block's non-INSERT entities
        transformed by the insert's matrix — terminating with a warning,
        never crashing.
        """
        try:
            cache = _bbox.Cache()
            ext = _bbox.extents([insert], cache=cache)
            if not ext.has_data:
                return None
            (x0, y0, _), (x1, y1, _) = ext.extmin, ext.extmax
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if parent is not None:
                corners = [tuple(parent.transform((cx, cy, 0.0)))[:2]
                           for cx, cy in corners]
            return to_polygon(corners)
        except Exception as exc:
            self.warn(f"bbox failed for INSERT '{insert.dxf.name}' ({exc}) — "
                      f"using shallow entity bbox")
            return self._shallow_bbox(insert, parent)

    def _shallow_bbox(self, insert,
                      parent: Optional[Matrix44]) -> Optional[Polygon]:
        """Bbox from the block's own (non-INSERT) entities only — the safe
        fallback for pathological/cyclic block definitions."""
        try:
            block = self.doc.blocks[insert.dxf.name]
        except Exception:
            return None
        pts: List[Tuple[float, float]] = []
        for e in block:
            t = e.dxftype()
            try:
                if t == "LINE":
                    pts += [(e.dxf.start.x, e.dxf.start.y),
                            (e.dxf.end.x, e.dxf.end.y)]
                elif t == "LWPOLYLINE":
                    pts += [(p[0], p[1]) for p in e.get_points("xy")]
                elif t == "CIRCLE":
                    c, r = e.dxf.center, e.dxf.radius
                    pts += [(c.x - r, c.y - r), (c.x + r, c.y + r)]
            except Exception:
                continue
        if not pts:
            return None
        m = insert.matrix44()
        combined = m if parent is None else parent @ m
        world = [tuple(combined.transform((x, y, 0.0)))[:2] for x, y in pts]
        xs = [p[0] for p in world]
        ys = [p[1] for p in world]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        # pad degenerate axes (e.g. a fixture drawn as a single line) so a
        # thin but valid box comes back instead of nothing
        if x1 - x0 < 1.0:
            x0, x1 = x0 - 0.5, x1 + 0.5
        if y1 - y0 < 1.0:
            y0, y1 = y0 - 0.5, y1 + 0.5
        return to_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])

    # ── kind: balloon ──────────────────────────────────────────────────────
    def _resolve_balloon(self, zone_def: Dict[str, Any]) -> List[Polygon]:
        from balloon_zone_extract import detect_zones
        keywords = zone_def.get("keywords") or []
        if not keywords:
            return []
        barriers = list(zone_def.get("barrier_layers") or [])
        # Auto-append the config-defined store outline + wall layers so the
        # customer never has to re-list them per zone; both are config values.
        barriers += (self.cfg.get("store_outline") or {}).get("layers") or []
        barriers += (self.cfg.get("walls") or {}).get("layers") or []
        barriers = list(dict.fromkeys(barriers))
        text_layers = zone_def.get("text_layers") or None
        name = zone_def.get("zone_name") or "ZONE"
        try:
            result = detect_zones(
                dxf_path=self.dxf_path,
                balloon_groups={name: {"layers": text_layers,
                                       "keywords": keywords,
                                       "barrier_layers": barriers}},
            ) or {}
        except Exception as exc:
            self.warn(f"balloon detection failed for '{name}': {exc}")
            return []
        polys: List[Polygon] = []
        for zdata in (result.get(name) or {}).get("zones", {}).values():
            poly = to_polygon(zdata.get("polygon") or [])
            if poly is not None:
                polys.append(poly)
        return polys

    # ── walls / doors / rooms ──────────────────────────────────────────────
    def wall_segments(self) -> List[LineString]:
        """LineStrings of every entity on the configured wall layers."""
        return self.segments_on_layers(
            (self.cfg.get("walls") or {}).get("layers") or [])

    def door_segments(self) -> List[LineString]:
        """LineStrings on the configured door layers (walls.door_layers)."""
        return self.segments_on_layers(
            (self.cfg.get("walls") or {}).get("door_layers") or [])

    def segments_on_layers(self, layer_names: Iterable[str]) -> List[LineString]:
        """Collect LINE/LWPOLYLINE/POLYLINE segments on layers, recursing into
        INSERTs with full transforms and a visited set (cyclic-block safe)."""
        wanted = {l.strip().lower() for l in self.match_layers(layer_names)}
        if not wanted:
            return []
        segs: List[LineString] = []

        def entity_layer(e) -> str:
            try:
                return (e.dxf.layer or "0").strip().lower()
            except Exception:
                return "0"

        def add_entity(e, transform: Optional[Matrix44]) -> None:
            pts: List[Tuple[float, float]] = []
            t = e.dxftype()
            try:
                if t == "LINE":
                    pts = [(e.dxf.start.x, e.dxf.start.y),
                           (e.dxf.end.x, e.dxf.end.y)]
                elif t == "LWPOLYLINE":
                    pts = [(p[0], p[1]) for p in e.get_points("xy")]
                    if getattr(e, "closed", False) and len(pts) > 2:
                        pts.append(pts[0])
                elif t == "POLYLINE":
                    pts = [(v.dxf.location.x, v.dxf.location.y)
                           for v in e.vertices]
                    if e.is_closed and len(pts) > 2:
                        pts.append(pts[0])
            except Exception:
                return
            if len(pts) < 2:
                return
            if transform is not None:
                pts = [tuple(transform.transform((x, y, 0.0)))[:2]
                       for x, y in pts]
            try:
                seg = LineString(pts)
                if seg.length > 0:
                    segs.append(seg)
            except Exception:
                pass

        def scan(container, transform: Optional[Matrix44],
                 visited: Set[str]) -> None:
            for e in container:
                if e.dxftype() == "INSERT":
                    name = e.dxf.name
                    if name in visited:
                        self.warn(f"cyclic block reference detected at "
                                  f"'{name}' — recursion stopped")
                        continue
                    try:
                        block = self.doc.blocks[name]
                    except Exception:
                        continue
                    m = e.matrix44()
                    combined = m if transform is None else transform @ m
                    scan(block, combined, visited | {name})
                    continue
                if entity_layer(e) in wanted:
                    add_entity(e, transform)

        scan(self.doc.modelspace(), None, set())
        return segs

    def closed_rooms(self, min_area_mm2: float = 1_000_000.0) -> List[Polygon]:
        """Closed rooms found by polygonizing the configured wall layers
        (plus the store outline boundary), filtered to the store interior.

        Geometric assumption: rooms are regions fully enclosed by wall
        segments.  Override by listing rooms explicitly in the docket config
        (e.g. ``sprinklers.rooms``).
        """
        store = self.store_region()
        segs = list(self.wall_segments())
        if store is not None:
            segs.append(LineString(store.exterior.coords))
        if not segs:
            return []
        merged = unary_union(segs)
        rooms: List[Polygon] = []
        for poly in polygonize(merged):
            if poly.area < min_area_mm2:
                continue
            if store is not None:
                # keep only rooms inside the store (and not the store itself)
                if not poly.representative_point().within(store):
                    continue
                if poly.area > store.area * 0.98:
                    continue
            rooms.append(poly)
        return rooms

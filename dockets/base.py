"""Shared docket infrastructure: result type, contexts, geometry helpers.

No drawing-specific names live here — every layer name and color flows from
``config_loader.output_layer`` (engineering defaults overridable via the
config's ``output.layers``).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ezdxf.enums import TextEntityAlignment
from shapely.geometry import (LineString, MultiLineString, MultiPolygon,
                              Point, Polygon, box)
from shapely.ops import nearest_points, unary_union
from shapely import affinity

import config_loader
from zone_service import ZoneResolver, iter_polys

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# result / contexts
# ---------------------------------------------------------------------------

@dataclass
class DocketResult:
    """Uniform machine-readable outcome of one docket run."""
    name: str
    ok: bool = True
    dxf_entities_written: int = 0
    boq: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def warn(self, message: str) -> None:
        log.warning("[%s] %s", self.name, message)
        self.warnings.append(message)

    def fail(self, message: str) -> "DocketResult":
        self.ok = False
        self.error = message
        log.error("[%s] %s", self.name, message)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "dxf_entities_written": self.dxf_entities_written,
            "boq": self.boq,
            "warnings": self.warnings,
            "error": self.error,
        }


class Ctx:
    """Shared per-run context handed to every docket engine.

    Carries the store outline, the zone resolver (for lazy lookups), wall and
    door segments from the configured wall layers, and the full config.
    """

    def __init__(self, doc, dxf_path: str, cfg: Dict[str, Any],
                 resolver: ZoneResolver):
        self.doc = doc
        self.dxf_path = dxf_path
        self.cfg = cfg
        self.resolver = resolver
        self._store_region: Optional[Polygon] = None
        self._wall_segments: Optional[List[LineString]] = None
        self._door_segments: Optional[List[LineString]] = None
        self._wall_buffers: Dict[float, Any] = {}

    @property
    def store_region(self) -> Optional[Polygon]:
        if self._store_region is None:
            self._store_region = self.resolver.store_region()
        return self._store_region

    @property
    def wall_segments(self) -> List[LineString]:
        if self._wall_segments is None:
            self._wall_segments = self.resolver.wall_segments()
        return self._wall_segments

    @property
    def door_segments(self) -> List[LineString]:
        if self._door_segments is None:
            self._door_segments = self.resolver.door_segments()
        return self._door_segments

    def wall_buffer(self, dist: float):
        """Union of wall segments buffered by ``dist`` (cached per dist)."""
        if dist not in self._wall_buffers:
            segs = self.wall_segments
            self._wall_buffers[dist] = (
                unary_union([s.buffer(dist, cap_style=2) for s in segs])
                if segs else None)
        return self._wall_buffers[dist]


class Out:
    """Output context: destination doc/msp plus a config-driven layer factory."""

    def __init__(self, out_doc, cfg: Dict[str, Any]):
        self.doc = out_doc
        self.msp = out_doc.modelspace()
        self.cfg = cfg
        self._made: Dict[str, str] = {}

    def layer(self, key: str) -> str:
        """Ensure the output layer for ``key`` exists; return its name."""
        if key in self._made:
            return self._made[key]
        spec = config_loader.output_layer(self.cfg, key)
        name, color = spec["name"], int(spec.get("color", 7))
        if name not in self.doc.layers:
            try:
                self.doc.layers.add(name, color=color)
            except Exception:
                pass
        else:
            try:
                self.doc.layers.get(name).dxf.color = color
            except Exception:
                pass
        self._made[key] = name
        return name

    def ensure_linetype(self, name: str, pattern: Sequence[float]) -> str:
        try:
            if name not in self.doc.linetypes:
                self.doc.linetypes.add(name, pattern=list(pattern))
            return name
        except Exception:
            return "Continuous"


# ---------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------

def add_polygon(out: Out, poly: Polygon, layer: str,
                dxfattribs: Optional[Dict] = None) -> None:
    attribs = {"layer": layer}
    attribs.update(dxfattribs or {})
    out.msp.add_lwpolyline(list(poly.exterior.coords), close=True,
                           dxfattribs=attribs)
    for ring in poly.interiors:
        out.msp.add_lwpolyline(list(ring.coords), close=True,
                               dxfattribs=attribs)


def add_line_geom(out: Out, geom, layer: str,
                  dxfattribs: Optional[Dict] = None) -> int:
    """Draw any LineString/MultiLineString; returns segments drawn."""
    attribs = {"layer": layer}
    attribs.update(dxfattribs or {})
    n = 0
    parts = []
    if isinstance(geom, LineString):
        parts = [geom]
    elif isinstance(geom, MultiLineString) or hasattr(geom, "geoms"):
        parts = [g for g in geom.geoms if isinstance(g, LineString)]
    for part in parts:
        coords = list(part.coords)
        if len(coords) >= 2 and part.length > 1.0:
            out.msp.add_lwpolyline(coords, dxfattribs=attribs)
            n += 1
    return n


def add_text(out: Out, xy: Tuple[float, float], text: str, height: float,
             layer: str, rotation: float = 0.0,
             align: TextEntityAlignment = TextEntityAlignment.MIDDLE_CENTER
             ) -> None:
    t = out.msp.add_text(text, dxfattribs={
        "layer": layer, "height": height, "rotation": rotation})
    t.set_placement(xy, align=align)


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

def union_polys(polys: Iterable[Polygon]):
    polys = [p for p in polys if p is not None and not p.is_empty]
    return unary_union(polys) if polys else None


def grid_points(region, pitch: float, pattern: str = "square",
                inset: float = 0.0) -> List[Tuple[float, float]]:
    """Grid points covering ``region`` with center pitch ``pitch``.

    ``square``: axis-aligned grid.  ``staggered``: triangular lattice — rows
    ``pitch·√3/2`` apart, every other row offset by ``pitch/2``.
    Points are kept when inside the (optionally inset) region.
    """
    if region is None or region.is_empty or pitch <= 0:
        return []
    target = region.buffer(-inset) if inset > 0 else region
    if target.is_empty:
        target = region
    minx, miny, maxx, maxy = region.bounds
    row_pitch = pitch * (math.sqrt(3) / 2 if pattern == "staggered" else 1.0)
    points: List[Tuple[float, float]] = []
    row = 0
    y = miny + row_pitch / 2
    while y < maxy:
        offset = (pitch / 2 if (pattern == "staggered" and row % 2) else 0.0)
        x = minx + pitch / 2 + offset
        while x < maxx:
            if target.contains(Point(x, y)):
                points.append((x, y))
            x += pitch
        y += row_pitch
        row += 1
    return points


def coverage_gaps(region, heads: List[Tuple[float, float]], radius: float,
                  sample_pitch: Optional[float] = None
                  ) -> List[Tuple[float, float]]:
    """Sample points of ``region`` farther than ``radius`` from every head."""
    if region is None or region.is_empty:
        return []
    sample_pitch = sample_pitch or radius / 2
    samples = grid_points(region, sample_pitch, "square")
    if not samples:
        return []
    if not heads:
        return samples
    r2 = radius * radius
    gaps = []
    for sx, sy in samples:
        if not any((sx - hx) ** 2 + (sy - hy) ** 2 <= r2 for hx, hy in heads):
            gaps.append((sx, sy))
    return gaps


def positional_points(poly: Polygon, position: Optional[str],
                      count: Optional[int], inset: float
                      ) -> List[Tuple[float, float]]:
    """Points for a named position inside ``poly``.

    ``corners`` → the 4 inset corners of the minimum rotated rectangle;
    ``center`` → centroid; ``front/back/left/right`` → inset midpoint of that
    side of the bounding box (front = min-Y side; assumption documented in the
    speaker engine).  ``count`` clamps/pads the corner set.
    """
    if poly is None or poly.is_empty:
        return []
    inner = poly.buffer(-inset)
    if inner.is_empty:
        inner = poly
    inner = max(iter_polys(inner), key=lambda p: p.area, default=poly)
    minx, miny, maxx, maxy = inner.bounds
    cx, cy = inner.centroid.x, inner.centroid.y
    pos = (position or "center").lower()
    if pos == "corners":
        mrr = inner.minimum_rotated_rectangle
        pts = list(mrr.exterior.coords)[:4]
        pts = [_pull_inside(inner, p) for p in pts]
        if count and count < len(pts):
            pts = pts[:count]
        return pts
    if pos == "center":
        pts = [(cx, cy)]
    elif pos == "front":
        pts = [(cx, miny)]
    elif pos == "back":
        pts = [(cx, maxy)]
    elif pos == "left":
        pts = [(minx, cy)]
    elif pos == "right":
        pts = [(maxx, cy)]
    else:
        pts = [(cx, cy)]
    pts = [_pull_inside(inner, p) for p in pts]
    n = int(count or 1)
    return (pts * n)[:n] if n > 1 else pts


def _pull_inside(poly: Polygon, pt: Tuple[float, float]
                 ) -> Tuple[float, float]:
    p = Point(pt)
    if poly.contains(p):
        return (p.x, p.y)
    q, _ = nearest_points(poly, p)
    return (q.x, q.y)


def axis_points(poly: Polygon, n: int, inset: float
                ) -> List[Tuple[float, float]]:
    """N points spaced evenly along the polygon's principal axis centerline
    (minimum rotated rectangle long axis)."""
    if poly is None or poly.is_empty or n <= 0:
        return []
    inner_geoms = iter_polys(poly.buffer(-inset))
    inner = max(inner_geoms, key=lambda p: p.area) if inner_geoms else poly
    mrr = inner.minimum_rotated_rectangle
    corners = list(mrr.exterior.coords)[:4]
    e1 = LineString([corners[0], corners[1]])
    e2 = LineString([corners[1], corners[2]])
    if e1.length >= e2.length:
        a = ((corners[0][0] + corners[3][0]) / 2, (corners[0][1] + corners[3][1]) / 2)
        b = ((corners[1][0] + corners[2][0]) / 2, (corners[1][1] + corners[2][1]) / 2)
    else:
        a = ((corners[0][0] + corners[1][0]) / 2, (corners[0][1] + corners[1][1]) / 2)
        b = ((corners[2][0] + corners[3][0]) / 2, (corners[2][1] + corners[3][1]) / 2)
    center = LineString([a, b])
    pts = []
    for i in range(n):
        t = (i + 0.5) / n
        p = center.interpolate(t, normalized=True)
        pts.append(_pull_inside(inner, (p.x, p.y)))
    return pts


def boundary_on_walls(ctx: Ctx, poly: Polygon, result: DocketResult,
                      extra_subtract=None):
    """Portions of ``poly``'s boundary that sit on configured walls.

    Returns a (Multi)LineString.  Door openings are removed two ways: the
    wall∩ step drops boundary parts with no wall behind them (openings that
    are gaps in the walls), and segments on the configured door layers are
    subtracted explicitly.  When no wall layers are configured, the full
    boundary is used and a structured warning is recorded (never guessed
    from magic layer names).
    """
    boundary = LineString(poly.exterior.coords)
    wall_buffer_mm = config_loader.DEFAULTS["walls"]["wall_buffer_mm"]
    walls = ctx.wall_buffer(wall_buffer_mm)
    if walls is None:
        result.warn("no wall layers configured — using the full zone "
                    "boundary (door openings cannot be excluded)")
        kept = boundary
    else:
        kept = boundary.intersection(walls)
    doors = ctx.door_segments
    if doors and not kept.is_empty:
        door_buf = unary_union([d.buffer(wall_buffer_mm, cap_style=2)
                                for d in doors])
        kept = kept.difference(door_buf)
    if extra_subtract is not None and not kept.is_empty:
        kept = kept.difference(extra_subtract)
    return kept


def geom_length(geom) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    return float(geom.length)


def mm2_to_m2(v: float) -> float:
    return round(v / 1e6, 3)


def m2_to_sqft(v: float) -> float:
    return round(v * 10.7639, 3)

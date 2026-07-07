"""
flooring_layout.py

Flooring Layout Generator.

Task 1 — Store Floor Boundary
    1. Start from the cleaned store boundary polyline (HATCH_OUTLINE).
    2. Subtract a granite strip along the façade (front glass edge),
       width is user-supplied (default 300 mm).
    3. Subtract the toilet room footprint.

Task 2 — Tile Start Point
    Detect the door opening as the largest gap between Glass-FrontGlazing /
    Rolling Shutter segments along the façade axis. Midpoint of that gap is
    the tile grid origin. Falls back to the façade line midpoint if no gap
    is detected.

Task 3 — Tile Grid Generation
    Build a 600×600 mm grid radiating from the tile origin, aligned with the
    façade axis, clipped to the net flooring polygon, and emitted as DXF
    LINE entities on the FLOORING_TILE layer.

Task 4 — Tile Area Calculation
    Compute the tile area from the net flooring polygon in sqft and sqm,
    and apply a wastage factor (default 10 %) to produce the order quantity.

Public API:
    FlooringLayout(dxf_path, granite_width=300.0, min_door_width=600.0,
                   tile_size=600.0, wastage_factor=0.10)
        .extract()              -> dict with polygons, area, tile origin,
                                   tile grid, and tile area breakdown
        .save_output(path)      -> writes flooring DXF (net floor, granite,
                                   toilet cut, tile origin marker, grid)
"""

import os
import sys
import math
from typing import Any, Dict, List, Optional, Tuple

# Force UTF-8 on Windows consoles that default to cp1252 so the ✓/✗/⚠ glyphs
# in the log output don't crash the run.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import ezdxf
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union


class FlooringLayout:
    """
    Generate the net flooring polygon for BOQ flooring calculations.

    The net polygon = HATCH_OUTLINE  -  granite_strip(façade)  -  toilet_footprint
    """

    # Planogram trade-boundary layers. These trace the REAL retail footprint
    # drawn by the space-planning team and take priority over STORE_OUTLINE /
    # HATCH_OUTLINE, which in some source files is a coarse sheet-bounding
    # rectangle that does not match the drawn walls (so tiles spill outside
    # the layout). First closed-ish polyline match (by area) wins.
    PLANO_BOUNDARY_LAYERS = ["PLANO-BOUNDARY", "PLANO-CARPET", "PLANO-TRADE AREA"]
    # Search order for the store-boundary polyline. First standardized name,
    # then the common non-standardized names we see in the wild.
    HATCH_LAYERS = ["STORE_OUTLINE", "HATCH_OUTLINE", "Floor-Unit"]
    # Layers whose HATCH entities should be copied into the flooring output.
    # Only wall-boundary hatches belong here — furniture / glass / etc. are noise.
    HATCH_SOURCE_LAYERS = ["HATCH_OUTLINE"]
    # Additional polyline-outline layers to copy (e.g. clinic / BOH walls / curtains / wall outlines).
    BOH_OUTLINE_LAYERS  = ["BOH_WALL_VALID_OUTLINE", "I-LK CURTAIN", "HATCH_OUTLINE"]
    # Layers whose ENTIRE contents (HATCH + outlines) are copied verbatim
    # from the input DXF to the output — no clipping, area, or interior
    # filters. Used to reproduce the wall cross-section exactly as drawn.
    # Includes the main hatch outline plus the inner partition walls for
    # the toilet / back-of-house rooms so the full wall frame appears.
    MIRROR_WALL_LAYERS = [
        "A-WALL HATCH",
        "A-WALL",
        "LK-PARTITION"
    ]
    # INSERTs whose own layer matches this list are NOT brought into the
    # output, even if their block definition contains wall-layer entities.
    # Use this to keep block-encapsulated drawings (e.g. clinic schematics)
    # out of the flooring DXF.
    MIRROR_EXCLUDE_INSERT_LAYERS = [
        "CLINIC_1",
        "Floor-Unit",
    ]
    # INSERTs whose block NAME matches this list are also excluded.
    # Use for decorative fixture blocks that happen to include a small
    # HATCH on a wall layer (e.g. shelf/elevation symbols) but are not
    # part of the actual wall structure.
    MIRROR_EXCLUDE_INSERT_BLOCK_NAMES = [
        "JJ_FIXTURE_LARGE",
    ]
    FRONT_GLASS_LAYERS = ["Glass-FrontGlazing", "Rolling Shutter"]
    # Layers that mark a toilet entry (curtain / internal door). If an
    # I-LK CURTAIN entity exists inside the store, the enclosed closed
    # polyline adjacent to it is the toilet — no flooring needed there.
    TOILET_CURTAIN_LAYERS = ["I-LK CURTAIN", "LENS_CURTAIN", "Lens_Curtain"]
    TOILET_CURTAIN_SEARCH_RADIUS = 500.0  # mm — how close a closed polyline
                                          # must be to a curtain to count as the toilet

    # Tolerance in mm for "edges on the façade axis" (and for "bottom edges" of
    # the store polygon when synthesizing a façade without glass-layer data).
    FACADE_EDGE_TOLERANCE = 10.0
    OUTPUT_LAYER_FLOOR = "FLOOR-NET"
    OUTPUT_LAYER_GRANITE = "FLOORING_GRANITE"
    OUTPUT_LAYER_TOILET = "FLOOR-TOILET-CUT"
    OUTPUT_LAYER_TILE_ORIGIN = "FLOORING_TILE_ORIGIN"
    OUTPUT_LAYER_TILE_GREY = "FLOORING_TILE_GREY"
    OUTPUT_LAYER_TILE_WHITE = "FLOORING_TILE_WHITE"
    OUTPUT_LAYER_TILE_START = "FLOORING_TILE_START"
    OUTPUT_LAYER_TILE_OUTLINE = "FLOORING_TILE_OUTLINE"
    OUTPUT_LAYER_ANNOT = "FLOORING_ANNOTATION"
    OUTPUT_LAYER_DIM = "FLOORING_DIMENSION"

    # AutoCAD Color Index (ACI) for each tile class.
    # Tuned to resemble the sample PDF (medium grey + light near-white + red start).
    ACI_TILE_GREY = 9      # medium grey
    ACI_TILE_WHITE = 254   # very light grey
    ACI_TILE_START = 1     # red
    ACI_TILE_OUTLINE = 8   # dark grey outlines
    ACI_GRANITE = 8        # dark grey granite hatch
    ACI_ORIGIN_MARKER = 1  # red crosshair
    ACI_ANNOT = 7          # leader+label (white/black depending on bg)
    ACI_DIM = 7            # dimension lines & text

    # Hatch wall net polygon — reproduced from the source HATCH entity so
    # wall material cross-section reads in the output just like the input.
    OUTPUT_LAYER_HATCH_OUTLINE = "FLOORING_HATCH_OUTLINE"
    OUTPUT_LAYER_HATCH_WALL    = "FLOORING_HATCH_WALL"
    ACI_HATCH_OUTLINE = 7   # white/black — matches AutoCAD default
    ACI_HATCH_WALL    = 9   # medium grey fill
    # Per-zone flooring hatch fill (wood / parquet / concrete patterns, etc.)
    OUTPUT_LAYER_ZONE_HATCH = "FLOORING_ZONE_HATCH"
    ACI_ZONE_HATCH = 9
    HATCH_WALL_PATTERN = "ANSI31"   # 45° diagonal lines (standard wall section)
    HATCH_WALL_SCALE   = 10.0

    # Annotation geometry (mm). Sized for typical retail-store plans
    # (~6 m × 12 m) so labels read clearly on a printed sheet.
    START_ARROW_LENGTH = 2400.0          # 4 tiles past the start-tile edge
    START_ARROW_HEAD_LENGTH = 250.0
    START_ARROW_HEAD_HALF_WIDTH = 80.0
    ANNOT_TEXT_HEIGHT = 250.0            # MTEXT char height for callout labels
    ANNOT_LEADER_GAP = 200.0              # clearance between text and leader start
    ANNOT_LEADER_KINK = 600.0             # length of the horizontal bend on a leader
    DIM_TEXT_HEIGHT = 220.0
    DIM_OFFSET = 800.0                   # distance from edge to dim line
    DIM_TICK_HALF = 120.0                # half-length of the diagonal tick on a dim
    DIM_EXT_OVERSHOOT = 120.0            # how far ext lines pass the dim line

    # Label drawn inside the red start diamond. Kept short so it doesn't
    # crowd the diamond's vertices.
    START_TILE_TEXT = "START"
    START_TILE_TEXT_HEIGHT = 100.0       # mm — fits inside a 600 mm tile

    # Granite hatch pattern — ANSI37 is a 45° crosshatch that reads well
    # on paper and matches the "JET BLACK GRANITE" hatching in the sample.
    GRANITE_HATCH_PATTERN = "ANSI37"
    GRANITE_HATCH_SCALE = 50.0

    MM2_TO_SQFT = 1.0 / (304.8 ** 2)
    MM2_TO_SQM = 1.0 / (1000.0 ** 2)

    # Door-gap detection tolerances (mm).
    DOOR_AXIS_TOLERANCE = 200.0   # how far a segment may sit off the façade axis
                                  # to still be considered part of the façade line
    TILE_ORIGIN_MARKER_RADIUS = 150.0

    # Grid generation
    GRID_MIN_SEGMENT_LENGTH = 1.0   # mm — discard clipped fragments shorter than this
    TILE_MIN_FRAGMENT_AREA = 100.0  # mm² — discard sliver tiles tinier than this

    # Layers searched for a USER-DRAWN reference that overrides the
    # auto-detected origin. Two flavours are accepted:
    #   • a single LINE / LWPOLYLINE / POLYLINE → its midpoint is the anchor
    #   • a strip of closed LWPOLYLINE rectangles → the inner-facing edge of
    #     their union envelope is used as the reference line
    # First match (case-insensitive substring) wins.
    START_LINE_LAYERS = [
        "FLOORING_START_LINE",
        "TILE_START_LINE",
        "TILE_START",
        "FLOORING_START",
        "ENTRY_LINE",
        "BLUE_LINE",
        "ENTRY",
        "LK-TILES FINISH",
        "LK-FLUTTED FINISH",
        "TILES FINISH",
    ]

    # Layers whose closed polygons are EXCLUDED from flooring (subtracted
    # from the net polygon, just like granite and toilet). The bulkhead /
    # bulk-headline is a structural fascia strip above the entry — no tiles
    # go there.
    BULKHEAD_LAYERS = [
        "BULK-HEADLINE",
        "BULKHEAD",
        "BULK_HEADLINE",
        "BULK-HEAD",
    ]

    # Door-lintel layers — the lintel strip at a door head (e.g. the toilet
    # door) is not floored, so its closed polygons are subtracted from the
    # net polygon just like the toilet / bulkhead.
    LINTEL_LAYERS = [
        "LK-DOOR LINTEL",
        "DOOR LINTEL",
        "DOOR-LINTEL",
        "LINTEL",
    ]

    # Structural column / pillar layers — LINE entities are polygonized;
    # closed LWPOLYLINE/POLYLINE entities are used directly.
    COLUMN_LAYERS = [
        "Column", "COLUMN", "COLUMNS",
        "Pillar", "PILLAR", "PILLARS",
        "STRUCT-COLUMN", "LK-COLUMN",
    ]
    OUTPUT_LAYER_COLUMN = "FLOORING_COLUMN_CUT"
    ACI_COLUMN = 8

    def __init__(
        self,
        dxf_path: str,
        granite_width: float = 300.0,
        min_door_width: float = 600.0,
        tile_size: float = 600.0,
        wastage_factor: float = 0.10,
        tile_rotation_deg: float = 45.0,
        start_line_layer: Optional[str] = None,
        ctx: Optional[Dict [str, Any]] = None
    ):
        self.ctx = ctx
        if not os.path.exists(dxf_path):
            raise FileNotFoundError(f"DXF file not found: {dxf_path}")
        if granite_width < 0:
            raise ValueError("granite_width must be >= 0")
        if min_door_width < 0:
            raise ValueError("min_door_width must be >= 0")
        if tile_size <= 0:
            raise ValueError("tile_size must be > 0")
        if wastage_factor < 0:
            raise ValueError("wastage_factor must be >= 0")

        self.dxf_path = dxf_path
        self.granite_width = float(granite_width)
        self.min_door_width = float(min_door_width)
        self.tile_size = float(tile_size)
        self.wastage_factor = float(wastage_factor)
        self.tile_rotation_deg = float(tile_rotation_deg)
        self.start_line_layer = start_line_layer  # explicit override for the
                                                  # user-drawn-line layer name

        self.doc = ezdxf.readfile(dxf_path)
        self.msp = self.doc.modelspace()

        self.store_polygon: Optional[Polygon] = None
        self.front_glass: Optional[LineString] = None
        self.granite_strip: Optional[Polygon] = None
        self.toilet_polygon: Optional[Polygon] = None
        self.bulkhead_polygon: Optional[Polygon] = None
        self.column_polygon: Optional[Polygon] = None
        self.lintel_polygon: Optional[Polygon] = None
        self.net_polygon: Optional[Polygon] = None
        # HATCH entity boundaries extracted from the source DXF.
        self.hatches: Optional[List [List [List[int]]]] = None
        self.tile_origin: Optional[Tuple[float, float]] = None
        self.tile_origin_source: str = ""       # "door_gap" | "facade_midpoint"
        self.door_gap_width: float = 0.0
        self.hatch_wall_polygon = None
        self.hatch_outer_polygon = None
        # Unit vectors of the rotated tile grid in world space — captured from
        # _compute_tile_grid() so annotations (start-tile arrows) align with
        # the same axes that produced the diamond pattern.
        self.tile_u_axis: Optional[Tuple[float, float]] = None
        self.tile_v_axis: Optional[Tuple[float, float]] = None
        # Tile grid: list of per-tile dicts
        #   {"polygon": Polygon, "color": "grey"|"white"|"start", "index": (i, j)}
        self.tile_polygons: List[Dict[str, Any]] = []
        # Kept for backwards compat — now populated from tile outlines
        self.tile_grid_segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        self.tile_area: Dict[str, float] = {}
        # Per-zone hatch fills: list of (shapely Polygon, flooring_type dict).
        # Populated only when the ctx supplies zone-based flooring.
        self.hatch_zones: List[Tuple[Any, Dict[str, Any]]] = []

    # ─────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────
    def extract(self) -> Dict[str, Any]:
        print("=" * 70)
        print("FLOORING LAYOUT")
        print("=" * 70)
        print(f"  Input:            {self.dxf_path}")
        print(f"  Granite width:    {self.granite_width:.0f} mm")
        print(f"  Min door width:   {self.min_door_width:.0f} mm")
        print(f"  Tile size:        {self.tile_size:.0f} mm")
        print(f"  Tile rotation:    {self.tile_rotation_deg:.0f}°")
        print(f"  Wastage factor:   {self.wastage_factor * 100:.1f} %")

        # ── Task 1: Store floor boundary ─────────────────────────
        # Prefer the planogram trade boundary (PLANO-BOUNDARY) when present —
        # it traces the real retail footprint. STORE_OUTLINE in some files is
        # a coarse sheet rectangle that lets tiles spill outside the drawn
        # walls, so we only fall back to it when no PLANO boundary exists.
        _outline = self.ctx.get("store-outline") or []
        if not _outline:
            raise ValueError(
                "ctx['store-outline'] is empty — no store/boundary polygon was "
                "extracted from the DXF (check store_layer / boundary_layer)"
            )
        self.store_polygon = Polygon(_outline[0])

        # Extract HATCH entity boundaries (outer outline, inner holes, wall net polygon).
        # This runs after store_polygon is confirmed so inner-hole sizes can be
        # validated against the store area.
        self.hatches = self.ctx.get("hatches") or []

        _mainglass = self.ctx.get("store-mainglass")
        self.front_glass = LineString(_mainglass) if _mainglass else None
        if self.front_glass is None:
            # No explicit façade layer. Prefer the polygon's bottom edges —
            # for Lenskart retail drawings the shop entry always sits at the
            # minimum-Y end of the plan. Only if there is no clear bottom do
            # we fall back to the longest polygon edge.
            self.front_glass = self._synthesize_facade_from_bottom(self.store_polygon)
            if self.front_glass is not None:
                print(f"  ✓ Façade synthesized from store polygon bottom edge: "
                      f"length {self.front_glass.length:.0f} mm")
            else:
                self.front_glass = self._synthesize_facade_from_polygon(self.store_polygon)
                if self.front_glass is not None:
                    print(f"  ✓ Façade synthesized from store polygon longest edge: "
                          f"length {self.front_glass.length:.0f} mm")
        # Granite strip is no longer used — flooring starts right after
        # the bulkhead exclusion zone.
        self.granite_strip = None
        print(list(self.ctx.keys()))
        # Toilet footprint — no flooring is laid in the proposed toilet.
        # Priority 1: the planogram non-trade zone (PLANO-BOUNDARY −
        # PLANO-TRADE AREA) when it carries a toilet room tag.
        # Priority 2: the closed room adjacent to a curtain/internal door.
        _toilet_zones = (
            (self.ctx.get("BALLOON") or {}).get("TOILET") or {}
        ).get("zones", {})
        # Exclusion / skip regions come exclusively from the ctx (config-driven
        # zone rules). Always a list — never None — so downstream area sums and
        # subtractions are safe even when there are no skip zones.
        self.toilet_polygon = [Polygon(val["polygon"]) for val in _toilet_zones.values()]

        # Bulkhead / bulk-headline — structural fascia strip above the
        # entry door.  Closed polygons on BULK-HEADLINE layers are
        # subtracted from the net polygon (no tiles there).
        _bulk = self.ctx.get("bulkhead") or []
        self.bulkhead_polygon = Polygon(_bulk[0]) if _bulk else None

        # Structural columns — from context segments, polygonized + clipped.
        self.column_polygon = self._columns_from_ctx(self.store_polygon)
        # print("column polygon: ", self.column_polygon)

        # Door lintels — the lintel strip at a door head gets no flooring.
        self.lintel_polygon = [Polygon(val) for val in (self.ctx.get("lintel") or [])]

        print("poly", type(self.store_polygon))
        print("gran", type(self.granite_strip))
        print("toilet", type(self.toilet_polygon ))
        print("bulk", type(self.bulkhead_polygon))
        print("col", type(self.column_polygon)) 
        print("lint", type(self.lintel_polygon))
        self.net_polygon = self._compute_net_polygon(
            self.store_polygon, self.granite_strip,
            self.toilet_polygon, self.bulkhead_polygon,
            self.column_polygon, self.lintel_polygon
        )

        # ── Task 2: Tile grid origin ─────────────────────────────
        # Priority 1: the CENTRE OF THE FAÇADE DOOR (the opening in the
        # Glass-FrontGlazing / PLANO-DOOR). Tiles are laid from the door
        # centre, shifted inward past any bulkhead.
        _maindoor = self.ctx.get("store-maindoor") or []
        door_center = self.midpoint(_maindoor[0]) if _maindoor else None
        user_line = self._find_user_start_line()
        if door_center is not None:
            self.tile_origin = self._shift_origin_past_bulkhead(
                door_center, self.front_glass, self.store_polygon
            )
            self.tile_origin_source = "facade_door"
            self.door_gap_width = 0.0
            print("door_center", self.tile_origin)
        # Priority 2: a USER-DRAWN reference line (LINE/LWPOLYLINE) on a
        # configured layer. The diamond's bottom corner snaps to the
        # midpoint of that line, with the diamond sitting on the side that
        # faces the store centroid.
        elif user_line is not None:
            self.tile_origin = self._origin_from_user_line(user_line, self.store_polygon)
            # Re-centre the origin on the store's geometric width so the
            # start diamond sits at the floorplan's horizontal centreline,
            # regardless of where the user drew the reference line.
            self.tile_origin = self._recenter_origin_on_facade(
                self.tile_origin, self.front_glass, self.store_polygon
            )
            self.tile_origin_source = "user_line"
            self.door_gap_width = 0.0
        else:
            # Priority 2: auto-detect the entry door midpoint, then re-centre
            # on the store's geometric width, then push past the bulkhead.
            self.tile_origin, self.tile_origin_source, self.door_gap_width = (
                self._compute_tile_origin(self.front_glass)
            )
            self.tile_origin = self._recenter_origin_on_facade(
                self.tile_origin, self.front_glass, self.store_polygon
            )
            self.tile_origin = self._shift_origin_past_bulkhead(
                self.tile_origin, self.front_glass, self.store_polygon
            )

        # ── Task 3: Tile grid (rotated checkerboard, clipped to net) ─
        # Zone-based mode: when the ctx supplies per-zone flooring types, tile
        # / hatch each zone with its own pattern. Otherwise the classic single
        # diamond grid across the whole net polygon.
        zones = (self.ctx or {}).get("zones") or []
        default_ft = (self.ctx or {}).get("default_flooring_type")
        if zones or default_ft:
            # Zone mode — also covers "only skip zones / no floor zones": the
            # whole net area is then floored with default_flooring_type.
            self.tile_polygons, self.hatch_zones = self._compute_zone_flooring(
                zones, self.net_polygon, self.tile_origin, self.front_glass
            )
        else:
            self.tile_polygons = self._compute_tile_grid(
                self.net_polygon, self.tile_origin, self.front_glass
            )
        self.tile_grid_segments = self._tile_polygons_to_segments(self.tile_polygons)

        # ── Task 4: Tile area (with wastage factor) ──────────────
        self.tile_area = self._compute_tile_area(self.net_polygon, self.wastage_factor)

        gross_sqft = self.store_polygon.area * self.MM2_TO_SQFT
        granite_sqft = (self.granite_strip.area if self.granite_strip else 0.0) * self.MM2_TO_SQFT
        toilet_sqft = [p.area * self.MM2_TO_SQFT for p in self.toilet_polygon if self.toilet_polygon]
        bulkhead_sqft = (self.bulkhead_polygon.area if self.bulkhead_polygon else 0.0) * self.MM2_TO_SQFT
        column_sqft = (self.column_polygon.area if self.column_polygon else 0.0) * self.MM2_TO_SQFT
        net_sqft = self.net_polygon.area * self.MM2_TO_SQFT

        print("\n  ── Areas ──")
        print(f"    Store (gross):   {gross_sqft:8.2f} sq ft")
        if bulkhead_sqft > 0:
            print(f"    Bulkhead:      - {bulkhead_sqft:8.2f} sq ft")
        if column_sqft > 0:
            print(f"    Columns:       - {column_sqft:8.2f} sq ft")
        if len(toilet_sqft) > 0:
            print(f"    Toilet:        - {sum(toilet_sqft):8.2f} sq ft")
        print(f"    Flooring net:    {net_sqft:8.2f} sq ft")
        if self.tile_origin is not None:
            ox, oy = self.tile_origin
            print(f"\n  ── Tile origin ──")
            print(f"    Point:   ({ox:.1f}, {oy:.1f})")
            print(f"    Source:  {self.tile_origin_source}"
                  + (f" (gap {self.door_gap_width:.0f} mm)"
                     if self.tile_origin_source == "door_gap" else ""))
        if self.tile_polygons:
            grey_n = sum(1 for t in self.tile_polygons if t["color"] == "grey")
            white_n = sum(1 for t in self.tile_polygons if t["color"] == "white")
            start_n = sum(1 for t in self.tile_polygons if t["color"] == "start")
            print(f"\n  ── Tile grid ──")
            print(f"    Tiles: {len(self.tile_polygons)}  "
                  f"(grey {grey_n}, white {white_n}, start {start_n})")
        if self.tile_area:
            print(f"\n  ── Tile area ──")
            print(f"    Net:         {self.tile_area['area_sqft']:8.2f} sq ft "
                  f"({self.tile_area['area_sqm']:7.2f} sq m)")
            print(f"    + Wastage:   {self.tile_area['wastage_sqft']:8.2f} sq ft "
                  f"({self.tile_area['wastage_sqm']:7.2f} sq m) "
                  f"@ {self.wastage_factor * 100:.0f}%")
            print(f"    Order qty:   {self.tile_area['order_sqft']:8.2f} sq ft "
                  f"({self.tile_area['order_sqm']:7.2f} sq m)")
        print("=" * 70)

        return {
            "store_polygon": self.store_polygon,
            "granite_strip": self.granite_strip,
            "toilet_polygon": self.toilet_polygon,
            "net_polygon": self.net_polygon,
            "gross_area_sqft": gross_sqft,
            "granite_area_sqft": granite_sqft,
            "toilet_area_sqft": sum(toilet_sqft),
            "net_area_sqft": net_sqft,
            "tile_origin": self.tile_origin,
            "tile_origin_source": self.tile_origin_source,
            "door_gap_width": self.door_gap_width,
            "tile_polygons": self.tile_polygons,
            "tile_grid_segments": self.tile_grid_segments,
            "tile_area": self.tile_area,
            # HATCH entity boundaries
            "hatches": self.hatches,
        }

    def midpoint(self, line):
        if not isinstance(line, LineString):
            line = LineString([p[:2] for p in line])
        pt = line.interpolate(0.5, normalized=True)
        return [pt.x, pt.y]

    # ─────────────────────────────────────────────────────────────
    # Step 1: Store boundary — largest closed polyline across the
    # fallback layer list (HATCH_OUTLINE → STORE_OUTLINE → Floor-Unit).
    # ─────────────────────────────────────────────────────────────
    def _largest_polyline_on_layers(self, layers: List[str]) -> Optional[Polygon]:
        """Return the largest valid, box-filling closed polygon found on any
        of `layers`. Thin legend-key swatches / stray segments are rejected
        (a real boundary fills most of its bounding box)."""
        targets = {l.upper() for l in layers}
        best: Optional[Polygon] = None
        best_area = 0.0
        for entity_type in ("LWPOLYLINE", "POLYLINE"):
            for entity in self.msp.query(entity_type):
                try:
                    if entity.dxf.layer.upper() not in targets:
                        continue
                except Exception:
                    continue
                pts = self._polyline_vertices(entity, entity_type)
                if pts is None or len(pts) < 4:
                    continue
                try:
                    poly = Polygon(pts)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    if poly.is_empty or poly.area <= 0:
                        continue
                    if poly.area < 0.25 * poly.envelope.area:
                        continue
                    if poly.area > best_area:
                        best_area = poly.area
                        best = poly
                except Exception:
                    continue
        return best

    def _extract_plano_boundary(self) -> Optional[Polygon]:
        """Return the planogram trade boundary as the tileable floor polygon.

        Retail planograms carry an explicit PLANO-BOUNDARY polyline that
        traces the real store footprint. We prefer it over STORE_OUTLINE
        because the latter is sometimes a coarse sheet-bounding rectangle
        that doesn't match the drawn walls — which makes the tile grid spill
        outside the layout.
        """
        for layer in self.PLANO_BOUNDARY_LAYERS:
            best = self._largest_polyline_on_layers([layer])
            if best is not None:
                print(f"  ✓ Tileable boundary from PLANO layer '{layer}': "
                      f"area {best.area * self.MM2_TO_SQFT:.1f} sq ft")
                return best
        return None
    


    def _toilet_tag_points(self) -> List[Tuple[float, float]]:
        """Insert points of every TEXT/MTEXT that names a toilet/washroom
        (e.g. the 'PROPOSED TOILET' room tag)."""
        keywords = ("TOILET", "WASHROOM", "REST ROOM", "RESTROOM", "BATHROOM", "W.C")
        pts: List[Tuple[float, float]] = []
        for e in self._iter_virtual_entities(self.msp):
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            raw = ""
            try:
                if e.dxftype() == "MTEXT" and hasattr(e, "plain_text"):
                    raw = e.plain_text()
                else:
                    raw = e.dxf.text
            except Exception:
                try:
                    raw = e.dxf.text
                except Exception:
                    continue
            if not any(k in str(raw).upper() for k in keywords):
                continue
            try:
                ip = e.dxf.insert
                pts.append((ip.x, ip.y))
            except Exception:
                try:
                    ap = e.dxf.align_point
                    pts.append((ap.x, ap.y))
                except Exception:
                    pass
        return pts

    # Layer keywords whose LINE / polyline geometry bounds interior rooms.
    # Used to carve out ONLY the proposed toilet by polygonizing the enclosed
    # face that carries a toilet room tag. The store-perimeter PLANO boundary
    # is included because a corner toilet's outer edge is the store wall.
    TOILET_BOUNDARY_KEYWORDS = (
        "HATCH_OUTLINE", "WALL", "PARTITION", "DOOR",
        "PLANO-BOUNDARY", "PLANO-CARPET", "COL",
    )

    def _extract_proposed_toilet(self, store: Polygon) -> Optional[Polygon]:
        """Footprint of the proposed toilet — the only interior room that
        gets no flooring. Clinic / pickup / store areas stay tiled.

        There is no single closed polyline for the toilet, so we polygonize
        the room-boundary geometry (store perimeter + partitions + door) and
        keep the smallest enclosed face that contains a toilet room tag
        (see `_toilet_tag_points`). That isolates just the toilet (~25 sq ft)
        instead of the whole non-trade zone, which also holds the clinic /
        pickup / store areas the user wants tiled.

        Returns None when no toilet tag is found, so the caller can fall back
        to curtain-based detection.
        """
        tags = [(x, y) for x, y in self._toilet_tag_points()
                if store.buffer(50.0).contains(Point(x, y))]
        if not tags:
            return None

        from shapely.ops import polygonize, unary_union as _uu

        lines: List[LineString] = []
        for e in self._iter_virtual_entities(self.msp):
            etype = e.dxftype()
            if etype not in ("LINE", "LWPOLYLINE", "POLYLINE"):
                continue
            try:
                layer_up = e.dxf.layer.upper()
            except Exception:
                continue
            if not any(k in layer_up for k in self.TOILET_BOUNDARY_KEYWORDS):
                continue
            try:
                if etype == "LINE":
                    s, en = e.dxf.start, e.dxf.end
                    if (s.x - en.x) ** 2 + (s.y - en.y) ** 2 > 1.0:
                        lines.append(LineString([(s.x, s.y), (en.x, en.y)]))
                else:
                    pts = self._polyline_vertices(e, etype)
                    if pts and len(pts) >= 2:
                        lines.append(LineString(pts))
            except Exception:
                continue
        if not lines:
            return None

        try:
            rooms = [p for p in polygonize(_uu(lines)) if p.is_valid and p.area > 0]
        except Exception:
            return None
        if not rooms:
            return None

        store_area = store.area
        keep: List[Polygon] = []
        for tx, ty in tags:
            pt = Point(tx, ty)
            # Smallest enclosing face = the tightest room around the tag.
            # Bound the area so we never grab the whole store or a fixture sliver.
            cands = [p for p in rooms
                     if p.contains(pt)
                     and self.HATCH_MIN_PATH_AREA_MM2 < p.area < 0.25 * store_area]
            if cands:
                keep.append(min(cands, key=lambda p: p.area))
        if not keep:
            return None

        toilet = _uu(keep)
        try:
            toilet = toilet.intersection(store)
        except Exception:
            pass
        if toilet.is_empty:
            return None
        print(f"  ✓ Proposed toilet excluded: area {toilet.area * self.MM2_TO_SQFT:.1f} "
              f"sq ft (clinic / pickup / store remain tiled)")
        return toilet

    def _extract_store_polygon(self) -> Optional[Polygon]:
        best: Optional[Polygon] = None
        best_area = 0.0
        best_layer = ""

        for layer in self.HATCH_LAYERS:
            for entity_type in ("LWPOLYLINE", "POLYLINE"):
                for entity in self.msp.query(entity_type):
                    if entity.dxf.layer.upper() == layer.upper():
                        vertices = self._polyline_vertices(entity, entity_type)
                        if vertices is None or len(vertices) < 4:
                            continue
                        try:
                            poly = Polygon(vertices)
                            if not poly.is_valid:
                                poly = poly.buffer(0)
                            if poly.is_empty:
                                continue
                                
                            # Prevent picking a "Wall Thickness Ring" (ribbon)
                            # A true floor polygon covers most of its bounding box.
                            # A thin wall ring covers very little.
                            if poly.area < 0.25 * poly.envelope.area:
                                continue

                            if poly.area > best_area:
                                best_area = poly.area
                                best = poly
                                best_layer = layer
                        except Exception:
                            continue
            if best is not None:
                break

        if best is not None:
            print(f"  ✓ Store boundary found on '{best_layer}': "
                  f"area {best.area * self.MM2_TO_SQFT:.1f} sq ft")
            return best

        # 1b. HATCH entity fallback — many DXF files store the floor boundary
        # as the outer boundary of a HATCH entity (no LWPOLYLINE present).
        for layer in self.HATCH_LAYERS:
            for entity in self._iter_virtual_entities(self.msp):
                if entity.dxftype() != "HATCH":
                    continue
                try:
                    if entity.dxf.layer.upper() != layer.upper():
                        continue
                    if not hasattr(entity, "paths") or not entity.paths:
                        continue
                    # Collect all path polygons, pick the largest as the outer boundary.
                    path_polys: List[Polygon] = []
                    for path in entity.paths:
                        pts = self._hatch_path_to_vertices(path)
                        if not pts or len(pts) < 3:
                            continue
                        try:
                            poly = Polygon(pts)
                            if not poly.is_valid:
                                poly = poly.buffer(0)
                            if not poly.is_empty and poly.area > 0:
                                # Reject thin wall rings
                                if poly.area >= 0.25 * poly.envelope.area:
                                    path_polys.append(poly)
                        except Exception:
                            continue
                    if not path_polys:
                        continue
                    # Largest path = outer boundary
                    outer = max(path_polys, key=lambda p: p.area)
                    if outer.area > best_area:
                        best_area = outer.area
                        best = outer
                        best_layer = layer
                except Exception:
                    continue
            if best is not None:
                break

        if best is not None:
            print(f"  ✓ Store boundary from HATCH entity on '{best_layer}': "
                  f"area {best.area * self.MM2_TO_SQFT:.1f} sq ft")
            return best

        # 2. Algorithmic Fallback: Assemble floor from walls and facades
        from shapely.geometry import LineString
        from shapely.ops import polygonize, unary_union
        
        print(f"  ⚠ Explicit boundary not found on {self.HATCH_LAYERS}. Assembling floor...")
        import math
        arch_lines = []
        for entity in self._iter_virtual_entities(self.msp):
            etype = entity.dxftype()
            if etype in ("LINE", "LWPOLYLINE", "POLYLINE"):
                layer = entity.dxf.layer.upper()
                if any(kw in layer for kw in ("WALL", "OUTLINE", "CURTAIN", "PARTITION", "HATCH", "FLOOR-UNIT", "GLASS", "SHUTTER")):
                    pts = self._polyline_vertices(entity, etype)
                    if pts and len(pts) > 1:
                        arch_lines.append(LineString(pts))

        if arch_lines:
            try:
                from shapely.geometry import box
                uu = unary_union(arch_lines)
                
                # To seal unclosed floor plans without merging detached buildings,
                # we group lines mathematically by buffering to identify "buildings".
                fat = uu.buffer(200)
                geoms = fat.geoms if hasattr(fat, 'geoms') else [fat]
                
                # Drop a local sealing box across the bottom edge of EVERY distinct building
                # to flawlessly patch vertically inconsistent CAD drafting gaps!
                for bldg in geoms:
                    bminx, bminy, bmaxx, bmaxy = bldg.bounds
                    seal_box = box(bminx - 200, bminy - 500, bmaxx + 200, bminy + 500)
                    arch_lines.append(seal_box.boundary)
            except Exception:
                pass

        rooms = []
        if arch_lines:
            try:
                merged = unary_union(arch_lines)
                for poly in polygonize(merged):
                    if poly.is_valid and poly.area > 0:
                        rooms.append(poly)
            except Exception:
                pass
                
        if rooms:
            store_gross = unary_union(rooms)
            if not store_gross.is_valid:
                store_gross = store_gross.buffer(0)
            print(f"  ✓ Store boundary assembled from {len(rooms)} interior spaces: "
                  f"area {store_gross.area * self.MM2_TO_SQFT:.1f} sq ft")
            return store_gross
            
        print("  ✗ Failed to find or assemble store boundary.")
        return None

    # ─────────────────────────────────────────────────────────────
    # HATCH entity boundary extraction helpers
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _hatch_path_to_vertices(path) -> Optional[List[Tuple[float, float]]]:
        """Convert a HATCH boundary path (PolylinePath or EdgePath) to a
        flat list of (x, y) tuples suitable for Shapely Polygon construction.

        PolylinePath vertices carry an optional bulge value (arc segments).
        We ignore bulge here — the approximation is good enough for wall
        outlines whose curvature is either zero or very slight.

        EdgePath arcs are approximated by sampling at ≤10° intervals so
        curved door-frames render cleanly without excessive vertex counts.
        """
        try:
            if hasattr(path, "vertices") and path.vertices:
                pts = [(float(v[0]), float(v[1])) for v in path.vertices]
                return pts if len(pts) >= 3 else None

            if hasattr(path, "edges") and path.edges:
                pts: List[Tuple[float, float]] = []
                for edge in path.edges:
                    etype = getattr(edge, "EDGE_TYPE", "")
                    if etype == "LineEdge":
                        s, e = edge.start, edge.end
                        if not pts:
                            pts.append((float(s[0]), float(s[1])))
                        pts.append((float(e[0]), float(e[1])))
                    elif etype == "ArcEdge":
                        cx, cy = float(edge.center[0]), float(edge.center[1])
                        r = float(edge.radius)
                        a0 = math.radians(float(edge.start_angle))
                        a1 = math.radians(float(edge.end_angle))
                        ccw = getattr(edge, "ccw", True)
                        if ccw and a1 < a0:
                            a1 += 2.0 * math.pi
                        elif not ccw and a1 > a0:
                            a1 -= 2.0 * math.pi
                        n = max(8, int(abs(a1 - a0) / math.radians(10)))
                        for i in range(n + 1):
                            t = a0 + (a1 - a0) * i / n
                            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
                    # SplineEdge / EllipseEdge: skip (rare in floor plans)
                return pts if len(pts) >= 3 else None
        except Exception:
            pass
        return None

    def _extract_hatch_polygons(self) -> None:
        """Scan the source DXF for HATCH entities on HATCH_LAYERS and
        populate:

          self.hatch_outer_polygon  – the outer (largest) boundary polygon
          self.hatch_inner_polygons – inner hole polygons (cutouts in the hatch)
          self.hatch_wall_polygon   – outer minus inner = wall material area

        The outer boundary is the store perimeter drawn by the hatch fill.
        The inner holes are enclosed regions where no flooring goes (toilet,
        column pockets, doorways, etc.) as encoded in the source HATCH entity.
        The wall polygon = outer − union(inner) reproduces the cross-hatched
        wall section that appears in the original drawing.
        """
        found_layer = ""
        for entity in self._iter_virtual_entities(self.msp):
            all_path_polys: List[Polygon] = []

        
            if entity.dxftype() != "HATCH":
                continue
            try:
                if entity.dxf.layer.upper() not in [l.upper() for l in self.HATCH_LAYERS]:
                    continue
                if not hasattr(entity, "paths"):
                    continue
                for path in entity.paths:
                    pts = self._hatch_path_to_vertices(path)
                    if not pts or len(pts) < 3:
                        continue
                    try:
                        poly = Polygon(pts)
                        if not poly.is_valid:
                            poly = poly.buffer(0)
                        if not poly.is_empty and poly.area > 0:
                            all_path_polys.append(poly)
                    except Exception:
                        continue
            except Exception:
                continue

            if not all_path_polys:
                continue

            # Largest polygon = outer boundary; smaller ones = inner holes.
            all_path_polys.sort(key=lambda p: p.area, reverse=True)
            outer = all_path_polys[0]
            inner = [p for p in all_path_polys[1:] if outer.contains(p) or outer.intersects(p)]

            self.hatch_outer_polygon = outer
            self.hatch_inner_polygons = inner
            found_layer = entity.dxf.layer.upper()

            # Wall net polygon = outer − all inner holes.
            if inner:
                try:
                    inner_union = unary_union(inner)
                    wall = outer.difference(inner_union)
                    if not wall.is_empty and wall.is_valid and wall.area > 0:
                        self.hatch_wall_polygon = wall
                except Exception:
                    pass

            area_sqft = outer.area * self.MM2_TO_SQFT
            print(
                f"  ✓ HATCH entity on '{found_layer}': "
                f"outer {area_sqft:.1f} sqft, {len(inner)} inner hole(s)"
            )
            if inner:
                for idx, ip in enumerate(inner):
                    print(f"      inner[{idx}]: {ip.area * self.MM2_TO_SQFT:.1f} sqft")
            break

        if not found_layer:
            print(f"  ⚠ No HATCH entities found on {self.HATCH_LAYERS}")

    @staticmethod
    def _get_exterior_coords(store) -> list:
        if hasattr(store, 'exterior'):
            return list(store.exterior.coords)
        elif hasattr(store, 'geoms'):
            largest = max(store.geoms, key=lambda g: g.area)
            return list(largest.exterior.coords)
        return []

    @classmethod
    def _synthesize_facade_from_polygon(cls, store) -> Optional[LineString]:
        """Pick the longest edge of the store polygon exterior as the façade."""
        try:
            coords = cls._get_exterior_coords(store)
        except Exception:
            return None
        if len(coords) < 2:
            return None

        best_seg: Optional[LineString] = None
        best_len = 0.0
        for i in range(len(coords) - 1):
            p1, p2 = coords[i], coords[i + 1]
            length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if length > best_len:
                best_len = length
                best_seg = LineString([p1, p2])
        return best_seg

    def _synthesize_facade_from_bottom(self, store) -> Optional[LineString]:
        """
        Build the façade line from the polygon's bottom edges (minimum Y).

        Retail floor-plans are always drawn with the shop entry at the
        minimum-Y side. We scan the polygon exterior, keep the edges whose
        endpoints sit at the minimum Y (within FACADE_EDGE_TOLERANCE), take
        the full X-span of those edges, and return a single LineString from
        (x_lo, y_min) to (x_hi, y_min). Door-gap detection then sees the
        polygon's notch as a gap between the retained edge projections.
        """
        try:
            coords = self._get_exterior_coords(store)
        except Exception:
            return None
        if len(coords) < 2:
            return None

        minx, miny, maxx, maxy = store.bounds
        tol = self.FACADE_EDGE_TOLERANCE

        xs_on_bottom: List[float] = []
        total_bottom_len = 0.0
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            # Edge counts as "on the bottom" only if BOTH endpoints are at
            # y == miny (within tolerance). This excludes the vertical legs
            # that go up into a door notch.
            if abs(y1 - miny) <= tol and abs(y2 - miny) <= tol:
                xs_on_bottom.extend([x1, x2])
                total_bottom_len += math.hypot(x2 - x1, y2 - y1)

        # Require at least ~half the polygon width to be along the bottom,
        # otherwise the polygon is rotated / irregular and we should fall
        # back to the longest-edge synthesis.
        width = maxx - minx
        if not xs_on_bottom or total_bottom_len < 0.25 * width:
            return None

        x_lo, x_hi = min(xs_on_bottom), max(xs_on_bottom)
        if x_hi - x_lo < 100.0:
            return None
        return LineString([(x_lo, miny), (x_hi, miny)])

    # ─────────────────────────────────────────────────────────────
    # Step 2a: Façade (front glass) line — longest candidate
    # ─────────────────────────────────────────────────────────────
    def _extract_front_glass_line(self) -> Optional[LineString]:
        candidates: List[LineString] = []

        for layer in self.FRONT_GLASS_LAYERS:
            for entity_type in ("LINE", "LWPOLYLINE", "POLYLINE"):
                for entity in self.msp.query(entity_type):
                    if entity.dxf.layer != layer:
                        continue
                    ls = self._entity_to_linestring(entity, entity_type)
                    if ls is not None and ls.length > 100.0:
                        candidates.append(ls)

        if not candidates:
            print(f"  ⚠ Front glass line not found; skipping granite strip")
            return None

        longest = max(candidates, key=lambda g: g.length)
        print(f"  ✓ Façade line found: length {longest.length:.0f} mm")
        return longest

    # ─────────────────────────────────────────────────────────────
    # Step 2b: Granite strip = buffer the façade line inward by
    # granite_width, intersected with the store polygon.
    # ─────────────────────────────────────────────────────────────
    def _build_granite_strip(
        self, store: Polygon, facade: Optional[LineString]
    ) -> Optional[Polygon]:
        if facade is None or self.granite_width <= 0:
            return None

        # Buffer both sides by granite_width, clip to store. Whichever side
        # lies inside the store becomes the granite strip. For a façade line
        # that runs along the store edge this yields a rectangular strip of
        # width ≈ granite_width along that edge.
        buffered = facade.buffer(self.granite_width, cap_style=2, join_style=2)
        strip = buffered.intersection(store)

        if strip.is_empty:
            print(f"  ⚠ Granite strip is empty (façade line does not overlap store)")
            return None

        # Normalize to a single Polygon (union multi-parts)
        if isinstance(strip, MultiPolygon):
            strip = unary_union(strip)
            if isinstance(strip, MultiPolygon):
                strip = max(strip.geoms, key=lambda g: g.area)

        print(
            f"  ✓ Granite strip built: width {self.granite_width:.0f} mm, "
            f"area {strip.area * self.MM2_TO_SQFT:.2f} sq ft"
        )
        return strip


    # ─────────────────────────────────────────────────────────────
    # Toilet detection via I-LK CURTAIN / LENS_CURTAIN door marker.
    #
    # Logic: If there is an I-LK CURTAIN entity inside the store,
    # the toilet is the nearest closed polyline (room) to that
    # curtain door. No flooring is needed inside the toilet.
    # ─────────────────────────────────────────────────────────────
    def _extract_toilet_via_curtain(self, store: Polygon) -> Optional[Polygon]:
        curtain_points: List[Tuple[float, float]] = []
        
        # First, collect all curtain doors
        for entity in self._iter_virtual_entities(self.msp):
            layer = entity.dxf.layer
            etype = entity.dxftype()
            if any(layer.upper() == cl.upper() for cl in self.TOILET_CURTAIN_LAYERS):
                if etype in ("LINE", "LWPOLYLINE", "POLYLINE", "INSERT", "ARC", "CIRCLE"):
                    pt = self._entity_center(entity, etype)
                    if pt is not None:
                        # Ignore markers drawn way outside the store bounds
                        if store.contains(Point(pt)) or store.buffer(500).contains(Point(pt)):
                            curtain_points.append(pt)

        if not curtain_points:
            return None

        # To avoid falsely identifying clinic/trial rooms as toilets, we must locate
        # the text entity that marks the 'TOILET' (or 'WC', 'BATHROOM') and ONLY use
        # the curtain door closest to that label.
        toilet_label_pts = []
        for entity in self._iter_virtual_entities(self.msp):
            if entity.dxftype() in ("TEXT", "MTEXT"):
                text = str(entity.dxf.text).upper()
                if "TOILET" in text or "WC" in text or "BATHROOM" in text:
                    if hasattr(entity.dxf, "insert"):
                        pt = (entity.dxf.insert.x, entity.dxf.insert.y)
                    else:
                        pt = (entity.dxf.align_point.x, entity.dxf.align_point.y)
                    if store.contains(Point(pt)) or store.buffer(500).contains(Point(pt)):
                        toilet_label_pts.append(pt)
        
        # Filter curtain doors to only those nearest the toilet text(s).
        # We find the single closest curtain for each toilet label to avoid
        # accidentally catching clinic/trial room curtains.
        active_curtain_pts = []
        if toilet_label_pts:
            print(f"  ✓ Found toilet label(s) at {len(toilet_label_pts)} location(s)")
            for lpt in toilet_label_pts:
                closest_cpt = None
                min_dist = float('inf')
                for cpt in curtain_points:
                    dist = Point(cpt).distance(Point(lpt))
                    if dist < min_dist:
                        min_dist = dist
                        closest_cpt = cpt
                if closest_cpt is not None:
                    active_curtain_pts.append(closest_cpt)
                    print(f"  ✓ Valid toilet curtain door confirmed at ({closest_cpt[0]:.0f}, {closest_cpt[1]:.0f})")
        else:
            # Fallback: if no text found, we have to assume all curtains might be toilets
            # (which causes false positives, but is the only fallback)
            active_curtain_pts = curtain_points
            
        if not active_curtain_pts:
            print("  ⚠ Curtains found, but none are near any 'TOILET' text marker.")
            return None

        # 1. Existing closed polylines (from layers)
        candidates: List[Polygon] = []
        store_area = store.area
        for entity in self._iter_virtual_entities(self.msp):
            etype = entity.dxftype()
            if etype not in ("LWPOLYLINE", "POLYLINE"):
                continue
            
            pts = self._polyline_vertices(entity, etype)
            if pts is None or len(pts) < 3:
                continue
                
            try:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.area <= 0 or poly.area >= 0.25 * store_area:
                    continue
                candidates.append(poly)
            except Exception:
                pass

        # 2. Polygonize architectural lines to catch rooms defined by disjoint lines
        from shapely.geometry import LineString
        from shapely.ops import polygonize, unary_union
        
        arch_lines = []
        for entity in self._iter_virtual_entities(self.msp):
            etype = entity.dxftype()
            if etype in ("LINE", "LWPOLYLINE", "POLYLINE"):
                layer = entity.dxf.layer.upper()
                if any(kw in layer for kw in ("WALL", "OUTLINE", "CURTAIN", "PARTITION", "HATCH", "FLOOR-UNIT")):
                    pts = self._polyline_vertices(entity, etype)
                    if pts and len(pts) > 1:
                        arch_lines.append(LineString(pts))
        
        if arch_lines:
            try:
                merged = unary_union(arch_lines)
                for poly in polygonize(merged):
                    if poly.is_valid and 0 < poly.area < 0.25 * store_area:
                        candidates.append(poly)
            except Exception:
                pass

        toilet_polys: List[Polygon] = []
        
        # Priority 1: Exact room containing the 'TOILET' label
        if toilet_label_pts:
            for poly in candidates:
                for lpt in toilet_label_pts:
                    if poly.contains(Point(lpt)):
                        area_sqft = poly.area * self.MM2_TO_SQFT
                        print(f"    → Found toilet room via text: area {area_sqft:.1f} sqft")
                        toilet_polys.append(poly)
                        break

        # Priority 2: Fallback to proximity to the correct I-LK CURTAIN door
        if not toilet_polys and active_curtain_pts:
            for poly in candidates:
                for cp in active_curtain_pts:
                    dist = poly.distance(Point(cp))
                    if dist <= self.TOILET_CURTAIN_SEARCH_RADIUS:
                        area_sqft = poly.area * self.MM2_TO_SQFT
                        print(f"    → Found toilet room: area {area_sqft:.1f} sqft, "
                              f"distance {dist:.0f} mm from curtain")
                        toilet_polys.append(poly)
                        break

        if not toilet_polys:
            print(f"  ⚠ Failed to identify toilet polygon "
                  f"(checked {len(candidates)} candidate rooms)")
            return None

        union = unary_union(toilet_polys)
        area_sqft = union.area * self.MM2_TO_SQFT
        print(f"  ✓ Toilet footprint via curtain: {len(toilet_polys)} region(s), "
              f"area {area_sqft:.2f} sq ft")
        return union

    @staticmethod
    def _entity_center(entity, entity_type: str) -> Optional[Tuple[float, float]]:
        try:
            if entity_type == "LINE":
                s, e = entity.dxf.start, entity.dxf.end
                return ((s.x + e.x) / 2.0, (s.y + e.y) / 2.0)
            if entity_type == "INSERT":
                ip = entity.dxf.insert
                return (ip.x, ip.y)
            if entity_type in ("ARC", "CIRCLE"):
                c = entity.dxf.center
                return (c.x, c.y)
            if entity_type == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in entity.get_points()]
            else:  # POLYLINE
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            if not pts:
                return None
            return (
                sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts),
            )
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────
    # Step 3b: Bulkhead / Bulk-Headline exclusion zone
    # ─────────────────────────────────────────────────────────────
    def _extract_bulkhead(self, store: Polygon) -> Optional[Polygon]:
        """Find closed polygons on BULKHEAD_LAYERS and return their union.

        These represent the structural fascia strip above the entry door —
        no flooring tiles should be placed there.  The polygons are
        collected from both modelspace entities and virtual (block) entities.

        Each detected polyline is snapped to its axis-aligned bounding box
        so that slightly skewed CAD geometry (common in rotated block INSERTs)
        doesn't leave thin slivers after the boolean subtraction.
        """
        targets = [lyr.upper() for lyr in self.BULKHEAD_LAYERS]
        polys: List[Polygon] = []

        for entity in self._iter_virtual_entities(self.msp):
            try:
                layer_up = entity.dxf.layer.upper()
            except Exception:
                continue
            if not any(t == layer_up or t in layer_up for t in targets):
                continue

            etype = entity.dxftype()
            try:
                pts = None
                is_closed = False

                if etype == "LWPOLYLINE":
                    pts = [(p[0], p[1]) for p in entity.get_points()]
                    is_closed = bool(getattr(entity, "is_closed", False))
                elif etype == "POLYLINE":
                    pts = [
                        (v.dxf.location.x, v.dxf.location.y)
                        for v in entity.vertices
                    ]
                    is_closed = bool(getattr(entity, "is_closed", False))

                if pts is None or not is_closed or len(pts) < 3:
                    continue

                # Snap to axis-aligned bounding box.  Bulkheads are
                # structural rectangular strips; the raw polyline may be
                # slightly skewed after a block INSERT rotation, causing
                # boolean-difference slivers.  Using the bbox guarantees a
                # clean rectangle and complete subtraction.
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                poly = box(min(xs), min(ys), max(xs), max(ys))

                if poly.area > 0:
                    polys.append(poly)

            except Exception:
                continue

        if not polys:
            return None

        union = unary_union(polys)
        # Keep only the part that overlaps the store footprint
        clipped = union.intersection(store)
        if clipped.is_empty:
            return None

        if not isinstance(clipped, Polygon):
            # MultiPolygon or GeometryCollection — keep it as-is for subtraction
            if hasattr(clipped, "area") and clipped.area > 0:
                area_sqft = clipped.area / 92903.04
                print(f"  ✓ Bulkhead exclusion zone: {len(polys)} shape(s), "
                      f"area {area_sqft:.2f} sq ft")
                return clipped
            return None

        area_sqft = clipped.area / 92903.04
        print(f"  ✓ Bulkhead exclusion zone: {len(polys)} shape(s), "
              f"area {area_sqft:.2f} sq ft")
        return clipped

    # ─────────────────────────────────────────────────────────────
    # Door lintel exclusion — closed polygons on LINTEL_LAYERS.
    # The lintel strip at a door head (e.g. the toilet door) is not
    # floored, so it is subtracted from the net polygon.
    # ─────────────────────────────────────────────────────────────
    def _extract_lintels(self, store: Polygon) -> Optional[Polygon]:
        targets = [lyr.upper() for lyr in self.LINTEL_LAYERS]
        polys: List[Polygon] = []
        for entity in self._iter_virtual_entities(self.msp):
            try:
                layer_up = entity.dxf.layer.upper()
            except Exception:
                continue
            if not any(t == layer_up or t in layer_up for t in targets):
                continue
            etype = entity.dxftype()
            if etype not in ("LWPOLYLINE", "POLYLINE"):
                continue
            pts = self._polyline_vertices(entity, etype)
            if not pts or len(pts) < 3:
                continue
            try:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.area > 0:
                    polys.append(poly)
            except Exception:
                continue

        if not polys:
            return None

        union = unary_union(polys)
        clipped = union.intersection(store)
        if clipped.is_empty:
            return None
        if not clipped.is_valid:
            clipped = clipped.buffer(0)
        print(f"  ✓ Door lintel exclusion: {len(polys)} shape(s), "
              f"area {clipped.area * self.MM2_TO_SQFT:.2f} sq ft")
        return clipped

    # ─────────────────────────────────────────────────────────────
    # Step 4: Net polygon = store − granite − toilet − bulkhead − lintels
    # ─────────────────────────────────────────────────────────────
    def _compute_net_polygon(
        self,
        store: Polygon,
        granite: Optional[Polygon],
        toilet: Optional[Polygon],
        bulkhead: Optional[Polygon] = None,
        columns: Optional[Polygon] = None,
        lintels: Optional[Polygon] = None,
    ) -> Polygon:
        from shapely.ops import unary_union as uu
        from shapely.geometry.base import BaseGeometry

        def _to_geom(val):
            """Coerce lists/arrays to a single Shapely geometry, or return None."""
            if val is None:
                return None
            if isinstance(val, BaseGeometry):
                return val
            if isinstance(val, (list, tuple)) and len(val) > 0:
                return uu([v for v in val if isinstance(v, BaseGeometry)])
            return None

        net = store
        for cutout in [granite, toilet, bulkhead, columns, lintels]:
            geom = _to_geom(cutout)
            if geom is not None and not geom.is_empty:
                net = net.difference(geom)

        if net.is_empty:
            raise RuntimeError("Net flooring polygon is empty after subtractions")

        # Fix any small topology issues from the boolean ops
        if not net.is_valid:
            net = net.buffer(0)

        return net

    # ─────────────────────────────────────────────────────────────
    # Step 3C: Structural column / pillar detection.
    # LINE entities on column layers are polygonized; closed
    # LWPOLYLINE/POLYLINE entities are used directly.
    # ─────────────────────────────────────────────────────────────
    def _columns_from_ctx(self, store: Polygon) -> Optional[Polygon]:
        from shapely.ops import polygonize, unary_union as uu

        segments = (self.ctx or {}).get("cols", [])
        if not segments:
            return None

        lines = [LineString([s[0][:2], s[1][:2]]) for s in segments]
        merged = uu(lines)
        merged = merged.buffer(0.1).buffer(-0.1)   # close micro-gaps
        polys = list(polygonize(merged))

        if not polys:
            return None

        union = uu(polys)
        clipped = union.intersection(store)
        if clipped.is_empty:
            return None
        if not clipped.is_valid:
            clipped = clipped.buffer(0)

        area_sqft = clipped.area * self.MM2_TO_SQFT
        print(f"  ✓ Column footprints (ctx): {len(polys)} shape(s), area {area_sqft:.2f} sq ft")
        return clipped

    def _extract_columns(self, store: Polygon) -> Optional[Polygon]:
        from shapely.ops import polygonize, unary_union as uu

        targets = [l.upper() for l in self.COLUMN_LAYERS]
        lines: List[LineString] = []
        closed_polys: List[Polygon] = []

        for entity in self._iter_virtual_entities(self.msp):
            try:
                layer_up = entity.dxf.layer.upper()
            except Exception:
                continue
            if not any(t == layer_up or t in layer_up for t in targets):
                continue

            etype = entity.dxftype()
            try:
                if etype == "LINE":
                    s, e = entity.dxf.start, entity.dxf.end
                    if math.hypot(e.x - s.x, e.y - s.y) > 1.0:
                        lines.append(LineString([(s.x, s.y), (e.x, e.y)]))
                elif etype in ("LWPOLYLINE", "POLYLINE"):
                    pts = self._polyline_vertices(entity, etype)
                    if pts and len(pts) >= 3:
                        is_closed = bool(getattr(entity, "is_closed", False))
                        if is_closed:
                            poly = Polygon(pts)
                            if not poly.is_valid:
                                poly = poly.buffer(0)
                            if poly.area > 0:
                                closed_polys.append(poly)
                        else:
                            if len(pts) >= 2:
                                lines.append(LineString(pts))
            except Exception:
                continue

        polys: List[Polygon] = list(closed_polys)

        if lines:
            try:
                merged = uu(lines)
                for p in polygonize(merged):
                    if p.is_valid and p.area > 0:
                        polys.append(p)
            except Exception:
                pass

        if not polys:
            return None

        union = uu(polys)
        clipped = union.intersection(store)
        if clipped.is_empty:
            return None
        if not clipped.is_valid:
            clipped = clipped.buffer(0)

        area_sqft = clipped.area * self.MM2_TO_SQFT
        print(f"  ✓ Column footprints: {len(polys)} shape(s), area {area_sqft:.2f} sq ft")
        return clipped

    # ─────────────────────────────────────────────────────────────
    # Task 2: Tile grid origin = midpoint of the door opening on the
    #         BOTTOM (min-Y) edge of the store polygon.
    #
    # In Lenskart retail plans the shop entry is ALWAYS at the
    # bottom (minimum-Y side) of the drawing. We scan the store
    # polygon's bottom edges, find the gap (door opening) between
    # them, and place the origin at the gap's midpoint.
    # ─────────────────────────────────────────────────────────────
    def _compute_tile_origin(
        self, facade: Optional[LineString]
    ) -> Tuple[Optional[Tuple[float, float]], str, float]:
        if self.store_polygon is None:
            print("  ⚠ No store polygon; cannot compute tile origin")
            return None, "", 0.0

        # Isolate the primary retail space for entrance scanning
        # If there are multiple buildings/islands, we only care about the doors on the main piece
        main_poly = self.store_polygon
        if hasattr(self.store_polygon, 'geoms') and len(self.store_polygon.geoms) > 1:
            main_poly = max(self.store_polygon.geoms, key=lambda g: g.area)

        # The entry is at the bottom (min-Y) of the store polygon.
        minx, miny, maxx, maxy = main_poly.bounds
        tol = self.FACADE_EDGE_TOLERANCE

        # Collect all polygon exterior edges that lie on the bottom (min-Y)
        coords = self._get_exterior_coords(main_poly)
        bottom_segments: List[Tuple[float, float]] = []  # (x_lo, x_hi) intervals
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            # Both endpoints must be at min-Y (within tolerance)
            if abs(y1 - miny) <= tol and abs(y2 - miny) <= tol:
                seg_len = math.hypot(x2 - x1, y2 - y1)
                if seg_len > 1.0:
                    bottom_segments.append((min(x1, x2), max(x1, x2)))

        if not bottom_segments:
            # No clear bottom edge — fall back to façade midpoint
            if facade is not None:
                (ax0, ay0) = facade.coords[0]
                (ax1, ay1) = facade.coords[-1]
                dx, dy = ax1 - ax0, ay1 - ay0
                axis_len = math.hypot(dx, dy)
                if axis_len > 1e-6:
                    ux, uy = dx / axis_len, dy / axis_len
                    return self._facade_midpoint_origin(facade, ax0, ay0, ux, uy, axis_len)
            print("  ⚠ No bottom edge found; cannot compute tile origin")
            return None, "", 0.0

        # Merge overlapping bottom-edge intervals
        merged = self._merge_intervals(bottom_segments)

        # The door gap = complement of merged intervals within [minx, maxx]
        # But more precisely, within the x-span of the bottom edges themselves
        full_x_lo = minx
        full_x_hi = maxx

        gaps: List[Tuple[float, float]] = []
        cursor = full_x_lo
        for lo, hi in sorted(merged, key=lambda x: x[0]):
            if lo > cursor + 1.0:
                gaps.append((cursor, lo))
            cursor = max(cursor, hi)
        if full_x_hi > cursor + 1.0:
            gaps.append((cursor, full_x_hi))

        # Pick the widest gap wider than min_door_width
        if gaps:
            widest = max(gaps, key=lambda g: g[1] - g[0])
            gap_width = widest[1] - widest[0]
            if gap_width >= self.min_door_width:
                mid_x = 0.5 * (widest[0] + widest[1])
                origin = (mid_x, miny)
                print(f"  ✓ Entry door gap detected at bottom: "
                      f"x={widest[0]:.0f}..{widest[1]:.0f}, "
                      f"width={gap_width:.0f} mm, origin=({mid_x:.0f}, {miny:.0f})")
                return origin, "door_gap", gap_width

        # No qualifying gap — use the midpoint of the bottom edge span of
        # the main retail floor! If there are scattered buildings on the layout,
        # we isolate the biggest contiguous piece to find the true central doors.
        if hasattr(self.store_polygon, 'geoms'):
            largest = max(self.store_polygon.geoms, key=lambda g: g.area)
            lx1, _, lx2, _ = largest.bounds
            mid_x = 0.5 * (lx1 + lx2)
        else:
            all_x = [x for lo, hi in merged for x in (lo, hi)]
            if not all_x:
                mid_x = minx + (maxx - minx) / 2
            else:
                mid_x = 0.5 * (min(all_x) + max(all_x))
        origin = (mid_x, miny)
        print(f"  ⚠ No door gap found on bottom edge; "
              f"using bottom midpoint ({mid_x:.0f}, {miny:.0f})")
        return origin, "bottom_midpoint", 0.0

    def _facade_midpoint_origin(
        self,
        facade: LineString,
        ax0: float,
        ay0: float,
        ux: float,
        uy: float,
        axis_len: float,
    ) -> Tuple[Tuple[float, float], str, float]:
        mid_s = axis_len / 2.0
        origin = (ax0 + mid_s * ux, ay0 + mid_s * uy)
        return origin, "facade_midpoint", 0.0

    # Layers that explicitly mark the façade door opening, in priority order.
    FACADE_DOOR_LAYERS = ["PLANO-DOOR", "A-GLASS DOOR", "LK-DOOR"]

    def _facade_door_center(
        self, facade: Optional[LineString]
    ) -> Optional[Tuple[float, float]]:
        """Centre point of the façade door opening (the gap in the
        Glass-FrontGlazing). Returns the midpoint of the widest door segment
        that lies ON the façade line, or None if no façade door is marked.

        Door segments are taken from FACADE_DOOR_LAYERS — first the explicit
        PLANO-DOOR marker, then glass-door / LK-DOOR — keeping only segments
        whose endpoints sit on the façade axis (so legend swatches and
        interior doors elsewhere are ignored).
        """
        if facade is None:
            return None
        try:
            (fx0, fy0) = facade.coords[0]
            (fx1, fy1) = facade.coords[-1]
        except Exception:
            return None
        dx, dy = fx1 - fx0, fy1 - fy0
        flen = math.hypot(dx, dy)
        if flen < 1e-6:
            return None
        ux, uy = dx / flen, dy / flen
        tol = 300.0   # max perpendicular distance from the façade line (mm)

        for layer in self.FACADE_DOOR_LAYERS:
            best: Optional[Tuple[float, float]] = None
            best_len = 0.0
            for entity in self.msp.query("LINE LWPOLYLINE POLYLINE"):
                try:
                    if entity.dxf.layer.upper() != layer.upper():
                        continue
                except Exception:
                    continue
                seg = self._entity_to_linestring(entity, entity.dxftype())
                if seg is None or seg.length < 1.0:
                    continue
                try:
                    (x0, y0) = seg.coords[0]
                    (x1, y1) = seg.coords[-1]
                except Exception:
                    continue
                # Perpendicular distance of each endpoint to the façade line.
                d0 = abs(-(x0 - fx0) * uy + (y0 - fy0) * ux)
                d1 = abs(-(x1 - fx0) * uy + (y1 - fy0) * ux)
                if d0 > tol or d1 > tol:
                    continue
                if seg.length > best_len:
                    best_len = seg.length
                    best = (0.5 * (x0 + x1), 0.5 * (y0 + y1))
            if best is not None:
                print(f"  ✓ Façade door centre from '{layer}': "
                      f"({best[0]:.0f}, {best[1]:.0f}), opening {best_len:.0f} mm")
                return best
        return None

    def _find_user_start_line(self) -> Optional[LineString]:
        """Search the source DXF for a user-drawn reference and return a
        single anchoring LineString.

        Two flavours are handled:
          1. **Line / open polyline** — used directly; the longest match
             wins so stray stubs on the same layer don't take over.
          2. **Strip of closed rectangles** (e.g. blue boxes representing
             an entry threshold) — the bounding envelope of the union is
             computed and the *inner-facing* edge (the side toward the
             store centroid) is returned as the reference line. This lets
             the user mark the entry as a row of shapes and have the floor
             tiles start neatly past the inner edge."""
        # Build the layer search list in priority order. The first layer
        # (by priority) that contains any geometry is used; remaining
        # layers are not searched. This prevents noisy architectural
        # layers (e.g. Steps) from swamping a deliberate user marker.
        layer_priority: List[str] = []
        if self.start_line_layer:
            layer_priority.append(self.start_line_layer.upper())
        for lyr in self.START_LINE_LAYERS:
            up = lyr.upper()
            if up not in layer_priority:
                layer_priority.append(up)
        if not layer_priority:
            return None

        def _collect_on_layer(target: str):
            """Gather lines and closed polygons whose layer matches `target`
            (exact match or case-insensitive substring)."""
            _lines: List[LineString] = []
            _polys: List[Polygon] = []
            for entity in self._iter_virtual_entities(self.msp):
                try:
                    layer_up = entity.dxf.layer.upper()
                except Exception:
                    continue
                if target != layer_up and target not in layer_up:
                    continue
                etype = entity.dxftype()
                try:
                    if etype == "LINE":
                        s = entity.dxf.start
                        e = entity.dxf.end
                        line = LineString([(s.x, s.y), (e.x, e.y)])
                        if line.length > 0:
                            _lines.append(line)
                    elif etype == "LWPOLYLINE":
                        pts = [(p[0], p[1]) for p in entity.get_points()]
                        is_closed = bool(getattr(entity, "is_closed", False))
                        if is_closed and len(pts) >= 3:
                            try:
                                poly = Polygon(pts)
                                if not poly.is_valid:
                                    poly = poly.buffer(0)
                                if isinstance(poly, Polygon) and poly.area > 0:
                                    _polys.append(poly)
                            except Exception:
                                pass
                        elif len(pts) >= 2:
                            line = LineString(pts)
                            if line.length > 0:
                                _lines.append(line)
                    elif etype == "POLYLINE":
                        pts = [
                            (v.dxf.location.x, v.dxf.location.y)
                            for v in entity.vertices
                        ]
                        is_closed = bool(getattr(entity, "is_closed", False))
                        if is_closed and len(pts) >= 3:
                            try:
                                poly = Polygon(pts)
                                if not poly.is_valid:
                                    poly = poly.buffer(0)
                                if isinstance(poly, Polygon) and poly.area > 0:
                                    _polys.append(poly)
                            except Exception:
                                pass
                        elif len(pts) >= 2:
                            line = LineString(pts)
                            if line.length > 0:
                                _lines.append(line)
                except Exception:
                    continue
            return _lines, _polys

        # Search each layer in priority order — first hit wins.
        lines: List[LineString] = []
        polys: List[Polygon] = []
        matched_layer = ""
        for target in layer_priority:
            _l, _p = _collect_on_layer(target)
            if _l or _p:
                lines, polys = _l, _p
                matched_layer = target
                break

        # Closed rectangles take precedence — they're a more deliberate
        # entry-line marker than a free-floating line.
        if polys and self.store_polygon is not None:
            try:
                union = unary_union(polys)
                inner_edge = self._inner_edge_of_strip(
                    union, self.store_polygon
                )
                if inner_edge is not None:
                    print(
                        f"  ✓ User entry-line strip detected "
                        f"({len(polys)} closed shape(s)); using its "
                        f"inner-facing edge as reference "
                        f"(length {inner_edge.length:.0f} mm)"
                    )
                    return inner_edge
            except Exception:
                pass

        if lines:
            best = max(lines, key=lambda l: l.length)
            print(
                f"  ✓ User-drawn start line detected on a configured layer "
                f"(length {best.length:.0f} mm)"
            )
            return best

        return None

    @staticmethod
    def _inner_edge_of_strip(
        geom, store: Polygon
    ) -> Optional[LineString]:
        """Given one or more closed polygons (a threshold strip), return the
        edge segment whose outward normal most closely points toward the
        store centroid — i.e. the side that faces into the store interior.

        Unlike the old envelope approach, this walks each polygon's actual
        exterior ring so sloped edges and multi-polygon door gaps are
        preserved."""
        try:
            sx, sy = store.centroid.x, store.centroid.y
        except Exception:
            return None

        # Collect all individual polygons from the geometry.
        if isinstance(geom, Polygon):
            polygons = [geom]
        elif isinstance(geom, MultiPolygon):
            polygons = list(geom.geoms)
        elif hasattr(geom, "geoms"):
            polygons = [g for g in geom.geoms if isinstance(g, Polygon)]
        else:
            return None

        # Centroid of the full strip (for direction computation).
        try:
            gcx, gcy = geom.centroid.x, geom.centroid.y
        except Exception:
            gcx = sum(p.centroid.x for p in polygons) / len(polygons)
            gcy = sum(p.centroid.y for p in polygons) / len(polygons)

        # Direction from strip centroid toward store centroid.
        dir_x, dir_y = sx - gcx, sy - gcy
        dir_len = math.hypot(dir_x, dir_y)
        if dir_len < 1e-6:
            return None
        dir_x, dir_y = dir_x / dir_len, dir_y / dir_len

        # Walk every exterior edge of every polygon; score each by how
        # closely its outward normal aligns with the store direction.
        # When two edges have nearly the same dot (within tolerance),
        # prefer the longer one — this ensures the dominant inner edge
        # wins over a shorter sloped variant.
        DOT_TOLERANCE = 0.01
        best_edge: Optional[LineString] = None
        best_dot = -math.inf
        best_len = 0.0
        for poly in polygons:
            coords = list(poly.exterior.coords)
            for i in range(len(coords) - 1):
                x1, y1 = coords[i]
                x2, y2 = coords[i + 1]
                seg_len = math.hypot(x2 - x1, y2 - y1)
                if seg_len < 1.0:
                    continue
                # Outward normal (for CCW winding, right-hand perpendicular).
                ex, ey = (x2 - x1) / seg_len, (y2 - y1) / seg_len
                # Right-hand perp = (ey, -ex); this is outward for CCW.
                nx, ny = ey, -ex
                # Verify it actually points outward (away from polygon centroid).
                # If (midpoint + n) is closer to centroid than (midpoint − n),
                # then n points inward → flip it.
                mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                pcx, pcy = poly.centroid.x, poly.centroid.y
                if (mx + nx - pcx) ** 2 + (my + ny - pcy) ** 2 < \
                   (mx - nx - pcx) ** 2 + (my - ny - pcy) ** 2:
                    nx, ny = -nx, -ny  # n was inward → flip to outward
                dot = nx * dir_x + ny * dir_y
                # Pick this edge if it has a clearly better dot, or if
                # the dot is effectively the same but this edge is longer.
                if dot > best_dot + DOT_TOLERANCE or \
                   (dot > best_dot - DOT_TOLERANCE and seg_len > best_len):
                    best_dot = dot
                    best_len = seg_len
                    best_edge = LineString([(x1, y1), (x2, y2)])
        return best_edge

    def _origin_from_user_line(
        self,
        line: LineString,
        store: Optional[Polygon],
    ) -> Tuple[float, float]:
        """Place the tile origin so the diamond's bottom corner snaps to the
        midpoint of `line`, with the diamond sitting on whichever side of
        the line faces the store centroid."""
        # Midpoint of the user line (handles polylines correctly thanks to
        # `interpolate(0.5, normalized=True)`).
        mid = line.interpolate(0.5, normalized=True)
        mx, my = mid.x, mid.y

        # Direction of the line (endpoint − startpoint), then perpendicular.
        coords = list(line.coords)
        dx = coords[-1][0] - coords[0][0]
        dy = coords[-1][1] - coords[0][1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            # Degenerate: zero-length line. Fall back to placing centre at
            # the midpoint with no offset — the granite shift logic, if
            # invoked elsewhere, would still produce a usable result.
            return (mx, my)
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux  # left perpendicular

        # Pick the perpendicular that points INTO the store centroid.
        if store is not None:
            try:
                cx, cy = store.centroid.x, store.centroid.y
                if (cx - mx) * nx + (cy - my) * ny < 0:
                    nx, ny = -nx, -ny
            except Exception:
                pass

        # Inward extent of the rotated diamond (centre → corner). For
        # θ = 45° this is `half · √2`; for θ = 0° it's just `half`.
        ang = math.radians(self.tile_rotation_deg)
        half = self.tile_size / 2.0
        diamond_extent = half * (abs(math.cos(ang)) + abs(math.sin(ang)))

        new_origin = (mx + diamond_extent * nx, my + diamond_extent * ny)
        print(
            f"  ✓ Tile origin from user line: midpoint ({mx:.0f}, {my:.0f}) "
            f"+ {diamond_extent:.0f} mm inward → "
            f"({new_origin[0]:.0f}, {new_origin[1]:.0f})"
        )
        return new_origin

    def _recenter_origin_on_facade(
        self,
        origin: Optional[Tuple[float, float]],
        facade: Optional[LineString],
        store: Optional[Polygon],
    ) -> Optional[Tuple[float, float]]:
        """Re-project the tile origin onto the centre of the store along
        the façade-parallel axis. The Y-component (perpendicular to the
        façade) is preserved — only the along-façade coordinate moves to
        the geometric middle of the store's footprint.

        This makes the start diamond sit on the floorplan's centreline
        regardless of where the actual door opening is drawn.
        """
        if origin is None or facade is None or store is None:
            return origin

        # 1) Façade unit-direction.
        try:
            (fx0, fy0) = facade.coords[0]
            (fx1, fy1) = facade.coords[-1]
        except Exception:
            return origin
        dx, dy = fx1 - fx0, fy1 - fy0
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return origin
        ux, uy = dx / L, dy / L

        # 2) Project every store-polygon exterior vertex onto the façade
        # axis (anchored at facade.coords[0]) and take the midpoint of the
        # extents — that is the centreline of the store along the façade.
        try:
            ext_coords = list(store.exterior.coords)
        except Exception:
            return origin
        s_min, s_max = math.inf, -math.inf
        for px, py in ext_coords:
            s = (px - fx0) * ux + (py - fy0) * uy
            if s < s_min:
                s_min = s
            if s > s_max:
                s_max = s
        if not (math.isfinite(s_min) and math.isfinite(s_max)) or s_max <= s_min:
            return origin
        s_mid = 0.5 * (s_min + s_max)

        # 3) Origin's existing along-façade coord, then replace it with s_mid.
        ox, oy = origin
        s_origin = (ox - fx0) * ux + (oy - fy0) * uy
        delta = s_mid - s_origin
        new_origin = (ox + delta * ux, oy + delta * uy)
        print(
            f"  ✓ Tile origin re-centred on store width: shift {delta:+.0f} mm "
            f"along façade → ({new_origin[0]:.0f}, {new_origin[1]:.0f})"
        )
        return new_origin

    def _shift_origin_past_bulkhead(
        self,
        origin: Optional[Tuple[float, float]],
        facade: Optional[LineString],
        store: Optional[Polygon],
    ) -> Optional[Tuple[float, float]]:
        """Move the tile origin from the façade line into the floor by
        `bulkhead_depth + tile_diamond_inward_extent`, so the start tile's
        bottom corner sits flush with the inner edge of the bulkhead strip.

        The bulkhead depth is measured as the perpendicular distance from
        the façade line to the farthest inner edge of the bulkhead polygon.
        If no bulkhead exists, falls back to diamond_extent only.

        Inward direction is the façade-perpendicular pointing toward the
        store centroid. Works for any façade orientation.
        """
        if origin is None or facade is None or store is None:
            return origin

        # 1) Façade direction → perpendicular candidate.
        try:
            (fx0, fy0) = facade.coords[0]
            (fx1, fy1) = facade.coords[-1]
        except Exception:
            return origin
        dx, dy = fx1 - fx0, fy1 - fy0
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return origin
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux  # left perpendicular

        # 2) Pick the perpendicular that points INTO the store.
        try:
            cx, cy = store.centroid.x, store.centroid.y
        except Exception:
            return origin
        ox, oy = origin
        if (cx - ox) * nx + (cy - oy) * ny < 0:
            nx, ny = -nx, -ny

        # 3) Compute bulkhead depth along the inward perpendicular.
        #    Project all bulkhead boundary coords onto (nx, ny) relative
        #    to the façade line to find how far the bulkhead extends inward.
        bulkhead_depth = 0.0
        if self.bulkhead_polygon is not None:
            try:
                # Get all boundary coordinates of the bulkhead union.
                bh = self.bulkhead_polygon
                coords = []
                if hasattr(bh, 'geoms'):
                    for g in bh.geoms:
                        coords.extend(g.exterior.coords)
                else:
                    coords = list(bh.exterior.coords)

                # Signed projection of each coord onto the inward normal,
                # relative to the origin (which sits on the façade line).
                projections = [
                    (px - ox) * nx + (py - oy) * ny
                    for px, py in coords
                ]
                if projections:
                    bulkhead_depth = max(0.0, max(projections))
            except Exception:
                pass

        # 4) Inward extent of the rotated tile from its centre to its
        # leading corner: half · (|cos θ| + |sin θ|). For θ=45° this is
        # `half · √2`; for θ=0° it's just `half`.
        ang = math.radians(self.tile_rotation_deg)
        half = self.tile_size / 2.0
        diamond_extent = half * (abs(math.cos(ang)) + abs(math.sin(ang)))

        shift = bulkhead_depth + diamond_extent
        new_origin = (ox + shift * nx, oy + shift * ny)
        print(
            f"  ✓ Tile origin shifted {shift:.0f} mm inward "
            f"(bulkhead {bulkhead_depth:.0f} + diamond {diamond_extent:.0f}) "
            f"→ ({new_origin[0]:.0f}, {new_origin[1]:.0f})"
        )
        return new_origin

    def _collect_facade_segments(self) -> List[LineString]:
        """
        Return every LINE/LWPOLYLINE/POLYLINE segment on the façade layers.

        When those layers are empty (common in un-standardized DXFs), fall
        back to the store polygon's exterior edges that lie on the façade
        axis. That fallback is what makes door-gap detection work on the
        polygon's actual door notch.
        """
        segments: List[LineString] = []
        for layer in self.FRONT_GLASS_LAYERS:
            for entity_type in ("LINE", "LWPOLYLINE", "POLYLINE"):
                for entity in self.msp.query(entity_type):
                    if entity.dxf.layer != layer:
                        continue
                    if entity_type == "LINE":
                        try:
                            s = entity.dxf.start
                            e = entity.dxf.end
                            if math.hypot(e.x - s.x, e.y - s.y) > 1.0:
                                segments.append(LineString([(s.x, s.y), (e.x, e.y)]))
                        except Exception:
                            continue
                    else:
                        pts = self._polyline_vertices(entity, entity_type)
                        if pts is None or len(pts) < 2:
                            continue
                        # Split polyline into individual edges so gaps between
                        # non-contiguous vertices are NOT auto-bridged.
                        for i in range(len(pts) - 1):
                            if math.hypot(
                                pts[i + 1][0] - pts[i][0],
                                pts[i + 1][1] - pts[i][1],
                            ) > 1.0:
                                segments.append(LineString([pts[i], pts[i + 1]]))

        if segments:
            return segments

        # Fallback — use the store polygon's edges that sit on the façade axis.
        if self.store_polygon is None or self.front_glass is None:
            return segments

        try:
            (ax0, ay0) = self.front_glass.coords[0]
            (ax1, ay1) = self.front_glass.coords[-1]
        except Exception:
            return segments
        dx, dy = ax1 - ax0, ay1 - ay0
        axis_len = math.hypot(dx, dy)
        if axis_len < 1e-6:
            return segments
        ux, uy = dx / axis_len, dy / axis_len

        tol = self.FACADE_EDGE_TOLERANCE
        try:
            coords = self._get_exterior_coords(self.store_polygon)
        except Exception:
            return segments

        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            # Perpendicular distance of each endpoint to the façade line
            d1 = abs(-(x1 - ax0) * uy + (y1 - ay0) * ux)
            d2 = abs(-(x2 - ax0) * uy + (y2 - ay0) * ux)
            if d1 <= tol and d2 <= tol:
                if math.hypot(x2 - x1, y2 - y1) > 1.0:
                    segments.append(LineString([(x1, y1), (x2, y2)]))
        return segments

    @staticmethod
    def _merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if not intervals:
            return []
        sorted_iv = sorted(intervals, key=lambda x: x[0])
        merged = [sorted_iv[0]]
        for lo, hi in sorted_iv[1:]:
            last_lo, last_hi = merged[-1]
            if lo <= last_hi:
                merged[-1] = (last_lo, max(last_hi, hi))
            else:
                merged.append((lo, hi))
        return merged

    # ─────────────────────────────────────────────────────────────
    # Task 4: Tile area from the net polygon, with wastage factor.
    # ─────────────────────────────────────────────────────────────
    def _compute_tile_area(
        self, net: Optional[Polygon], wastage_factor: float
    ) -> Dict[str, float]:
        if net is None or net.is_empty:
            return {}

        area_mm2 = float(net.area)
        area_sqft = area_mm2 * self.MM2_TO_SQFT
        area_sqm = area_mm2 * self.MM2_TO_SQM

        wastage_sqft = area_sqft * wastage_factor
        wastage_sqm = area_sqm * wastage_factor

        return {
            "area_mm2": area_mm2,
            "area_sqft": area_sqft,
            "area_sqm": area_sqm,
            "wastage_factor": wastage_factor,
            "wastage_sqft": wastage_sqft,
            "wastage_sqm": wastage_sqm,
            "order_sqft": area_sqft + wastage_sqft,
            "order_sqm": area_sqm + wastage_sqm,
        }

    # ─────────────────────────────────────────────────────────────
    # Task 3: 600×600 mm tile grid radiating from the tile origin,
    # rotated by tile_rotation_deg relative to the façade, and clipped
    # to the net polygon. Produces per-tile polygons in a checkerboard
    # (grey / white) plus a distinct "start" tile at the origin —
    # matching the sample flooring layout style.
    # ─────────────────────────────────────────────────────────────
    def _compute_tile_grid(
        self,
        net: Optional[Polygon],
        origin: Optional[Tuple[float, float]],
        facade: Optional[LineString],
    ) -> List[Dict[str, Any]]:
        if net is None or origin is None:
            return []
        if net.is_empty:
            return []

        # Start from the façade direction (u), then rotate by tile_rotation_deg
        # so the tile grid sits at 45° (sample-style) without rotating the
        # whole coordinate system.
        if facade is not None:
            (fx0, fy0) = facade.coords[0]
            (fx1, fy1) = facade.coords[-1]
            dx, dy = fx1 - fx0, fy1 - fy0
            axis_len = math.hypot(dx, dy)
            if axis_len < 1e-6:
                base_ux, base_uy = 1.0, 0.0
            else:
                base_ux, base_uy = dx / axis_len, dy / axis_len
        else:
            base_ux, base_uy = 1.0, 0.0

        ang = math.radians(self.tile_rotation_deg)
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        ux = base_ux * cos_a - base_uy * sin_a
        uy = base_ux * sin_a + base_uy * cos_a
        vx, vy = -uy, ux  # perpendicular (90° CCW)

        self.tile_u_axis = (ux, uy)
        self.tile_v_axis = (vx, vy)

        ox, oy = origin
        ts = self.tile_size
        half = ts / 2.0

        # Project bbox corners into (u, v) to size the grid. Pad by one tile
        # so edge tiles have room to clip against the polygon boundary.
        minx, miny, maxx, maxy = net.bounds
        u_vals, v_vals = [], []
        for px, py in [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]:
            rx, ry = px - ox, py - oy
            u_vals.append(rx * ux + ry * uy)
            v_vals.append(rx * vx + ry * vy)
        u_min, u_max = min(u_vals), max(u_vals)
        v_min, v_max = min(v_vals), max(v_vals)
        k_u_lo = math.floor(u_min / ts) - 1
        k_u_hi = math.ceil(u_max / ts) + 1
        k_v_lo = math.floor(v_min / ts) - 1
        k_v_hi = math.ceil(v_max / ts) + 1

        tiles: List[Dict[str, Any]] = []

        for i in range(k_u_lo, k_u_hi + 1):
            for j in range(k_v_lo, k_v_hi + 1):
                cu = i * ts
                cv = j * ts
                cx = ox + cu * ux + cv * vx
                cy = oy + cu * uy + cv * vy

                corners = []
                for du, dv in ((-half, -half), (half, -half), (half, half), (-half, half)):
                    corners.append((cx + du * ux + dv * vx, cy + du * uy + dv * vy))
                tile_square = Polygon(corners)
                if not tile_square.is_valid:
                    tile_square = tile_square.buffer(0)

                try:
                    clipped = tile_square.intersection(net)
                except Exception:
                    continue
                if clipped.is_empty:
                    continue

                parts: List[Polygon] = []
                if isinstance(clipped, Polygon):
                    parts = [clipped]
                elif isinstance(clipped, MultiPolygon):
                    parts = list(clipped.geoms)
                elif hasattr(clipped, "geoms"):
                    parts = [g for g in clipped.geoms if isinstance(g, Polygon)]

                parts = [p for p in parts if p.area >= self.TILE_MIN_FRAGMENT_AREA]
                if not parts:
                    continue

                # Checkerboard: (i + j) even → grey, odd → white.
                # Tile at (0, 0) is the START tile, painted regardless of parity.
                is_start = (i == 0 and j == 0)
                color_key = "start" if is_start else ("grey" if (i + j) % 2 == 0 else "white")

                for part in parts:
                    tiles.append({
                        "polygon": part,
                        "color": color_key,
                        "index": (i, j),
                    })

        print(
            f"  ✓ Tile grid built: {len(tiles)} tiles "
            f"(tile {ts:.0f} mm, rotation {self.tile_rotation_deg:.0f}°, "
            f"origin ({ox:.1f}, {oy:.1f}))"
        )
        return tiles

    # ─────────────────────────────────────────────────────────────
    # Zone-based flooring: each zone gets its own tile/hatch type;
    # leftover net area uses default_flooring_type.
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _iter_polys(geom):
        if geom is None or geom.is_empty:
            return []
        if geom.geom_type == "Polygon":
            return [geom]
        if hasattr(geom, "geoms"):
            return [g for g in geom.geoms if g.geom_type == "Polygon" and not g.is_empty]
        return []

    def _render_region(self, region, ft, origin, facade, tiles, hatch_zones):
        """Render one region with a flooring type into `tiles`/`hatch_zones`."""
        kind = (ft.get("kind") or "tile").lower()
        if kind == "skip":
            return
        if kind == "hatch":
            for part in self._iter_polys(region):
                hatch_zones.append((part, ft))
            return
        # tile: reuse the diamond/checkerboard grid with this type's size/rotation
        old_ts, old_rot = self.tile_size, self.tile_rotation_deg
        self.tile_size = float(ft.get("length_mm") or ft.get("tile_size_mm") or self.tile_size)
        self.tile_rotation_deg = float(ft.get("rotation_deg", self.tile_rotation_deg))
        try:
            for part in self._iter_polys(region):
                tiles.extend(self._compute_tile_grid(part, origin, facade))
        finally:
            self.tile_size, self.tile_rotation_deg = old_ts, old_rot

    def _compute_zone_flooring(self, zones, net, origin, facade):
        """Per-zone tile/hatch; leftover net area uses default_flooring_type."""
        from shapely.geometry import Polygon as _Poly
        from shapely.ops import unary_union as _uu

        tiles: List[Dict[str, Any]] = []
        hatch_zones: List[Tuple[Any, Dict[str, Any]]] = []
        if net is None or net.is_empty:
            return tiles, hatch_zones

        covered = []
        for z in zones:
            try:
                region = _Poly(z["polygon"]).intersection(net)
            except Exception:
                continue
            if region.is_empty:
                continue
            covered.append(region)
            self._render_region(region, z.get("flooring_type") or {}, origin, facade, tiles, hatch_zones)

        dft = (self.ctx or {}).get("default_flooring_type")
        if dft:
            remainder = net.difference(_uu(covered)) if covered else net
            if not remainder.is_empty:
                self._render_region(remainder, dft, origin, facade, tiles, hatch_zones)

        n_hatch = len(hatch_zones)
        print(f"  ✓ Zone flooring: {len(tiles)} tiles + {n_hatch} hatch region(s) "
              f"across {len(zones)} zone(s)")
        return tiles, hatch_zones

    @staticmethod
    def _tile_polygons_to_segments(
        tiles: List[Dict[str, Any]],
    ) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Derive line segments from tile polygon outlines (backwards compat)."""
        segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for tile in tiles:
            poly = tile.get("polygon")
            if poly is None or poly.is_empty:
                continue
            coords = list(poly.exterior.coords)
            for k in range(len(coords) - 1):
                p1 = (coords[k][0], coords[k][1])
                p2 = (coords[k + 1][0], coords[k + 1][1])
                if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) >= 1.0:
                    segments.append((p1, p2))
        return segments

    @classmethod
    def _clip_line_to_polygon(
        cls, line: LineString, poly: Polygon
    ) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        try:
            clipped = line.intersection(poly)
        except Exception:
            return []
        if clipped.is_empty:
            return []

        parts: List[LineString] = []
        if isinstance(clipped, LineString):
            parts = [clipped]
        elif isinstance(clipped, MultiLineString):
            parts = list(clipped.geoms)
        elif hasattr(clipped, "geoms"):
            for g in clipped.geoms:
                if isinstance(g, LineString):
                    parts.append(g)

        out: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for seg in parts:
            if seg.length < cls.GRID_MIN_SEGMENT_LENGTH:
                continue
            coords = list(seg.coords)
            for i in range(len(coords) - 1):
                p1 = (coords[i][0], coords[i][1])
                p2 = (coords[i + 1][0], coords[i + 1][1])
                if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) >= cls.GRID_MIN_SEGMENT_LENGTH:
                    out.append((p1, p2))
        return out

    # ─────────────────────────────────────────────────────────────
    # Output: write the flooring DXF with filled tile polygons,
    # hatched granite strip, toilet cut, net floor outline, and a
    # highlighted start tile — styled to match the sample flooring plan.
    # ─────────────────────────────────────────────────────────────
    def save_output(self, output_path: str) -> str:
        if self.net_polygon is None:
            raise RuntimeError("Call extract() before save_output()")

        out_doc = ezdxf.new(dxfversion="R2018")
        out_msp = out_doc.modelspace()

        # Create layers with meaningful default colors so the DXF renders
        # sensibly straight out of AutoCAD / any viewer.
        layer_colors = {
            self.OUTPUT_LAYER_TILE_GREY:      self.ACI_TILE_GREY,
            self.OUTPUT_LAYER_TILE_WHITE:     self.ACI_TILE_WHITE,
            self.OUTPUT_LAYER_TILE_START:     self.ACI_TILE_START,
            self.OUTPUT_LAYER_TILE_OUTLINE:   self.ACI_TILE_OUTLINE,
            self.OUTPUT_LAYER_FLOOR:          self.ACI_TILE_OUTLINE,
            self.OUTPUT_LAYER_TOILET:         self.ACI_TILE_OUTLINE,
            self.OUTPUT_LAYER_COLUMN:         self.ACI_COLUMN,
            self.OUTPUT_LAYER_GRANITE:        self.ACI_GRANITE,
            self.OUTPUT_LAYER_TILE_ORIGIN:    self.ACI_ORIGIN_MARKER,
            self.OUTPUT_LAYER_ANNOT:          self.ACI_ANNOT,
            self.OUTPUT_LAYER_DIM:            self.ACI_DIM,
            self.OUTPUT_LAYER_HATCH_OUTLINE:  self.ACI_HATCH_OUTLINE,
            self.OUTPUT_LAYER_HATCH_WALL:     self.ACI_HATCH_WALL,
            self.OUTPUT_LAYER_ZONE_HATCH:     self.ACI_ZONE_HATCH,
        }
        for layer, aci in layer_colors.items():
            if layer not in out_doc.layers:
                out_doc.layers.add(layer, color=aci)
            else:
                out_doc.layers.get(layer).color = aci

        # 1) Tile polygons FIRST so granite / outlines overdraw neatly on top.
        self._draw_tile_polygons(out_msp, self.tile_polygons)

        # 1b) Per-zone hatch fills (wood / parquet / concrete, etc.)
        for zpoly, ft in self.hatch_zones:
            self._draw_zone_hatch(out_msp, zpoly, ft)

        # 2) Net floor exterior outline only — drawing interior holes here would
        #    duplicate the toilet/column boundary lines drawn in steps 4–5.
        print("********DEBUG")
        print("net polygon", self.net_polygon)
        self._draw_polygon_exterior(out_msp, self.net_polygon, self.OUTPUT_LAYER_FLOOR)

        # 3) Granite strip with ANSI37 crosshatch — "JET BLACK GRANITE" look.
        if self.granite_strip is not None:
            self._draw_granite(out_msp, self.granite_strip, self.OUTPUT_LAYER_GRANITE)

        
        # 4) Toilet cut outline so it reads as an excluded region.
        if self.toilet_polygon is not None:
            for t in self.toilet_polygon:
                print("toilet", t)
                self._draw_polygon_exterior(out_msp, t, self.OUTPUT_LAYER_TOILET)

        # 5) Column cut (solid grey hatch)
        if self.column_polygon is not None:
            print("column", self.column_polygon)
            self._draw_solid_hatch(out_msp, self.column_polygon,
                                   self.OUTPUT_LAYER_COLUMN, self.ACI_COLUMN)

        # 5a) Copy the entire wall frame (HATCH fill + outlines on
        #     HATCH_OUTLINE / BOH / curtains) verbatim using ezdxf's
        #     Importer — the only reliable way to do cross-document HATCH
        #     copy without position drift. Block-aware: also pulls in any
        #     INSERT whose block definition contains wall-layer entities.
        self._import_wall_frame(out_doc)

        # 5b) Copy any HATCH / outline entities still left on non-mirrored
        #     layers (no-op when MIRROR_WALL_LAYERS covers all wall layers).
        if set(l.upper() for l in self.HATCH_SOURCE_LAYERS) - set(
            l.upper() for l in self.MIRROR_WALL_LAYERS
        ):
            self._copy_source_hatches(out_msp)

        # 6) Start-tile crosshair marker at origin.
        if self.tile_origin is not None:
            self._draw_tile_origin_marker(
                out_msp, self.tile_origin, self.OUTPUT_LAYER_TILE_ORIGIN
            )

        # 6b) Annotations: directional arrows, leader-text labels, dimensions.
        self._draw_annotations(out_msp)

        # 7) Copy the source DXF's wall/outline entities into the output LAST
        #    so they correctly render ON TOP of the solid tile hatches!
        self._copy_context_from_source(out_doc, out_msp)

        # 7b) Strip unwanted layers (e.g. LK-PARTITION / LK-PART-HATCH) that
        #     leak in nested inside imported wall-frame blocks.
        self._strip_output_layers(out_doc)

        # 7c) Strip specific layer geometry within world-space regions
        #     (e.g. the stray LK-PARTITION chamfer at CLINIC-1's corner).
        self._strip_output_regions(out_doc)

        # 8) Set the active viewport to zoom-extents so the drawing is visible
        #    immediately when opened in AutoCAD / any DXF viewer — without the
        #    user having to type ZOOM → E or scroll to find the content.
        self._set_zoom_extents(out_doc)

        out_doc.saveas(output_path)
        print(f"  ✓ Saved flooring layout DXF: {output_path}")
        return output_path

    # Layers removed from the output DXF after all imports/copies. These leak
    # in nested inside imported wall-frame block definitions but are not
    # wanted in the flooring layout.
    OUTPUT_STRIP_LAYERS = ["LK-PART-HATCH", "PHE-FITTING", "LK-FF&E WALL", "LK-FF&E FLOOR"]

    def _strip_output_layers(self, out_doc) -> None:
        """Delete every entity on OUTPUT_STRIP_LAYERS from all layouts and
        block definitions in the output document, then drop the now-unused
        layer table entries. Touches nothing else."""
        targets = {l.upper() for l in self.OUTPUT_STRIP_LAYERS}
        if not targets:
            return
        removed = 0
        # Iterating out_doc.blocks covers modelspace, paperspace, and every
        # imported block definition (where these layers actually live).
        for block in out_doc.blocks:
            to_delete = []
            for e in block:
                try:
                    if e.dxf.layer.upper() in targets:
                        to_delete.append(e)
                except Exception:
                    continue
            for e in to_delete:
                try:
                    block.delete_entity(e)
                    removed += 1
                except Exception:
                    pass
        # Remove the (now unreferenced) layer table entries.
        for layer_name in list(self.OUTPUT_STRIP_LAYERS):
            try:
                if layer_name in out_doc.layers:
                    out_doc.layers.remove(layer_name)
            except Exception:
                pass
        if removed:
            print(f"  ✓ Stripped {removed} entit{'y' if removed == 1 else 'ies'} "
                  f"on {self.OUTPUT_STRIP_LAYERS}")

    # (layer, (minx, miny, maxx, maxy)) world-space boxes to clear from the
    # output. Only entities on `layer` whose geometry lies FULLY inside the box
    # are removed — the rest of the layer is kept. Used to delete the stray
    # LK-PARTITION chamfer at the bottom-right corner of CLINIC-1.
    OUTPUT_STRIP_REGIONS = [
        ("LK-PARTITION", (8900.0, 17150.0, 9360.0, 17860.0)),   # CLINIC-1 corner
        ("LK-PARTITION", (2960.0, 9350.0, 3280.0, 18200.0)),    # left A-COL strip
        # Right-side off-store noise (legend / other-sheet content). The real
        # store walls all sit at x <= ~9574, so anything past x = 10000 on
        # these layers is stray and removed.
        ("HATCH_OUTLINE", (10000.0, -1.0e9, 1.0e12, 1.0e12)),
        ("LN-ELECTRICAL", (10000.0, -1.0e9, 1.0e12, 1.0e12)),
    ]

    def _strip_output_regions(self, out_doc) -> None:
        """Remove entities on a layer that fall fully within a world-space box,
        including block-nested entities (their coordinates are transformed to
        world via the owning INSERT). Leaves the rest of the layer intact."""
        if not self.OUTPUT_STRIP_REGIONS:
            return

        def _world_pts(entity, m):
            etype = entity.dxftype()
            try:
                if etype == "LINE":
                    raw = [(entity.dxf.start.x, entity.dxf.start.y),
                           (entity.dxf.end.x, entity.dxf.end.y)]
                elif etype == "LWPOLYLINE":
                    raw = [(p[0], p[1]) for p in entity.get_points()]
                elif etype == "POLYLINE":
                    raw = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                elif etype == "INSERT":
                    ip = entity.dxf.insert
                    raw = [(ip.x, ip.y)]
                elif etype in ("CIRCLE", "ARC"):
                    c = entity.dxf.center
                    r = float(entity.dxf.radius)
                    raw = [(c.x - r, c.y - r), (c.x + r, c.y + r)]
                elif etype == "HATCH":
                    raw = []
                    for p in entity.paths:
                        vs = self._hatch_path_to_vertices(p)
                        if vs:
                            raw.extend(vs)
                    if not raw:
                        return None
                else:
                    return None
            except Exception:
                return None
            if m is None:
                return raw
            out = []
            for x, y in raw:
                v = m.transform((x, y, 0.0))
                out.append((v.x, v.y))
            return out

        def _fully_in(pts, bbox):
            x0, y0, x1, y1 = bbox
            return bool(pts) and all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in pts)

        msp = out_doc.modelspace()
        # Modelspace entities use identity transform; each INSERT's block uses
        # that INSERT's matrix. Blocks here are singly-instanced, so deleting
        # from a block definition is safe.
        work = [(msp, None)]
        for ins in msp.query("INSERT"):
            blk = out_doc.blocks.get(ins.dxf.name)
            if blk is not None:
                try:
                    work.append((blk, ins.matrix44()))
                except Exception:
                    work.append((blk, None))

        removed = 0
        for container, m in work:
            to_delete = []
            for e in container:
                try:
                    layer_up = e.dxf.layer.upper()
                except Exception:
                    continue
                for layer, bbox in self.OUTPUT_STRIP_REGIONS:
                    if layer_up != layer.upper():
                        continue
                    pts = _world_pts(e, m)
                    if pts is not None and _fully_in(pts, bbox):
                        to_delete.append(e)
                        break
            for e in to_delete:
                try:
                    container.delete_entity(e)
                    removed += 1
                except Exception:
                    pass
        if removed:
            print(f"  ✓ Stripped {removed} region entit{'y' if removed == 1 else 'ies'} "
                  f"on {[r[0] for r in self.OUTPUT_STRIP_REGIONS]}")

    def _set_zoom_extents(self, out_doc) -> None:
        """Configure the DXF so the drawing opens centred and fully visible
        across all viewers.  Three complementary methods are applied:
          1. DXF header $EXTMIN/$EXTMAX — the authoritative drawing extents
             used by AutoCAD and virtually every third-party viewer.
          2. *Active VPORT table entry — sets the initial viewport window.
          3. ezdxf.zoom.extents() — ezdxf's own recalculation pass.
        """
        if self.store_polygon is None:
            return

        minx, miny, maxx, maxy = self.store_polygon.bounds
        pad = max((maxx - minx), (maxy - miny)) * 0.15   # 15 % margin
        x0, y0 = minx - pad, miny - pad
        x1, y1 = maxx + pad, maxy + pad
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        w  = x1 - x0
        h  = y1 - y0

        # 1. Header variables — most reliable across all viewers
        try:
            out_doc.header["$EXTMIN"] = (x0, y0, 0.0)
            out_doc.header["$EXTMAX"] = (x1, y1, 0.0)
            out_doc.header["$LIMMIN"] = (x0, y0)
            out_doc.header["$LIMMAX"] = (x1, y1)
        except Exception:
            pass

        # 2. *Active VPORT table entry
        try:
            vp_table = out_doc.viewports
            entries = vp_table.get_config("*Active")
            if entries:
                vp = entries[0]
            else:
                vp = vp_table.new("*Active")
            vp.dxf.center = (cx, cy)
            vp.dxf.height = max(h, w)
        except Exception:
            pass

        # 3. ezdxf zoom helper (best-effort)
        try:
            from ezdxf import zoom as _zoom
            _zoom.extents(out_doc.modelspace())
        except Exception:
            pass

    # ── Minimum area (mm²) for a hatch boundary path to be considered real.
    # Paths smaller than this are degenerate/decorative fragments — skip them.
    HATCH_MIN_PATH_AREA_MM2 = 5000.0   # ≈ 70 mm × 70 mm

    def _copy_source_hatches(self, out_msp) -> None:
        """Optimised two-pass hatch extraction.

        Pass 1 — direct modelspace scan
            Iterates ``self.msp`` directly.  All HATCH entities that live
            at the top level of modelspace are found here, coordinates are
            already in world space.

        Pass 2 — INSERT virtual-entity expansion
            For each INSERT in modelspace, calls ``virtual_entities()`` to
            expand the block contents with the INSERT's full transformation
            (position, rotation, scale) already applied.  This catches wall
            and partition hatches that are nested inside block definitions.

        Deduplication
            Virtual entity expansion can yield the same underlying block
            entity multiple times when a block is inserted more than once.
            Each candidate path is deduplicated by a rounded-coordinate
            signature (1 mm resolution on the first four vertices).

        Per-path filtering (not per-entity)
            Each boundary path is checked individually so that a single HATCH
            entity that partly overlaps the store boundary is still included.
            Filters applied per path:
              • bounding-box intersection with store polygon + 1 000 mm buffer
              • minimum area ≥ HATCH_MIN_PATH_AREA_MM2

        Output per accepted HATCH entity
              1. One LWPOLYLINE per boundary path  → FLOORING_HATCH_OUTLINE
              2. One HATCH entity (with original pattern / fill copied)
                 → FLOORING_HATCH_WALL
        """
        if self.hatches is not None:
            self._draw_hatch_wall_fallback(out_msp)
            return

        # 300 mm buffer so perimeter-wall hatches that straddle the edge are caught.
        store_buf = self.store_polygon.buffer(300)
        bx0, by0, bx1, by1 = store_buf.bounds

        # strict interior check using net floor outline buffered inset by 10mm
        interior_check_poly = None
        if self.net_polygon is not None and not self.net_polygon.is_empty:
            try:
                interior_check_poly = self.net_polygon.buffer(-10.0)
            except Exception:
                pass

        seen_path_sigs: set = set()   # deduplication signatures
        total_hatches  = 0
        total_outlines = 0

        def _path_sig(pts: List[Tuple[float, float]]) -> tuple:
            """Fast dedup key: first 4 rounded vertices."""
            return tuple((round(x), round(y)) for x, y in pts[:4])

        def _accept_path(pts: List[Tuple[float, float]]) -> bool:
            """Return True when the path should appear in the output."""
            if not pts or len(pts) < 3:
                return False
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            # Fast axis-aligned bounds rejection before Shapely call
            if max(xs) < bx0 or min(xs) > bx1 or max(ys) < by0 or min(ys) > by1:
                return False
            try:
                path_poly = Polygon(pts)
                if not store_buf.intersects(path_poly):
                    return False
                if interior_check_poly is not None and path_poly.intersects(interior_check_poly):
                    return False
                if path_poly.area < self.HATCH_MIN_PATH_AREA_MM2:
                    return False
            except Exception:
                if not store_buf.intersects(box(min(xs), min(ys), max(xs), max(ys))):
                    return False
                if interior_check_poly is not None and box(min(xs), min(ys), max(xs), max(ys)).intersects(interior_check_poly):
                    return False
            return True

        # Only accept HATCH entities from wall-boundary layers, and skip
        # layers already mirrored verbatim by _mirror_hatch_outline_layer()
        # to avoid drawing the same wall data twice.
        _mirror_upper = {l.upper() for l in self.MIRROR_WALL_LAYERS}
        _hatch_src_upper = {
            l.upper() for l in self.HATCH_SOURCE_LAYERS
        } - _mirror_upper

        def _process_hatch(entity) -> None:
            nonlocal total_hatches, total_outlines
            if entity.dxftype() != "HATCH":
                return
            try:
                # ── Layer filter: skip furniture / glass / decorative hatches ──
                if entity.dxf.layer.upper() not in _hatch_src_upper:
                    return
                if not hasattr(entity, "paths"):
                    return

                # ── collect valid boundary paths ─────────────────────────────
                valid: List[Tuple[List, int]] = []   # (pts, flags)
                for i, path in enumerate(entity.paths):
                    pts = self._hatch_path_to_vertices(path)
                    if not _accept_path(pts):
                        continue
                    sig = _path_sig(pts)
                    if sig in seen_path_sigs:
                        continue
                    seen_path_sigs.add(sig)
                    flags = getattr(path, "path_type_flags", 1 if i == 0 else 0)
                    valid.append((pts, flags))

                if not valid:
                    return

                total_hatches += 1

                # ── 1. draw net-polyline outlines ─────────────────────────────
                for pts, _ in valid:
                    try:
                        out_msp.add_lwpolyline(
                            pts, close=True,
                            dxfattribs={
                                "layer": self.OUTPUT_LAYER_HATCH_OUTLINE,
                                "color": self.ACI_HATCH_OUTLINE,
                            },
                        )
                        total_outlines += 1
                    except Exception:
                        pass

                # ── 2. copy the original hatch directly to preserve its pattern line styling perfectly ──────
                try:
                    src_color = int(entity.dxf.color)
                    if src_color in (0, 256):
                        src_color = self.ACI_HATCH_WALL
                except Exception:
                    src_color = self.ACI_HATCH_WALL

                try:
                    copied = entity.copy()
                    copied.dxf.layer = self.OUTPUT_LAYER_HATCH_WALL
                    copied.dxf.color = src_color
                    # Clean up any potential handles and associations to prevent ODA failures
                    try:
                        copied.discard_reactor_handle()
                    except Exception:
                        pass
                    try:
                        copied.discard_extension_dict()
                    except Exception:
                        pass
                    try:
                        copied.remove_association()
                    except Exception:
                        pass
                    try:
                        copied.remove_dependencies()
                    except Exception:
                        pass

                    # Reconstruct paths of copied hatch to include only valid/filtered ones
                    copied.paths.clear()
                    for pts, flags in valid:
                        copied.paths.add_polyline_path(pts, is_closed=True, flags=flags)

                    out_msp.add_entity(copied)
                except Exception:
                    pass

            except Exception:
                pass

        # ── Pass 1: direct modelspace entities ───────────────────────────────
        for entity in self.msp:
            _process_hatch(entity)

        # ── Pass 2: block-nested via INSERT virtual-entity expansion ──────────
        for insert in self.msp.query("INSERT"):
            try:
                for ve in insert.virtual_entities():
                    _process_hatch(ve)
            except Exception:
                continue

        # ── Pass 3: copy BOH / clinic / wall outlines (LWPOLYLINE / LINE) ───
        # Skip layers already mirrored verbatim above to avoid duplicates.
        boh_upper = {l.upper() for l in self.BOH_OUTLINE_LAYERS} - _mirror_upper
        outline_count = 0
        for entity in self._iter_virtual_entities(self.msp):
            etype = entity.dxftype()
            if etype not in ("LWPOLYLINE", "POLYLINE", "LINE"):
                continue
            if entity.dxf.layer.upper() not in boh_upper:
                continue

            if etype == "LINE":
                s = entity.dxf.start
                e = entity.dxf.end
                # Bounding-box check against store
                if max(s.x, e.x) < bx0 or min(s.x, e.x) > bx1 or max(s.y, e.y) < by0 or min(s.y, e.y) > by1:
                    continue
                # Precise intersection check with store buffer
                try:
                    ls = LineString([(s.x, s.y), (e.x, e.y)])
                    if not ls.intersects(store_buf):
                        continue
                    if interior_check_poly is not None and ls.intersects(interior_check_poly):
                        continue
                except Exception:
                    pass
                try:
                    out_msp.add_line(
                        (s.x, s.y), (e.x, e.y),
                        dxfattribs={
                            "layer": self.OUTPUT_LAYER_HATCH_OUTLINE,
                            "color": self.ACI_HATCH_OUTLINE,
                        },
                    )
                    outline_count += 1
                except Exception:
                    pass
            else:
                pts = self._polyline_vertices(entity, etype)
                if not pts or len(pts) < 2:
                    continue
                # Bounding-box check against store
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if max(xs) < bx0 or min(xs) > bx1 or max(ys) < by0 or min(ys) > by1:
                    continue
                # Precise intersection check with store buffer
                try:
                    ls = LineString(pts)
                    if not ls.intersects(store_buf):
                        continue
                    if interior_check_poly is not None and ls.intersects(interior_check_poly):
                        continue
                except Exception:
                    pass
                try:
                    out_msp.add_lwpolyline(
                        pts, close=entity.is_closed,
                        dxfattribs={
                            "layer": self.OUTPUT_LAYER_HATCH_OUTLINE,
                            "color": self.ACI_HATCH_OUTLINE,
                        },
                    )
                    outline_count += 1
                except Exception:
                    pass
        if outline_count > 0:
            print(f"  ✓ Wall/BOH/clinic outlines: {outline_count} line(s)/polyline(s) → {self.OUTPUT_LAYER_HATCH_OUTLINE}")

        if total_outlines > 0:
            print(
                f"  ✓ Hatch outlines: {total_outlines} boundary path(s) "
                f"from {total_hatches} HATCH entit{'y' if total_hatches == 1 else 'ies'} "
                f"→ {self.OUTPUT_LAYER_HATCH_OUTLINE} / {self.OUTPUT_LAYER_HATCH_WALL}"
            )
        else:
            print("  ⚠ No hatch outlines found within layout — using fallback")
            self._draw_hatch_wall_fallback(out_msp)

    def get_entity_position(self, entity):
        dxftype = entity.dxftype()
        
        if dxftype == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            return ((s.x + e.x) / 2, (s.y + e.y) / 2)
        
        elif dxftype == "LWPOLYLINE":
            pts = list(entity.get_points("xy"))
            return self.centroid(pts)
        
        elif dxftype == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            return self.centroid(pts)
        
        elif dxftype in ("CIRCLE", "ARC"):
            c = entity.dxf.center
            return (c.x, c.y)
        
        elif dxftype == "DIMENSION":
            p = entity.dxf.defpoint
            return (p.x, p.y)
        
        else:
            try:
                p = entity.dxf.insert
                return (p.x, p.y)
            except Exception:
                return None
        

    def centroid(self, points):
        x = sum(p[0] for p in points) / len(points)
        y = sum(p[1] for p in points) / len(points)
        return (x, y)
    
    def point_to_segment_distance(self, pos, a, b):
        px, py = pos[0], pos[1]
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return self.distance(pos, a)
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        closest = (ax + t * dx, ay + t * dy)
        return self.distance(pos, closest)

    def distance(self, p1, p2):
        return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)

    def in_outline(self, pos, thresh=100):
        outline = self.ctx["store-outline"][0]

        if self.point_in_polygon(pos, outline):
            return True

        n = len(outline)
        for i in range(n):
            a = outline[i]
            b = outline[(i + 1) % n]
            # print("dist: ", self.point_to_segment_distance(pos, a, b))
            if self.point_to_segment_distance(pos, a, b) <= thresh:
                return True

        return False
    
    def point_in_polygon(self, point, polygon):
        """Ray casting algorithm. polygon is a list of (x, y) tuples."""
        x, y = point[0], point[1]
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i][0], polygon[i][1]
            xj, yj = polygon[j][0], polygon[j][1]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _import_wall_frame(self, out_doc) -> None:
        from shapely.geometry import Point, Polygon
        from shapely.wkt import loads
        """Copy the entire wall frame from input to output, verbatim.

        Uses ``ezdxf.addons.Importer`` — the supported tool for cross-
        document entity copy. Importer preserves world coordinates,
        layer/linetype/style definitions, and handles HATCH entities
        correctly (the earlier positioning bug came from manual
        ``entity.copy()`` across documents, not from the source data).

        Scope: ALL modelspace entities on layers in ``MIRROR_WALL_LAYERS``
        (default: ``HATCH_OUTLINE``). That includes:
          • the HATCH entity (the diagonal cross-hatched wall fill)
          • LWPOLYLINE / POLYLINE outlines
          • LINE / ARC segments
          • anything else on that layer

        The frame appears on its original layer name in the output.
        """
        from ezdxf.addons import Importer

        mirror_layers = {l.upper() for l in self.MIRROR_WALL_LAYERS}
        exclude_insert_layers = {
            l.upper() for l in self.MIRROR_EXCLUDE_INSERT_LAYERS
        }
        exclude_block_names = {
            n.upper() for n in self.MIRROR_EXCLUDE_INSERT_BLOCK_NAMES
        }
        if not mirror_layers:
            return

        entities = []
        seen_handles: set = set()
        excluded_count = 0

        # Pass 1: top-level modelspace entities on wall layers.
        for entity in self.msp:
            try:
                if entity.dxf.layer.upper() in mirror_layers:
                    h = entity.dxf.handle
                    pos = self.get_entity_position(entity)
                    # print(pos)
                    if h not in seen_handles and self.in_outline(pos):
                        entities.append(entity)
                        seen_handles.add(h)
            except Exception:
                continue

        # Pass 2: INSERT entities whose BLOCK DEFINITION contains entities
        # on a wall layer. Importing the INSERT pulls in the full block
        # definition (Importer re-resolves block references across docs),
        # so any HATCH/polyline nested inside comes along with original
        # coordinates intact. INSERTs on layers listed in
        # MIRROR_EXCLUDE_INSERT_LAYERS are filtered out here so unrelated
        # block-encapsulated drawings don't leak into the flooring DXF.
        for insert in self.msp.query("INSERT"):
            try:
                h = insert.dxf.handle
                if h in seen_handles:
                    continue
                if insert.dxf.layer.upper() in exclude_insert_layers:
                    excluded_count += 1
                    continue
                block_name = insert.dxf.name
                if block_name.upper() in exclude_block_names:
                    excluded_count += 1
                    continue
                # block = self.doc.blocks.get(block_name)
                # if block is None:
                #     continue
                for sub, transform in self.iter_leaf_entities(insert, self.doc):
                    try:
                        if sub.dxf.layer.upper() in mirror_layers:
                            # print("layer", sub.dxf.layer.upper())
                            # print("dxftyoe", sub.dxf.dxftype)
                            pos = self.get_entity_position(sub)
                            # print("local pos:", pos)
                            pos_world = tuple(transform.transform((pos[0], pos[1], 0)))
                            # print("world pos:", pos_world)
                            if self.in_outline(pos_world):
                            # if pos[0] != 0:
                                entities.append(insert)
                                seen_handles.add(h)
                            break
                    except Exception:
                        continue
            except Exception:
                continue

        if not entities:
            print(f"  ⚠ No entities found on {sorted(mirror_layers)} to import")
            return

        try:
            importer = Importer(self.doc, out_doc)
            importer.import_entities(entities, target_layout=out_doc.modelspace())
            importer.finalize()
        except Exception as exc:
            print(f"  ⚠ Importer failed on wall frame: {exc}")
            return

        type_counts: Dict[str, int] = {}
        for e in entities:
            t = e.dxftype()
            type_counts[t] = type_counts.get(t, 0) + 1
        breakdown = ", ".join(f"{n} {t}" for t, n in sorted(type_counts.items()))
        excl_parts = []
        if exclude_insert_layers:
            excl_parts.append(f"layers {sorted(exclude_insert_layers)}")
        if exclude_block_names:
            excl_parts.append(f"blocks {sorted(exclude_block_names)}")
        excl_note = (
            f" (excluded {excluded_count} INSERT by {' / '.join(excl_parts)})"
            if excluded_count else ""
        )
        print(f"  ✓ Imported wall frame from {sorted(mirror_layers)}: {breakdown}{excl_note}")


    def iter_leaf_entities(self, insert, doc, xref_transform=None):
        m = insert.matrix44()
        combined = m if xref_transform is None else xref_transform @ m
        # print("insert name:", insert.dxf.name)
        # print("insert dxf.insert:", insert.dxf.insert)
        # print("matrix44:", m)
        block = doc.blocks.get(insert.dxf.name)
        if block is None:
            return
            
        for sub in block:
            if sub.dxftype() == "INSERT":
                yield from self.iter_leaf_entities(sub, doc, combined)
            else:
                yield sub, combined

    def _draw_hatch_wall_fallback(self, msp) -> None:
        """Fallback wall drawing used when the source has no copyable HATCH
        entities.  Reconstructs the wall ring (hatch_outer − inner_holes) and
        fills it with ANSI31, then draws boundary outlines."""
        if self.hatch_wall_polygon is not None:
            geom = self.hatch_wall_polygon
            polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
            drawn = 0
            for poly in polys:
                if poly.is_empty:
                    continue
                exterior = [(x, y) for x, y in poly.exterior.coords]
                if len(exterior) < 3:
                    continue
                try:
                    h = msp.add_hatch(
                        color=self.ACI_HATCH_WALL,
                        dxfattribs={"layer": self.OUTPUT_LAYER_HATCH_WALL},
                    )
                    h.paths.add_polyline_path(exterior, is_closed=True, flags=1)
                    for interior in poly.interiors:
                        h.paths.add_polyline_path(
                            [(x, y) for x, y in interior.coords],
                            is_closed=True, flags=0,
                        )
                    h.set_pattern_fill(
                        self.HATCH_WALL_PATTERN,
                        scale=self.HATCH_WALL_SCALE,
                        angle=45,
                    )
                    drawn += 1
                except Exception:
                    pass
                try:
                    msp.add_lwpolyline(
                        exterior, close=True,
                        dxfattribs={
                            "layer": self.OUTPUT_LAYER_HATCH_OUTLINE,
                            "color": self.ACI_HATCH_OUTLINE,
                        },
                    )
                    for interior in poly.interiors:
                        msp.add_lwpolyline(
                            [(x, y) for x, y in interior.coords],
                            close=True,
                            dxfattribs={
                                "layer": self.OUTPUT_LAYER_HATCH_OUTLINE,
                                "color": self.ACI_HATCH_OUTLINE,
                            },
                        )
                except Exception:
                    pass
            if drawn:
                print(f"  ✓ Drew hatch wall fallback ({drawn} region(s))")
            return

        if self.hatch_outer_polygon is not None:
            self._draw_polygon(
                msp, self.hatch_outer_polygon, self.OUTPUT_LAYER_HATCH_OUTLINE
            )
            for inner in self.hatch_inner_polygons:
                self._draw_polygon(msp, inner, self.OUTPUT_LAYER_HATCH_OUTLINE)
            print(
                f"  ✓ Drew hatch outer + "
                f"{len(self.hatch_inner_polygons)} inner outline(s)"
            )

    # Layers from the source DXF to copy into the flooring output as context.
    # This ensures the outer walls, columns, back wall, and structural outlines
    # appear around the tiled area so the output is meaningful.
    CONTEXT_LAYERS = [
        "OUTLINE", "WALL", "COLUMN"
    ]
    OUTPUT_LAYER_CONTEXT = "FLOORING_CONTEXT"
    ACI_CONTEXT = 8  # dark grey for walls/structure

    def _copy_context_from_source(self, out_doc, out_msp) -> None:
        """Draw the store boundary as clean context.

        When HATCH wall data was successfully extracted and drawn by
        _draw_hatch_wall, ALL wall geometry (outer perimeter + internal
        partitions + outlines) is already present in the output — copying
        source entities would only duplicate lines and add noise from
        door swings, furniture, dimensions, and construction geometry.

        We therefore skip entity copying entirely when HATCH data is
        available and only fall back to a single clean store-polygon
        boundary when there is no HATCH data at all.
        """
        # Ensure the context layer exists (keeps the layer panel clean).
        if self.OUTPUT_LAYER_CONTEXT not in out_doc.layers:
            out_doc.layers.add(self.OUTPUT_LAYER_CONTEXT, color=self.ACI_CONTEXT)

        # HATCH wall drawn → walls are fully represented; nothing more to add.
        if self.hatches is not None:
            return

        # No HATCH data at all — draw just the store polygon as a single
        # clean closed boundary so the output has a visible perimeter.
        if self.store_polygon is not None:
            self._draw_polygon(out_msp, self.store_polygon, self.OUTPUT_LAYER_CONTEXT)
            print("  ✓ Drew store polygon as boundary reference (no HATCH data)")

    # ── Drawing helpers for the styled output ──────────────────────────
    def _draw_zone_hatch(self, msp, poly, ft: Dict[str, Any]) -> None:
        """Fill a zone region so it clearly reads as a distinct finish: a SOLID
        colour fill (visible in any viewer) plus the chosen pattern on top for
        texture (wood / parquet / etc.)."""
        pattern = (ft.get("pattern") or "SOLID")
        scale = float(ft.get("scale", 1.0) or 1.0)
        angle = float(ft.get("angle", 0.0) or 0.0)
        color = int(ft.get("color", self.ACI_ZONE_HATCH) or self.ACI_ZONE_HATCH)
        for part in self._iter_polys(poly):
            ext = list(part.exterior.coords)
            holes = [list(r.coords) for r in part.interiors]

            def _path(h):
                h.paths.add_polyline_path(ext, is_closed=True)
                for hl in holes:
                    h.paths.add_polyline_path(hl, is_closed=True)

            # 1) solid colour fill — guarantees the zone is visible & distinct
            try:
                bg = msp.add_hatch(color=color, dxfattribs={"layer": self.OUTPUT_LAYER_ZONE_HATCH})
                bg.set_solid_fill(color=color)
                bg.dxf.color = color
                _path(bg)
            except Exception as exc:
                print(f"  ! zone fill failed: {exc}")

            # 2) pattern lines on top for texture (contrast colour)
            if pattern.upper() != "SOLID":
                try:
                    pf = msp.add_hatch(color=7, dxfattribs={"layer": self.OUTPUT_LAYER_ZONE_HATCH})
                    pf.set_pattern_fill(pattern, scale=scale, angle=angle)
                    pf.dxf.color = 7
                    _path(pf)
                except Exception as exc:
                    print(f"  ! zone pattern '{pattern}' failed: {exc}")

    def _draw_tile_polygons(self, msp, tiles: List[Dict[str, Any]]) -> None:
        """Draw each tile as a solid-hatched polygon on its colour layer,
        plus a thin outline so tile joints are visible on paper."""
        if not tiles:
            return
        layer_for_color = {
            "grey":  (self.OUTPUT_LAYER_TILE_GREY,  self.ACI_TILE_GREY),
            "white": (self.OUTPUT_LAYER_TILE_WHITE, self.ACI_TILE_WHITE),
            "start": (self.OUTPUT_LAYER_TILE_START, self.ACI_TILE_START),
        }
        for tile in tiles:
            poly = tile.get("polygon")
            if poly is None or poly.is_empty:
                continue
            color_key = tile.get("color", "white")
            layer, aci = layer_for_color.get(color_key, layer_for_color["white"])
            coords = [(x, y) for x, y in poly.exterior.coords]
            if len(coords) < 4:
                continue

            # Solid fill
            try:
                hatch = msp.add_hatch(
                    color=aci, dxfattribs={"layer": layer}
                )
                hatch.paths.add_polyline_path(coords, is_closed=True, flags=1)
                hatch.set_solid_fill(color=aci, style=0)
            except Exception:
                pass

            # Thin outline so tile joints read on paper
            try:
                msp.add_lwpolyline(
                    coords, close=True,
                    dxfattribs={"layer": self.OUTPUT_LAYER_TILE_OUTLINE,
                                "color": self.ACI_TILE_OUTLINE},
                )
            except Exception:
                pass

    def _draw_solid_hatch(self, msp, geom, layer: str, aci: int) -> None:
        """Draw a solid-fill hatch for any polygon geometry (columns, etc.)."""
        polys = []
        if isinstance(geom, MultiPolygon):
            polys = list(geom.geoms)
        elif isinstance(geom, Polygon):
            polys = [geom]
        elif hasattr(geom, "geoms"):
            polys = [g for g in geom.geoms if isinstance(g, Polygon)]
        for poly in polys:
            if poly.is_empty:
                continue
            exterior = [(x, y) for x, y in poly.exterior.coords]
            if len(exterior) < 3:
                continue
            try:
                hatch = msp.add_hatch(color=aci, dxfattribs={"layer": layer})
                hatch.paths.add_polyline_path(exterior, is_closed=True, flags=1)
                hatch.set_solid_fill(color=aci, style=0)
            except Exception:
                pass
            try:
                msp.add_lwpolyline(
                    exterior, close=True,
                    dxfattribs={"layer": layer, "color": aci},
                )
            except Exception:
                pass

    def _draw_granite(self, msp, geom, layer: str) -> None:
        """Draw granite strip with an ANSI37 crosshatch + a bold outline."""
        polys = []
        if isinstance(geom, MultiPolygon):
            polys = list(geom.geoms)
        elif isinstance(geom, Polygon):
            polys = [geom]
        elif hasattr(geom, "geoms"):
            polys = [g for g in geom.geoms if isinstance(g, Polygon)]
        else:
            polys = [geom]
        for poly in polys:
            if poly.is_empty:
                continue
            exterior = [(x, y) for x, y in poly.exterior.coords]
            # Hatch fill
            try:
                hatch = msp.add_hatch(
                    color=self.ACI_GRANITE, dxfattribs={"layer": layer}
                )
                hatch.paths.add_polyline_path(exterior, is_closed=True, flags=1)
                for interior in poly.interiors:
                    hatch.paths.add_polyline_path(
                        [(x, y) for x, y in interior.coords], is_closed=True, flags=0
                    )
                hatch.set_pattern_fill(
                    self.GRANITE_HATCH_PATTERN,
                    scale=self.GRANITE_HATCH_SCALE,
                    angle=0,
                )
            except Exception:
                pass
            # Outline
            msp.add_lwpolyline(
                exterior, close=True,
                dxfattribs={"layer": layer, "color": self.ACI_GRANITE},
            )
            for interior in poly.interiors:
                msp.add_lwpolyline(
                    [(x, y) for x, y in interior.coords], close=True,
                    dxfattribs={"layer": layer, "color": self.ACI_GRANITE},
                )

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _polyline_vertices(entity, entity_type: str) -> Optional[List[Tuple[float, float]]]:
        try:
            if entity_type == "LWPOLYLINE":
                return [(p[0], p[1]) for p in entity.get_points()]
            vertices = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            return vertices
        except Exception:
            return None

    @staticmethod
    def _entity_to_linestring(entity, entity_type: str) -> Optional[LineString]:
        try:
            if entity_type == "LINE":
                s, e = entity.dxf.start, entity.dxf.end
                return LineString([(s.x, s.y), (e.x, e.y)])
            if entity_type == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in entity.get_points()]
                return LineString(pts) if len(pts) >= 2 else None
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            return LineString(pts) if len(pts) >= 2 else None
        except Exception:
            return None

    @staticmethod
    def _insert_bbox_polygon(insert) -> Optional[Polygon]:
        try:
            bb = insert.bounding_box
            if bb is None:
                return None
            if hasattr(bb, "bounds"):
                minx, miny, maxx, maxy = bb.bounds
            elif isinstance(bb, (tuple, list)) and len(bb) >= 4:
                minx, miny, maxx, maxy = bb[0], bb[1], bb[2], bb[3]
            else:
                return None
            if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
                return None
            if maxx <= minx or maxy <= miny:
                return None
            return box(minx, miny, maxx, maxy)
        except Exception:
            return None

    def _draw_tile_origin_marker(
        self, msp, origin: Tuple[float, float], layer: str
    ) -> None:
        x, y = origin
        r = self.TILE_ORIGIN_MARKER_RADIUS
        msp.add_circle((x, y), r, dxfattribs={"layer": layer})
        msp.add_line((x - r, y), (x + r, y), dxfattribs={"layer": layer})
        msp.add_line((x, y - r), (x, y + r), dxfattribs={"layer": layer})

    # ── Annotation drawing (matches the sample flooring plan) ──────────
    def _draw_annotations(self, msp) -> None:
        """Add the callouts and dimensions that turn the raw tile/granite
        plan into a presentation-style flooring layout: 4 directional
        arrows from the start tile, two leader-text labels, and linear
        dimensions along the granite strip + store perimeter."""
        if self.tile_origin is None or self.store_polygon is None:
            return

        self._draw_start_tile_arrows(msp, self.OUTPUT_LAYER_ANNOT)
        self._draw_start_tile_text(msp, self.OUTPUT_LAYER_ANNOT)
        self._draw_start_tile_label(msp, self.OUTPUT_LAYER_ANNOT)
        self._draw_granite_label(msp, self.OUTPUT_LAYER_ANNOT)
        self._draw_perimeter_dimensions(msp, self.OUTPUT_LAYER_DIM)

    def _draw_start_tile_arrows(self, msp, layer: str) -> None:
        """Four arrows radiating from the start tile through its CORNERS
        (i.e., along the diamond's tips), showing the lay direction.

        The (u, v) axes from `_compute_tile_grid` are the tile's EDGE
        directions; the corners sit along the bisectors (u+v) and (u-v).
        For a 45°-rotated tile on a horizontal façade these bisectors
        line up exactly with the world axes — so the arrows come out
        as straight up / down / left / right, matching the reference."""
        if self.tile_u_axis is None or self.tile_v_axis is None:
            return
        ox, oy = self.tile_origin
        ux, uy = self.tile_u_axis
        vx, vy = self.tile_v_axis

        # Corner directions = bisectors of the (u, v) edge axes.
        # For perpendicular unit u, v: |u + v| = |u - v| = sqrt(2),
        # so divide by sqrt(2) to normalise.
        inv_root2 = 1.0 / math.sqrt(2.0)
        c1 = ((ux + vx) * inv_root2, (uy + vy) * inv_root2)  # toward "top" corner
        c2 = ((ux - vx) * inv_root2, (uy - vy) * inv_root2)  # toward "right" corner
        corner_dirs = [c1, (-c1[0], -c1[1]), c2, (-c2[0], -c2[1])]

        # Distance from tile centre to corner = half · sqrt(2) (diagonal).
        diag = (self.tile_size / 2.0) * math.sqrt(2.0)
        L = self.START_ARROW_LENGTH

        for dx, dy in corner_dirs:
            sx = ox + diag * dx
            sy = oy + diag * dy
            ex = ox + (diag + L) * dx
            ey = oy + (diag + L) * dy
            self._draw_arrow(msp, (sx, sy), (ex, ey), layer)

    def _draw_start_tile_text(self, msp, layer: str) -> None:
        """Place 'START HERE' centred inside the red start diamond.
        Uses TEXT (not MTEXT) so positioning works identically across all
        ezdxf versions; falls back gracefully if any helper is missing."""
        if self.tile_origin is None:
            return
        ox, oy = self.tile_origin

        try:
            text_entity = msp.add_text(
                self.START_TILE_TEXT,
                dxfattribs={
                    "layer": layer,
                    "height": self.START_TILE_TEXT_HEIGHT,
                    "rotation": 0,
                },
            )
        except Exception:
            return

        # Centre the text on the tile origin. ezdxf >= 1.0 uses
        # `set_placement` with the TextEntityAlignment enum; older builds
        # fall through to `set_pos` then to a raw `insert` assignment.
        placed = False
        try:
            from ezdxf.enums import TextEntityAlignment
            text_entity.set_placement((ox, oy), align=TextEntityAlignment.MIDDLE_CENTER)
            placed = True
        except Exception:
            pass
        if not placed:
            try:
                text_entity.set_pos((ox, oy), align="MIDDLE_CENTER")
                placed = True
            except Exception:
                pass
        if not placed:
            try:
                text_entity.dxf.insert = (ox, oy)
                text_entity.dxf.halign = 4   # MIDDLE
                text_entity.dxf.valign = 2   # MIDDLE
                text_entity.dxf.align_point = (ox, oy)
            except Exception:
                pass

    def _draw_arrow(
        self,
        msp,
        start: Tuple[float, float],
        end: Tuple[float, float],
        layer: str,
    ) -> None:
        """Open-V arrowhead at `end`, shaft from `start`."""
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return
        ux, uy = dx / L, dy / L
        # Perpendicular (right-hand)
        px, py = -uy, ux

        msp.add_line((sx, sy), (ex, ey), dxfattribs={"layer": layer})

        head_len = self.START_ARROW_HEAD_LENGTH
        half_w = self.START_ARROW_HEAD_HALF_WIDTH
        bx = ex - head_len * ux
        by = ey - head_len * uy
        wing1 = (bx + half_w * px, by + half_w * py)
        wing2 = (bx - half_w * px, by - half_w * py)
        msp.add_line(wing1, (ex, ey), dxfattribs={"layer": layer})
        msp.add_line(wing2, (ex, ey), dxfattribs={"layer": layer})

    def _draw_leader(
        self,
        msp,
        text_anchor: Tuple[float, float],
        target: Tuple[float, float],
        layer: str,
    ) -> None:
        """Draw a 2-segment leader from the *side of the text* to the
        target point, with an arrow at the target end. The first segment
        is horizontal (the kink), the second runs to the target."""
        ax, ay = text_anchor
        # First node sits one kink-length toward the target on the X axis.
        side = 1.0 if target[0] > ax else -1.0
        node = (ax + side * self.ANNOT_LEADER_KINK, ay)
        msp.add_line(text_anchor, node, dxfattribs={"layer": layer})
        self._draw_arrow(msp, node, target, layer)

    def _draw_start_tile_label(self, msp, layer: str) -> None:
        """Multi-line callout on the LEFT side of the store, leader pointing
        to the start tile. Matches 'START LAYING VITRIFIED TILE FROM THE
        CENTRE OF THE STORE' in the reference."""
        if self.tile_origin is None or self.store_polygon is None:
            return
        ox, oy = self.tile_origin
        minx, _, _, _ = self.store_polygon.bounds

        # Place text well to the left of the store so it never overlaps tiles.
        text_x = minx - 4500.0
        text_y = oy
        text_lines = "START LAYING\\PVITRIFIED TILE\\PFROM THE\\PCENTRE OF THE\\PSTORE"

        try:
            mtext = msp.add_mtext(
                text_lines,
                dxfattribs={
                    "layer": layer,
                    "char_height": self.ANNOT_TEXT_HEIGHT,
                    "attachment_point": 6,  # MIDDLE_RIGHT — text grows leftward from the anchor
                    "insert": (text_x, text_y),
                },
            )
            # Constrain MTEXT box width so lines wrap predictably.
            try:
                mtext.dxf.width = 3500.0
            except Exception:
                pass
        except Exception:
            # Fallback to plain TEXT if MTEXT unsupported.
            for i, line in enumerate(text_lines.split("\\P")):
                msp.add_text(
                    line,
                    dxfattribs={
                        "layer": layer,
                        "height": self.ANNOT_TEXT_HEIGHT,
                        "insert": (text_x, text_y - i * self.ANNOT_TEXT_HEIGHT * 1.4),
                    },
                )

        # Leader: from the right edge of the text to a point just outside
        # the start tile (one tile-width away so the arrow doesn't cover
        # the red diamond).
        leader_start = (text_x + self.ANNOT_LEADER_GAP, text_y)
        leader_target = (ox - self.tile_size, oy)
        self._draw_leader(msp, leader_start, leader_target, layer)

    def _draw_granite_label(self, msp, layer: str) -> None:
        """Callout on the LEFT side pointing to the granite strip.
        Matches 'JET BLACK GRANITE AS APP.' in the reference."""
        if self.granite_strip is None or self.store_polygon is None:
            return
        # Pick a point on the granite strip — its centroid is reliable.
        try:
            centroid = self.granite_strip.centroid
            target = (centroid.x, centroid.y)
        except Exception:
            return

        minx, miny, _, maxy = self.store_polygon.bounds
        # Place this label LOWER than the start-tile label so they don't
        # overlap. Sit it at ~25% up from the bottom of the store.
        text_x = minx - 4500.0
        text_y = miny + (maxy - miny) * 0.25
        text_lines = "JET BLACK\\PGRANITE\\PAS APP."

        try:
            mtext = msp.add_mtext(
                text_lines,
                dxfattribs={
                    "layer": layer,
                    "char_height": self.ANNOT_TEXT_HEIGHT,
                    "attachment_point": 6,  # MIDDLE_RIGHT
                    "insert": (text_x, text_y),
                },
            )
            try:
                mtext.dxf.width = 2800.0
            except Exception:
                pass
        except Exception:
            for i, line in enumerate(text_lines.split("\\P")):
                msp.add_text(
                    line,
                    dxfattribs={
                        "layer": layer,
                        "height": self.ANNOT_TEXT_HEIGHT,
                        "insert": (text_x, text_y - i * self.ANNOT_TEXT_HEIGHT * 1.4),
                    },
                )

        leader_start = (text_x + self.ANNOT_LEADER_GAP, text_y)
        self._draw_leader(msp, leader_start, target, layer)

    def _draw_perimeter_dimensions(self, msp, layer: str) -> None:
        """Linear dimensions on the store's bounding box: granite width
        on the bottom (façade) edge, overall store width below the granite,
        overall store depth on the right side. Auto-derived from geometry
        — values shown in mm."""
        if self.store_polygon is None:
            return
        minx, miny, maxx, maxy = self.store_polygon.bounds
        store_w = maxx - minx
        store_h = maxy - miny

        # ── 1) Granite width across the bottom (perpendicular to façade).
        # Drawn just outside the bottom edge of the store, on the LEFT,
        # so its meaning ('granite_width' in the strip) is unambiguous.
        if self.granite_strip is not None and self.granite_width > 0:
            # Two reference points: outer façade edge and inner edge of the
            # granite strip. We use the bottom-left corner of the store and
            # a point granite_width above it.
            p1 = (minx, miny)
            p2 = (minx, miny + self.granite_width)
            # Dim line sits to the LEFT of the wall.
            offset_dir = (-1.0, 0.0)
            self._draw_linear_dim(
                msp, p1, p2, offset_dir, self.DIM_OFFSET, f"{int(self.granite_width)}", layer,
            )

        # ── 2) Overall store width along the bottom edge, below the granite.
        p1 = (minx, miny)
        p2 = (maxx, miny)
        offset_dir = (0.0, -1.0)  # below the bottom edge
        self._draw_linear_dim(
            msp, p1, p2, offset_dir, self.DIM_OFFSET, f"{int(round(store_w))}", layer,
        )

        # ── 3) Overall store depth on the right side.
        p1 = (maxx, miny)
        p2 = (maxx, maxy)
        offset_dir = (1.0, 0.0)  # to the right of the right edge
        self._draw_linear_dim(
            msp, p1, p2, offset_dir, self.DIM_OFFSET, f"{int(round(store_h))}", layer,
        )

    def _draw_linear_dim(
        self,
        msp,
        p1: Tuple[float, float],
        p2: Tuple[float, float],
        offset_dir: Tuple[float, float],
        offset_dist: float,
        label: str,
        layer: str,
    ) -> None:
        """Draw a manual linear dimension between p1 and p2:
            • two extension lines from p1, p2 to the dim-line offset
            • a dim line with arrowheads at both ends
            • centred text aligned with the dim line
        offset_dir is a unit vector indicating which side the dim sits on."""
        ox, oy = offset_dir
        ext1 = (p1[0] + ox * offset_dist, p1[1] + oy * offset_dist)
        ext2 = (p2[0] + ox * offset_dist, p2[1] + oy * offset_dist)

        # Extension lines (slightly past the dim line for a clean look).
        over = self.DIM_EXT_OVERSHOOT
        msp.add_line(
            p1,
            (ext1[0] + ox * over, ext1[1] + oy * over),
            dxfattribs={"layer": layer},
        )
        msp.add_line(
            p2,
            (ext2[0] + ox * over, ext2[1] + oy * over),
            dxfattribs={"layer": layer},
        )

        # Dim line itself with arrowheads at both ends.
        self._draw_arrow(msp, ext2, ext1, layer)
        self._draw_arrow(msp, ext1, ext2, layer)

        # Text — centred along the dim, slightly offset off the line.
        dx = ext2[0] - ext1[0]
        dy = ext2[1] - ext1[1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return
        rotation = math.degrees(math.atan2(dy, dx))
        # Keep text right-side up.
        if rotation > 90.0:
            rotation -= 180.0
        elif rotation < -90.0:
            rotation += 180.0

        mid = ((ext1[0] + ext2[0]) / 2.0, (ext1[1] + ext2[1]) / 2.0)
        text_offset = self.DIM_TEXT_HEIGHT * 0.4
        text_pos = (mid[0] + ox * text_offset, mid[1] + oy * text_offset)

        try:
            text_entity = msp.add_text(
                str(label),
                dxfattribs={
                    "layer": layer,
                    "height": self.DIM_TEXT_HEIGHT,
                    "rotation": rotation,
                },
            )
            # ezdxf >= 1.0 uses set_placement; older builds use set_pos.
            placed = False
            try:
                from ezdxf.enums import TextEntityAlignment
                text_entity.set_placement(text_pos, align=TextEntityAlignment.MIDDLE_CENTER)
                placed = True
            except Exception:
                pass
            if not placed:
                try:
                    text_entity.set_pos(text_pos, align="MIDDLE_CENTER")
                except Exception:
                    text_entity.dxf.insert = text_pos
        except Exception:
            pass

    @staticmethod
    def _draw_polygon(msp, geom, layer: str) -> None:
        """Draw exterior + all interior rings of every polygon in geom."""
        polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        for poly in polys:
            if poly.is_empty:
                continue
            exterior = list(poly.exterior.coords)
            msp.add_lwpolyline(exterior, close=True, dxfattribs={"layer": layer})
            for interior in poly.interiors:
                msp.add_lwpolyline(list(interior.coords), close=True, dxfattribs={"layer": layer})

    @staticmethod
    def _draw_polygon_exterior(msp, geom, layer: str) -> None:
        """Draw only the exterior ring of every polygon — no interior holes.
        Use this instead of _draw_polygon when interior-ring lines would
        duplicate boundaries already drawn on other layers (toilet, columns)."""
        from shapely.geometry import GeometryCollection
        if isinstance(geom, GeometryCollection):
            polys = [g for g in geom.geoms if hasattr(g, 'exterior')]
        elif isinstance(geom, MultiPolygon):
            polys = list(geom.geoms)
        else:
            polys = [geom]
        for poly in polys:
            if poly.is_empty:
                continue
            exterior = list(poly.exterior.coords)
            msp.add_lwpolyline(exterior, close=True, dxfattribs={"layer": layer})

    def _iter_virtual_entities(self, container) -> Any:
        """Recursively yield entities, unpacking INSERT blocks into their 
        virtual entities so we can detect toilet curtains and outer walls 
        even if they are nested inside blocks."""
        for entity in container:
            yield entity
            if entity.dxftype() == "INSERT":
                try:
                    yield from self._iter_virtual_entities(entity.virtual_entities())
                except Exception:
                    pass


def generate(dxf_path: str, output_path: str, ctx=None):
    """Module-level entry point used by main.py.

    Parameters
    ----------
    dxf_path    : str  – path to the source DXF file
    output_path : str  – desired path for the generated flooring DXF
    ctx         : DXFContext | None – shared pipeline context. Accepted for
                  signature parity with the other dockets; the flooring
                  layout re-extracts all geometry from the DXF itself, so
                  this is currently unused.

    Returns
    -------
    str | None  – output_path on success, None on failure
    """
    try:
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        layout = FlooringLayout(dxf_path, ctx=ctx)
        layout.extract()
        return layout.save_output(output_path)
    except Exception as exc:
        import traceback
        print(f"  Flooring layout exception: {exc}")
        traceback.print_exc()
        return None


def main():

    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python flooring_layout.py <input.dxf> "
            "[granite_width_mm] [min_door_width_mm] [tile_size_mm] "
            "[wastage_pct] [tile_rotation_deg] [output.dxf]"
        )
        print()
        print("Drag-and-drop your DXF file onto this script, or run:")
        print("  python flooring_layout.py path/to/file.dxf")
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass
        sys.exit(1)

    dxf_path = sys.argv[1]
    granite_width     = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    min_door_width    = float(sys.argv[3]) if len(sys.argv) > 3 else 600.0
    tile_size         = float(sys.argv[4]) if len(sys.argv) > 4 else 600.0
    wastage_pct       = float(sys.argv[5]) if len(sys.argv) > 5 else 10.0
    tile_rotation_deg = float(sys.argv[6]) if len(sys.argv) > 6 else 45.0
    output_path       = sys.argv[7] if len(sys.argv) > 7 else None

    try:
        layout = FlooringLayout(
            dxf_path,
            granite_width=granite_width,
            min_door_width=min_door_width,
            tile_size=tile_size,
            wastage_factor=wastage_pct / 100.0,
            tile_rotation_deg=tile_rotation_deg,
        )
        layout.extract()

        if output_path is None:
            base = os.path.splitext(os.path.basename(dxf_path))[0]
            output_path = os.path.join(
                os.path.dirname(os.path.abspath(dxf_path)),
                "output",
                f"{base}_FLOORING.dxf",
            )
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

        layout.save_output(output_path)
        print(f"\n  Output saved to: {output_path}")

    except Exception as exc:
        import traceback
        print(f"\n  ERROR: {exc}")
        traceback.print_exc()

    # Keep the console window open so the user can read the log output
    # before it closes (important when launched by double-click on Windows).
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass


if __name__ == "__main__":
    main()

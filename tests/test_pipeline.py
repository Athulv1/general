"""Unit tests for the schema-3.1 pipeline.

Every test builds its own small synthetic DXF with ezdxf (rectangle store,
one walled inner room, one closed zone, one fixture block + a mirrored copy)
— no dependency on any customer sample file, and no brand strings.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import config_loader
from config_loader import ConfigError, load_config_dict
import run_pipeline
from zone_service import ZoneResolver


# ---------------------------------------------------------------------------
# synthetic drawing
# ---------------------------------------------------------------------------

STORE_W, STORE_H = 20_000.0, 12_000.0


def build_store_dxf(path: str) -> None:
    """20 m × 12 m store, a walled pantry room top-left, a closed zone
    bottom-middle, a fixture block placed straight and mirrored."""
    doc = ezdxf.new("R2010")
    for name, color in [("STORE-EDGE", 7), ("WALLS", 1), ("ZONE-A", 3),
                        ("FIX-LAYER", 4), ("TXT", 2)]:
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (STORE_W, 0), (STORE_W, STORE_H),
                        (0, STORE_H)], close=True,
                       dxfattribs={"layer": "STORE-EDGE"})
    # walled inner room 5 m × 4 m in the top-left corner
    msp.add_lwpolyline([(0, 8000), (5000, 8000), (5000, 12000), (0, 12000)],
                       close=True, dxfattribs={"layer": "WALLS"})
    # closed zone 4 m × 4 m on the bottom edge
    msp.add_lwpolyline([(6000, 0), (10000, 0), (10000, 4000), (6000, 4000)],
                       close=True, dxfattribs={"layer": "ZONE-A"})
    # zone drawn as TWO OPEN polylines that chain end-to-end (sub-mm gaps),
    # a common drafting pattern the resolver must recover via chaining
    doc.layers.add("ZONE-B", color=5)
    msp.add_lwpolyline([(12000, 0), (15000, 0), (15000, 3000)],
                       dxfattribs={"layer": "ZONE-B"})
    msp.add_lwpolyline([(15000.4, 3000.2), (12000, 3000), (12000.3, 0.4)],
                       dxfattribs={"layer": "ZONE-B"})
    # fixture block: 800 × 400 rectangle, one straight + one mirrored insert
    blk = doc.blocks.new("FIXTURE_X")
    blk.add_lwpolyline([(0, 0), (800, 0), (800, 400), (0, 400)], close=True)
    msp.add_blockref("FIXTURE_X", (12000, 6000),
                     dxfattribs={"layer": "FIX-LAYER"})
    mirrored = msp.add_blockref("FIXTURE_X", (16000, 6000),
                                dxfattribs={"layer": "FIX-LAYER"})
    mirrored.dxf.xscale = -1.0
    # room label for balloon detection
    t = msp.add_text("PANTRY", dxfattribs={"layer": "TXT", "height": 200})
    t.set_placement((2500, 10000))
    doc.saveas(path)


def build_cyclic_dxf(path: str) -> None:
    """A block that references itself — recursion must terminate."""
    doc = ezdxf.new("R2010")
    doc.layers.add("WALLS", color=1)
    blk = doc.blocks.new("LOOPY")
    blk.add_line((0, 0), (1000, 0), dxfattribs={"layer": "WALLS"})
    blk.add_blockref("LOOPY", (100, 100))
    doc.modelspace().add_blockref("LOOPY", (0, 0))
    doc.saveas(path)


def base_config() -> dict:
    """Canonical schema-3.1 config for the synthetic store."""
    return {
        "schema": "3.1",
        "store_outline": {"layers": ["STORE-EDGE"]},
        "walls": {"layers": ["WALLS", "STORE-EDGE"], "door_layers": []},
        "zones": {
            "zone_a": {"kind": "polyline", "layers": ["ZONE-A"]},
            "fixtures": {"kind": "block", "block_names": ["FIXTURE_X"]},
        },
        "flooring_types": [
            {"name": "tile600", "kind": "tile", "length_mm": 600,
             "width_mm": 600, "rotation_deg": 0},
            {"name": "none", "kind": "skip"},
        ],
    }


class PipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dxf_path = os.path.join(cls.tmp.name, "store.dxf")
        build_store_dxf(cls.dxf_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make_resolver(self, cfg_extra: dict | None = None) -> ZoneResolver:
        cfg = load_config_dict({**base_config(), **(cfg_extra or {}),
                                "flooring": {"default_flooring_type": "tile600"}})
        doc = ezdxf.readfile(self.dxf_path)
        return ZoneResolver(doc, self.dxf_path, cfg)

    def run_dockets(self, extra: dict) -> dict:
        out_dir = tempfile.mkdtemp(dir=self.tmp.name)
        cfg = {**base_config(), **extra}
        cfg["io"] = {"input_dxf": self.dxf_path, "output_dir": out_dir}
        result = run_pipeline.run(cfg_dict=cfg)
        result["_out_dir"] = out_dir
        return result


# ---------------------------------------------------------------------------
# config loader
# ---------------------------------------------------------------------------

class TestConfigLoader(PipelineTestCase):
    def test_missing_ref_fails_with_json_path(self):
        cfg = base_config()
        cfg["flooring"] = {"zones": [{"zone": {"$ref": "#/zones/nope"},
                                      "flooring_type": "none"}],
                           "default_flooring_type": "tile600"}
        with self.assertRaises(ConfigError) as cm:
            load_config_dict(cfg)
        message = str(cm.exception)
        self.assertIn("flooring.zones[0].zone", message)
        self.assertIn("#/zones/nope", message)

    def test_ref_cycle_rejected(self):
        cfg = base_config()
        cfg["zones"]["a"] = {"$ref": "#/zones/b"}
        cfg["zones"]["b"] = {"$ref": "#/zones/a"}
        cfg["flooring"] = {"zones": [{"zone": {"$ref": "#/zones/a"},
                                      "flooring_type": "none"}],
                           "default_flooring_type": "tile600"}
        with self.assertRaises(ConfigError) as cm:
            load_config_dict(cfg)
        self.assertIn("cycle", str(cm.exception))

    def test_form_shaped_config_normalizes(self):
        """The intake form's spelling maps onto the canonical schema."""
        form_cfg = {
            "schema_version": "3.1",
            "zones": {"Toilet": {"identifier": "balloon", "keyword": "WC",
                                 "boundaries": ["WALLS"]}},
            "flooring_types": [{"name": "t", "kind": "tile"}],
            "flooring": {"store_outline": {"layers": ["STORE-EDGE"]},
                         "zones": [{"zone": {"$ref": "#/zones/Toilet"},
                                    "flooring_type": "t"}],
                         "wall_frame": {"mirror_layers": ["WALLS"]}},
            "default_flooring_type": "t",
            "speaker": {"mode": "placement",
                        "model": {"speaker_size_mm": 300,
                                  "coverage_radius_mm": 4000},
                        "placements": [{"target": {"$ref": "#/zones/Toilet"},
                                        "position": "center", "count": 1}],
                        "boxes": []},
            "sprinkler": {"model": {"coverage_radius_mm": 2300,
                                    "max_spacing_mm": 3700,
                                    "pattern": "square"},
                          "target": "store", "min_one_per_room": True},
            "electrical": {"groups": [{"match": ["FIXTURE_X"],
                                       "place": "wall", "label": "WP1",
                                       "note": "note"}]},
            "finishes": {"materials": [{"name": "Paint",
                                        "applies_to": ["Toilet"],
                                        "height_mm": 2400,
                                        "color_aci": 5}]},
            "skirting": {"zones": ["Toilet"], "height_mm": 100},
        }
        cfg = load_config_dict(form_cfg)
        zone = cfg["zones"]["Toilet"]
        self.assertEqual(zone["kind"], "balloon")
        self.assertEqual(zone["keywords"], ["WC"])
        self.assertEqual(zone["barrier_layers"], ["WALLS"])
        self.assertEqual(cfg["walls"]["layers"], ["WALLS"])
        self.assertEqual(cfg["speakers"]["placements"][0]["zone"]["kind"],
                         "balloon")
        self.assertTrue(cfg["sprinklers"]["rooms_min_one"])
        group = cfg["electrical"]["point_groups"][0]
        self.assertEqual(group["place_as"], "WP")
        self.assertIn("FIXTURE_X", group["match"]["block_names"])
        self.assertEqual(cfg["finishes"]["items"][0]["material"], "Paint")
        self.assertEqual(cfg["finishes"]["items"][0]["color"], 5)
        self.assertEqual(cfg["skirting"]["rooms"][0]["kind"], "balloon")

    def test_docket_isolation(self):
        """A config with only sprinklers runs only sprinklers."""
        cfg = load_config_dict({**base_config(),
                                "sprinklers": {"coverage_radius_mm": 2300}})
        self.assertEqual(config_loader.enabled_dockets(cfg), ["sprinklers"])


# ---------------------------------------------------------------------------
# zone resolver
# ---------------------------------------------------------------------------

class TestZoneResolver(PipelineTestCase):
    def test_polyline_zone(self):
        resolver = self.make_resolver()
        polys = resolver.resolve({"kind": "polyline", "layers": ["ZONE-A"]})
        self.assertEqual(len(polys), 1)
        self.assertAlmostEqual(polys[0].area, 4000 * 4000, delta=1.0)

    def test_mirrored_block_bbox(self):
        resolver = self.make_resolver()
        polys = resolver.resolve({"kind": "block",
                                  "block_names": ["FIXTURE_X"]})
        self.assertEqual(len(polys), 2)
        for p in polys:
            self.assertAlmostEqual(p.area, 800 * 400, delta=1.0)
        # the mirrored insert extends −800 in X from its insert point
        mirrored = min(polys, key=lambda p: p.bounds[0] - 15000
                       if p.bounds[0] > 14000 else 1e9)
        bounds = sorted(p.bounds[0] for p in polys)
        self.assertAlmostEqual(bounds[0], 12000, delta=1.0)
        self.assertAlmostEqual(bounds[1], 15200, delta=1.0)

    def test_missing_layer_warns_not_crashes(self):
        resolver = self.make_resolver()
        polys = resolver.resolve({"kind": "polyline",
                                  "layers": ["NO-SUCH-LAYER"]})
        self.assertEqual(polys, [])
        self.assertTrue(any("NO-SUCH-LAYER" in w for w in resolver.warnings))

    def test_cyclic_block_terminates(self):
        path = os.path.join(self.tmp.name, "cyclic.dxf")
        build_cyclic_dxf(path)
        cfg = load_config_dict({
            "store_outline": {"layers": ["WALLS"]},
            "walls": {"layers": ["WALLS"]},
            "sprinklers": {"coverage_radius_mm": 2300},
        })
        doc = ezdxf.readfile(path)
        resolver = ZoneResolver(doc, path, cfg)
        segs = resolver.wall_segments()  # must terminate
        self.assertTrue(any("cyclic" in w.lower() for w in resolver.warnings))
        polys = resolver.resolve({"kind": "block", "block_names": ["LOOPY"]})
        self.assertGreaterEqual(len(polys), 1)

    def test_open_polylines_chain_into_zone(self):
        """A zone drawn as two open polylines (sub-mm end gaps) resolves via
        chaining to the expected 3 m × 3 m polygon."""
        resolver = self.make_resolver()
        polys = resolver.resolve({"kind": "polyline", "layers": ["ZONE-B"]})
        self.assertEqual(len(polys), 1)
        self.assertAlmostEqual(polys[0].area, 3000 * 3000, delta=3000 * 20)
        self.assertTrue(any("chained" in w for w in resolver.warnings))

    def test_closed_rooms_found(self):
        resolver = self.make_resolver()
        rooms = resolver.closed_rooms()
        self.assertTrue(any(abs(r.area - 5000 * 4000) < 1e5 for r in rooms),
                        f"room areas: {[r.area for r in rooms]}")

    def test_balloon_zone_bounded_by_walls(self):
        resolver = self.make_resolver()
        polys = resolver.resolve({"kind": "balloon", "keywords": ["PANTRY"],
                                  "text_layers": None, "barrier_layers": [],
                                  "zone_name": "pantry"})
        self.assertEqual(len(polys), 1)
        # inflated room must stay within the walled 5 m × 4 m corner (+margin)
        minx, miny, maxx, maxy = polys[0].bounds
        self.assertGreaterEqual(minx, -300)
        self.assertLessEqual(maxx, 5300)
        self.assertGreaterEqual(miny, 7700)
        self.assertLessEqual(maxy, 12300)


# ---------------------------------------------------------------------------
# docket engines (through the orchestrator)
# ---------------------------------------------------------------------------

class TestDockets(PipelineTestCase):
    def test_flooring(self):
        result = self.run_dockets({
            "flooring": {
                "zones": [{"zone": {"$ref": "#/zones/zone_a"},
                           "flooring_type": "none"}],
                "default_flooring_type": "tile600",
                "wall_frame": {"mirror_layers": ["WALLS"]},
            }})
        self.assertTrue(result["ok"], result)
        boq = result["boq_summary"]["flooring"]
        net = boq["net_area_m2"]
        self.assertAlmostEqual(net, (STORE_W * STORE_H - 4000 * 4000) / 1e6,
                               delta=1.0)
        tiles = boq["types"]["tile600"]
        self.assertGreater(tiles["full_tiles"], 100)
        self.assertGreater(tiles["order_qty"], tiles["tiles_total"])
        self.assertTrue(os.path.exists(
            os.path.join(result["_out_dir"], "flooring.dxf")))

    def test_sprinklers_full_coverage_and_rooms(self):
        result = self.run_dockets({
            "sprinklers": {"coverage_radius_mm": 2300,
                           "max_spacing_mm": 3700, "pattern": "square",
                           "rooms_min_one": True, "rooms": [],
                           "strict_coverage": True}})
        self.assertTrue(result["ok"], result)
        boq = result["boq_summary"]["sprinklers"]
        self.assertGreater(boq["total_heads"], 10)
        self.assertTrue(all(r["heads"] >= 1 for r in boq["rooms"]))
        warnings = result["dockets"]["sprinklers"]["warnings"]
        self.assertFalse(any("uncovered" in w for w in warnings), warnings)

    def test_speakers_auto_and_positional(self):
        result = self.run_dockets({
            "speakers": {"coverage_radius_mm": 4000, "speaker_size_mm": 300,
                         "placements": [
                             {"zone": {"$ref": "#/zones/zone_a"},
                              "count": None},
                             {"zone": "store", "count": 4,
                              "position": "corners"}],
                         "boxes": [{"zone": {"$ref": "#/zones/zone_a"},
                                    "width_mm": 400, "height_mm": 200}]}})
        self.assertTrue(result["ok"], result)
        boq = result["boq_summary"]["speakers"]
        self.assertGreaterEqual(boq["total_speakers"], 5)
        self.assertEqual(boq["total_boxes"], 1)

    def test_partition_whitelist_and_style(self):
        result = self.run_dockets({
            "partition_plan": {
                "keep_layers": {"outer_boundary": ["STORE-EDGE"],
                                "walls": ["WALLS"], "dividers": [],
                                "labels": ["TXT"]},
                "zone_styles": [{"zone": {"$ref": "#/zones/zone_a"},
                                 "color": 1, "lineweight": 50,
                                 "area_label": True}]}})
        self.assertTrue(result["ok"], result)
        boq = result["boq_summary"]["partition_plan"]
        self.assertGreaterEqual(boq["entities_kept"], 3)
        self.assertAlmostEqual(boq["styled_zones"][0]["area_m2"], 16.0,
                               delta=0.1)

    def test_electrical_wall_points(self):
        result = self.run_dockets({
            "electrical": {"point_groups": [
                {"match": {"block_names": ["FIXTURE_X"], "layers": []},
                 "place_as": "WP", "label": "WP1", "note": "test note"}]}})
        self.assertTrue(result["ok"], result)
        boq = result["boq_summary"]["electrical"]
        self.assertEqual(boq["counts"]["WP1"], 2)
        self.assertEqual(boq["legend"][0]["note"], "test note")

    def test_finishes_length_area(self):
        result = self.run_dockets({
            "finishes": [{"material": "Paint",
                          "targets": [{"$ref": "#/zones/zone_a"}],
                          "height_mm": 2400, "color": 5}]})
        self.assertTrue(result["ok"], result)
        item = result["boq_summary"]["finishes"]["items"][0]
        # ZONE-A's bottom edge (4 m) lies on the store edge = a wall
        self.assertGreaterEqual(item["length_m"], 3.9)
        self.assertAlmostEqual(item["area_m2"],
                               item["length_m"] * 2.4, delta=0.1)

    def test_skirting_running_metres(self):
        result = self.run_dockets({
            "skirting": {"rooms": [{"$ref": "#/zones/zone_a"}],
                         "height_mm": 100, "exclude": []}})
        self.assertTrue(result["ok"], result)
        boq = result["boq_summary"]["skirting"]
        self.assertGreaterEqual(boq["total_length_m"], 3.9)
        self.assertEqual(boq["height_mm"], 100)

    def test_end_to_end_all_dockets(self):
        result = self.run_dockets({
            "flooring": {"zones": [], "default_flooring_type": "tile600"},
            "speakers": {"placements": [{"zone": "store", "count": 2}]},
            "sprinklers": {"coverage_radius_mm": 2300},
            "partition_plan": {"keep_layers": {"walls": ["WALLS"]}},
            "electrical": {"point_groups": [
                {"match": {"block_names": ["FIXTURE_X"], "layers": []},
                 "place_as": "FP", "label": "FP1", "note": ""}]},
            "finishes": [{"material": "Paint", "targets": ["store"],
                          "height_mm": 2400, "color": 5}],
            "skirting": {"rooms": [{"$ref": "#/zones/zone_a"}],
                         "height_mm": 100},
        })
        self.assertTrue(result["ok"], result)
        out_dir = result["_out_dir"]
        for name in ("flooring", "speakers", "sprinklers", "partition_plan",
                     "electrical", "finishes", "skirting"):
            self.assertTrue(result["dockets"][name]["ok"],
                            f"{name}: {result['dockets'][name]}")
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, f"{name}.dxf")), name)
        self.assertTrue(os.path.exists(
            os.path.join(out_dir, "boq_summary.json")))
        self.assertTrue(os.path.exists(os.path.join(out_dir, "run_log.json")))
        with open(os.path.join(out_dir, "boq_summary.json"),
                  encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertEqual(set(summary.keys()),
                         {"flooring", "speakers", "sprinklers",
                          "partition_plan", "electrical", "finishes",
                          "skirting"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for scripts/tune.py (spec G1 to G5, F3 G-1 to G-4).

The end-to-end test runs make.py tune in the scratch repository of
tests/test_make.py, whose render stub writes the C5 tune files.

    python3 tests/test_tune.py [-v]
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import make  # noqa: E402
import test_make as tm  # noqa: E402
import tune  # noqa: E402

PANEL_BOXES = [{"name": "title", "x0": 72, "y0": 260, "x1": 600, "y1": 330, "color": "#f2f6fb", "size": 64},
               {"name": "clock", "x0": 88, "y0": 1168, "x1": 400, "y1": 1240, "color": "#ffffff", "size": 88},
               {"name": "count", "x0": 88, "y0": 1250, "x1": 500, "y1": 1290, "color": "#a8d8ff", "size": 40},
               {"name": "credit", "x0": 88, "y0": 1430, "x1": 560, "y1": 1480, "color": "#8e9bae", "size": 22}]


class Scores(unittest.TestCase):
    """G-3 on hand-made frames."""

    def test_white_frame_whiteout_is_one(self):
        white = np.full((1920, 1080, 3), 255, np.uint8)
        self.assertEqual(tune.whiteout(white, tune.map_mask(1920, 1080, PANEL_BOXES)), 1.0)
        dark = np.full((1920, 1080, 3), 12, np.uint8)
        self.assertEqual(tune.whiteout(dark, tune.map_mask(1920, 1080, PANEL_BOXES)), 0.0)

    def test_static_clip_has_no_motion(self):
        with tempfile.TemporaryDirectory() as td:
            frames = []
            for i in range(6):
                p = os.path.join(td, f"rt-clip-{i:03d}.png")
                im = Image.new("RGB", (720, 1280), (10, 12, 14))
                im.paste((200, 180, 90), (300, 700, 340, 740))
                im.save(p)
                frames.append(p)
            mask = tune.map_mask(1280, 720, PANEL_BOXES, scale=720 / 1080)
            motion, strobe = tune.motion_strobe(frames, mask)
            self.assertEqual((motion, strobe), (0.0, 0.0))
            Image.new("RGB", (720, 1280), (250, 250, 250)).save(frames[3])
            motion, strobe = tune.motion_strobe(frames, mask)
            self.assertGreater(motion, 0)
            self.assertGreater(strobe, tune.LIMITS["strobe"])

    def test_vehicles_under_the_panel_give_zero(self):
        pr = tune.panel_rect(PANEL_BOXES)
        self.assertEqual(pr, [60, 1140, 620, 1500])
        under = []
        for i in range(50):
            under += [100 + i * 8, 1300 + (i % 10) * 15, 1]
        self.assertEqual(tune.safe_share(under, PANEL_BOXES), 0.0)
        clear = []
        for i in range(50):
            clear += [700 + (i % 5) * 20, 500 + i * 10, 1]
        clear += [10, 10, 0]  # outside the boundary: not counted
        self.assertEqual(tune.safe_share(clear, PANEL_BOXES), 1.0)
        self.assertEqual(tune.safe_share(under + clear, PANEL_BOXES), 0.5)

    def test_card_over_a_white_map_breaks_contrast(self):
        card = [{"name": "card_title", "x0": 72, "y0": 480, "x1": 800, "y1": 600, "color": "#f2f6fb", "size": 120},
                {"name": "card_line1", "x0": 72, "y0": 700, "x1": 800, "y1": 740, "color": "rgba(242,246,251,0.85)", "size": 32}]
        white = np.full((1920, 1080, 3), 250, np.uint8)
        c = tune.text_contrast(card, white, tune.CARD_TEXT)
        self.assertLess(c, tune.LIMITS["contrast"])
        self.assertIn("contrast", tune.breaks_of({"contrast": c}))
        band = np.full((1920, 1080, 3), 250, np.uint8)
        band[400:800] = (8, 13, 21)
        self.assertGreater(tune.text_contrast(card, band, tune.CARD_TEXT), 6)

    def test_card_cover(self):
        card = [{"name": "card_title", "x0": 72, "y0": 480, "x1": 800, "y1": 600},
                {"name": "card_line0", "x0": 72, "y0": 650, "x1": 700, "y1": 700}, {"name": "clock", "y0": 1200, "y1": 1290}]
        # Rows 420..760 are the band: 2 of the 4 on-screen inside vehicles sit in it, whatever their x.
        veh = [900, 430, 1, 100, 755, 1, 500, 300, 1, 500, 1000, 1, 500, 600, 0, 2000, 600, 1]
        self.assertEqual(tune.card_cover(veh, card), 0.5)
        self.assertIsNone(tune.card_cover(veh, card[2:]))
        self.assertIn("card_cover", tune.breaks_of({"card_cover": 0.47}))
        self.assertEqual(tune.breaks_of({"card_cover": 0.3}), [])

    def test_sizes(self):
        boxes = [dict(PANEL_BOXES[3], size=20), {"name": "card_line1", "size": 30}]
        self.assertEqual(tune.size_breaks([boxes]), ["credit 20 < 22"])
        self.assertIn("sizes", tune.breaks_of({"sizes": ["credit 20 < 22"]}))


class Arms(unittest.TestCase):
    def test_table(self):
        self.assertEqual(tune.arm_values("trailmin", 8), (5, 12))
        self.assertEqual(tune.arm_values("dotcore", 0), (1.0, 1.8))
        self.assertEqual(tune.arm_values("dotcore", 2.4), (1.8, 3.0))
        self.assertEqual(tune.arm_values("dotcore", 1.5), (1.2, 2.1))
        self.assertEqual(tune.arm_values("cardline", 1), (0, 2))
        self.assertEqual(tune.arm_values("cardsize", 132), (108, 156))
        self.assertEqual(tune.arm_values("layeralpha", 0.4), (0.3, 0.55))
        self.assertEqual(tune.arm_values("shoulder", 0.8), (0.48, 1))
        self.assertEqual(tune.arm_values("cx", 0.0, 49.0), (-1.96, 1.96))
        self.assertEqual(tune.arm_values("halor", 11), (8, 14))
        self.assertEqual(tune.arm_values("outside", 0.55), (0.35, 0.75))
        self.assertEqual(tune.arm_values("outside", 0.8), (0.6, 0.95))
        self.assertEqual(tune.arm_values("outside", 0.1), (0, 0.3))
        self.assertEqual(tune.arm_values("warpfloor", 0.15), (0.08, 0.25))

    def test_frame_arms_stay_inside_the_trim_box(self):
        frame = {"km_vertical": 19.0, "center_km": [3.0, -2.2]}
        trim = make.trim_box(frame, 1.25)
        self.assertEqual(tune.fit_frame_arm("zoom", 0.9, frame, trim, 1, 0, 0), 0.9)
        z = tune.fit_frame_arm("zoom", 0.7, frame, trim, 1, 0, 0)
        self.assertGreater(z, 0.7)
        self.assertTrue(make.box_inside(make.frame_box(frame, z), trim))
        self.assertFalse(make.box_inside(make.frame_box(frame, z - 0.01), trim))
        dx = tune.fit_frame_arm("cx", 3.0, frame, trim, 1, 0, 0)
        self.assertTrue(make.box_inside(make.frame_box(frame, 1, dx, 0), trim))
        self.assertLess(dx, 3.0)

    def test_pass_knobs(self):
        knobs = lambda v: [k[0] for k in tune.KNOBS if v in k[5]]
        self.assertEqual(knobs("day"), list(range(1, 24)))
        self.assertEqual(knobs("rush"), [1, 2, 3, 4, 19])
        self.assertEqual(knobs("week"), [4, 10, 12, 17, 19])

    def test_auto_pick(self):
        def arm(label, value, breaks=(), **scores):
            return {"label": label, "value": value, "scores": scores, "breaks": list(breaks)}
        # frame: highest safe_share; ties within 0.01 go to the arm nearest v
        self.assertEqual(tune.auto_pick("frame", [arm("a", 0.9, safe_share=0.95), arm("b", 1.0, safe_share=0.91),
                                                  arm("c", 1.1, safe_share=0.80)], 1.0), "a")
        self.assertEqual(tune.auto_pick("frame", [arm("a", 0.9, safe_share=0.95), arm("b", 1.0, safe_share=0.945),
                                                  arm("c", 1.1, safe_share=0.80)], 1.0), "b")
        # look: lowest whiteout among calm arms, ties within 0.002 to b
        self.assertEqual(tune.auto_pick("look", [arm("a", 1, whiteout=0.001, strobe=0.003), arm("b", 2, whiteout=0.010, strobe=0.001),
                                                 arm("c", 3, whiteout=0.004, strobe=0.001)], 2), "c")
        self.assertEqual(tune.auto_pick("look", [arm("a", 1, whiteout=0.004), arm("b", 2, whiteout=0.005),
                                                 arm("c", 3, whiteout=0.009)], 2), "b")
        # hard limits first; when every arm breaks, the fewest breaks
        self.assertEqual(tune.auto_pick("look", [arm("a", 1, ["whiteout"], whiteout=0.04), arm("b", 2, ["whiteout", "strobe"], whiteout=0.031),
                                                 arm("c", 3, ["whiteout"], whiteout=0.05)], 2), "a")
        # card: highest contrast, ties within 0.5 to b; warp: b
        self.assertEqual(tune.auto_pick("card", [arm("a", 1, contrast=7.9), arm("b", 2, contrast=7.5), arm("c", 3, contrast=6)], 2), "b")
        self.assertEqual(tune.auto_pick("card", [arm("a", 1, contrast=9.0), arm("b", 2, contrast=7.5), arm("c", 3, contrast=6)], 2), "a")
        self.assertEqual(tune.auto_pick("warp", [arm("a", 1), arm("b", 2), arm("c", 3)], 2), "b")

    def test_why(self):
        tune.check_why("12 min keeps Highway 7 continuous without fusing Viva into YRT")
        for bad in ("", "x" * 121, "long \u2014 dash", "two\nlines"):
            with self.assertRaises(tune.TuneError):
                tune.check_why(bad)


class EndToEnd(tm.Scratch):
    """G-1, G-2 and G-4 with the render stub."""

    def setUp(self):
        super().setUp()
        self.repo = tm.FakeRepo(self.tmp)
        r = self.repo.read_json("cities/recipes/test-centre.json")
        r["variety"]["zoom"] = 1.05
        self.repo.write_json("cities/recipes/test-centre.json", r)
        self.original = r
        self.repo.run("lock", "test")
        self.repo.run("build", "test")

    def stamps(self):
        out = {}
        for root, _d, names in os.walk(os.path.join(self.repo.root, "build")):
            for n in names:
                if n in ("stamp.json", "manifest.json") or root.endswith("stamps") or n.endswith(".json.gz"):
                    p = os.path.join(root, n)
                    out[os.path.relpath(p, self.repo.root)] = tm.read_bytes(p)
        return out

    def test_one_pass(self):
        before = self.stamps()
        self.repo.clear_calls()
        rid = "test-centre"
        d = os.path.join(self.repo.root, "build", rid, "tune", "day")
        out = self.repo.run("tune", rid, "--next").stdout
        self.assertIn("auto pick", out)
        k1 = os.path.join(d, "01-zoom")
        for x in ("a", "b", "c"):
            self.assertTrue(os.path.exists(os.path.join(k1, x, "config.json")))
        scores = tm.load(os.path.join(k1, "scores.json"))
        self.assertEqual([a["value"] for a in scores["arms"]], [0.95, 1.05, 1.16])
        self.assertEqual(set(scores["arms"][0]["scores"]), {"whiteout", "contrast", "safe_share", "motion", "strobe",
                                                            "card_cover", "sizes", "ms_per_frame"})
        res = self.repo.run("tune", rid, "--next", check=False)
        self.assertIn("waiting for --pick", res.stdout)
        self.repo.run("tune", rid, "--pick", "zoom=c", "--why", "the city fills the space above the panel")
        state = tm.load(os.path.join(d, "state.json"))
        self.assertEqual(state["values"], {"FRAME_ZOOM": 1.16})
        # The next knob's arm b is the zoom knob's chosen arm, linked, not rendered again.
        self.repo.clear_calls()
        self.repo.run("tune", rid, "--next")
        self.assertEqual(len([c for c in self.repo.calls() if c.startswith("render tune")]), 2)
        self.assertEqual(os.stat(os.path.join(d, "02-cx", "b", "still-0800.png")).st_ino,
                         os.stat(os.path.join(k1, "c", "still-0800.png")).st_ino)
        self.repo.run("tune", rid, "--pick", "cx=b", "--why", "centred already")
        for knob, label in (("trailmin", "c"), ("halor", "a"), ("cardline", "c"), ("warpfloor", "b")):
            self.repo.run("tune", rid, "--knob", knob)
            self.repo.run("tune", rid, "--pick", f"{knob}={label}", "--why", f"{knob} reads better at the peak")
        kt = os.path.join(d, "04-trailmin")
        for sheet in ("sheet.jpg", "crops.jpg", "motion.jpg"):
            self.assertTrue(os.path.exists(os.path.join(kt, sheet)), sheet)
        self.assertTrue(os.path.exists(os.path.join(d, "19-cardline", "card.jpg")))
        self.assertTrue(os.path.exists(os.path.join(d, "17-warpfloor", "clock.jpg")))
        self.assertEqual(tm.load(os.path.join(d, "19-cardline", "scores.json"))["arms"][0]["value"], 1)
        # G-4: every sheet within the judge's image limit
        for root, _d, names in os.walk(d):
            for n in names:
                if n.endswith(".jpg"):
                    with Image.open(os.path.join(root, n)) as im:
                        self.assertLessEqual(max(im.size), 1568, n)
        with Image.open(os.path.join(k1, "sheet.jpg")) as im:
            self.assertEqual(im.size, (1398, 1556))
        with Image.open(os.path.join(kt, "crops.jpg")) as im:
            self.assertEqual(im.size, (744, 1188))
        # G-1 --apply
        self.repo.run("tune", rid, "--apply")
        r = self.repo.read_json(f"cities/recipes/{rid}.json")
        ov = r["override"]
        stored = make.r6(1.16 / 1.05)
        self.assertEqual(ov["variant_render"]["day"]["FRAME_ZOOM"], stored)
        self.assertEqual(ov["variant_render"]["week"]["FRAME_ZOOM"], stored)
        self.assertNotIn("FRAME_DX_KM", ov["variant_render"]["day"], "a pick equal to the current value writes nothing")
        self.assertEqual(ov["variant_render"]["day"]["TRAIL_MINUTES"], 60)
        self.assertNotIn("TRAIL_MINUTES", ov["render"])
        self.assertEqual(ov["render"]["BUS_HALO_R"], 8)
        self.assertNotIn("TIME_WARP_FLOOR", json.dumps(ov["variant_render"]))
        self.assertEqual(r["variety"]["card_line"], 2)
        self.assertEqual(ov["_why"]["day.FRAME_ZOOM"], "the city fills the space above the panel")
        self.assertEqual(ov["_why"]["BUS_HALO_R"], "halor reads better at the peak")
        self.assertEqual(ov["_why"]["CARD_LINES"], "cardline reads better at the peak")
        # nothing outside override and variety.card_line
        strip = lambda x: {k: (dict(v, card_line=None) if k == "variety" else v) for k, v in x.items() if k != "override"}
        self.assertEqual(strip(r), strip(self.original))
        self.assertEqual(list(r), list(self.original))
        self.assertTrue(tm.read_text(os.path.join(self.repo.root, f"cities/recipes/{rid}.json")).startswith('{\n "id": '))
        # D3.8 turns the stored zoom back into the tuned one
        pl = make.Pipeline(self.repo.root)
        rj, _ = pl.render_query(pl.recipe(rid), "day")
        self.assertAlmostEqual(rj["FRAME_ZOOM"], 1.16, places=5)
        # G-2: no data step ran and no stamp changed
        self.assertFalse([c for c in self.repo.calls() if c.split()[0] in ("build_area", "trim", "basemap", "fetch_overture")])
        self.assertEqual(self.stamps(), before)

    def test_rush_and_week_passes(self):
        out = self.repo.run("tune", "test-centre", "--variant", "week", "--auto").stdout
        self.assertIn("every knob of the week pass has a pick", out)
        state = tm.load(os.path.join(self.repo.root, "build/test-centre/tune/week/state.json"))
        self.assertEqual([h["knob"] for h in state["history"]], ["trailmin", "dotcore", "haloalpha", "warpfloor", "cardline"])
        self.assertTrue(all(h["picked"] for h in state["history"]))
        res = self.repo.run("tune", "test-north", "--variant", "week", "--next", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.repo.run("tune", "test-north", "--variant", "rush", "--knob", "zoom")
        self.repo.run("tune", "test-north", "--variant", "rush", "--pick", "zoom=c", "--why", "a real close-up")
        self.repo.run("tune", "test-north", "--variant", "rush", "--apply")
        r = self.repo.read_json("cities/recipes/test-north.json")
        self.assertEqual(r["override"]["variant_render"], {"rush": {"FRAME_ZOOM": 1.1}})
        self.assertEqual(r["override"]["_why"], {"rush.FRAME_ZOOM": "a real close-up"})


if __name__ == "__main__":
    unittest.main()

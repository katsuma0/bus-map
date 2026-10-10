"""Part A tests of the city trim, counts, brands and output (A8 to A12; F3 A-5 to A-10, A-12, A-13).

    python3 -I tests/test_trim.py           # fixtures and unit tests, under a minute
    python3 -I tests/test_trim.py --gta     # also the ten GTA cities (needs data/gta/gtfs and Overture access
                                            # for the CA-ON divisions; about 10 minutes the first time)

make.py does not exist yet, so city configs come from the stand-in
derivation in tests/fixtures/gtfs/v4util.py.
"""
import collections
import datetime as dt
import gzip
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fixtures", "gtfs"))
if "--gta" in sys.argv:
    sys.argv.remove("--gta")
    os.environ["BUSMAP_GTA"] = "1"
import v4util as U  # noqa: E402

import numpy as np  # noqa: E402
import shapely  # noqa: E402
from shapely.geometry import shape as geo_shape  # noqa: E402

import area_store  # noqa: E402
import build_network as bn  # noqa: E402
import trim_network as tn  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "gtfs")
BOUNDARY = os.path.join(FIX, "boundary.geojson")
ORIGIN = (-79.47, 43.80)
AREA_BOX = [-80.06, 43.06, -78.58, 44.25]
CLIP = [-79.60, 43.60, -79.20, 43.85]
WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NETWORK_META_KEYS = [
    "schema", "kind", "id", "batch", "area", "place", "title", "subtitle", "service_date", "origin", "day_start", "day_end",
    "frame", "trim", "modes", "attribution", "credit", "build_key", "feeds", "trips_total", "timeline", "hist_period",
    "am_peak", "pm_peak", "hist_by_mode", "groups", "hist_by_group", "boundary", "panel", "brands", "color_by", "preset",
    "theme", "render", "variants", "card"]
FEED_KEYS = ["id", "name", "publisher", "licence_id", "licence_text", "version", "sha256", "month_used", "dates", "rule",
             "excluded", "classes", "drawn", "mean_trips_per_day", "inside_vehicle_minutes", "inside_share", "major"]


def fixture_feed(fid):
    return {"id": fid, "name": fid.title(), "publisher": "Fixture", "licence_id": "cc-by-4.0", "licence_text": "CC BY 4.0"}


def build_fixture(tmp, fid, kind, month="2026-10", seed="0", variants=None):
    """Area store and trimmed network of one fixture feed in `tmp`; returns (store dir, network path, city config)."""
    area = U.area_config("fx", ORIGIN, AREA_BOX, FIX, [fixture_feed(fid)], kind, month=month, chunk=5000)
    tag = f"{fid}-{kind}-{month}"
    acfg = os.path.join(tmp, f"area.{tag}.json")
    U.dump_json(acfg, area)
    store = os.path.join(tmp, "areas", tag)
    U.run("build_area.py", "--config", acfg, "--out", store, seed=seed)
    frame = U.fit_frame(U.boundary_bbox_km(BOUNDARY, ORIGIN))
    vs = variants or (["day", "rush"] if kind == "day" else ["week"])
    city = U.city_config(f"fx-{fid}", "Fixture", ORIGIN, BOUNDARY, frame, vs, kind, batch="fx", area="fx")
    ccfg = os.path.join(tmp, f"city.{tag}.json")
    U.dump_json(ccfg, city)
    out = os.path.join(tmp, f"fx-{fid}", tag, kind, "network.json.gz")
    args = ["--config", ccfg, "--area", store, "--out", out]
    if kind == "week":
        day = os.path.join(tmp, f"fx-{fid}", f"{fid}-day-{month}", "day", "network.json.gz")
        if os.path.exists(day):
            args += ["--day-network", day]
    p, _s, _g = U.run("trim_network.py", *args, seed=seed, check=False)
    return store, out, city, p


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def boundary_polygon(city):
    bn.set_origin(city["origin"])
    feat = tn.load_feature(city["boundary"]["file"])
    return tn.project(geo_shape(feat["geometry"])).simplify(city["boundary"]["simplify_km"], preserve_topology=True)


def raw_hist(store_dir, city):
    """hist before rounding, as trim_network computes it."""
    st = area_store.Store(store_dir)
    bn.set_origin(st.meta["origin"])
    poly = boundary_polygon(city)
    shapely.prepare(poly)
    bbox = [round(float(v), 3) for v in poly.bounds]
    routes = st.meta["routes"]
    route_local = np.arange(len(routes))
    cnt, _n = tn.inside_counts(st, poly, bbox, route_local, len(routes))
    feed_idx = {f["id"]: i for i, f in enumerate(st.meta["feeds"])}
    fold = tn.Folder(st, cnt, np.array([feed_idx[r["feed"]] for r in routes]))
    return fold(np.ones(len(routes), dtype=bool)), st, poly


def scalar_position(t, d, xy, cum, T):
    """The CONTRACT rule, written out one step at a time."""
    if T >= t[-1]:
        dist = d[-1]
    else:
        i = max(j for j in range(len(t)) if t[j] <= T)
        dist = d[i] + (T - t[i]) / (t[i + 1] - t[i]) * (d[i + 1] - d[i])
    k = 0
    while k < len(cum) - 2 and cum[k + 1] <= dist:
        k += 1
    f = min(1.0, max(0.0, (dist - cum[k]) / (cum[k + 1] - cum[k])))
    return xy[k][0] + f * (xy[k + 1][0] - xy[k][0]), xy[k][1] + f * (xy[k + 1][1] - xy[k][1])


def inside_minutes(t, d, xy, cum, poly):
    n = 0
    for m in range(-(-t[0] // 60), t[-1] // 60 + 1):
        x, y = scalar_position(t, d, xy, cum, 60 * m)
        n += bool(shapely.contains_xy(poly, x, y))
    return n


def brute_force_vm(fid, store_meta, poly):
    """A-5: every member trip on every selected date, built by build_feed for that date; mean per day class."""
    total = 0.0
    f = [x for x in store_meta["feeds"] if x["id"] == fid][0]
    for k, bc in f["by_class"].items():
        per_date = []
        for ds in bc["dates"]:
            bn.set_origin(ORIGIN)
            feed = bn.Feed(fid, os.path.join(FIX, f"{fid}.zip"))
            routes, ri, shapes, si = [], {}, [], {}
            stats = bn.build_feed(feed, dt.date.fromisoformat(ds), {"id": "bf", "clip": CLIP, "modes": U.MODES},
                                  routes, ri, shapes, si, collections.defaultdict(list))
            vm = 0
            for tr in stats["trips"]:
                sh = shapes[tr["s"]]
                xy = list(zip(sh["xy"][0::2], sh["xy"][1::2]))
                vm += inside_minutes(tr["t"], tr["d"], xy, sh["cum"], poly)
            per_date.append(vm)
        if per_date:
            total += sum(per_date) / len(per_date)
    return total


class Fixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="test_trim_")
        cls.built = {}
        for fid in ("stable", "drtlike"):
            for kind in ("day", "week"):
                cls.built[(fid, kind)] = build_fixture(cls.tmp, fid, kind)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def net(self, fid, kind):
        store, out, city, p = self.built[(fid, kind)]
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return U.load_json(out), store, city

    def test_mean_hist_equals_brute_force(self):
        """A-5, both rules: drtlike's Tuesday and Wednesday use the median-date rule."""
        for fid in ("stable", "drtlike"):
            for kind in ("day", "week"):
                net, store, city = self.net(fid, kind)
                h, st, poly = raw_hist(store, city)
                want = brute_force_vm(fid, st.meta, poly)
                self.assertGreater(want, 0)
                self.assertLess(abs(h.sum() - want) / want, 1e-9, f"{fid} {kind}: {h.sum()} vs {want}")
                self.assertEqual(net["hist"], np.round(h, 2).tolist())
        rules = self.net("drtlike", "week")[0]["meta"]["feeds"][0]["rule"]
        self.assertEqual(rules["tue"], "median-date")
        self.assertEqual(rules["thu"], "half")

    def test_folding_loses_nothing(self):
        """A-6: the folded hist holds every inside minute of every trip, whatever its clock time."""
        for fid in ("stable", "drtlike"):
            for kind in ("day", "week"):
                net, store, city = self.net(fid, kind)
                h, st, poly = raw_hist(store, city)
                unfolded = 0.0
                n_dates = st.meta["n_dates"][fid]
                runs = np.asarray(st.trip_runs)
                for i in range(st.n_trips):
                    t, d = st.trip_arrays(i)
                    xy, cum = st.shape_arrays(int(st.trip_s[i]))
                    n = inside_minutes(t.tolist(), d.tolist(), xy.tolist(), cum.tolist(), poly)
                    for kj, k in enumerate(st.day_classes):
                        if n_dates[k]:
                            unfolded += runs[i, kj] * n / n_dates[k]
                self.assertLess(abs(h.sum() - unfolded), 1e-6 * unfolded)
                self.assertLessEqual(abs(sum(net["hist"]) - unfolded), 0.005 * len(net["hist"]))
                if fid == "stable":
                    self.assertTrue(any(t["t"][-1] > 86400 for t in net["trips"]), "the fixture has after-midnight trips")

    def test_meta_keys_and_shapes(self):
        for kind in ("day", "week"):
            net, _store, _city = self.net("stable", kind)
            m = net["meta"]
            self.assertEqual([k for k in NETWORK_META_KEYS if k not in m], [])
            self.assertEqual(m["schema"], 4)
            self.assertEqual(m["trips_total"], len(net["trips"]))
            self.assertEqual(len(net["hist"]), m["hist_period"])
            self.assertEqual(m["hist_period"], 1440 if kind == "day" else 10080)
            self.assertEqual(m["timeline"]["period"], 86400 if kind == "day" else 604800)
            for f in m["feeds"]:
                self.assertEqual([k for k in FEED_KEYS if k not in f], [])
            for g, h in m["hist_by_group"].items():
                self.assertEqual(len(h), m["hist_period"])
            self.assertEqual(sorted(m["hist_by_group"]), sorted(g["id"] for g in m["groups"]))
            for r in net["routes"]:
                self.assertTrue(0 <= r["brand"] < len(m["brands"]))
                for k in ("id", "short", "long", "color", "feed", "mode", "agency", "type", "color_raw", "group"):
                    self.assertIn(k, r)
            shares = [b["share"] for b in m["brands"]]
            self.assertEqual(shares, sorted(shares, reverse=True))
            for t in net["trips"]:
                self.assertEqual(list(t)[:4], ["r", "s", "t", "d"])
                self.assertEqual("w" in t, kind == "week")
                if kind == "week":
                    self.assertTrue(1 <= t["w"] <= 127)
            self.assertNotIn("{month}", json.dumps(m["variants"]))
            self.assertEqual(m["subtitle"], next(iter(m["variants"].values()))["label"])

    def test_peaks_read_the_rounded_hist(self):
        for fid in ("stable", "drtlike"):
            for kind in ("day", "week"):
                m = self.net(fid, kind)[0]["meta"]
                h = self.net(fid, kind)[0]["hist"]
                P = m["hist_period"]
                for v, V in m["variants"].items():
                    mins = range(-(-V["start"] // 60), -(-V["end"] // 60))
                    best = max(h[x % P] for x in mins)
                    self.assertEqual(V["peak"]["count"], round(best))
                    self.assertEqual(h[(V["peak"]["time"] // 60) % P], best)
                    self.assertTrue(V["start"] <= V["peak"]["time"] < V["end"])
                am = m["am_peak"]
                self.assertTrue(300 * 60 <= am["time"] <= 630 * 60)
                self.assertEqual(m["day_start"], next(iter(m["variants"].values()))["start"])
                if kind == "day":
                    self.assertEqual(m["variants"]["day"]["start"], am["time"])
                    self.assertEqual(m["variants"]["day"]["end"], am["time"] + 86400)
                    self.assertEqual((m["variants"]["rush"]["start"], m["variants"]["rush"]["end"]), (23400, 34200))

    def test_mask_and_trim_box(self):
        """A-7 on the fixture: every cell agrees with contains_xy at its centre; bbox inside the trim box."""
        net, store, city = self.net("stable", "day")
        b = net["meta"]["boundary"]
        poly = boundary_polygon(city)
        grid = tn.decode_mask(b["mask"])
        mk = b["mask"]
        self.assertEqual(grid.shape, (mk["ny"], mk["nx"]))
        cx = mk["x0"] + (np.arange(mk["nx"]) + 0.5) * mk["cell_km"]
        cy = mk["y0"] + (np.arange(mk["ny"]) + 0.5) * mk["cell_km"]
        gx, gy = np.meshgrid(cx, cy)
        self.assertTrue(np.array_equal(grid, shapely.contains_xy(poly, gx, gy)))
        self.assertGreater(grid.sum(), 0)
        self.assertAlmostEqual(grid.sum() * mk["cell_km"] ** 2, poly.area, delta=0.05)
        box = net["meta"]["trim"]["box_km"]
        bb = b["bbox_km"]
        self.assertTrue(box[0] <= bb[0] and box[1] <= bb[1] and bb[2] <= box[2] and bb[3] <= box[3])
        self.assertEqual(len(b["rings"]), 1)
        self.assertEqual(b["holes"], [])

    def test_copies_in_a_row(self):
        """The duplicate pair is one class drawn twice, in consecutive entries."""
        net, store, city = self.net("drtlike", "day")
        seen = collections.Counter(json.dumps([t["r"], t["s"], t["t"]]) for t in net["trips"])
        twice = [k for k, n in seen.items() if n == 2]
        self.assertEqual(len(twice), 1)
        idx = [i for i, t in enumerate(net["trips"]) if json.dumps([t["r"], t["s"], t["t"]]) == twice[0]]
        self.assertEqual(idx[1], idx[0] + 1)

    def test_week_masks_follow_the_rules(self):
        net, store, city = self.net("drtlike", "week")
        st = area_store.Store(store)
        tue = [t for t in net["trips"] if t["w"] >> 1 & 1]
        # Tuesday under the median-date rule: exactly the trips of Oct 6.
        self.assertEqual(len(tue), st.meta["feeds"][0]["by_class"]["tue"]["drawn"])
        days = collections.Counter(k for t in net["trips"] for k in range(7) if t["w"] >> k & 1)
        self.assertEqual(sorted(days), list(range(7)))

    def test_determinism_across_seeds(self):
        """A-8 on the fixtures: store and network bytes do not depend on PYTHONHASHSEED."""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for kind in ("day", "week"):
                s1, n1, _c, p1 = build_fixture(a, "drtlike", kind, seed="0")
                s2, n2, _c, p2 = build_fixture(b, "drtlike", kind, seed="1")
                self.assertEqual(p1.returncode, 0)
                for name in sorted(os.listdir(s1)):
                    with open(os.path.join(s1, name), "rb") as f1, open(os.path.join(s2, name), "rb") as f2:
                        self.assertEqual(f1.read(), f2.read(), name)
                with open(n1, "rb") as f1, open(n2, "rb") as f2:
                    self.assertEqual(f1.read(), f2.read())

    def test_week_not_eligible_exits_3(self):
        """A-9 on a fixture: a major feed whose dates all come from a fallback window."""
        with tempfile.TemporaryDirectory() as tmp:
            _s, day, _c, p = build_fixture(tmp, "stable", "day", month="2026-09")
            self.assertEqual(p.returncode, 0, p.stderr)
            m = U.load_json(day)["meta"]
            self.assertEqual(m["timeline"]["month_label"], "October")
            self.assertEqual(m["variants"]["day"]["label"], "An average October weekday")
            self.assertEqual(m["timeline"]["fallback_feeds"], ["stable"])
            self.assertTrue(m["feeds"][0]["major"])
            _s, week, _c, p = build_fixture(tmp, "stable", "week", month="2026-09")
            self.assertEqual(p.returncode, 3)
            line = json.loads(p.stdout.strip().splitlines()[-1])
            self.assertFalse(line["eligible"])
            self.assertIn("stable", line["why"][0])
            self.assertFalse(os.path.exists(week))


    def test_missing_config_key_fails(self):
        """make.py always writes these keys; a config without one fails by name, never takes a private default."""
        with tempfile.TemporaryDirectory() as tmp:
            store, out, city, p = build_fixture(tmp, "stable", "day")
            self.assertEqual(p.returncode, 0, p.stderr)
            for drop in ("fit_box", "major_share"):
                cfg = {k: v for k, v in city.items() if k != drop}
                path = os.path.join(tmp, f"no-{drop}.json")
                U.dump_json(path, cfg)
                p, _s, _g = U.run("trim_network.py", "--config", path, "--area", store, "--out",
                                  os.path.join(tmp, drop, "network.json.gz"), check=False)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(f"missing {drop}", p.stderr)


class Units(unittest.TestCase):
    def test_informative(self):
        self.assertTrue(tn.informative("ED1C24"))
        self.assertTrue(tn.informative("ff8000"))
        self.assertFalse(tn.informative("808080"))
        self.assertFalse(tn.informative("FFFFFF"))
        self.assertFalse(tn.informative("000000"))
        self.assertFalse(tn.informative(""))
        self.assertFalse(tn.informative("12345"))

    def test_brands(self):
        """A-10 rules on synthetic routes, with the GTA brand entries."""
        entries = U.load_json(os.path.join(U.GTA_FIX, "brands.json"))["agencies"]
        R = lambda fid, rid, short, mode, color, agency="X": {"id": f"{fid}:{rid}", "feed": fid, "short": short,  # noqa: E731
                                                              "long": "", "mode": mode, "color_raw": color, "agency": agency}
        routes = [R("ttc", "1", "1", "rail", "D5C82B"), R("ttc", "6", "6", "streetcar", "808080"),
                  R("ttc", "504", "504", "streetcar", "ED1C24"), R("ttc", "29", "29", "bus", "ED1C24"),
                  R("ttc", "5", "5", "streetcar", "FF8000"),
                  R("brampton", "501", "501", "bus", ""), R("brampton", "1", "1", "bus", ""),
                  R("miway", "1", "1", "bus", "D33517"), R("go", "LW", "LW", "rail", "98002E"),
                  R("other", "a", "a", "bus", "112233", "Acme"), R("other", "b", "b", "bus", "112233", "Acme"),
                  R("other", "c", "c", "bus", "445566", "Acme"), R("blank", "z", "z", "bus", "", "Nobody"),
                  R("blank", "y", "y", "rail", "", "Nobody")]
        brand, defs = tn.assign_brands(routes, entries, U.MODES)
        self.assertEqual(brand, ["ttc:line:d5c82b", "ttc", "ttc", "ttc", "ttc:line:ff8000", "brampton:zum", "brampton",
                                 "miway", "go", "other:acme", "other:acme", "other:acme", "mode:bus", "mode:rail"])
        self.assertEqual(defs["ttc:line:d5c82b"]["kind"], "line")
        self.assertEqual(defs["ttc:line:d5c82b"]["entry"], "ttc")
        self.assertEqual(defs["brampton:zum"]["kind"], "rule")
        self.assertEqual(defs["brampton:zum"]["hex"], "e31837")
        self.assertEqual(defs["other:acme"], {**defs["other:acme"], "kind": "gtfs", "hex": "112233", "verified": False})
        self.assertEqual(defs["mode:bus"]["hex"], "")
        self.assertEqual(defs["miway"]["alt"], "026bcd")

    def test_auto_rush_frame(self):
        rng = np.random.default_rng(7)
        # 80 vehicles in a 3 km cluster at (5, 2), 20 spread over the day frame
        vx = np.concatenate([5 + rng.normal(0, 0.8, 80), rng.uniform(-12, 12, 20)])
        vy = np.concatenate([2 + rng.normal(0, 0.8, 80), rng.uniform(-20, 20, 20)])
        day = {"km_vertical": 44.0, "center_km": [0.0, 0.0]}
        box = [-15.0, -29.0, 15.0, 29.0]
        r = tn.auto_rush_frame(vx, vy, day, box, [50, 390, 870, 1300], [1.4, 2.2], 0.6)
        self.assertEqual(r["zoom"], 2.2)
        self.assertEqual(r["frame"]["km_vertical"], 20.0)
        self.assertGreaterEqual(r["count"], 78)
        # the cluster centre lies well inside the fit box (screen x 50..870, y 390..1300)
        sx, sy = tn.to_screen(np.array([5.0]), np.array([2.0]), r["frame"])
        self.assertTrue(150 <= sx[0] <= 770 and 490 <= sy[0] <= 1200, (sx, sy))
        # spread out: no zoom holds 60%, so 1.4 with its best centre
        vx2, vy2 = rng.uniform(-12, 12, 100), rng.uniform(-20, 20, 100)
        r2 = tn.auto_rush_frame(vx2, vy2, day, box, [50, 390, 870, 1300], [1.4, 2.2], 0.6)
        self.assertEqual(r2["zoom"], 1.4)
        f = tn.frame_box(r2["frame"])
        self.assertTrue(tn.inside_box(f, box))

    def test_panel_side(self):
        frame = {"km_vertical": 40.0, "center_km": [0.0, 0.0]}
        s = 1920 / 40.0
        # screen (200, 1300) is under the left panel, (700, 1300) under the right one
        lx, ly = (200 - 540) / s, (960 - 1300) / s
        rx = (700 - 540) / s
        panel = {"preferred": "right", "tie": 0.10, "rect": [60, 1140, 620, 1500]}
        p = tn.panel_side(np.array([lx] * 10 + [rx] * 3), np.array([ly] * 13), frame, panel)
        self.assertEqual(p["side"], "right")
        self.assertEqual(p["inside_under"], {"left": 10, "right": 3})
        p = tn.panel_side(np.array([lx] * 10 + [rx] * 10), np.array([ly] * 20), frame, dict(panel, preferred="left"))
        self.assertEqual(p["side"], "left")
        p = tn.panel_side(np.array([lx] * 3 + [rx] * 10), np.array([ly] * 13), frame, panel)
        self.assertEqual(p["side"], "left")

    def test_credit_wrap(self):
        short = "Data: YRT, GO, TTC · Map: Overture, OSM"
        self.assertEqual(len(tn.wrap_lines(short, 22, 504)), 1)
        long = "Data: " + ", ".join(["Durham Region Transit"] * 6) + " · Map: Overture, OSM"
        self.assertGreater(len(tn.wrap_lines(long, 22, 504)), 2)

    def test_mask_rle_round_trip(self):
        poly = shapely.Polygon([(0, 0), (1, 0), (1, 1), (0.5, 0.4), (0, 1)])
        mask, grid = tn.build_mask(poly, [0, 0, 1, 1], 0.025)
        self.assertTrue(np.array_equal(tn.decode_mask(mask), grid))
        self.assertEqual(sum(mask["rle"]), mask["nx"] * mask["ny"])
        self.assertEqual(mask["x0"], -0.1)


# ---------------------------------------------------------------- GTA

E2 = {"gta-toronto": ("county", 661.7), "gta-mississauga": ("locality", 295.8), "gta-brampton": ("locality", 267.8),
      "gta-markham": ("locality", 211.4), "gta-vaughan": ("locality", 272.4), "gta-oakville": ("locality", 153.5),
      "gta-richmond-hill": ("locality", 101.9), "gta-burlington": ("locality", 198.7),
      "gta-oshawa": ("locality", 161.6), "gta-whitby": ("locality", 167.0)}


@unittest.skipUnless(os.environ.get("BUSMAP_GTA") == "1", "GTA tests: run with --gta")
class GTA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(U.TEST_BUILD, "cities")
        cls.stores = {k: U.gta_store(k) for k in ("day", "week")}
        cls.res = {}
        for cid in U.gta_batch()["cities"]:
            paths = U.gta_city(cid, cls.root)
            day = os.path.join(cls.root, cid, "day", "network.json.gz")
            p, secs, gb = U.run("trim_network.py", "--config", paths["day"], "--area", cls.stores["day"].path, "--out", day)
            cls.res[(cid, "day")] = (p, secs, gb, day)
            week_cfg = paths.get("week")
            if week_cfg is None:
                # Burlington's recipe has no week; trim one anyway to see A3.6 refuse it.
                cfg = U.load_json(paths["day"])
                cfg["variants"] = {"week": {"start": "am_peak", "label": U.LABELS["week"], "frame": None,
                                            "render": U.VARIANTS["week"]["render"]}}
                week_cfg = os.path.join(cls.root, cid, "city.week.json")
                U.dump_json(week_cfg, cfg)
            week = os.path.join(cls.root, cid, "week", "network.json.gz")
            p, secs, gb = U.run("trim_network.py", "--config", week_cfg, "--area", cls.stores["week"].path,
                                "--day-network", day, "--out", week, check=False)
            cls.res[(cid, "week")] = (p, secs, gb, week)

    def net(self, cid, kind):
        p, _s, _g, path = self.res[(cid, kind)]
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return U.load_json(path)

    def test_boundaries_match_e2(self):
        """A-7: subtypes and areas within 1%; Toronto takes the county; masks and trim boxes."""
        for cid, (subtype, km2) in E2.items():
            feat = U.load_json(os.path.join(self.root, cid, "boundary.geojson"))
            self.assertEqual(feat["properties"]["subtype"], subtype, cid)
            self.assertLess(abs(feat["properties"]["area_km2"] - km2) / km2, 0.01, cid)
            b = self.net(cid, "day")["meta"]["boundary"]
            poly = boundary_polygon(U.load_json(os.path.join(self.root, cid, "city.day.json")))
            mk = b["mask"]
            grid = tn.decode_mask(mk)
            cx = mk["x0"] + (np.arange(mk["nx"]) + 0.5) * mk["cell_km"]
            cy = mk["y0"] + (np.arange(mk["ny"]) + 0.5) * mk["cell_km"]
            gx, gy = np.meshgrid(cx, cy)
            self.assertTrue(np.array_equal(grid, shapely.contains_xy(poly, gx, gy)), cid)
            box = self.net(cid, "day")["meta"]["trim"]["box_km"]
            bb = b["bbox_km"]
            self.assertTrue(box[0] <= bb[0] and box[1] <= bb[1] and bb[2] <= box[2] and bb[3] <= box[3], cid)

    def test_week_eligibility(self):
        """A-9: Burlington exits 3 with the reason; the other nine have week files with masks."""
        for cid in E2:
            p, _s, _g, path = self.res[(cid, "week")]
            if cid == "gta-burlington":
                self.assertEqual(p.returncode, 3)
                why = json.loads(p.stdout.strip().splitlines()[-1])
                self.assertFalse(why["eligible"])
                self.assertIn("burlington", why["why"][0])
                continue
            net = self.net(cid, "week")
            self.assertTrue(all(1 <= t["w"] <= 127 for t in net["trips"]), cid)
            self.assertEqual(list(net["meta"]["variants"]), ["week"])
            self.assertEqual(net["meta"]["hist_period"], 10080)

    def test_brands(self):
        """A-10."""
        for cid in E2:
            net = self.net(cid, "day")
            brands = net["meta"]["brands"]
            for r in net["routes"]:
                b = brands[r["brand"]]
                if r["feed"] == "brampton":
                    self.assertNotEqual(b["kind"], "gtfs", r["id"])
                    if r["short"] in ("501", "502", "505", "511", "561"):
                        self.assertEqual(b["id"], "brampton:zum", r["id"])
                if r["feed"] == "ttc" and r["type"] == 1:
                    self.assertEqual(b["kind"], "line", r["id"])
                if r["feed"] == "ttc" and r["color_raw"].lower() == "808080":
                    self.assertEqual(b["id"], "ttc", r["id"])
        toronto = self.net("gta-toronto", "day")
        ids = {b["id"] for b in toronto["meta"]["brands"]}
        self.assertTrue({"ttc", "ttc:line:d5c82b", "ttc:line:008000", "ttc:line:ff8000"} <= ids)
        bram = self.net("gta-brampton", "day")
        self.assertIn("brampton:zum", {brands["id"] for brands in bram["meta"]["brands"]})
        self.assertEqual(bram["meta"]["groups"][0]["id"], "brampton")

    def test_month_labels_and_rules(self):
        for cid in E2:
            m = self.net(cid, "day")["meta"]
            want = "November" if cid == "gta-burlington" else "October"
            self.assertEqual(m["timeline"]["month_label"], want, cid)
            self.assertEqual(m["variants"]["day"]["label"], f"An average {want} weekday")
            go = [f for f in m["feeds"] if f["id"] == "go"][0]
            self.assertEqual(go["rule"], "half")
        w = self.net("gta-oshawa", "week")["meta"]
        drt = [f for f in w["feeds"] if f["id"] == "drt"][0]
        self.assertEqual(drt["rule"]["tue"], "median-date")
        self.assertEqual(drt["median_date"], {"tue": "2026-10-20", "wed": "2026-10-21"})

    def test_peaks_and_counts(self):
        for cid in E2:
            for kind in ("day", "week"):
                if cid == "gta-burlington" and kind == "week":
                    continue
                net = self.net(cid, kind)
                m, h = net["meta"], net["hist"]
                P = m["hist_period"]
                for V in m["variants"].values():
                    mins = range(-(-V["start"] // 60), -(-V["end"] // 60))
                    best = max(h[x % P] for x in mins)
                    self.assertEqual(V["peak"]["count"], round(best), cid)
                    self.assertGreater(V["peak"]["count"], 0, cid)
                self.assertTrue(all(abs(sum(x) - y) < 0.05 * len(m["hist_by_mode"]) + 1e-9
                                    for x, y in zip(zip(*m["hist_by_mode"].values()), h)), cid)
                self.assertTrue(all(abs(sum(x) - y) < 0.05 * len(m["hist_by_group"]) + 1e-9
                                    for x, y in zip(zip(*m["hist_by_group"].values()), h)), cid)

    def test_rush_frames(self):
        tor = self.net("gta-toronto", "day")["meta"]["variants"]["rush"]
        self.assertEqual(tor["frame"], {"km_vertical": 40.0, "center_km": [8.5, -15.4]})
        self.assertEqual(tor["render"]["TRAIL_MINUTES"], 5)
        for cid in E2:
            m = self.net(cid, "day")["meta"]
            rush = m["variants"]["rush"]
            self.assertIsNotNone(rush["frame"], cid)
            self.assertLess(rush["frame"]["km_vertical"], m["frame"]["km_vertical"], cid)
            self.assertTrue(tn.inside_box(tn.frame_box(rush["frame"]), m["trim"]["box_km"]), cid)
            self.assertTrue(5 <= rush["render"]["TRAIL_MINUTES"] <= 10)
            self.assertIn(m["panel"]["side"], ("left", "right"))
            self.assertLessEqual(len(tn.wrap_lines(m["credit"], 22, 504)), 2)

    def test_trim_limits(self):
        """A-13: a trim at most 90 s and 3 GB."""
        for (cid, kind), (p, secs, gb, _path) in sorted(self.res.items()):
            sys.stderr.write(f"\n  trim {cid} {kind}: {secs:.1f} s, peak {gb:.2f} GB")
            self.assertLessEqual(secs, 90, f"{cid} {kind}")
            self.assertLessEqual(gb, 3.0, f"{cid} {kind}")

    def test_hist_is_brute_force_mean(self):
        """A-6 on a real network: the folded hist holds every inside vehicle-minute of the store."""
        cid = "gta-richmond-hill"
        city = U.load_json(os.path.join(self.root, cid, "city.day.json"))
        h, st, poly = raw_hist(self.stores["day"].path, city)
        bbox = [round(float(v), 3) for v in poly.bounds]
        idx = st.touching(bbox)
        unfolded = 0.0
        t0, t1 = st.trip_t0_t1()
        runs = np.asarray(st.trip_runs)
        nd = {f["id"]: st.meta["n_dates"][f["id"]]["wd"] for f in st.meta["feeds"]}
        feeds = [f["id"] for f in st.meta["feeds"]]
        for i in idx.tolist():
            lo, hi = -(-int(t0[i]) // 60), int(t1[i]) // 60
            if hi < lo:
                continue
            mins = np.arange(lo, hi + 1)
            x, y = tn.positions(st, np.array([i]), np.zeros(len(mins), dtype=np.int64), mins * 60)
            n = int(shapely.contains_xy(poly, x, y).sum())
            w = nd[feeds[int(st.trip_feed[i])]]
            if w:
                unfolded += runs[i, 0] * n / w
        self.assertLess(abs(h.sum() - unfolded), 1e-6 * unfolded)
        net = self.net(cid, "day")
        self.assertLessEqual(abs(sum(net["hist"]) - unfolded), 0.005 * 1440)

    def test_eleventh_city_changes_nothing(self):
        """A-12: trimming another city of the area leaves a city's network bytes as they were."""
        path = self.res[("gta-markham", "day")][3]
        before = read_bytes(path)
        paths = U.gta_city("gta-markham", self.root)
        extra = os.path.join(self.root, "gta-pickering")
        U.run("fetch_boundary.py", "select", "--in", U.ensure_divisions(), "--name", "Pickering", "--subtypes", "locality",
              "--area-km2", "231.0", "--out", os.path.join(extra, "boundary.geojson"))
        bfile = os.path.join(extra, "boundary.geojson")
        cfg = U.city_config("gta-pickering", "Pickering", ORIGIN, bfile, U.fit_frame(U.boundary_bbox_km(bfile, ORIGIN)),
                            ["day", "rush"], "day")
        U.dump_json(os.path.join(extra, "city.day.json"), cfg)
        U.run("trim_network.py", "--config", os.path.join(extra, "city.day.json"), "--area", self.stores["day"].path,
              "--out", os.path.join(extra, "day", "network.json.gz"))
        U.run("trim_network.py", "--config", paths["day"], "--area", self.stores["day"].path, "--out", path, seed="1")
        self.assertEqual(read_bytes(path), before)
        area = U.gta_area_config("day")
        self.assertNotIn("cities", json.dumps(area))

    def test_determinism_of_the_area_build(self):
        """A-8: the GTA day store rebuilt with PYTHONHASHSEED=1 is byte-identical."""
        out = os.path.join(U.TEST_BUILD, "areas", "gta", "day-seed1")
        cfg = os.path.join(U.TEST_BUILD, "areas", "gta", "area.day.json")
        U.run("build_area.py", "--config", cfg, "--out", out, seed="1")
        ref = self.stores["day"].path
        for name in sorted(os.listdir(ref)):
            if name.startswith("perf"):
                continue
            self.assertEqual(read_bytes(os.path.join(ref, name)), read_bytes(os.path.join(out, name)), name)
        shutil.rmtree(out)

    def test_basemap(self):
        """A11 and A-8 for the base map: basemap_v4 builds twice to the same bytes."""
        city = U.load_json(os.path.join(self.root, "gta-richmond-hill", "city.day.json"))
        kv = city["frame"]["km_vertical"]
        cx, cy = city["frame"]["center_km"]
        hw, hh = kv * 9 / 32 * 1.25 + 3, kv / 2 * 1.25 + 3
        kx = 111.32 * math.cos(math.radians(ORIGIN[1]))
        clip = [round(ORIGIN[0] + (cx - hw) / kx, 4), round(ORIGIN[1] + (cy - hh) / 110.574, 4),
                round(ORIGIN[0] + (cx + hw) / kx, 4), round(ORIGIN[1] + (cy + hh) / 110.574, 4)]
        p = kv / 1920 / 1.1
        outs = []
        for n in ("a", "b"):
            built = os.path.join(U.TEST_BUILD, "basemap", n)
            cfg = {"schema": 4, "id": "gta-richmond-hill", "origin": list(ORIGIN), "clip": clip,
                   "basemap_dir": "data/gta/basemap",
                   "basemap": {"simplify_km": {"major": 0.2 * p, "minor": 0.33 * p, "rail": 0.25 * p, "water": 0.25 * p,
                                               "water_line": 0.33 * p},
                               "min_road_km": 1.2 * p, "min_water_area_km2": 14 * p * p, "min_stream_km": 17 * p,
                               "water_polys": ["water", "lake", "pond", "reservoir", "river", "ocean", "sea", "bay",
                                               "lagoon", "harbour"], "water_lines": ["river", "canal"]},
                   "gzip": True, "boundary": None, "built_dir": built}
            U.dump_json(os.path.join(built, "basemap.config.json"), cfg)
            U.run("basemap_v4.py", "--config", os.path.join(built, "basemap.config.json"), "--key", "k" * 64, seed="1" if n == "b" else "0")
            outs.append(read_bytes(os.path.join(built, "basemap.json.gz")))
            self.assertEqual(read_bytes(os.path.join(built, "basemap.json.gz.key")).decode().strip(), "k" * 64)
            man = U.load_json(os.path.join(built, "manifest.json"))
            self.assertEqual(list(man["files"].values()), [area_store.sha256_file(os.path.join(built, "basemap.json.gz"))])
        self.assertEqual(outs[0], outs[1])
        bm = json.loads(gzip.decompress(outs[0]))
        self.assertGreater(len(bm["roads"]["major"]), 100)
        self.assertEqual(bm["boundary"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

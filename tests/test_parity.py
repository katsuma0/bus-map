"""Part A parity test (A5, F3 A-11): build_trips_v4 equals build_feed value for value.

    python3 -I tests/test_parity.py

On the stable-id fixture with one selected date and the box of a legacy
config, the v4 trip builder must produce the same trips (t, d), shapes (xy,
cum) and route fields as build_network.build_feed. A second test takes the
whole path (area store, trim) and checks that the integer store gives exactly
the JSON numbers of the float build.
"""
import collections
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fixtures", "gtfs"))
import v4util as U  # noqa: E402

import numpy as np  # noqa: E402

import build_area  # noqa: E402
import build_network as bn  # noqa: E402
import composite  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "gtfs")
ORIGIN = (-79.47, 43.80)
CLIP = [-79.60, 43.60, -79.20, 43.85]


def legacy_city(frame_km=12.0):
    # color_by makes it a v3 config, so build_feed also writes routes[].agency.
    return {"id": "parity", "origin": list(ORIGIN), "frame": {"km_vertical": frame_km, "center_km": [9.0, -8.0]}, "clip": CLIP,
            "modes": U.MODES, "color_by": "route"}


def legacy_build(name, date, city):
    bn.set_origin(ORIGIN)
    feed = bn.Feed(name, os.path.join(FIX, f"{name}.zip"))
    routes, route_index, shapes, shape_index = [], {}, [], {}
    st = bn.build_feed(feed, date, city, routes, route_index, shapes, shape_index, collections.defaultdict(list))
    return st, routes, shapes


def v4_build(name, date, box):
    bn.set_origin(ORIGIN)
    f = composite.Feed(name, os.path.join(FIX, f"{name}.zip"))
    svc = composite.service_dates(f)
    trips = {t["trip_id"] for t in f.table("trips")}
    st = composite.read_stop_times(f, trips, 13)
    counts = composite.trips_per_date(f, svc, composite.first_times(st))
    sel = {"wd": {"dates": [date], "excluded": [], "fallback": False, "month_used": "2026-10", "median": counts[date]}}
    classes = composite.trip_classes(f, svc, sel, st)
    composite.apply_guard(classes, sel, counts, {"low": 0, "high": 100, "min_dates": 1})
    return build_area.build_trips_v4(f, classes, st, box, U.MODES, chunksize=17)


class Parity(unittest.TestCase):
    def check_date(self, date):
        city = legacy_city()
        box = bn.frame_box(city)
        legacy, l_routes, l_shapes = legacy_build("stable", date, city)
        built, shapes, routes, stats = v4_build("stable", date, box)
        by_id = {t["_id"]: t for t in legacy["trips"]}
        self.assertEqual(sorted(by_id), sorted(bt.cls.rep for bt in built))
        self.assertEqual(stats["dropped_box"], legacy["dropped_clip"])
        self.assertEqual(stats["skipped_short"], legacy["skipped_short"])
        self.assertEqual(stats["shape_from_stops"], legacy["shape_from_stops"])
        self.assertEqual(stats["spread"], legacy["spread"])
        self.assertEqual(stats["time_fixes"], legacy["time_fixes"])
        self.assertEqual(stats["forced_monotone"], legacy["forced_monotone"])
        for bt in built:
            lt = by_id[bt.cls.rep]
            self.assertEqual(bt.t.tolist(), lt["t"], bt.cls.rep)
            self.assertEqual((np.rint(bt.d * 1000.0) / 1000.0).tolist(), lt["d"], bt.cls.rep)
            self.assertEqual(np.round(bt.d, 3).tolist(), lt["d"])
            xy, cum = shapes[bt.shape_key]
            ls = l_shapes[lt["s"]]
            self.assertEqual((np.rint(xy.ravel() * 1000.0) / 1000.0).tolist(), ls["xy"], bt.cls.rep)
            self.assertEqual((np.rint(cum * 10000.0) / 10000.0).tolist(), ls["cum"], bt.cls.rep)
            lr = l_routes[lt["r"]]
            vr = routes[bt.route_key]
            for k in ("id", "short", "long", "color", "feed", "mode", "agency"):
                self.assertEqual(vr[k], lr[k], f"{bt.cls.rep} {k}")
        return built, routes

    def test_regular_weekday(self):
        built, routes = self.check_date(dt.date(2026, 10, 6))
        # The fixture reaches every build_feed path.
        self.assertIn("stable:R2", routes)
        self.assertEqual(routes["stable:R2"]["short"], "H")
        self.assertEqual(routes["stable:R2"]["color"], "2f6bff")
        self.assertEqual(routes["stable:R2"]["color_raw"], "")
        self.assertEqual(routes["stable:R4"]["mode"], "bus")
        self.assertEqual(routes["stable:R4"]["type"], 1700)
        self.assertEqual(routes["stable:R3"]["mode"], "rail")
        self.assertTrue(any(bt.shape_key.startswith("stable\x1fstops") for bt in built))
        self.assertTrue(any(int(bt.t[-1]) > 86400 for bt in built))
        self.assertTrue(any(bt.cls.rep.startswith("T-freq#") for bt in built))

    def test_day_with_the_half_trip(self):
        built, _routes = self.check_date(dt.date(2026, 10, 7))
        self.assertTrue(any(bt.cls.rep == "T-half" for bt in built))

    def test_weekend(self):
        self.check_date(dt.date(2026, 10, 3))


class StoreRoundTrip(unittest.TestCase):
    """Integers in the store, divided once, give the float build's JSON numbers."""

    def test_network_numbers_equal_the_float_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            area = U.area_config("fx", ORIGIN, [-80.06, 43.06, -78.58, 44.25], FIX,
                                 [{"id": "stable", "name": "Stable", "publisher": "Fixture", "licence_id": "cc-by-4.0",
                                   "licence_text": "CC BY 4.0"}], "day", chunk=5000)
            U.dump_json(os.path.join(tmp, "area.json"), area)
            U.run("build_area.py", "--config", os.path.join(tmp, "area.json"), "--out", os.path.join(tmp, "store"))
            # A boundary far larger than the network, so every trip is counted and kept.
            bfile = os.path.join(tmp, "big.geojson")
            ring = [[-79.9, 43.5], [-79.0, 43.5], [-79.0, 44.0], [-79.9, 44.0], [-79.9, 43.5]]
            U.dump_json(bfile, {"type": "Feature", "properties": {"area_km2": 1.0},
                                "geometry": {"type": "Polygon", "coordinates": [ring]}})
            frame = U.fit_frame(U.boundary_bbox_km(bfile, ORIGIN))
            city = U.city_config("fx-big", "Big", ORIGIN, bfile, frame, ["day"], "day", batch="fx", area="fx")
            U.dump_json(os.path.join(tmp, "city.json"), city)
            out = os.path.join(tmp, "net.json.gz")
            U.run("trim_network.py", "--config", os.path.join(tmp, "city.json"), "--area", os.path.join(tmp, "store"),
                  "--out", out)
            net = U.load_json(out)

        # Every class's representative runs on Oct 1, so build_feed on that date builds them all.
        legacy, l_routes, l_shapes = legacy_build("stable", dt.date(2026, 10, 1),
                                                  {"id": "x", "clip": CLIP, "modes": U.MODES, "color_by": "route"})

        def key(t, routes, shapes):
            s = shapes[t["s"]]
            return json.dumps([routes[t["r"]]["id"], t["t"], t["d"], s["xy"], s["cum"]])

        got = collections.Counter(key(t, net["routes"], net["shapes"]) for t in net["trips"])
        want = collections.Counter(key(t, l_routes, l_shapes) for t in legacy["trips"])
        self.assertEqual(sum(got.values()), sum(want.values()))
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)

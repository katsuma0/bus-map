"""Part A tests of composite dates and trip classes (A3, A4; F3 A-2, A-3, A-4).

    python3 -I tests/test_composite.py           # fixtures only, seconds
    python3 -I tests/test_composite.py --gta     # also the GTA zips: builds build/test_a/areas/gta/{day,week}

The GTA part builds both area stores once (about 3 minutes on the sandbox)
and compares their dates and rules with spec E3.
"""
import collections
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fixtures", "gtfs"))
if "--gta" in sys.argv:
    sys.argv.remove("--gta")
    os.environ["BUSMAP_GTA"] = "1"
import v4util as U  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import build_network as bn  # noqa: E402
import composite  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "gtfs")
D = dt.date


def feed(name):
    return composite.Feed(name, os.path.join(FIX, f"{name}.zip"))


def timeline(kind, month="2026-10", holidays=None):
    return dict(U.TIMELINE, kind=kind, month=month, timezone="America/Toronto",
                holidays=holidays if holidays is not None else {"2026-10-12": "Thanksgiving"})


def classes_of(name, kind, month="2026-10", chunk=5000):
    """The A3 pipeline of build_area.py for one fixture feed: (feed, sel, classes, guard, counts)."""
    import build_area
    f = feed(name)
    tl = timeline(kind, month)
    svc = composite.service_dates(f)
    counts0 = composite.trips_per_date(f, svc)
    st = composite.read_stop_times(f, build_area.candidate_trips(f, svc, tl, counts0), chunk)
    counts = composite.trips_per_date(f, svc, composite.first_times(st))
    sel = composite.select_dates(counts, tl, composite.feed_timezone(f, "America/Toronto"),
                                 max(st.max_t, composite.frequency_max_t(f, st)))
    cls = composite.trip_classes(f, svc, sel, st)
    guard = composite.apply_guard(cls, sel, counts, tl["guard"])
    return f, sel, cls, guard, counts, st


class Calendar(unittest.TestCase):
    def test_service_dates_match_active_services(self):
        for name in ("stable", "golike", "drtlike"):
            f = feed(name)
            svc = composite.service_dates(f)
            d = D(2026, 9, 25)
            while d <= D(2026, 11, 5):
                want = bn.active_services(f, d)
                got = {sid for sid, dates in svc.items() if d in dates}
                self.assertEqual(got, want, f"{name} {d}")
                d += dt.timedelta(days=1)

    def test_trips_per_date_counts_frequency_runs(self):
        f = feed("stable")
        svc = composite.service_dates(f)
        counts = composite.trips_per_date(f, svc)
        # 48 + 48 R1, 24 R2, 6 R3, 3 R4, late, short, far, 4 frequency runs; plus the half trip on half the days
        self.assertEqual(counts[D(2026, 10, 6)], 136)
        self.assertEqual(counts[D(2026, 10, 1)], 137)
        self.assertEqual(counts[D(2026, 10, 12)], 0)

    def test_lower_median(self):
        self.assertEqual(composite.lower_median([4, 1, 3, 2]), 2)
        self.assertEqual(composite.lower_median([5, 1, 3]), 3)
        self.assertIsNone(composite.lower_median([]))

    def test_dst(self):
        # Canada changes at 2026-11-01 02:00: Saturday Oct 31 with a 31 h day and Sunday Nov 1.
        self.assertTrue(composite.dst_change(D(2026, 10, 31), "America/Toronto", 31))
        self.assertTrue(composite.dst_change(D(2026, 11, 1), "America/Toronto", 30))
        self.assertFalse(composite.dst_change(D(2026, 10, 30), "America/Toronto", 31))
        # Europe changes on 2026-10-25: Saturday Oct 24 and Sunday Oct 25 (section 10).
        self.assertTrue(composite.dst_change(D(2026, 10, 24), "Europe/Berlin", 30))
        self.assertTrue(composite.dst_change(D(2026, 10, 25), "Europe/Berlin", 30))
        self.assertFalse(composite.dst_change(D(2026, 10, 23), "Europe/Berlin", 30))


class Selection(unittest.TestCase):
    def test_exclusions_in_order(self):
        counts = collections.Counter({D(2026, 10, d): 100 for d in range(1, 32)})
        counts[D(2026, 10, 12)] = 50          # holiday and low: holiday wins
        counts[D(2026, 10, 14)] = 0           # no service
        counts[D(2026, 10, 15)] = 91          # 91 < 0.92 x 100: low
        counts[D(2026, 10, 16)] = 93          # 93 >= 92: kept
        sel = composite.select_dates(counts, timeline("day"), "America/Toronto", 31 * 3600)
        ex = {e["date"]: e for e in sel["wd"]["excluded"]}
        self.assertEqual(ex["2026-10-12"]["why"], "holiday")
        self.assertEqual(ex["2026-10-12"]["name"], "Thanksgiving")
        self.assertEqual(ex["2026-10-14"]["why"], "no service")
        self.assertEqual(ex["2026-10-15"]["why"], "low")
        self.assertEqual(ex["2026-10-15"]["median"], 100)
        self.assertNotIn("2026-10-16", ex)
        self.assertEqual(len(sel["wd"]["dates"]), 22 - 3)

    def test_fallback_window(self):
        # Burlington: valid from Sunday Nov 1; October has nothing.
        counts = collections.Counter({D(2026, 11, 1) + dt.timedelta(days=i): 40 for i in range(49)})
        day = composite.select_dates(counts, timeline("day", holidays=U.GTA_HOLIDAYS), "America/Toronto", 26 * 3600)
        self.assertTrue(day["wd"]["fallback"])
        self.assertEqual(day["wd"]["month_used"], "2026-11")
        self.assertEqual([d.day for d in day["wd"]["dates"]],
                         [2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25, 26, 27])
        week = composite.select_dates(counts, timeline("week", holidays=U.GTA_HOLIDAYS), "America/Toronto", 26 * 3600)
        self.assertEqual([d.day for d in week["sun"]["dates"]], [8, 15, 22])
        self.assertEqual([e["why"] for e in week["sun"]["excluded"]], ["dst"])
        self.assertEqual([d.day for d in week["sat"]["dates"]], [7, 14, 21, 28])
        self.assertEqual(composite.feed_month_used(week, "2026-10"), "2026-11")

    def test_no_dates_at_all(self):
        sel = composite.select_dates(collections.Counter(), timeline("day"), "America/Toronto", 0)
        self.assertEqual(sel["wd"]["dates"], [])


class Reading(unittest.TestCase):
    def test_parse_times_fast_path(self):
        s = pd.Series(["08:05:00", "8:05:00", "25:10:00", "", "  07:00:00", "07:00", "xx:00:00", "100:00:00", "07:00:00 ",
                       "23:59:59", "00:00:00"], dtype=object)
        a, b = composite.parse_times(s), bn.parse_times(s)
        self.assertTrue(np.array_equal(a, b, equal_nan=True))

    def test_read_frame_equals_feed_frame(self):
        f = feed("golike")
        cols = ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"]
        keep = {"EW", "LOOP"}
        whole = f.frame("shapes", cols)
        whole = whole[whole["shape_id"].str.strip().isin(keep)].reset_index(drop=True)
        chunked = composite.read_frame(f, "shapes", cols, "shape_id", keep, 7)
        pd.testing.assert_frame_equal(chunked, whole)

    def test_read_stop_times_equals_build_feed_arrays(self):
        f = feed("stable")
        trips = {t["trip_id"] for t in f.table("trips")}
        trips = set(sorted(trips)[::3])
        st = composite.read_stop_times(f, trips, 11)
        # build_feed lines 511 to 519, verbatim
        stops = composite.valid_stops(f)
        ref = f.frame("stop_times", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
        ref = ref[ref["trip_id"].str.strip().isin(trips)]
        ref = ref.assign(trip_id=ref["trip_id"].str.strip(), stop_id=ref["stop_id"].str.strip())
        ref = ref[ref["stop_id"].isin(stops.keys())]
        seq = pd.to_numeric(ref["stop_sequence"].str.strip(), errors="coerce").fillna(0).to_numpy(dtype=np.int64)
        perm, ranges = bn.grouped(ref["trip_id"].to_numpy(), seq)
        self.assertEqual(st.ranges.keys(), ranges.keys())
        for tid, (a, b) in ranges.items():
            a2, b2 = st.ranges[tid]
            self.assertEqual(list(st.stop[a2:b2]), list(ref["stop_id"].to_numpy()[perm][a:b]))
            self.assertTrue(np.array_equal(st.arr[a2:b2], bn.parse_times(ref["arrival_time"])[perm][a:b], equal_nan=True))
            self.assertTrue(np.array_equal(st.dep[a2:b2], bn.parse_times(ref["departure_time"])[perm][a:b], equal_nan=True))


class Classes(unittest.TestCase):
    """A-2."""

    def class_table(self, cls):
        return sorted((c.hex, tuple(sorted(c.runs.items())), c.mult, tuple(sorted(c.copies.items()))) for c in cls)

    def test_stable_and_golike_give_the_same_classes(self):
        for kind in ("day", "week"):
            _f, sel_s, cls_s, g_s, _c, _st = classes_of("stable", kind)
            _f, sel_g, cls_g, g_g, _c, _st = classes_of("golike", kind)
            self.assertEqual(self.class_table(cls_s), self.class_table(cls_g), kind)
            self.assertEqual({k: [d.isoformat() for d in v["dates"]] for k, v in sel_s.items()},
                             {k: [d.isoformat() for d in v["dates"]] for k, v in sel_g.items()})
            self.assertEqual(g_s, g_g)

    def test_exact_half_tie_is_drawn(self):
        f, sel, cls, guard, counts, _st = classes_of("stable", "day")
        w = len(sel["wd"]["dates"])
        self.assertEqual(w, 20)
        half = [c for c in cls if c.template == "T-half"]
        self.assertEqual(len(half), 1)
        self.assertEqual(2 * half[0].dates_k["wd"], w)
        self.assertEqual(half[0].copies["wd"], 1)
        self.assertEqual(guard["wd"]["rule"], "half")

    def test_frequency_runs_are_classes(self):
        f, sel, cls, guard, counts, _st = classes_of("stable", "day")
        runs = sorted(c.rep for c in cls if c.template == "T-freq")
        self.assertEqual(runs, ["T-freq#0", "T-freq#1", "T-freq#2", "T-freq#3"])

    def test_duplicate_pair_has_multiplicity_two(self):
        f, sel, cls, guard, counts, _st = classes_of("drtlike", "week")
        dups = [c for c in cls if c.template.endswith(("-dupA", "-dupB"))]
        self.assertEqual(len(dups), 2)   # timetable A and timetable B
        for c in dups:
            self.assertEqual(c.mult, 2)
            self.assertEqual(c.members, 2 * len(c.runs))
        a = [c for c in dups if min(c.runs) < D(2026, 10, 19)][0]
        self.assertEqual(a.copies["thu"], 2)      # half rule: the multiplicity
        self.assertEqual(a.copies["tue"], 2)      # median-date: the runs on that date

    def test_drtlike_median_date_takes_the_lower_median(self):
        f, sel, cls, guard, counts, _st = classes_of("drtlike", "week")
        for k, date in (("tue", "2026-10-06"), ("wed", "2026-10-07")):
            wk = sel[k]["dates"]
            self.assertEqual(len(wk) % 2, 0)
            vals = sorted(counts[d] for d in wk)
            self.assertLess(vals[len(vals) // 2 - 1], vals[len(vals) // 2])   # the two middle values differ
            self.assertEqual(guard[k]["rule"], "median-date")
            self.assertEqual(guard[k]["median_date"], date)
            self.assertEqual(guard[k]["median"], vals[len(vals) // 2 - 1])
            self.assertEqual(guard[k]["drawn"], counts[D.fromisoformat(date)])
            self.assertGreater(guard[k]["ratio_half"], 1.03)
        for k in ("mon", "thu", "fri", "sat", "sun"):
            self.assertEqual(guard[k]["rule"], "half", k)
        excl = {e["date"]: e["why"] for e in sel["sat"]["excluded"]}
        self.assertEqual(excl, {"2026-10-10": "low", "2026-10-31": "dst"})

    def test_drawn_over_median_within_limits(self):
        for name in ("stable", "golike", "drtlike"):
            for kind in ("day", "week"):
                _f, sel, cls, guard, counts, _st = classes_of(name, kind)
                for k, g in guard.items():
                    self.assertTrue(0.97 <= g["ratio"] <= 1.03, f"{name} {kind} {k} {g}")

    def test_representative_is_earliest_member(self):
        _f, sel, cls, guard, counts, _st = classes_of("golike", "day")
        for c in cls:
            self.assertTrue(c.rep.startswith(min(c.runs).strftime("%Y%m%d")), c.rep)


# ---------------------------------------------------------------- GTA (spec E3)

R = lambda a, b: list(range(a, b + 1))  # noqa: E731
OCT_WD = [1, 2] + R(5, 9) + R(13, 16) + R(19, 23) + R(26, 30)
E3_DAY = {
    **{f: OCT_WD for f in ("ttc", "yrt", "miway", "brampton", "drt", "oakville")},
    "go": R(5, 9) + R(13, 16) + R(19, 23) + R(26, 30),
    "upx": R(7, 9) + R(13, 16) + R(19, 23) + R(26, 30),
    "milton": [8, 9] + R(13, 16) + R(19, 23) + R(26, 30),
}
E3_DAY_BURLINGTON = R(2, 6) + R(9, 13) + R(16, 20) + R(23, 27)
STD_WEEK = {"mon": [5, 19, 26], "tue": [6, 13, 20, 27], "wed": [7, 14, 21, 28], "thu": [1, 8, 15, 22, 29],
            "fri": [2, 9, 16, 23, 30], "sat": [3, 10, 17, 24], "sun": [4, 11, 18, 25]}
E3_WEEK = {
    **{f: (STD_WEEK, {}) for f in ("ttc", "yrt", "miway", "brampton", "oakville")},
    "drt": (STD_WEEK, {"tue": "2026-10-20", "wed": "2026-10-21"}),
    "go": ({"mon": [5, 19, 26], "tue": [6, 13, 20, 27], "wed": [7, 14, 21, 28], "thu": [8, 15, 22, 29],
            "fri": [9, 16, 23, 30], "sat": [17, 24], "sun": [18, 25]},
           {"tue": "2026-10-20", "sat": "2026-10-24", "sun": "2026-10-18"}),
    "upx": ({"mon": [19, 26], "tue": [13, 20, 27], "wed": [7, 14, 21, 28], "thu": [8, 15, 22, 29], "fri": [9, 16, 23, 30],
             "sat": [10, 17, 24], "sun": [11, 18, 25]}, {"mon": "2026-10-19"}),
    "milton": ({"mon": [19, 26], "tue": [13, 20, 27], "wed": [14, 21, 28], "thu": [8, 15, 22, 29], "fri": [9, 16, 23, 30],
                "sat": [10, 17, 24], "sun": [11, 18, 25]}, {"mon": "2026-10-19"}),
}
E3_WEEK_BURLINGTON = {"mon": [2, 9, 16, 23], "tue": [3, 10, 17, 24], "wed": [4, 11, 18, 25], "thu": [5, 12, 19, 26],
                      "fri": [6, 13, 20, 27], "sat": [7, 14, 21, 28], "sun": [8, 15, 22]}


@unittest.skipUnless(os.environ.get("BUSMAP_GTA") == "1", "GTA tests: run with --gta")
class GTA(unittest.TestCase):
    """A-3, A-4 and the A-13 area limits on the real GTA zips."""

    def oct(self, days, month=10):
        return [f"2026-{month:02d}-{d:02d}" for d in days]

    def test_day_dates_and_rules_equal_e3(self):
        st = U.gta_store("day")
        feeds = {f["id"]: f for f in st.meta["feeds"]}
        for fid, days in E3_DAY.items():
            bc = feeds[fid]["by_class"]["wd"]
            self.assertEqual(bc["dates"], self.oct(days), fid)
            self.assertEqual(bc["rule"], "half", fid)
            self.assertFalse(bc["fallback"])
            ex = [(e["date"], e["why"], e["name"]) for e in feeds[fid]["excluded"]]
            self.assertEqual(ex, [("2026-10-12", "holiday", "Thanksgiving")], fid)
        bur = feeds["burlington"]
        self.assertEqual(bur["by_class"]["wd"]["dates"], self.oct(E3_DAY_BURLINGTON, 11))
        self.assertEqual(bur["month_used"], "2026-11")
        self.assertEqual(bur["excluded"], [])

    def test_week_dates_and_rules_equal_e3(self):
        st = U.gta_store("week")
        feeds = {f["id"]: f for f in st.meta["feeds"]}
        for fid, (dates, md) in E3_WEEK.items():
            for k in composite.WEEK_CLASSES:
                bc = feeds[fid]["by_class"][k]
                self.assertEqual(bc["dates"], self.oct(dates[k]), f"{fid} {k}")
                self.assertEqual(bc["rule"], "median-date" if k in md else "half", f"{fid} {k}")
                self.assertEqual(bc["median_date"], md.get(k), f"{fid} {k}")
            why = {e["date"]: e["why"] for e in feeds[fid]["excluded"]}
            self.assertEqual(why.get("2026-10-31"), "dst", fid)
            self.assertEqual(why.get("2026-10-12"), "holiday", fid)
        why = {e["date"]: e for e in feeds["go"]["excluded"]}
        self.assertEqual(why["2026-10-10"]["why"], "low")
        self.assertEqual((why["2026-10-10"]["trips"], why["2026-10-10"]["median"]), (1249, 1392))
        self.assertEqual(why["2026-10-11"]["why"], "low")
        self.assertEqual((why["2026-10-11"]["trips"], why["2026-10-11"]["median"]), (1230, 1418))
        bur = feeds["burlington"]
        for k in composite.WEEK_CLASSES:
            self.assertEqual(bur["by_class"][k]["dates"], self.oct(E3_WEEK_BURLINGTON[k], 11), k)
            self.assertTrue(bur["by_class"][k]["fallback"])
        self.assertEqual([(e["date"], e["why"]) for e in bur["excluded"]], [("2026-11-01", "dst")])

    def test_drawn_over_median_within_limits(self):
        for kind in ("day", "week"):
            st = U.gta_store(kind)
            for f in st.meta["feeds"]:
                for k, bc in f["by_class"].items():
                    self.assertTrue(0.97 <= bc["ratio"] <= 1.03, f"{kind} {f['id']} {k} {bc['ratio']}")

    def test_area_build_limits(self):
        for kind, secs, gb in (("day", 20 * 60, 8.0), ("week", 40 * 60, 12.0)):
            U.gta_store(kind)
            path = os.path.join(U.TEST_BUILD, "areas", "gta", f"perf.{kind}.json")
            if not os.path.exists(path):
                self.skipTest("store reused from an earlier run without timing")
            perf = U.load_json(path)
            sys.stderr.write(f"\n  area {kind}: {perf['seconds']} s, peak {perf['peak_gb']} GB\n")
            self.assertLessEqual(perf["seconds"], secs)
            self.assertLessEqual(perf["peak_gb"], gb)

    def test_parse_times_fast_path_on_every_gta_row(self):
        for name in sorted(os.listdir(U.GTA_GTFS)):
            f = composite.Feed(name[:-4], os.path.join(U.GTA_GTFS, name))
            with f.zf.open(f.members["stop_times"]) as fh:
                for ch in pd.read_csv(fh, dtype=str, usecols=lambda c: c.strip() in {"arrival_time", "departure_time"},
                                      keep_default_na=False, na_filter=False, encoding="utf-8-sig",
                                      skipinitialspace=True, chunksize=2_000_000):
                    ch.columns = [c.strip() for c in ch.columns]
                    for col in ("arrival_time", "departure_time"):
                        self.assertTrue(np.array_equal(composite.parse_times(ch[col]), bn.parse_times(ch[col]),
                                                       equal_nan=True), f"{name} {col}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

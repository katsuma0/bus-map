#!/usr/bin/env python3
"""Tests for scripts/shorts_meta.py (spec D5, F3 D-6) on the GTA fixtures with synthetic netmeta files.

    python3 tests/test_meta.py [-v]
"""

import csv
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import make  # noqa: E402
import shorts_meta as sm  # noqa: E402

OCT_WEEKDAYS = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 13, 14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30)]
NOV_WEEKDAYS = [f"2026-11-{d:02d}" for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25, 26, 27)]
SHARES = {"gta-toronto": {"ttc": 0.92, "go": 0.033, "miway": 0.023, "yrt": 0.011, "upx": 0.004},
          "gta-burlington": {"burlington": 0.8, "go": 0.15, "oakville": 0.05},
          "gta-richmond-hill": {"yrt": 0.95, "go": 0.05}}
PEAKS = {"day": {"count": 3951, "time": 28860}, "rush": {"count": 3951, "time": 28860},
         "week": {"count": 3620, "time": 4 * 86400 + 62040}}


def week_dates(feed):
    if feed == "go":
        return {"mon": ["2026-10-05", "2026-10-19", "2026-10-26"], "tue": ["2026-10-06", "2026-10-13", "2026-10-20", "2026-10-27"],
                "wed": ["2026-10-07", "2026-10-14"], "thu": ["2026-10-08"], "fri": ["2026-10-09"],
                "sat": ["2026-10-17", "2026-10-24"], "sun": ["2026-10-18", "2026-10-25"]}
    return {k: ["2026-10-05", "2026-10-19", "2026-10-26"] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def netmeta(batch, recipe, variant):
    """A netmeta shaped like make.py's, with the GTA facts of spec 0.1 and E3."""
    shares = SHARES.get(recipe["id"], {"yrt": 0.7, "ttc": 0.15, "go": 0.1, "brampton": 0.05})
    feeds = []
    for f in batch["areas"][0]["feeds"]:
        share = shares.get(f["id"], 0.0)
        week = variant == "week"
        fb = f["id"] == "burlington"
        entry = {k: f[k] for k in ("id", "name", "publisher", "licence_id", "licence_text")}
        entry.update({"inside_share": share, "inside_vehicle_minutes": share * 1e5, "major": share >= 0.05,
                      "month_used": "2026-11" if fb else "2026-10",
                      "dates": week_dates(f["id"]) if week else (NOV_WEEKDAYS if fb else OCT_WEEKDAYS),
                      "rule": ({"tue": "median-date", "sat": "median-date", "sun": "median-date"} if f["id"] == "go"
                               else {"mon": "half"}) if week else "half",
                      "excluded": [] if fb else [{"date": "2026-10-12", "why": "holiday", "name": "Thanksgiving",
                                                  "trips": 1249, "median": 1791}]})
        if week:
            entry["excluded"].append({"date": "2026-10-31", "why": "dst", "trips": 1, "median": 1})
            if f["id"] == "go":
                entry["excluded"].append({"date": "2026-10-10", "why": "low", "trips": 1249, "median": 1392})
        feeds.append(entry)
    modes = ["bus", "streetcar", "rail"] if recipe["id"] == "gta-toronto" else ["bus", "rail"]
    groups = [{"id": k, "label": k.upper() if k in ("ttc", "go", "yrt") else k.title(), "brand": k, "share": v}
              for k, v in sorted(shares.items(), key=lambda kv: -kv[1])[:3]] + [{"id": "other", "label": "other"}]
    return {"id": recipe["id"], "variant": variant, "place": recipe["place"], "title": recipe["place"].upper(),
            "label": "x", "month_label": "November" if recipe["id"] == "gta-burlington" else "October", "month": "2026-10",
            "peak": PEAKS[variant], "am_peak": PEAKS["day"], "pm_peak": {"count": 3500, "time": 62040},
            "feeds": feeds, "modes_present": modes, "modes": batch["modes"], "groups": groups,
            "credit": "Data: TTC · Map: Overture, OSM", "seconds": {"day": 50, "rush": 25, "week": 60}[variant],
            "frames": {"day": 1500, "rush": 750, "week": 1800}[variant], "build_key": "0" * 64}


class GTA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pl = make.Pipeline(REPO, "tests/fixtures/gta")
        cls.batch = cls.pl.load_batch("gta")
        cls.metas = {}
        for i, (rid, v) in enumerate(cls.pl.videos(cls.batch), 1):
            cls.metas[f"{rid}-{v}"] = cls.build(rid, v, i)

    @classmethod
    def build(cls, rid, v, order=1, nm=None, batch=None, **kw):
        recipe = cls.pl.recipe(rid, cls.batch)
        return sm.build_meta(batch=batch or cls.batch, recipe=recipe, netmeta=nm or netmeta(cls.batch, recipe, v),
                             templates=cls.pl.templates(), licences=cls.pl.licences(), defaults=cls.pl.defaults(),
                             publish_order=order, meta_key="0" * 64, **kw)

    def test_29_rows_in_publish_order(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "gta-youtube.csv")
            sm.write_csv(p, list(self.metas.values()))
            with open(p, "rb") as fh:
                raw = fh.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        self.assertEqual(len(rows), 29)
        self.assertEqual(list(rows[0]), sm.CSV_COLUMNS)
        self.assertEqual([r["publish_order"] for r in rows], [str(i) for i in range(1, 30)])
        self.assertEqual(rows[0]["file"], "gta-toronto-day.mp4")
        self.assertEqual(rows[10]["file"], "gta-toronto-rush.mp4")
        self.assertEqual(rows[20]["file"], "gta-toronto-week.mp4")
        self.assertNotIn("gta-burlington-week.mp4", [r["file"] for r in rows])
        for r in rows:
            m = self.metas[r["file"][:-4]]
            self.assertEqual(r["description"], m["description"], "multi-line descriptions survive RFC 4180 quoting")
            self.assertEqual((r["made_for_kids"], r["visibility"]), ("no", "private"))

    def test_text_rules(self):
        for stem, m in self.metas.items():
            self.assertLessEqual(len(m["title"]), 99, stem)
            self.assertLessEqual(len(m["description"]), 4900, stem)
            self.assertLessEqual(len(",".join(m["tags"])), 500, stem)
            for field in (m["title"], m["description"], ",".join(m["tags"])):
                self.assertNotIn("\u2014", field)
                self.assertNotIn("\u2013", field)
            self.assertEqual(m["hashtags"][0], "#shorts")
            self.assertLessEqual(len(m["hashtags"]), 5)
        self.assertEqual(self.metas["gta-toronto-day"]["title"], "Every bus, streetcar and train in Toronto in 24 hours")
        self.assertEqual(self.metas["gta-richmond-hill-rush"]["title"],
                         "Morning rush in Richmond Hill: every bus and train, 6:30 to 9:30 am")
        self.assertIn("#richmondhill", self.metas["gta-richmond-hill-day"]["hashtags"])
        self.assertIn("Ontario", self.metas["gta-markham-day"]["tags"])
        # Feed names, busiest inside first, not the chip labels.
        tags = self.metas["gta-markham-day"]["tags"]
        self.assertEqual(tags[:5], ["Markham", "York Region Transit", "TTC", "GO Transit", "Brampton Transit"])
        self.assertNotIn("GO", tags)

    def test_numbers_are_the_peak(self):
        d = self.metas["gta-toronto-day"]["description"]
        self.assertIn("Busiest moment: 3,951 vehicles at 8:01 am.", d)
        self.assertIn("in 50 seconds", d)
        w = self.metas["gta-toronto-week"]["description"]
        self.assertIn("Busiest moment: 3,620 vehicles, Friday at 5:14 pm.", w)

    def test_licence_flags(self):
        m = self.metas["gta-toronto-day"]
        go_up = [f for f in m["licence_flags"] if f.startswith(("GO Transit", "UP Express"))]
        self.assertEqual(len(go_up), 2)
        self.assertTrue(all("metrolinx-open-data" in f for f in go_up))
        self.assertEqual(len(m["licence_flags"]), 2, "only Metrolinx is 'check' in the GTA")
        ent = {e["feed"]: e["commercial"] for e in m["licences"]}
        self.assertEqual(ent["go"], "check")
        self.assertEqual(ent["ttc"], "yes")

    def test_free_text_never_decides(self):
        b = json.loads(json.dumps(self.batch))
        b["areas"][0]["feeds"][0]["licence_text"] = "No non-commercial clause found"
        recipe = self.pl.recipe("gta-toronto", self.batch)
        nm = netmeta(b, recipe, "day")
        m = self.build("gta-toronto", "day", nm=nm, batch=b)
        self.assertFalse([f for f in m["licence_flags"] if f.startswith("TTC")])
        b["areas"][0]["feeds"][0]["licence_id"] = "cc-by-nc-4.0"
        nm = netmeta(b, recipe, "day")
        with self.assertRaises(sm.MetaError):
            self.build("gta-toronto", "day", nm=nm, batch=b)
        m = self.build("gta-toronto", "day", nm=nm, batch=b, allow_nc=True)
        self.assertTrue([f for f in m["licence_flags"] if f.startswith("TTC: non-commercial")])
        b["areas"][0]["feeds"][0]["allow_nc"] = "owner checked: personal channel, no ads"
        m = self.build("gta-toronto", "day", nm=nm, batch=b)
        self.assertTrue(any("owner checked" in f for f in m["licence_flags"]))

    def test_banned_words_and_em_dash(self):
        templates = self.pl.templates()
        for word in templates["banned"]:
            with self.assertRaises(sm.MetaError, msg=word):
                sm.check_text("title", f"Every bus in {word.capitalize()}ton", templates["banned"])
            recipe = dict(self.pl.recipe("gta-markham", self.batch), place=f"{word.capitalize()}ville")
            with self.assertRaises(sm.MetaError, msg=word):
                sm.build_meta(batch=self.batch, recipe=recipe, netmeta=netmeta(self.batch, recipe, "day"),
                              templates=templates, licences=self.pl.licences(), defaults=self.pl.defaults(),
                              publish_order=1, meta_key="0" * 64)
        with self.assertRaises(sm.MetaError):
            sm.check_text("description", "Markham \u2014 every bus", templates["banned"])
        recipe = dict(self.pl.recipe("gta-markham", self.batch), place="Mark\u2014ham")
        with self.assertRaises(sm.MetaError):
            sm.build_meta(batch=self.batch, recipe=recipe, netmeta=netmeta(self.batch, recipe, "day"),
                          templates=templates, licences=self.pl.licences(), defaults=self.pl.defaults(),
                          publish_order=1, meta_key="0" * 64)
        sm.check_text("description", "Every bus in Markham", templates["banned"])

    def test_title_limit(self):
        recipe = dict(self.pl.recipe("gta-markham", self.batch), place="A" * 80)
        with self.assertRaises(sm.MetaError):
            sm.build_meta(batch=self.batch, recipe=recipe, netmeta=netmeta(self.batch, recipe, "day"),
                          templates=self.pl.templates(), licences=self.pl.licences(), defaults=self.pl.defaults(),
                          publish_order=1, meta_key="0" * 64)

    def test_dates_sentences(self):
        d = self.metas["gta-burlington-day"]["description"]
        self.assertIn("The weekday average uses every October 2026 weekday in each timetable and leaves out "
                      "Thanksgiving (October 12).", d)
        self.assertIn("Burlington Transit has no October timetable, so it uses November 2 to 27.", d)
        self.assertIn("on an average November weekday", d)
        # The subtitle month follows Burlington's fallback; the timetables are mostly October's.
        self.assertIn("I took the October 2026 timetables", d)
        self.assertIn("The timetables are from October 2026.", self.metas["gta-burlington-rush"]["description"])
        w = self.metas["gta-toronto-week"]["description"]
        self.assertIn("October 31 is left out because the clocks change that night.", w)
        self.assertIn("GO Transit ran a reduced timetable on October 10, so that Saturday is left out.", w)
        self.assertIn("GO Transit changed its timetables during October, so its Tuesday uses one typical date.", w)
        self.assertIn("GO Transit has fewer than three usable October Saturdays and Sundays, so its Saturday and "
                      "Sunday each use one typical date.", w)
        # Only feeds with vehicles inside the city are mentioned.
        self.assertNotIn("Burlington", self.metas["gta-toronto-day"]["description"])

    def test_credits(self):
        m = self.metas["gta-toronto-day"]
        self.assertEqual(m["credits"][0], "TTC (Toronto Transit Commission): Open Government Licence - Toronto. "
                                          "Contains information licensed under the Open Government Licence - Toronto.")
        self.assertEqual(m["credits"][1], "GO Transit (Metrolinx): Metrolinx Open Data Licence.")
        self.assertEqual(len(m["credits"]), 5)
        self.assertIn("\n".join(m["credits"]), m["description"])

    def test_credit_without_repeated_publisher(self):
        """A feed whose publisher is its own name is credited once."""
        nm = {"feeds": [{"id": "cta", "name": "Chicago Transit Authority", "publisher": "Chicago Transit Authority",
                         "licence_id": "cc-by-4.0", "inside_share": 0.9, "inside_vehicle_minutes": 9.0},
                        {"id": "go", "name": "GO Transit", "publisher": "Metrolinx", "licence_id": "cc-by-4.0",
                         "inside_share": 0.1, "inside_vehicle_minutes": 1.0}]}
        lines = sm.credit_lines(nm, self.pl.licences(), self.pl.templates())
        self.assertEqual(lines, ["Chicago Transit Authority: CC BY 4.0.", "GO Transit (Metrolinx): CC BY 4.0."])

    def test_unknown_placeholder(self):
        t = json.loads(json.dumps(self.pl.templates()))
        t["title"]["day"] = "Every {vehicle} in {place}"
        recipe = self.pl.recipe("gta-markham", self.batch)
        with self.assertRaises(sm.MetaError):
            sm.build_meta(batch=self.batch, recipe=recipe, netmeta=netmeta(self.batch, recipe, "day"), templates=t,
                          licences=self.pl.licences(), defaults=self.pl.defaults(), publish_order=1, meta_key="0" * 64)

    def test_templates_obey_the_style_rules(self):
        t = self.pl.templates()
        text = json.dumps({k: v for k, v in t.items() if k != "banned"}, ensure_ascii=False)
        self.assertNotIn("\u2014", text)
        sm.check_text("templates", text, t["banned"])


class Formatting(unittest.TestCase):
    def test_clock_and_ranges(self):
        self.assertEqual(sm.clock_text(28860), "8:01 am")
        self.assertEqual(sm.clock_text(86400 + 3600 * 13), "1:00 pm")
        self.assertEqual(sm.clock_text(0), "12:00 am")
        self.assertEqual(sm.range_label(["2026-11-02", "2026-11-27"]), "November 2 to 27")
        self.assertEqual(sm.range_label(["2026-10-28", "2026-11-24"]), "October 28 to November 24")
        self.assertEqual(sm.join_words(["bus", "streetcar", "train"]), "bus, streetcar and train")
        self.assertEqual(sm.with_commas(12345), "12,345")


if __name__ == "__main__":
    unittest.main()

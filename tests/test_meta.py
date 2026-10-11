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

    def test_made_by(self):
        """Every description credits the channel, after the map line and before the hashtags (D5)."""
        self.assertEqual(self.pl.defaults()["author"], "SOtownships")
        for stem, m in self.metas.items():
            self.assertIn("\nMap: Overture Maps Foundation, © OpenStreetMap contributors (ODbL).\nMade by SOtownships.\n\n#",
                          m["description"], stem)
            self.assertNotIn("Katsuma", m["description"], stem)

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


GTA_PARTS = [{"name": "Toronto", "subtypes": ["county"], "area_km2": 631.1},
             {"name": "Peel Region", "subtypes": ["county"], "area_km2": 1246.9},
             {"name": "York Region", "subtypes": ["county"], "area_km2": 1762.1},
             {"name": "Durham Region", "subtypes": ["county"], "area_km2": 2523.8},
             {"name": "Halton Region", "subtypes": ["county"], "area_km2": 964.0}]


class Split(unittest.TestCase):
    """Videos that keep some of the batch's modes, and a boundary that joins several divisions."""

    @classmethod
    def setUpClass(cls):
        cls.pl = make.Pipeline(REPO, "tests/fixtures/gta")
        cls.batch = cls.pl.load_batch("gta")

    def meta(self, base, variant, modes, present=None, peak=None, **recipe_keys):
        recipe = dict(self.pl.recipe(base, self.batch), modes=modes, **recipe_keys)
        nm = netmeta(self.batch, recipe, variant)
        nm.update(modes=[m for m in self.batch["modes"] if m["id"] in modes],
                  modes_present=modes if present is None else present, place=recipe["place"])
        if peak:
            nm["peak"] = peak
        return sm.build_meta(batch=self.batch, recipe=recipe, netmeta=nm, templates=self.pl.templates(),
                             licences=self.pl.licences(), defaults=self.pl.defaults(), publish_order=1, meta_key="0" * 64)

    def test_trains(self):
        titles = {v: self.meta("gta-toronto", v, ["rail"], id="gta-toronto-trains")["title"] for v in PEAKS}
        self.assertEqual(titles, {"day": "Every train in Toronto in 24 hours",
                                  "rush": "Morning rush in Toronto: every train, 6:30 to 9:30 am",
                                  "week": "A week of every train in Toronto"})
        d = self.meta("gta-toronto", "day", ["rail"], id="gta-toronto-trains")["description"]
        self.assertIn("Every train running in Toronto on an average October weekday", d)
        self.assertIn("Busiest moment: 3,951 trains at 8:01 am.", d)
        self.assertIn("only includes vehicles inside the Toronto city limits.", d)
        w = self.meta("gta-toronto", "week", ["rail"], id="gta-toronto-trains")["description"]
        self.assertIn("Busiest moment: 3,620 trains, Friday at 5:14 pm.", w)
        one = self.meta("gta-toronto", "day", ["rail"], peak={"count": 1, "time": 28860}, id="gta-toronto-trains")
        self.assertIn("Busiest moment: 1 train at 8:01 am.", one["description"])
        tags = self.meta("gta-toronto", "day", ["rail"], id="gta-toronto-trains")["tags"]
        self.assertIn("train map", tags)
        self.assertNotIn("bus map", tags)
        self.assertEqual(tags[-3:], ["train map", "GTFS", "Ontario"])

    def test_buses_and_streetcars(self):
        m = self.meta("gta-toronto", "day", ["bus", "streetcar"], id="gta-toronto-buses")
        self.assertEqual(m["title"], "Every bus and streetcar in Toronto in 24 hours")
        self.assertIn("Busiest moment: 3,951 vehicles at 8:01 am.", m["description"])
        r = self.meta("gta-toronto", "rush", ["bus", "streetcar"], id="gta-toronto-buses")
        self.assertIn("Every bus and streetcar inside the city on an average October weekday.", r["description"])
        self.assertIn("Vehicles outside the Toronto city limits are faded and not counted.", r["description"])
        # A window with no streetcar inside names the buses alone, as the count line does.
        m = self.meta("gta-toronto", "day", ["bus", "streetcar"], present=["bus"], id="gta-toronto-buses")
        self.assertEqual(m["title"], "Every bus in Toronto in 24 hours")
        self.assertIn("Busiest moment: 3,951 buses at 8:01 am.", m["description"])
        tags = self.meta("gta-toronto", "day", ["bus", "streetcar"], id="gta-toronto-buses")["tags"]
        self.assertEqual(tags[-5:], ["transit map", "bus map", "streetcar map", "GTFS", "Ontario"])
        m = self.meta("gta-markham", "rush", ["bus"])
        self.assertEqual(m["title"], "Morning rush in Markham: every bus, 6:30 to 9:30 am")
        self.assertEqual(m["tags"][-4:], ["transit map", "bus map", "GTFS", "Ontario"])
        self.assertIn("Busiest moment of the morning: 3,951 buses at 8:01 am.", m["description"])

    def test_without_modes(self):
        # 2.15 changes this copy on purpose: a recipe without `modes` names the modes of its window too.
        recipe = self.pl.recipe("gta-toronto", self.batch)
        self.assertNotIn("modes", recipe)
        nm = netmeta(self.batch, recipe, "day")

        def meta(**keys):
            return sm.build_meta(batch=self.batch, recipe=recipe, netmeta=dict(nm, **keys),
                                 templates=self.pl.templates(), licences=self.pl.licences(),
                                 defaults=self.pl.defaults(), publish_order=1, meta_key="0" * 64)

        m = meta()
        self.assertEqual(m["tags"][-6:], ["transit map", "bus map", "streetcar map", "train map", "GTFS", "Ontario"])
        self.assertIn("Busiest moment: 3,951 vehicles at 8:01 am.", m["description"])
        m = meta(modes_present=["bus"])
        self.assertIn("Busiest moment: 3,951 buses at 8:01 am.", m["description"])
        self.assertEqual(m["tags"][-4:], ["transit map", "bus map", "GTFS", "Ontario"])

    def test_region_of_several_divisions(self):
        m = self.meta("gta-toronto", "day", ["rail"], id="gta-trains", place="the GTA", boundary=GTA_PARTS)
        self.assertEqual(m["title"], "Every train in the GTA in 24 hours")
        self.assertIn("Every train running in the GTA on an average October weekday", m["description"])
        self.assertIn("only includes vehicles inside Toronto, Peel Region, York Region, Durham Region and Halton "
                      "Region. The faded ones are just outside.", m["description"])
        self.assertNotIn("city limits", m["description"])
        self.assertIn("#thegta", m["hashtags"])
        self.assertNotIn("#gta", m["hashtags"], "on YouTube #gta files a video with Grand Theft Auto")
        r = self.meta("gta-toronto", "rush", ["rail"], id="gta-trains", place="the GTA", boundary=GTA_PARTS)
        self.assertEqual(r["title"], "Morning rush in the GTA: every train, 6:30 to 9:30 am")
        self.assertIn("Every train inside the region on an average October weekday.", r["description"])
        self.assertIn("Vehicles outside Toronto, Peel Region, York Region, Durham Region and Halton Region are faded "
                      "and not counted.", r["description"])
        w = self.meta("gta-toronto", "week", ["rail"], id="gta-trains", place="the GTA", boundary=GTA_PARTS)
        self.assertEqual(w["title"], "A week of every train in the GTA")
        # A list of one division reads as that division.
        one = self.meta("gta-toronto", "day", ["rail"], boundary=GTA_PARTS[:1])
        self.assertIn("inside the Toronto city limits.", one["description"])


class Colours(unittest.TestCase):
    """2.16: a colour-group video's description names its colours in one plain sentence."""

    EXAMPLES = {
        "Colours: Bus, Express and Streetcar, with everything else in grey.": [
            ("bus", "Bus", True), ("express", "Express", True), ("streetcar", "Streetcar", True), ("other", "Other", False)],
        "Colours: Line 1, Line 2, GO, Line 5, Line 6, Line 4 and UP.": [
            (k, k.replace("line", "Line ") if k.startswith("line") else k.upper(), True)
            for k in ("line1", "line2", "go", "line5", "line6", "line4", "up")],
        "Colours: Frequent and Express, with Local in grey.": [
            ("frequent", "Frequent", True), ("express", "Express", True), ("other", "Local", True)],
        "Colours: Bus, Rapid and Trolley.": [
            ("bus", "Bus", True), ("rapid", "Rapid", True), ("blue_line", "Blue Line", "Trolley"),
            ("green_line", "Green Line", "Trolley"), ("orange_line", "Orange Line", "Trolley")],
    }

    @classmethod
    def setUpClass(cls):
        cls.pl = make.Pipeline(REPO, "tests/fixtures/gta")
        cls.batch = cls.pl.load_batch("gta")
        cls.recipe = cls.pl.recipe("gta-markham", cls.batch)

    def meta(self, variant, groups=None):
        nm = netmeta(self.batch, self.recipe, variant)
        if groups is not None:
            nm = dict(nm, color_by="group", groups=[{"id": i, "label": lab, "brand": None, "share": 0.1, "legend": lg}
                                                    for i, lab, lg in groups])
        return sm.build_meta(batch=self.batch, recipe=self.recipe, netmeta=nm, templates=self.pl.templates(),
                             licences=self.pl.licences(), defaults=self.pl.defaults(), publish_order=1, meta_key="0" * 64)

    def test_colors_sentence(self):
        templates = self.pl.templates()
        for want, groups in self.EXAMPLES.items():
            nm = {"color_by": "group", "groups": [{"id": i, "label": lab, "legend": lg} for i, lab, lg in groups]}
            self.assertEqual(sm.colors_sentence(nm, templates), " " + want)
            day, rush, week = (self.meta(v, groups)["description"] for v in ("day", "rush", "week"))
            self.assertIn(f"The faded ones are just outside. {want}\n\n", day)
            self.assertIn(f"are faded and not counted. {want}\n\n", rush)
            self.assertIn(f"are faded and not counted. {want}\n\n", week)
            sm.check_text("colours", want, templates.get("banned", []))
        # A brand network, with or without color_by in its netmeta, reads as it always has.
        for v in ("day", "rush", "week"):
            plain = self.meta(v)["description"]
            self.assertNotIn("Colours:", plain)
            nm = dict(netmeta(self.batch, self.recipe, v), color_by="brand")
            branded = sm.build_meta(batch=self.batch, recipe=self.recipe, netmeta=nm, templates=self.pl.templates(),
                                    licences=self.pl.licences(), defaults=self.pl.defaults(), publish_order=1,
                                    meta_key="0" * 64)["description"]
            self.assertEqual(branded, plain)


class Review(unittest.TestCase):
    """Findings of the USA batch's description review, made general for every batch."""

    @classmethod
    def setUpClass(cls):
        cls.pl = make.Pipeline(REPO, "tests/fixtures/gta")
        cls.batch = cls.pl.load_batch("gta")

    def meta(self, rid, variant, nm=None, recipe=None, batch=None, **kw):
        recipe = recipe or self.pl.recipe(rid, self.batch)
        return sm.build_meta(batch=batch or self.batch, recipe=recipe, netmeta=nm or netmeta(self.batch, recipe, variant),
                             templates=self.pl.templates(), licences=self.pl.licences(), defaults=self.pl.defaults(),
                             publish_order=1, meta_key="0" * 64, **kw)

    def test_place_named_city(self):
        for place in ("New York City", "Mexico City", "Quebec City", "Kansas City", "Panama City"):
            recipe = dict(self.pl.recipe("gta-markham", self.batch), place=place)
            d = {v: self.meta("gta-markham", v, recipe=recipe)["description"] for v in ("day", "rush", "week")}
            self.assertIn(f"only includes vehicles inside the {place} limits. The faded ones", d["day"])
            self.assertIn(f"Vehicles outside the {place} limits are faded and not counted.", d["rush"])
            self.assertIn(f"Vehicles outside the {place} limits are faded and not counted.", d["week"])
            for text in d.values():
                self.assertNotIn("City city", text)
        # Only the whole word: Felicity is a city like any other, and Markham keeps its city limits.
        recipe = dict(self.pl.recipe("gta-markham", self.batch), place="Felicity")
        self.assertIn("inside the Felicity city limits.", self.meta("gta-markham", "day", recipe=recipe)["description"])
        self.assertIn("inside the Markham city limits.", self.meta("gta-markham", "day")["description"])

    def split_netmeta(self, rid, variant, modes, drawn, inside):
        """A netmeta of a video that keeps `modes`, with window_trips per feed and inside shares."""
        recipe = dict(self.pl.recipe(rid, self.batch), modes=modes)
        nm = netmeta(self.batch, recipe, variant)
        for f in nm["feeds"]:
            f["window_trips"] = drawn.get(f["id"], 0)
            f["inside_share"] = inside.get(f["id"], 0.0)
            f["inside_vehicle_minutes"] = f["inside_share"] * 1e5
        nm.update(modes=[m for m in self.batch["modes"] if m["id"] in modes], modes_present=modes)
        return recipe, nm

    def test_flags_follow_the_feeds_the_video_draws(self):
        # Buses: GO's buses are drawn, UP Express (a train) is not, and MiWay runs only outside the city.
        recipe, nm = self.split_netmeta("gta-toronto", "day", ["bus", "streetcar"],
                                        {"ttc": 30000, "go": 900, "miway": 400}, {"ttc": 0.97, "go": 0.03})
        m = self.meta("gta-toronto", "day", nm=nm, recipe=recipe)
        self.assertEqual([e["feed"] for e in m["licences"]], ["ttc", "go", "miway"])
        self.assertEqual([f.split(":")[0] for f in m["licence_flags"]], ["GO Transit"])
        self.assertEqual(m["flags"], m["licence_flags"])
        self.assertEqual([c.split(" (")[0] for c in m["credits"]], ["TTC", "GO Transit", "MiWay"])
        self.assertNotIn("UP Express", m["description"])
        # Trains: UP Express is drawn, so its flag is back, and the buses' feeds are gone.
        recipe, nm = self.split_netmeta("gta-toronto", "day", ["rail"], {"ttc": 2700, "go": 350, "upx": 160},
                                        {"ttc": 0.85, "go": 0.12, "upx": 0.03})
        m = self.meta("gta-toronto", "day", nm=nm, recipe=recipe)
        self.assertEqual([f.split(":")[0] for f in m["licence_flags"]], ["GO Transit", "UP Express"])
        self.assertEqual([e["feed"] for e in m["licences"]], ["ttc", "go", "upx"])
        # A non-commercial feed the video does not draw no longer stops it.
        b = json.loads(json.dumps(self.batch))
        next(f for f in b["areas"][0]["feeds"] if f["id"] == "milton")["licence_id"] = "cc-by-nc-4.0"
        next(f for f in nm["feeds"] if f["id"] == "milton")["licence_id"] = "cc-by-nc-4.0"
        self.meta("gta-toronto", "day", nm=nm, recipe=recipe, batch=b)
        # A rush window can leave out a feed the day draws.
        recipe, nm = self.split_netmeta("gta-toronto", "rush", ["rail"], {"ttc": 600, "go": 99}, {"ttc": 0.85, "go": 0.15})
        m = self.meta("gta-toronto", "rush", nm=nm, recipe=recipe)
        self.assertEqual([f.split(":")[0] for f in m["licence_flags"]], ["GO Transit"])
        # A netmeta of an older make.py has only the inside counts, as before.
        nm = netmeta(self.batch, self.pl.recipe("gta-toronto", self.batch), "day")
        self.assertEqual([f["id"] for f in sm.drawn_feeds(nm)], ["ttc", "go", "miway", "yrt", "upx"])

    def test_grey_only_when_other_is_drawn(self):
        t = self.pl.templates()
        groups = [{"id": "rr", "label": "Regional Rail", "share": 0.4311, "legend": True, "window_trips": 634},
                  {"id": "b", "label": "B", "share": 0.3576, "legend": True, "window_trips": 568},
                  {"id": "l", "label": "L", "share": 0.2113, "legend": True, "window_trips": 332},
                  {"id": "other", "label": "Other", "share": 0.0, "legend": False, "window_trips": 0}]
        nm = {"color_by": "group", "groups": groups}
        self.assertEqual(sm.colors_sentence(nm, t), " Colours: Regional Rail, B and L.")
        # Share 0 is the inside share at the morning peak alone: Philadelphia's M line runs grey west of the
        # city line all day, so its grey is named.
        groups[-1]["window_trips"] = 126
        self.assertEqual(sm.colors_sentence(nm, t), " Colours: Regional Rail, B and L, with everything else in grey.")
        # A netmeta of an older make.py says nothing of the window, so the clause stays as it was.
        del groups[-1]["window_trips"]
        self.assertEqual(sm.colors_sentence(nm, t), " Colours: Regional Rail, B and L, with everything else in grey.")
        # A named other is dropped the same way.
        nm = {"color_by": "group", "groups": [{"id": "frequent", "label": "Frequent", "window_trips": 10},
                                              {"id": "other", "label": "Local", "legend": True, "window_trips": 0}]}
        self.assertEqual(sm.colors_sentence(nm, t), " Colours: Frequent.")
        recipe = self.pl.recipe("gta-markham", self.batch)
        nm = dict(netmeta(self.batch, recipe, "rush"), color_by="group", groups=groups[:3] + [dict(groups[3], window_trips=0)])
        self.assertIn("are faded and not counted. Colours: Regional Rail, B and L.\n\n",
                      self.meta("gta-markham", "rush", nm=nm)["description"])

    def test_data_date_line(self):
        lic = self.pl.licences()["licences"]
        self.assertEqual([k for k, v in lic.items() if v.get("data_date")], ["metra-gtfs"])
        recipe, nm = self.split_netmeta("gta-toronto", "day", ["rail"], {"ttc": 2700, "go": 350},
                                        {"ttc": 0.85, "go": 0.15})
        next(f for f in nm["feeds"] if f["id"] == "go")["licence_id"] = "metra-gtfs"
        m = self.meta("gta-toronto", "day", nm=nm, recipe=recipe, data_dates={"go": "2026-10-04", "ttc": "2026-09-16"})
        i = next(i for i, c in enumerate(m["credits"]) if c.startswith("GO Transit"))
        self.assertEqual(m["credits"][i + 1], "GO Transit timetables as of 4 October 2026.")
        self.assertEqual(len(m["credits"]), 3, "a date only where the licence asks for one")
        self.assertIn("Not sponsored, affiliated or operated by Metra.\nGO Transit timetables as of 4 October 2026.\n",
                      m["description"])
        # Without the date the metadata fails rather than publish a description the licence does not allow.
        for dates in (None, {}, {"ttc": "2026-09-16"}):
            with self.assertRaises(sm.MetaError) as cm:
                self.meta("gta-toronto", "day", nm=nm, recipe=recipe, data_dates=dates)
            self.assertIn("go: licence metra-gtfs asks for the date the data was last updated", str(cm.exception))
        # A video that does not draw the feed needs no date.
        recipe, nm = self.split_netmeta("gta-toronto", "day", ["bus"], {"ttc": 30000}, {"ttc": 1.0})
        next(f for f in nm["feeds"] if f["id"] == "go")["licence_id"] = "metra-gtfs"
        self.assertNotIn("as of", self.meta("gta-toronto", "day", nm=nm, recipe=recipe)["description"])

    def test_legend_word_names_one_mode(self):
        b = json.loads(json.dumps(self.batch))
        b["modes"].append({"id": "ferry", "label": "ferries", "singular": "ferry", "route_types": [4]})
        recipe = self.pl.recipe("gta-markham", self.batch)

        def nm_with(groups):
            nm = dict(netmeta(b, recipe, "day"), color_by="group",
                      groups=[{"id": i, "label": lab, "legend": lg, "window_modes": wm, "window_trips": sum(wm.values())}
                              for i, lab, lg, wm in groups])
            nm["modes"], nm["modes_present"] = b["modes"], ["bus", "rail", "ferry"]
            return nm

        sd = [("bus", "Bus", True, {"bus": 5801, "ferry": 90}), ("rapid", "Rapid", True, {"bus": 940}),
              ("blue_line", "Blue Line", "Trolley", {"rail": 295})]
        with self.assertRaises(sm.MetaError) as cm:
            self.meta("gta-markham", "day", nm=nm_with(sd), batch=b)
        self.assertIn("group bus reads 'Bus' in the legend, but the video draws ferries in its colour (90 ferry trips)",
                      str(cm.exception))
        # The ferry in a group of its own: title, legend and colours sentence all name it.
        sd[0] = ("bus", "Bus", True, {"bus": 5801})
        m = self.meta("gta-markham", "day", nm=nm_with(sd + [("ferry", "Ferry", True, {"ferry": 90})]), batch=b)
        self.assertEqual(m["title"], "Every bus, train and ferry in Markham in 24 hours")
        self.assertIn("Colours: Bus, Rapid, Trolley and Ferry.", m["description"])
        # A mode id is a mode word too, and a group that mixes modes under a word that names none passes.
        with self.assertRaises(sm.MetaError):
            self.meta("gta-markham", "day", nm=nm_with([("rail", "Rail", True, {"rail": 700, "streetcar": 20})]), batch=b)
        self.meta("gta-markham", "day", nm=nm_with([("go", "GO", True, {"bus": 900, "rail": 350})]), batch=b)
        # An older netmeta has no window_modes and passes as it did.
        self.meta("gta-markham", "day", nm=nm_with([("bus", "Bus", True, {})]), batch=b)


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

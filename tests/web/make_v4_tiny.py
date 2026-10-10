#!/usr/bin/env python3
"""Writes tests/fixtures/v4_tiny/: a hand-made schema 4 city per spec 2.9 for the renderer tests.

  network.json  day network: 3 routes in 2 brands (two Alpha buses, one Beta train), 6 trips (one
                with a dwell, one after midnight), a 4 km square boundary with its 25 m mask, the
                day and rush variants
  week.json     the same trips with weekday masks ("w"), the week variant
  basemap.json  a few roads, a rail line, a lake and a river around the square

Counts follow A9 by brute force: every trip is on the road at minute m for m from ceil(t0 / 60)
to floor(t1 / 60), its position at 60 m by the CONTRACT rule (dwell when t[i+1] == t[i]), inside
when strictly inside the square; minutes fold modulo the period. There is one composite date,
so the means are whole numbers. Run it again after changing anything here:

  python3 -I tests/web/make_v4_tiny.py
"""

import json
import math
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT = os.path.join(ROOT, "tests", "fixtures", "v4_tiny")
ORIGIN = [-79.47, 43.8]
HALF = 2.0                     # boundary: the square |x| < 2, |y| < 2 km
CELL = 0.025
GROW = 0.1


def line(x0, y0, x1, y1, n):
    xy, cum = [], []
    for k in range(n + 1):
        f = k / n
        xy += [round(x0 + (x1 - x0) * f, 3), round(y0 + (y1 - y0) * f, 3)]
    for k in range(n + 1):
        if k == 0:
            cum.append(0.0)
        else:
            cum.append(round(cum[-1] + math.dist(xy[2 * k - 2:2 * k], xy[2 * k:2 * k + 2]), 4))
    return {"xy": xy, "cum": cum}


def curve(x0, y0, y1, n, amp):
    """South to north with a gentle bend, so the ribbon simplification has vertices to drop."""
    xy, cum = [], [0.0]
    for k in range(n + 1):
        f = k / n
        xy += [round(x0 + amp * math.sin(math.pi * f), 3), round(y0 + (y1 - y0) * f, 3)]
        if k:
            cum.append(round(cum[-1] + math.dist(xy[2 * k - 2:2 * k], xy[2 * k:2 * k + 2]), 4))
    return {"xy": xy, "cum": cum}


SHAPES = [line(-3, 0.5, 3, 0.5, 12), curve(-0.5, -3, 3, 48, 0.3), line(-3, -3, 3, 3, 12)]
L1 = SHAPES[1]["cum"][-1]
L2 = SHAPES[2]["cum"][-1]
MODES = [
    {"id": "bus", "label": "buses", "singular": "bus", "route_types": [3, 700]},
    {"id": "rail", "label": "trains", "singular": "train", "route_types": [1, 2]},
]
ROUTES = [
    {"id": "alpha:1", "short": "1", "long": "Main Street", "color": "ed1c24", "color_raw": "ed1c24", "feed": "alpha",
     "mode": "bus", "agency": "Alpha Transit", "type": 3, "brand": 0, "group": "alpha"},
    {"id": "alpha:2", "short": "2", "long": "North Road", "color": "2f6bff", "color_raw": "", "feed": "alpha",
     "mode": "bus", "agency": "Alpha Transit", "type": 3, "brand": 0, "group": "alpha"},
    {"id": "beta:L", "short": "L", "long": "Lakeshore Line", "color": "00853e", "color_raw": "00853e", "feed": "beta",
     "mode": "rail", "agency": "Beta Rail", "type": 2, "brand": 1, "group": "beta"},
]
# (route, shape, t, d, weekday mask for the week file; bit 0 = Monday)
TRIPS = [
    (0, 0, [25200, 27000, 28800], [0, 3, 6], 0b0011111),
    (0, 0, [27900, 29700, 31500], [0, 3, 6], 0b0011111),
    (1, 1, [27600, 28800, 30000], [0, round(L1 / 2, 4), L1], 0b1111111),
    (1, 1, [61200, 63000, 64800], [0, round(L1 / 2, 4), L1], 0b0011111),
    (2, 2, [28200, 28800, 28860, 30000], [0, round(L2 / 2, 4), round(L2 / 2, 4), L2], 0b1100000),
    (2, 2, [88200, 90000], [0, L2], 0b1111111),
]
TEMPLATES = {
    "day": [["Every {modes_singular} in {place}, 24 hours", "Busiest at {peak_time} with {peak_count} vehicles"],
            ["24 hours of {modes_plural} in {place}", "One average {month} weekday"],
            ["One {month} weekday in {place}", "{trips} trips, rush hour to rush hour"]],
    "rush": [["Every {modes_singular} in {place}, 6:30 to 9:30 am", "Busiest at {peak_time} with {peak_count} vehicles"],
             ["The morning rush in {place}", "{modes_plural} on an average {month} weekday"],
             ["Three hours of {modes_plural} in {place}", "Busiest at {peak_time}"]],
    "week": [["Every {modes_singular} in {place}, Monday to Sunday", "{month} timetables, {trips} trips"],
             ["A week of {modes_plural} in {place}", "Busiest at {peak_time} with {peak_count} vehicles"],
             ["Seven {month} days in {place}", "Weekdays, then the weekend"]],
}


def position(trip, T):
    """km position at T by the CONTRACT rule, or None when the trip is not on the road."""
    _, s, t, d, _ = trip
    if T < t[0] or T > t[-1]:
        return None
    i = 0
    while i < len(t) - 2 and t[i + 1] <= T:
        i += 1
    dist = d[i] if t[i + 1] == t[i] else d[i] + (T - t[i]) / (t[i + 1] - t[i]) * (d[i + 1] - d[i])
    xy, cum = SHAPES[s]["xy"], SHAPES[s]["cum"]
    k = 0
    while k < len(cum) - 2 and cum[k + 1] <= dist:
        k += 1
    seg = cum[k + 1] - cum[k]
    f = min(1, max(0, (dist - cum[k]) / seg)) if seg > 0 else 0
    return (xy[2 * k] + (xy[2 * k + 2] - xy[2 * k]) * f, xy[2 * k + 1] + (xy[2 * k + 3] - xy[2 * k + 1]) * f)


def inside(p):
    return abs(p[0]) < HALF and abs(p[1]) < HALF


def hists(period_min, week):
    hist = [0.0] * period_min
    by_mode = {m["id"]: [0.0] * period_min for m in MODES}
    by_group = {"alpha": [0.0] * period_min, "beta": [0.0] * period_min}
    for trip in TRIPS:
        r, _, t, _, w = trip
        days = [k for k in range(7) if w >> k & 1] if week else [0]
        for m in range(math.ceil(t[0] / 60), math.floor(t[-1] / 60) + 1):
            p = position(trip, 60 * m)
            if p is None or not inside(p):
                continue
            for k in days:
                b = (m + 1440 * k) % period_min
                hist[b] += 1
                by_mode[ROUTES[r]["mode"]][b] += 1
                by_group[ROUTES[r]["group"]][b] += 1
    return hist, by_mode, by_group


def argmax(h, lo, hi):
    """First argmax of h over minutes [lo, hi), indices taken modulo len(h)."""
    best = None
    for m in range(lo, hi):
        if best is None or h[m % len(h)] > h[best % len(h)]:
            best = m
    return best


def mask():
    x0 = y0 = -HALF - GROW
    n = round((2 * (HALF + GROW)) / CELL)
    rle = []
    for iy in range(n):
        cy = y0 + (iy + 0.5) * CELL
        runs, cur, run = [], False, 0
        for ix in range(n):
            cx = x0 + (ix + 0.5) * CELL
            v = inside((cx, cy))
            if v != cur:
                runs.append(run)
                cur, run = v, 0
            run += 1
        runs.append(run)
        rle += runs
    return {"cell_km": CELL, "x0": x0, "y0": y0, "nx": n, "ny": n, "rle": rle}


def variant(start, end, label, frames, render, h, frame=None):
    pm = argmax(h, start // 60, math.ceil(end / 60))
    return {"start": start, "end": end, "label": label, "frame": frame,
            "peak": {"count": round(h[pm % len(h)]), "time": pm * 60},
            "render": dict({"DURATION_FRAMES": frames, "HOLD_START": 0, "HOLD_END": 0}, **render)}


def network(week):
    period_min = 10080 if week else 1440
    hist, by_mode, by_group = hists(period_min, week)
    am = argmax(hist, 300, 631)
    pm = argmax(hist, 870, 1171)
    am_peak = {"count": round(hist[am]), "time": am * 60}
    pm_peak = {"count": round(hist[pm]), "time": pm * 60}
    total = sum(by_group.values(), [])
    share = {g: round(by_group[g][am] / hist[am], 4) for g in by_group} if hist[am] else {"alpha": 0.5, "beta": 0.5}
    if week:
        variants = {"week": variant(am * 60, am * 60 + 604800, "An average October week", 1800,
                                    {"LOOP": "wrap", "TIME_WARP_MODE": "activity-daily", "TIME_WARP_FLOOR": 0.3,
                                     "SPARK_SMOOTH_MIN": 60, "TRAIL_MINUTES": 30, "BUS_HALO_ALPHA": 0.1,
                                     "BUS_CORE_R": 0, "WEEKEND_BAND": True, "CLOCK_ROUND": 60}, hist)}
    else:
        variants = {
            "day": variant(am * 60, am * 60 + 86400, "An average October weekday", 1500,
                           {"LOOP": "wrap", "TIME_WARP_MODE": "activity", "SPARK_SMOOTH_MIN": 35}, hist),
            "rush": variant(23400, 34200, "Morning rush, an average October weekday", 750,
                            {"LOOP": "xfade", "TIME_WARP_MODE": "linear", "SPARK_SMOOTH_MIN": 15, "TRAIL_MINUTES": 8},
                            hist, frame={"km_vertical": 5.0, "center_km": [0.0, 0.3]}),
        }
    first = next(iter(variants.values()))
    copies = sum(bin(w).count("1") for *_, w in TRIPS) if week else len(TRIPS)
    trips = []
    for r, s, t, d, w in TRIPS:
        trip = {"r": r, "s": s, "t": t, "d": d}
        if week:
            trip["w"] = w
        trips.append(trip)
    feed = lambda fid, name, publisher, lic: {
        "id": fid, "name": name, "publisher": publisher, "licence_id": lic, "licence_text": lic, "version": "fixture",
        "sha256": "0" * 64, "month_used": "2026-10",
        "dates": {k: ["2026-10-05"] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")} if week else ["2026-10-05"],
        "rule": {k: "half" for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")} if week else "half",
        "excluded": [], "classes": 3, "drawn": 3, "mean_trips_per_day": 3.0,
        "inside_vehicle_minutes": 0, "inside_share": share.get(fid, 0), "major": True}
    meta = {
        "schema": 4, "kind": "city", "id": "test-tiny", "batch": "test", "area": "test", "place": "Tinytown",
        "title": "TINYTOWN", "subtitle": first["label"], "service_date": "2026-10", "origin": ORIGIN,
        "day_start": first["start"], "day_end": first["end"],
        "frame": {"km_vertical": 9.5, "center_km": [0.4, -0.6]},
        "trim": {"scale": 1.25, "box_km": [-4.9, -7.5, 5.7, 6.3]},
        "modes": MODES,
        "attribution": ["Data: Alpha, Beta · Map: Overture, OSM"],
        "credit": "Data: Alpha, Beta · Map: Overture, OSM",
        "build_key": "0" * 64,
        "feeds": [feed("alpha", "Alpha Transit", "Alpha", "cc-by-4.0"), feed("beta", "Beta Rail", "Beta", "cc-by-4.0")],
        "trips_total": copies,
        "timeline": {"kind": "week" if week else "day", "period": 604800 if week else 86400,
                     "basis": "average-week" if week else "average-weekday", "month": "2026-10",
                     "month_label": "October", "fallback_feeds": []},
        "hist_period": period_min,
        "am_peak": am_peak, "pm_peak": pm_peak,
        "hist_by_mode": by_mode,
        "groups": [{"id": "alpha", "label": "Alpha", "brand": "alpha", "share": share["alpha"]},
                   {"id": "beta", "label": "Beta", "brand": "beta", "share": share["beta"]}],
        "hist_by_group": by_group,
        "boundary": {"name": "Tinytown", "rings": [[-2, -2, 2, -2, 2, 2, -2, 2, -2, -2]], "holes": [],
                     "area_km2": 16.0, "bbox_km": [-2, -2, 2, 2], "source": "hand-made fixture", "mask": mask()},
        "panel": {"side": "left", "inside_under": {"left": 0, "right": 0}, "why": "fixture"},
        "brands": [
            {"id": "alpha", "label": "Alpha", "hex": "ed1c24", "kind": "agency", "entry": "alpha", "rail": False,
             "share": share["alpha"], "verified": True, "alt": None, "source": "fixture"},
            {"id": "beta", "label": "Beta", "hex": "00853e", "kind": "agency", "entry": "beta", "rail": False,
             "share": share["beta"], "verified": True, "alt": None, "source": "fixture"},
        ],
        "color_by": "brand", "preset": "shorts", "theme": {"batch": "lake"}, "render": {},
        "variants": variants,
        "card": {"title": "TINYTOWN", "templates": {k: TEMPLATES[k] for k in variants}},
    }
    assert len(total) == 2 * period_min
    return {"meta": meta, "routes": ROUTES, "shapes": SHAPES, "trips": trips,
            "hist": [round(v, 2) for v in hist]}


def basemap():
    def poly(*pts):
        return [c for p in pts for c in p]
    return {
        "origin": ORIGIN, "km_per_deg": [80.34, 110.574],
        "roads": {
            "major": [poly((-6, y), (6, y)) for y in (-3, -1, 1, 3)] + [poly((x, -8), (x, 8)) for x in (-3, -1, 1, 3)],
            "minor": [poly((-6, y + 0.5), (6, y + 0.5)) for y in (-4, -2, 0, 2)]
                     + [poly((x + 0.5, -8), (x + 0.5, 8)) for x in (-4, -2, 0, 2)],
            "rail": [poly((-6, -6), (6, 6))],
        },
        "water": {"poly": [poly((1.2, -6), (5, -6), (5, -2.6), (3, -2.2), (1.2, -3))],
                  "line": [{"c": "river", "xy": poly((-5, 4), (-2.5, 3.2), (0, 4.6), (4, 3.8))}]},
        "boundary": [],
    }


def write(name, obj):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")


def main():
    os.makedirs(OUT, exist_ok=True)
    write("network.json", network(False))
    write("week.json", network(True))
    write("basemap.json", basemap())
    print(f"wrote {OUT}/network.json, week.json, basemap.json")


if __name__ == "__main__":
    main()

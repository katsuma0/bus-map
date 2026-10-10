"""Write the three tiny GTFS fixtures of the v4 tests (and a square boundary).

    python3 -I tests/fixtures/gtfs/make_fixtures.py

* stable.zip: stable trip ids on weekday/Saturday/Sunday services (TTC-like),
  plus a trip on exactly half of the selected weekdays, a frequency template,
  a trip without a shape, a loop with repeated stops, blank and same-minute
  times, a dwell, an after-midnight trip, a stop without coordinates, a trip
  outside the area box and a reduced weekday (low).
* golike.zip: the same timetable with one service_id per date, dated trip ids
  and versioned route ids (GO-like). Its classes must equal stable.zip's.
* drtlike.zip: every trip re-signed on Monday 2026-10-19 (DRT-like), so an
  even number of Tuesdays splits 2 and 2 between the two timetables, plus a
  same-date duplicate pair, a reduced Saturday and a weekday without service.

The output is deterministic: fixed zip timestamps, rows in a fixed order.
Everything is placed around the GTA origin (-79.47, 43.80).
"""
import datetime as dt
import io
import json
import math
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ORIGIN = (-79.47, 43.80)
KX = 111.32 * math.cos(math.radians(ORIGIN[1]))
KY = 110.574
MONTH = [dt.date(2026, 10, d) for d in range(1, 32)]


def km_to_lonlat(x, y):
    return round(ORIGIN[0] + x / KX, 6), round(ORIGIN[1] + y / KY, 6)


def hms(sec):
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- geography (km about the origin)

STOPS = {
    # east-west street at y = -8
    "E1": (6.0, -8.0), "E2": (7.0, -8.0), "E3": (8.0, -8.0), "E4": (9.0, -8.0), "E5": (10.0, -8.0), "E6": (12.0, -8.0),
    # north-south street at x = 9.5
    "N1": (9.5, -12.0), "N2": (9.5, -10.0), "N3": (9.5, -8.1), "N4": (9.5, -6.5), "N5": (9.5, -4.0),
    # the top of the loop
    "L1": (8.5, -7.0),
    # far away, outside the area box
    "F1": (-90.0, -70.0), "F2": (-89.0, -70.0),
}


def line(points, step=0.25):
    """Densify a polyline (km) so stop snapping has real segments to choose from."""
    out = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for i in range(n):
            out.append((x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n))
    out.append(points[-1])
    return out


SHAPES = {
    "EW": line([(5.8, -8.02), (12.2, -8.02)]),
    "WE": line([(12.2, -7.98), (5.8, -7.98)]),
    "NS": line([(9.48, -12.2), (9.48, -3.8)]),
    # out along the street, round a block and back along the same street
    "LOOP": line([(6.0, -8.0), (9.0, -8.0), (9.0, -7.0), (8.0, -7.0), (8.0, -8.0), (6.0, -8.0)]),
    "FAR": line([(-90.0, -70.0), (-89.0, -70.0)]),
}

ROUTES = [
    # route key, short, long, type, colour
    ("R1", "1", "King", "3", "ED1C24"),
    ("R2", "", "H　北部シャトル", "3", ""),
    ("R3", "GO", "Lakeshore", "2", "00853E"),
    ("R4", "77", "Odd", "1700", "FFFFFF"),
    ("R5", "99", "Far", "3", "123456"),
]


# ---------------------------------------------------------------- the timetable


def pattern_times(stops, t0, gaps, blank=(), same_minute=False, dwell=None):
    """(stop, arrival, departure) rows; blank arrival/departure at indices in `blank`."""
    rows, t = [], t0
    for i, s in enumerate(stops):
        if i:
            t += gaps[(i - 1) % len(gaps)]
        arr, dep = hms(t), hms(t)
        if dwell is not None and i == dwell:
            dep = hms(t + 120)
            t += 120
        if i in blank:
            arr = dep = ""
        rows.append((s, arr, dep))
    if same_minute and len(rows) > 2:
        # two consecutive stops published in the same minute
        s, a, d = rows[2]
        rows[2] = (s, rows[1][1], rows[1][1])
    return rows


def day_trips(day_kind):
    """Trips of one service day kind: list of dicts (route, shape, direction, rows)."""
    out = []
    n = {"wd": 48, "sat": 24, "sun": 16}[day_kind]
    for i in range(n):
        t0 = 5 * 3600 + i * 20 * 60 + (0 if day_kind == "wd" else 300)
        out.append({"key": f"{day_kind}-e{i}", "route": "R1", "shape": "EW", "dir": "0", "svc": "base",
                    "rows": pattern_times(["E1", "E2", "E3", "E4", "E5", "E6"], t0, [180, 240, 150],
                                          blank=(3,) if i % 5 == 0 else (), same_minute=(i % 7 == 0))})
        out.append({"key": f"{day_kind}-w{i}", "route": "R1", "shape": "WE", "dir": "1", "svc": "west",
                    "rows": pattern_times(["E6", "E5", "E4", "E3", "E2", "E1"], t0 + 600, [150, 240, 180])})
    for i in range(n // 2):
        t0 = 6 * 3600 + i * 40 * 60
        out.append({"key": f"{day_kind}-n{i}", "route": "R2", "shape": "NS", "dir": "0", "svc": "base",
                    "rows": pattern_times(["N1", "N2", "N3", "N4", "N5"], t0, [300, 200, 250])})
    for i in range(6):
        t0 = 7 * 3600 + i * 2 * 3600
        out.append({"key": f"{day_kind}-g{i}", "route": "R3", "shape": "", "dir": "0", "svc": "base",
                    "rows": pattern_times(["E1", "E3", "E6"], t0, [420, 540], dwell=1)})
    for i in range(3):
        t0 = 9 * 3600 + i * 3 * 3600
        out.append({"key": f"{day_kind}-l{i}", "route": "R4", "shape": "LOOP", "dir": "0", "svc": "base",
                    "rows": pattern_times(["E1", "E2", "E3", "E4", "L1", "E3", "E2", "E1"], t0, [120, 120, 120, 150, 150, 120, 120])})
    # after midnight
    out.append({"key": f"{day_kind}-late", "route": "R1", "shape": "EW", "dir": "0", "svc": "base",
                "rows": pattern_times(["E1", "E2", "E3", "E4", "E5", "E6"], 25 * 3600 + 600, [180, 240, 150])})
    # one valid stop only: skipped as short
    out.append({"key": f"{day_kind}-short", "route": "R2", "shape": "NS", "dir": "0", "svc": "base",
                "rows": [("N1", hms(8 * 3600), hms(8 * 3600)), ("XX", hms(8 * 3600 + 300), hms(8 * 3600 + 300))]})
    # outside the area box
    out.append({"key": f"{day_kind}-far", "route": "R5", "shape": "FAR", "dir": "0", "svc": "base",
                "rows": pattern_times(["F1", "F2"], 10 * 3600, [600])})
    return out


FREQ = {"key": "freq", "route": "R2", "shape": "NS", "dir": "1", "svc": "base",
        "rows": pattern_times(["N5", "N4", "N3", "N2", "N1"], 12 * 3600, [240, 240, 240]),
        "freq": [(12 * 3600, 14 * 3600, 1800)]}
HALF = {"key": "half", "route": "R1", "shape": "EW", "dir": "0", "svc": "half",
        "rows": pattern_times(["E1", "E2", "E3", "E4", "E5", "E6"], 12 * 3600 + 300, [200, 200, 200])}


def kind_of(d):
    return "wd" if d.weekday() < 5 else ("sat" if d.weekday() == 5 else "sun")


def stable_plan():
    """date -> list of trips running that date, for the stable and the GO-like feed."""
    plan = {}
    weekdays = [d for d in MONTH if d.weekday() < 5]
    selected = [d for d in weekdays if d not in (dt.date(2026, 10, 12), dt.date(2026, 10, 23))]
    half_dates = set(selected[::2])     # exactly half of the 20 selected weekdays
    for d in MONTH:
        k = kind_of(d)
        if d == dt.date(2026, 10, 12):
            plan[d] = []                 # the holiday
            continue
        trips = [t for t in day_trips(k)]
        if d == dt.date(2026, 10, 23):
            trips = [t for t in trips if t["svc"] != "west"]   # a reduced day: 'low'
        if k == "wd":
            trips.append(FREQ)
            if d in half_dates:
                trips.append(HALF)
        plan[d] = trips
    return plan


def drt_plan():
    """Timetable A to Oct 18, B (every time + 60 s) from Oct 19; a same-date duplicate pair."""
    plan = {}
    for d in MONTH:
        k = kind_of(d)
        if d in (dt.date(2026, 10, 12), dt.date(2026, 10, 30)):
            plan[d] = []                 # the holiday, and a weekday with no service at all
            continue
        shift = 60 if d >= dt.date(2026, 10, 19) else 0
        trips = []
        for t in day_trips(k):
            if t["route"] in ("R4", "R5") or t["key"].endswith(("short", "late")):
                continue
            rows = [(s, hms_shift(a, shift), hms_shift(b, shift)) for s, a, b in t["rows"]]
            trips.append(dict(t, key=t["key"] + ("-b" if shift else "-a"), rows=rows))
        if shift and k in ("wd",):
            # timetable B runs two more weekday trips
            for j in range(2):
                trips.append({"key": f"extra{j}", "route": "R2", "shape": "NS", "dir": "1", "svc": "base",
                              "rows": pattern_times(["N5", "N4", "N3", "N2", "N1"], 15 * 3600 + 900 * j, [240, 240, 240])})
        if k == "wd":
            dup = pattern_times(["E1", "E2", "E3", "E4"], 16 * 3600 + 120 + shift, [200, 200, 200])
            trips.append({"key": "dupA", "route": "R1", "shape": "EW", "dir": "0", "svc": "base", "rows": dup})
            trips.append({"key": "dupB", "route": "R1", "shape": "EW", "dir": "0", "svc": "base", "rows": dup})
        if d == dt.date(2026, 10, 10):
            trips = [t for t in trips if t["svc"] != "west"]   # reduced Saturday: 'low'
        plan[d] = trips
    return plan


def hms_shift(s, shift):
    if not s:
        return s
    h, m, sec = (int(x) for x in s.split(":"))
    return hms(h * 3600 + m * 60 + sec + shift)


# ---------------------------------------------------------------- writing


def csv_text(header, rows):
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(v) for v in r) + "\n")
    return buf.getvalue()


def write_zip(path, files):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])


def common_files(agency, route_prefix):
    stops = []
    for sid in sorted(STOPS):
        lon, lat = km_to_lonlat(*STOPS[sid])
        stops.append((sid, sid.lower(), lat, lon))
    stops.append(("XX", "no coordinates", "", ""))
    shapes = []
    for sid in sorted(SHAPES):
        for i, (x, y) in enumerate(SHAPES[sid]):
            lon, lat = km_to_lonlat(x, y)
            shapes.append((sid, lat, lon, i + 1))
    return {
        "agency.txt": csv_text(["agency_id", "agency_name", "agency_url", "agency_timezone"],
                               [("A1", agency, "https://example.invalid", "America/Toronto")]),
        "stops.txt": csv_text(["stop_id", "stop_name", "stop_lat", "stop_lon"], stops),
        "routes.txt": csv_text(["route_id", "agency_id", "route_short_name", "route_long_name", "route_type", "route_color"],
                               [(route_prefix + r[0], "A1", r[1], r[2], r[3], r[4]) for r in ROUTES]),
        "shapes.txt": csv_text(["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], shapes),
    }


def stop_rows(trip_id, rows):
    return [(trip_id, a, b, s, i + 1) for i, (s, a, b) in enumerate(rows)]


def write_stable(plan, path):
    """Services by date set: one service per distinct set of dates a trip key runs on."""
    runs = {}
    defs = {}
    for d in sorted(plan):
        for t in plan[d]:
            runs.setdefault(t["key"], set()).add(d)
            defs[t["key"]] = t
    services = {}
    for key in sorted(runs):
        services.setdefault(frozenset(runs[key]), []).append(key)
    files = common_files("Stable Transit", "")
    trips, st, cal_dates, cal, freq = [], [], [], [], []
    for n, (dates, keys) in enumerate(sorted(services.items(), key=lambda kv: (min(kv[0]), sorted(kv[1])))):
        sid = f"S{n}"
        dates = sorted(dates)
        # a weekday pattern from the calendar where it fits, the rest by exceptions
        wd = sorted({d.weekday() for d in dates})
        start, end = dates[0], dates[-1]
        span = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        by_cal = {d for d in span if d.weekday() in wd}
        flags = ["1" if i in wd else "0" for i in range(7)]
        cal.append((sid, *flags, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        for d in sorted(by_cal - set(dates)):
            cal_dates.append((sid, d.strftime("%Y%m%d"), 2))
        for d in sorted(set(dates) - by_cal):
            cal_dates.append((sid, d.strftime("%Y%m%d"), 1))
        for key in sorted(keys):
            t = defs[key]
            tid = f"T-{key}"
            trips.append((t["route"], sid, tid, t["dir"], t["shape"]))
            st += stop_rows(tid, t["rows"])
            for a, b, h in t.get("freq", []):
                freq.append((tid, hms(a), hms(b), h))
    files["trips.txt"] = csv_text(["route_id", "service_id", "trip_id", "direction_id", "shape_id"], trips)
    files["stop_times.txt"] = csv_text(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], st)
    files["calendar.txt"] = csv_text(["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                      "sunday", "start_date", "end_date"], cal)
    files["calendar_dates.txt"] = csv_text(["service_id", "date", "exception_type"], cal_dates)
    if freq:
        files["frequencies.txt"] = csv_text(["trip_id", "start_time", "end_time", "headway_secs"], freq)
    write_zip(path, files)


def write_dated(plan, path, agency, route_version):
    """One service_id per date, trip ids carrying the date, route ids carrying a version (GO-like)."""
    files = common_files(agency, route_version)
    trips, st, cal_dates, freq = [], [], [], []
    for d in sorted(plan):
        ymd = d.strftime("%Y%m%d")
        if not plan[d]:
            continue
        sid = f"D{ymd}"
        cal_dates.append((sid, ymd, 1))
        for t in sorted(plan[d], key=lambda t: t["key"]):
            tid = f"{ymd}-{t['key']}"
            trips.append((route_version + t["route"], sid, tid, t["dir"], t["shape"]))
            st += stop_rows(tid, t["rows"])
            for a, b, h in t.get("freq", []):
                freq.append((tid, hms(a), hms(b), h))
    files["trips.txt"] = csv_text(["route_id", "service_id", "trip_id", "direction_id", "shape_id"], trips)
    files["stop_times.txt"] = csv_text(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], st)
    files["calendar_dates.txt"] = csv_text(["service_id", "date", "exception_type"], cal_dates)
    if freq:
        files["frequencies.txt"] = csv_text(["trip_id", "start_time", "end_time", "headway_secs"], freq)
    write_zip(path, files)


def write_boundary(path):
    """A 4 km square, x 7.3..11.3, y -10.3..-6.3 km, as a GeoJSON Feature in degrees.

    Its edges miss every stop, so no tested position sits on the outline.
    """
    sq = [(7.3, -10.3), (11.3, -10.3), (11.3, -6.3), (7.3, -6.3), (7.3, -10.3)]
    ring = [km_to_lonlat(x, y) for x, y in sq]
    feat = {"type": "Feature",
            "properties": {"id": "fixture-square", "name": "Fixture", "subtype": "locality", "area_km2": 16.0,
                           "source": "file tests/fixtures/gtfs/boundary.geojson"},
            "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(feat, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")


def main():
    plan = stable_plan()
    write_stable(plan, os.path.join(HERE, "stable.zip"))
    write_dated(plan, os.path.join(HERE, "golike.zip"), "Golike Transit", "0926-")
    write_dated(drt_plan(), os.path.join(HERE, "drtlike.zip"), "Drtlike Transit", "")
    write_boundary(os.path.join(HERE, "boundary.geojson"))
    for n in ("stable.zip", "golike.zip", "drtlike.zip", "boundary.geojson"):
        print(n, os.path.getsize(os.path.join(HERE, n)))


if __name__ == "__main__":
    main()

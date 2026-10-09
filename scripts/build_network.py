"""Turn the GTFS-JP zips in data/gtfs/ into data/built/network.json.

The renderer only needs, for one service day, every trip as a list of
(time, km-along-shape) pairs plus the shapes themselves, so this script
resolves the calendar, snaps stops onto shapes, fills blank times and
writes the compact structure described in docs/CONTRACT.md.

    python3 scripts/build_network.py [--date 20261009] [--feeds tsukuba|all]
                                     [--inspect TRIP_ID]
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import zipfile
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.join(HERE, "..", "data", "gtfs")
OUT_PATH = os.path.join(HERE, "..", "data", "built", "network.json")

ORIGIN = (140.085, 36.09)
KX = 111.32 * math.cos(math.radians(ORIGIN[1]))
KY = 110.574
DAY_START = 16200    # 04:30
DAY_END = 102600     # 28:30, the next morning
HIST_MINUTES = 1800  # 30 h of one-minute bins
MAX_KM_FROM_ORIGIN = 40.0

# Attribution is per publisher, not per the vendor named in feed_info, so it is
# pinned here. Feeds not listed fall back to feed_info and are treated as
# outside Tsukuba (only included with --feeds all).
FEEDS = {
    "tsukubus": {"publisher": "つくば市", "license": "CC BY 4.0", "tsukuba": True},
    "tsukubane": {"publisher": "つくば市", "license": "CC BY 4.0", "tsukuba": True},
    "tsuchimaru": {"publisher": "土浦市", "license": "CC BY 4.0", "tsukuba": False},
}

# Stops are matched to a shape by a monotone chain over near-optimal
# candidates; these bound what counts as a candidate.
CAND_SLACK_KM = 0.05
CAND_MAX = 8
MONOTONE_PENALTY_KM = 1.0


def to_km(lon, lat):
    return (lon - ORIGIN[0]) * KX, (lat - ORIGIN[1]) * KY


# ---------------------------------------------------------------- reading


class Feed:
    def __init__(self, feed_id, path):
        self.id = feed_id
        self.path = path
        self.tables = {}
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                base, ext = os.path.splitext(os.path.basename(info.filename))
                if ext.lower() not in (".txt", ".csv") or not base:
                    continue
                with zf.open(info) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                    rows = list(csv.DictReader(text))
                # Header cells sometimes carry stray spaces or quotes.
                self.tables[base] = [
                    {(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows
                ]

    def table(self, name):
        return self.tables.get(name, [])


def parse_time(s):
    """GTFS 'HH:MM:SS' to seconds; hours past 24 stay past 86400. Blank -> None."""
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 2:
        parts.append("0")
    h, m, sec = (int(p) for p in parts)
    return h * 3600 + m * 60 + sec


def active_services(feed, date):
    """Service ids running on `date` (datetime.date) per calendar + calendar_dates."""
    ymd = date.strftime("%Y%m%d")
    weekday = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][date.weekday()]
    active = set()
    for row in feed.table("calendar"):
        if row.get(weekday) != "1":
            continue
        if row.get("start_date") and row["start_date"] > ymd:
            continue
        if row.get("end_date") and row["end_date"] < ymd:
            continue
        active.add(row["service_id"])
    for row in feed.table("calendar_dates"):
        if row.get("date") != ymd:
            continue
        if row.get("exception_type") == "1":
            active.add(row["service_id"])
        elif row.get("exception_type") == "2":
            active.discard(row["service_id"])
    return active


# ---------------------------------------------------------------- geometry


def polyline_km(lonlat):
    """(n,2) lon/lat -> (xy (n,2) km, cum (n,)) with duplicate consecutive points dropped.

    Dropping duplicates keeps cum strictly increasing so the renderer never
    divides by a zero-length segment.
    """
    x, y = to_km(lonlat[:, 0], lonlat[:, 1])
    xy = np.column_stack([x, y])
    if len(xy) > 1:
        keep = np.ones(len(xy), dtype=bool)
        keep[1:] = np.any(np.abs(np.diff(xy, axis=0)) > 1e-6, axis=1)
        xy = xy[keep]
    seg = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.zeros(0)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    return xy, cum


def project_stops(xy, cum, stops_xy):
    """Snap stops onto the polyline, returning km along it (non-decreasing).

    Each stop gets a handful of candidate segments (the near-optimal local
    minima of perpendicular distance), then a chain is chosen that is monotone
    along the shape and minimises total snap distance. Looping routes that
    pass the same corner twice therefore resolve to the right pass, which a
    plain nearest-point snap gets wrong. Returns (d, n_forced) where n_forced
    counts stops that had no monotone candidate and were clamped.
    """
    A = xy[:-1]
    AB = xy[1:] - A
    L2 = (AB ** 2).sum(axis=1)
    L = np.sqrt(L2)
    safe = np.where(L2 > 0, L2, 1.0)
    # Broadcast every stop against every segment: (m, n-1).
    Q = stops_xy[:, None, :]
    t = np.clip(((Q - A) * AB).sum(axis=2) / safe, 0.0, 1.0)
    foot = A + t[..., None] * AB
    err = np.hypot(*(Q - foot).transpose(2, 0, 1))
    along = cum[:-1] + t * L

    # Candidate pruning: keep segments within CAND_SLACK_KM of the best, then
    # collapse consecutive runs (one pass of the route) to their best segment.
    cands = []
    for j in range(len(stops_xy)):
        e = err[j]
        ks = np.flatnonzero(e <= e.min() + CAND_SLACK_KM)
        runs = np.split(ks, np.flatnonzero(np.diff(ks) > 1) + 1)
        best = [int(run[np.argmin(e[run])]) for run in runs]
        best.sort(key=lambda k: e[k])
        best = best[:CAND_MAX]
        cands.append([(float(along[j, k]), float(e[k])) for k in best])

    # Dynamic programme over the candidate chain. A tiny bias toward smaller
    # `along` breaks exact ties in favour of the earlier pass.
    INF = float("inf")
    cost = [[c[1] + 1e-6 * c[0] for c in cands[0]]]
    back = [[-1] * len(cands[0])]
    forced = [False]
    for j in range(1, len(cands)):
        row, bp = [], []
        for a, e in cands[j]:
            bi, bc = -1, INF
            for i, (pa, _pe) in enumerate(cands[j - 1]):
                if pa <= a + 1e-9 and cost[j - 1][i] < bc:
                    bi, bc = i, cost[j - 1][i]
            row.append(bc + e + 1e-6 * a)
            bp.append(bi)
        if all(c == INF for c in row):
            # Nothing ahead of the previous stop fits; take the cheapest
            # predecessor anyway and let the clamp below turn it into a dwell.
            pi = int(np.argmin(cost[j - 1]))
            row = [cost[j - 1][pi] + e + MONOTONE_PENALTY_KM for _a, e in cands[j]]
            bp = [pi] * len(cands[j])
            forced.append(True)
        else:
            forced.append(False)
        cost.append(row)
        back.append(bp)

    d = np.zeros(len(cands))
    i = int(np.argmin(cost[-1]))
    for j in range(len(cands) - 1, -1, -1):
        d[j] = cands[j][i][0]
        i = back[j][i]
    d = np.maximum.accumulate(d)
    return d, sum(forced)


def fill_times(times, d):
    """Fill None entries by linear interpolation along shape distance.

    Leading or trailing blanks (not valid GTFS, but seen in the wild) are
    extrapolated at the speed of the nearest filled interval.
    """
    t = np.array([np.nan if v is None else float(v) for v in times])
    known = np.flatnonzero(~np.isnan(t))
    if len(known) == len(t):
        return t
    if len(known) < 2:
        return None
    # np.interp needs strictly increasing x; nudge dwell duplicates apart.
    x = d + np.arange(len(d)) * 1e-9
    out = np.interp(x, x[known], t[known])
    k0, k1 = known[0], known[1]
    kN, kM = known[-1], known[-2]
    v_head = (t[k1] - t[k0]) / max(x[k1] - x[k0], 1e-9)
    v_tail = (t[kN] - t[kM]) / max(x[kN] - x[kM], 1e-9)
    head = np.arange(len(t)) < k0
    tail = np.arange(len(t)) > kN
    out[head] = t[k0] - (x[k0] - x[head]) * v_head
    out[tail] = t[kN] + (x[tail] - x[kN]) * v_tail
    return out


def spread_same_minute(arr, dep, d):
    """Separate consecutive stops that share one published time.

    GTFS-JP timetables are minute-resolution, so a run of stops with the same
    time was really passed at different moments. Drawn from the raw times the
    bus would sit still and then jump a few hundred metres in a single frame,
    so the run is re-timed at constant speed from its first stop through to
    the next stop with a distinct time. That can move a stop a few minutes past
    its published minute when the following gap is long, but it never implies
    a speed the timetable itself does not (a cap would). A run at the very end
    of a trip has nothing to aim at and is spread over 59 s instead. Dwells
    (same place, same time) are left alone. Returns the number of stops moved.
    """
    arr, dep = arr.copy(), dep.copy()
    m, i, moved = len(arr), 0, 0
    while i < m:
        j = i
        while j + 1 < m and arr[j + 1] == arr[i] and dep[j] == arr[i]:
            j += 1
        if j > i and d[j] > d[i]:
            if j + 1 < m:
                end, span = arr[j + 1], d[j + 1] - d[i]
            else:
                end, span = arr[i] + 59, d[j] - d[i]
            for k in range(i + 1, j + 1):
                arr[k] = arr[i] + (end - arr[i]) * (d[k] - d[i]) / span
                dep[k] = max(dep[k], arr[k])
            moved += j - i
        i = j + 1
    return arr, dep, moved


def split_route_name(short, long):
    """Tsukubus writes 'H　北部シャトル' with no short name; split that off."""
    long = long.strip()
    if not short:
        m = re.match(r"^([A-Za-z0-9]{1,3})[\s　]+(.+)$", long)
        if m:
            return m.group(1), m.group(2).strip()
    return short.strip(), long


# ---------------------------------------------------------------- building


def expand_frequencies(feed, trips_by_id, stop_times):
    """Return extra (trip_id, template_trip_id, offset_seconds) for frequencies.txt."""
    extra = []
    for row in feed.table("frequencies"):
        tid = row.get("trip_id")
        if tid not in trips_by_id or tid not in stop_times:
            continue
        start, end, headway = parse_time(row.get("start_time")), parse_time(row.get("end_time")), int(row.get("headway_secs") or 0)
        if start is None or end is None or headway <= 0:
            continue
        first = next((parse_time(r.get("departure_time") or r.get("arrival_time")) for r in stop_times[tid]), None)
        if first is None:
            continue
        n = 0
        while start + n * headway < end:
            extra.append((f"{tid}#{n}", tid, start + n * headway - first))
            n += 1
    return extra


def build_feed(feed, date, routes, route_index, shapes, shape_index, smooth=True):
    """Append this feed's routes/shapes/trips to the shared lists; return stats."""
    stats = {"id": feed.id, "trips_in_file": 0, "trips_on_date": 0, "skipped_short": 0,
             "forced_monotone": 0, "time_fixes": 0, "shape_from_stops": 0, "blank_filled": 0,
             "spread": 0, "first": None, "last": None, "trips": []}

    agency = feed.table("agency")
    info = feed.table("feed_info")
    stats["name"] = agency[0].get("agency_name", feed.id) if agency else feed.id
    stats["version"] = info[0].get("feed_version", "") if info else ""
    stats["publisher_in_feed"] = info[0].get("feed_publisher_name", "") if info else ""

    stops = {}
    for r in feed.table("stops"):
        try:
            stops[r["stop_id"]] = (float(r["stop_lon"]), float(r["stop_lat"]), r.get("stop_name", ""))
        except (KeyError, ValueError):
            continue

    raw_shapes = defaultdict(list)
    for r in feed.table("shapes"):
        try:
            raw_shapes[r["shape_id"]].append((int(float(r["shape_pt_sequence"])), float(r["shape_pt_lon"]), float(r["shape_pt_lat"])))
        except (KeyError, ValueError):
            continue
    shape_km = {}
    for sid, pts in raw_shapes.items():
        pts.sort()
        xy, cum = polyline_km(np.array([(p[1], p[2]) for p in pts]))
        if len(xy) >= 2:
            shape_km[sid] = (xy, cum)

    stop_times = defaultdict(list)
    for r in feed.table("stop_times"):
        stop_times[r["trip_id"]].append(r)
    for rows in stop_times.values():
        rows.sort(key=lambda r: int(float(r.get("stop_sequence") or 0)))

    active = active_services(feed, date)
    trips = feed.table("trips")
    stats["trips_in_file"] = len(trips)
    trips_by_id = {t["trip_id"]: t for t in trips}

    # Frequency-based trips become ordinary trips with shifted times; the trip
    # named in frequencies.txt is only a template and is not scheduled itself.
    freq = [x for x in expand_frequencies(feed, trips_by_id, stop_times) if trips_by_id[x[1]]["service_id"] in active]
    templates = {x[1] for x in freq}
    schedule = [(t["trip_id"], t["trip_id"], 0) for t in trips if t["service_id"] in active and t["trip_id"] not in templates]
    schedule += freq

    route_rows = {r["route_id"]: r for r in feed.table("routes")}
    pattern_cache = {}

    for trip_id, template_id, offset in schedule:
        trip = trips_by_id[template_id]
        rows = [r for r in stop_times.get(template_id, []) if r.get("stop_id") in stops]
        if len(rows) < 2:
            stats["skipped_short"] += 1
            continue

        stop_ids = tuple(r["stop_id"] for r in rows)
        stops_xy = np.array([to_km(stops[s][0], stops[s][1]) for s in stop_ids])

        shape_id = trip.get("shape_id", "")
        if shape_id in shape_km:
            key = (feed.id, "shape", shape_id)
            xy, cum = shape_km[shape_id]
        else:
            # No usable shape: the stop sequence itself becomes the polyline.
            key = (feed.id, "stops") + stop_ids
            xy, cum = polyline_km(np.array([(stops[s][0], stops[s][1]) for s in stop_ids]))
            if len(xy) < 2:
                stats["skipped_short"] += 1
                continue
            stats["shape_from_stops"] += 1
        if key not in shape_index:
            shape_index[key] = len(shapes)
            shapes.append({"xy": np.round(xy.ravel(), 4).tolist(), "cum": np.round(cum, 4).tolist()})
        s_idx = shape_index[key]

        pkey = (key, stop_ids)
        if pkey not in pattern_cache:
            d, forced = project_stops(xy, cum, stops_xy)
            pattern_cache[pkey] = (d, forced)
        d_stop, forced = pattern_cache[pkey]
        stats["forced_monotone"] += forced

        arr = [parse_time(r.get("arrival_time")) for r in rows]
        dep = [parse_time(r.get("departure_time")) for r in rows]
        arr = [a if a is not None else b for a, b in zip(arr, dep)]
        dep = [b if b is not None else a for a, b in zip(arr, dep)]
        stats["blank_filled"] += sum(1 for a in arr if a is None)
        arr_f = fill_times(arr, d_stop)
        dep_f = fill_times(dep, d_stop)
        if arr_f is None or dep_f is None:
            stats["skipped_short"] += 1
            continue
        if smooth:
            arr_f, dep_f, moved = spread_same_minute(arr_f, dep_f, d_stop)
            stats["spread"] += moved

        # A stop where the bus waits contributes two entries at the same d.
        t_list, d_list = [], []
        for a, b, dd in zip(arr_f, dep_f, d_stop):
            a, b = int(round(a)) + offset, int(round(b)) + offset
            t_list.append(a); d_list.append(dd)
            if b > a:
                t_list.append(b); d_list.append(dd)
        t_arr = np.array(t_list, dtype=np.int64)
        fixed = np.maximum.accumulate(t_arr)
        stats["time_fixes"] += int((fixed != t_arr).sum())
        t_arr = fixed

        route_id = trip.get("route_id", "")
        rkey = f"{feed.id}:{route_id}"
        if rkey not in route_index:
            rr = route_rows.get(route_id, {})
            short, long = split_route_name(rr.get("route_short_name", ""), rr.get("route_long_name", "") or route_id)
            color = (rr.get("route_color") or "2f6bff").lower()
            route_index[rkey] = len(routes)
            routes.append({"id": rkey, "short": short, "long": long, "color": color, "feed": feed.id})

        stats["trips"].append({
            "r": route_index[rkey], "s": s_idx,
            "t": t_arr.tolist(), "d": np.round(np.array(d_list), 4).tolist(),
            "_id": trip_id, "_stops": [stops[s][2] for s in stop_ids],
        })
        stats["trips_on_date"] += 1
        t0, t1 = int(t_arr[0]), int(t_arr[-1])
        stats["first"] = t0 if stats["first"] is None else min(stats["first"], t0)
        stats["last"] = t0 if stats["last"] is None else max(stats["last"], t0)

    return stats


def histogram(trips):
    """Running buses per minute: trip is running when t[0] <= T <= t[-1]."""
    hist = np.zeros(HIST_MINUTES, dtype=np.int64)
    for tr in trips:
        lo = math.ceil(tr["t"][0] / 60)
        hi = math.floor(tr["t"][-1] / 60)
        lo, hi = max(lo, 0), min(hi, HIST_MINUTES - 1)
        if hi >= lo:
            hist[lo:hi + 1] += 1
    return hist


def fmt_time(sec):
    if sec is None:
        return "-"
    h, m = divmod(int(sec) // 60, 60)
    return f"{h:02d}:{m:02d}"


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def validate(shapes, trips):
    for i, sh in enumerate(shapes):
        xy = np.array(sh["xy"]).reshape(-1, 2)
        cum = np.array(sh["cum"])
        assert len(xy) >= 2, f"shape {i} has {len(xy)} points"
        assert len(cum) == len(xy), f"shape {i} cum/xy length mismatch"
        assert np.all(np.diff(cum) >= 0), f"shape {i} cum decreases"
        assert np.all(np.hypot(xy[:, 0], xy[:, 1]) <= MAX_KM_FROM_ORIGIN), f"shape {i} has a point > {MAX_KM_FROM_ORIGIN} km from origin"
    for i, tr in enumerate(trips):
        t, d = tr["t"], tr["d"]
        assert len(t) == len(d) >= 2, f"trip {i} has {len(t)} times / {len(d)} distances"
        assert all(b >= a for a, b in zip(t, t[1:])), f"trip {i} times decrease"
        assert all(b >= a for a, b in zip(d, d[1:])), f"trip {i} distances decrease"
        end = shapes[tr["s"]]["cum"][-1] + 0.01
        assert all(0 <= v <= end for v in d), f"trip {i} distance outside [0, {end}]"
        assert 0 <= tr["r"] and 0 <= tr["s"] < len(shapes), f"trip {i} bad indices"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default="20261009", help="service date YYYYMMDD (default 20261009)")
    ap.add_argument("--feeds", default="tsukuba", help="'tsukuba' (default), 'all', or comma-separated zip base names")
    ap.add_argument("--out", default=OUT_PATH, help="output path (default data/built/network.json)")
    ap.add_argument("--gtfs-dir", default=GTFS_DIR, help="directory of GTFS zips (default data/gtfs)")
    ap.add_argument("--no-smooth", action="store_true", help="keep raw minute-resolution times for stops that share a minute")
    ap.add_argument("--inspect", default=None, help="print stop-by-stop times and distances for this trip_id")
    args = ap.parse_args()

    date = dt.datetime.strptime(args.date, "%Y%m%d").date()
    gtfs_dir = args.gtfs_dir
    zips = sorted(f for f in os.listdir(gtfs_dir) if f.lower().endswith(".zip"))
    all_ids = [os.path.splitext(f)[0] for f in zips]
    if args.feeds == "all":
        wanted = all_ids
    elif args.feeds == "tsukuba":
        wanted = [f for f in all_ids if FEEDS.get(f, {}).get("tsukuba")]
    else:
        wanted = [f.strip() for f in args.feeds.split(",") if f.strip()]
    missing = [f for f in wanted if f not in all_ids]
    if missing:
        sys.exit(f"no such feed zip in {gtfs_dir}: {missing}")

    routes, route_index, shapes, shape_index = [], {}, [], {}
    feed_stats = []
    for fid in wanted:
        feed = Feed(fid, os.path.join(gtfs_dir, fid + ".zip"))
        feed_stats.append(build_feed(feed, date, routes, route_index, shapes, shape_index, smooth=not args.no_smooth))

    trips = [t for st in feed_stats for t in st["trips"]]
    trips.sort(key=lambda t: (t["t"][0], t["r"]))
    hist = histogram(trips)
    peak_min = int(np.argmax(hist)) if len(trips) else 0

    if args.inspect:
        found = [t for t in trips if t["_id"] == args.inspect]
        if not found:
            print(f"trip {args.inspect!r} not on {date}", file=sys.stderr)
        for tr in found:
            route = routes[tr["r"]]
            print(f"\ntrip {tr['_id']}  route {route['short']} {route['long']}  shape #{tr['s']} ({shapes[tr['s']]['cum'][-1]:.2f} km)")
            print(f"  {'time':>8s}  {'d km':>7s}  {'step km':>8s}  stop")
            names = iter(tr["_stops"])
            prev_d, prev_t = None, None
            for t, d in zip(tr["t"], tr["d"]):
                # Dwell entries share a stop with the previous row.
                name = next(names) if prev_d is None or d != prev_d or t == prev_t else ""
                step = "" if prev_d is None else f"{d - prev_d:8.3f}"
                print(f"  {fmt_time(t)}:{t % 60:02d}  {d:7.3f}  {step:>8s}  {name}")
                prev_d, prev_t = d, t

    public_trips = [{"r": t["r"], "s": t["s"], "t": t["t"], "d": t["d"]} for t in trips]
    validate(shapes, public_trips)

    meta = {
        "service_date": date.isoformat(),
        "title": "TSUKUBA BUSES",
        "subtitle": f"{date.strftime('%A')} {ordinal(date.day)} {date.strftime('%B')}",
        "origin": list(ORIGIN),
        "day_start": DAY_START,
        "day_end": DAY_END,
        "feeds": [
            {
                "id": st["id"],
                "name": st["name"],
                "publisher": FEEDS.get(st["id"], {}).get("publisher", st["publisher_in_feed"]),
                "license": FEEDS.get(st["id"], {}).get("license", ""),
                "version": st["version"],
                "trips_on_date": st["trips_on_date"],
            }
            for st in feed_stats
        ],
        "trips_total": len(public_trips),
        "peak": {"count": int(hist[peak_min]), "time": peak_min * 60},
    }
    out = {"meta": meta, "routes": routes, "shapes": shapes, "trips": public_trips, "hist": hist.tolist()}

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))

    print(f"\nservice date {meta['service_date']} ({meta['subtitle']})   feeds: {', '.join(wanted)}")
    print(f"{'feed':<11s}{'name':<36s}{'in file':>8s}{'on date':>8s}{'first':>7s}{'last':>7s}{'peak':>6s}{'at':>7s}  notes")
    for st in feed_stats:
        fh_ = histogram(st["trips"]) if st["trips"] else np.zeros(1, dtype=int)
        pk = int(np.argmax(fh_))
        notes = []
        if st["skipped_short"]:
            notes.append(f"{st['skipped_short']} skipped (<2 stops)")
        if st["shape_from_stops"]:
            notes.append(f"{st['shape_from_stops']} shapes from stops")
        if st["blank_filled"]:
            notes.append(f"{st['blank_filled']} blank times filled")
        if st["spread"]:
            notes.append(f"{st['spread']} same-minute stops spread")
        if st["forced_monotone"]:
            notes.append(f"{st['forced_monotone']} stops clamped monotone")
        if st["time_fixes"]:
            notes.append(f"{st['time_fixes']} times clamped")
        print(f"{st['id']:<11s}{st['name'][:34]:<36s}{st['trips_in_file']:>8d}{st['trips_on_date']:>8d}"
              f"{fmt_time(st['first']):>7s}{fmt_time(st['last']):>7s}{int(fh_[pk]):>6d}{fmt_time(pk * 60):>7s}  {'; '.join(notes)}")
    firsts = [st["first"] for st in feed_stats if st["first"] is not None]
    lasts = [st["last"] for st in feed_stats if st["last"] is not None]
    print(f"{'total':<11s}{'':<36s}{sum(s['trips_in_file'] for s in feed_stats):>8d}{len(public_trips):>8d}"
          f"{fmt_time(min(firsts) if firsts else None):>7s}{fmt_time(max(lasts) if lasts else None):>7s}"
          f"{meta['peak']['count']:>6d}{fmt_time(meta['peak']['time']):>7s}")
    print(f"routes {len(routes)}, shapes {len(shapes)}, trips {len(public_trips)}, "
          f"hist bins {len(hist)} (running at 04:30 {hist[DAY_START // 60]}, at 28:29 {hist[-1]})")
    print(f"wrote {os.path.abspath(args.out)}  {os.path.getsize(args.out) / 1e6:.2f} MB")


if __name__ == "__main__":
    main()

"""Turn the GTFS zips of one city into the network.json the renderer animates.

The renderer only needs, for one service day, every trip as a list of
(time, km-along-shape) pairs plus the shapes themselves, so this script
resolves the calendar, snaps stops onto shapes, fills blank times and
writes the compact structure described in docs/CONTRACT.md.

    python3 scripts/build_network.py [--city <id>] [--date 20261009]
                                     [--feeds default|all|a,b] [--no-smooth]
                                     [--inspect TRIP_ID] [--out PATH] [--gtfs-dir DIR]

Everything city-specific (origin, clip box, feeds, modes, output directory,
gzip, route-type filter, agency groups) comes from cities/<id>.json; the
command line only overrides it.
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import re
import resource
import sys
import time
import zipfile
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))

DAY_START = 16200    # 04:30
DAY_END = 102600     # 28:30, the next morning
HIST_MINUTES = 1800  # 30 h of one-minute bins

# The projection is process-wide state set from the city config in main();
# the Tsukuba values keep the module importable on its own.
ORIGIN = (140.085, 36.09)
KX = 111.32 * math.cos(math.radians(ORIGIN[1]))
KY = 110.574

# Stops are matched to a shape by a monotone chain over near-optimal
# candidates; these bound what counts as a candidate.
CAND_SLACK_KM = 0.05
CAND_MAX = 8
MONOTONE_PENALTY_KM = 1.0


def set_origin(origin):
    global ORIGIN, KX, KY
    ORIGIN = (float(origin[0]), float(origin[1]))
    KX = 111.32 * math.cos(math.radians(ORIGIN[1]))
    KY = 110.574


def to_km(lon, lat):
    return (lon - ORIGIN[0]) * KX, (lat - ORIGIN[1]) * KY


def load_city(city_id):
    with open(os.path.join(ROOT, "cities", f"{city_id}.json"), encoding="utf-8") as fh:
        return json.load(fh)


# The config keys the v3 contract added. A config using none of them gets
# exactly the v2 output, so the committed Tsukuba and GTA files stay byte
# identical; one using any of them also gets routes[].agency/modelled and
# meta.feeds[].modelled.
V3_KEYS = ("basemap_city", "subtitle", "include_route_types", "color_by", "group_by", "theme", "render")


def uses_v3(city):
    feeds = city.get("feeds", []) + city.get("optional_feeds", [])
    return any(k in city for k in V3_KEYS) or any("modelled" in f for f in feeds)


class Grouper:
    """Assigns routes to the config's group_by groups.

    Only the agency field exists so far: the match is the exact agency_name of
    the route's agency, because the modelled feeds name their agencies after
    the 事業者名 of the counts file and the real ones after the operator.
    """

    def __init__(self, spec):
        self.spec = spec
        if spec is None:
            return
        if spec.get("field") != "agency":
            sys.exit(f"group_by field {spec.get('field')!r} is not implemented; only 'agency' is")
        self.groups = [{"id": g["id"], "label": g["label"]} for g in spec["groups"]] + \
                      [{"id": spec["default"]["id"], "label": spec["default"]["label"]}]
        ids = [g["id"] for g in self.groups]
        if len(set(ids)) != len(ids):
            sys.exit(f"group_by ids must be unique, got {ids}")
        self.by_agency = {}
        for g in spec["groups"]:
            for name in g["match"]:
                if name in self.by_agency and self.by_agency[name] != g["id"]:
                    sys.exit(f"agency {name!r} matches both {self.by_agency[name]!r} and {g['id']!r}")
                self.by_agency[name] = g["id"]
        self.default = spec["default"]["id"]
        self.unmatched = defaultdict(int)

    def __bool__(self):
        return self.spec is not None

    def __call__(self, agency_name):
        gid = self.by_agency.get(agency_name)
        if gid is None:
            self.unmatched[agency_name] += 1
            return self.default
        return gid


# ---------------------------------------------------------------- reading


class Feed:
    """One GTFS zip. Small tables come out as lists of dicts; stop_times and
    shapes come out as string DataFrames, because TTC's stop_times alone is
    207 MB and a dict per row would need gigabytes and minutes to build."""

    def __init__(self, feed_id, path):
        self.id = feed_id
        self.path = path
        self.zf = zipfile.ZipFile(path)
        self.members = {}
        for info in self.zf.infolist():
            # GTFS-JP files may be .txt or .csv and may sit in a folder.
            base, ext = os.path.splitext(os.path.basename(info.filename))
            if ext.lower() in (".txt", ".csv") and base and base not in self.members:
                self.members[base] = info

    def table(self, name):
        info = self.members.get(name)
        if info is None:
            return []
        with self.zf.open(info) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
            rows = list(csv.DictReader(text))
        # Header cells sometimes carry stray spaces or quotes.
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]

    def frame(self, name, columns):
        """The named columns as strings, blank where a column is missing."""
        info = self.members.get(name)
        if info is None:
            return pd.DataFrame({c: pd.Series(dtype=object) for c in columns})
        wanted = set(columns)
        with self.zf.open(info) as fh:
            df = pd.read_csv(fh, dtype=str, usecols=lambda c: c.strip() in wanted, keep_default_na=False,
                             na_filter=False, encoding="utf-8-sig", skipinitialspace=True)
        df.columns = [c.strip() for c in df.columns]
        for c in columns:
            if c not in df.columns:
                df[c] = ""
        # Rows shorter than the header leave NaN behind even with na_filter off.
        return df[list(columns)].fillna("")


def parse_time(s):
    """GTFS 'HH:MM:SS' to seconds; hours past 24 stay past 86400. Blank -> None."""
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 2:
        parts.append("0")
    h, m, sec = (int(p) for p in parts)
    return h * 3600 + m * 60 + sec


def parse_times(series):
    """Vectorised parse_time: float seconds with NaN for blank or malformed cells."""
    m = series.str.strip().str.extract(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?$")
    h = pd.to_numeric(m[0])
    mi = pd.to_numeric(m[1])
    sec = pd.to_numeric(m[2]).fillna(0.0)
    return (h * 3600 + mi * 60 + sec).to_numpy(dtype=float)


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


def service_span(feed):
    """'2026-11-01 to 2026-12-19': the dates the feed has any service at all."""
    days = []
    for row in feed.table("calendar"):
        days += [row.get("start_date", ""), row.get("end_date", "")]
    days += [row.get("date", "") for row in feed.table("calendar_dates") if row.get("exception_type") == "1"]
    days = sorted(d for d in days if len(d) == 8)
    if not days:
        return "no calendar at all"
    fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return f"{fmt(days[0])} to {fmt(days[-1])}"


def grouped(keys, *order):
    """Stable sort rows by key then by `order` columns; return (perm, {key: (start, end)}).

    Ranges index into arrays permuted by `perm`, so each group is a contiguous
    slice and no per-row Python objects are created.
    """
    codes, uniq = pd.factorize(keys)
    if len(codes) == 0:
        return np.zeros(0, dtype=np.int64), {}
    perm = np.lexsort(tuple(reversed(order)) + (codes,))
    sorted_codes = codes[perm]
    cuts = np.flatnonzero(np.diff(sorted_codes)) + 1
    starts = np.concatenate([[0], cuts])
    ends = np.concatenate([cuts, [len(perm)]])
    ranges = {uniq[sorted_codes[s]]: (int(s), int(e)) for s, e in zip(starts, ends)}
    return perm, ranges


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


def fill_times(t, d):
    """Fill NaN entries by linear interpolation along shape distance.

    Leading or trailing blanks (not valid GTFS, but seen in the wild) are
    extrapolated at the speed of the nearest filled interval.
    """
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


def bbox_touches(b, clip):
    """b and clip are (west, south, east, north) in degrees."""
    return b[0] <= clip[2] and b[2] >= clip[0] and b[1] <= clip[3] and b[3] >= clip[1]


def frame_box(city):
    """The 9:16 frame in degrees, with a 1 km margin, intersected with the
    basemap clip; the clip alone when the config has no frame."""
    frame = city.get("frame") or {}
    kmv = frame.get("km_vertical")
    if not kmv:
        return city["clip"]
    cx, cy = frame.get("center_km") or [0, 0]
    lon0, lat0 = city["origin"]
    kx = 111.32 * math.cos(math.radians(lat0))
    ky = 110.574
    hw = kmv * 9 / 16 / 2 + 1
    hh = kmv / 2 + 1
    box = (lon0 + (cx - hw) / kx, lat0 + (cy - hh) / ky, lon0 + (cx + hw) / kx, lat0 + (cy + hh) / ky)
    c = city["clip"]
    return (max(box[0], c[0]), max(box[1], c[1]), min(box[2], c[2]), min(box[3], c[3]))


# ---------------------------------------------------------------- building


def expand_frequencies(feed, trips_by_id, first_time):
    """Return extra (trip_id, template_trip_id, offset_seconds) for frequencies.txt."""
    extra = []
    for row in feed.table("frequencies"):
        tid = row.get("trip_id")
        if tid not in trips_by_id or first_time.get(tid) is None:
            continue
        start, end, headway = parse_time(row.get("start_time")), parse_time(row.get("end_time")), int(row.get("headway_secs") or 0)
        if start is None or end is None or headway <= 0:
            continue
        n = 0
        while start + n * headway < end:
            extra.append((f"{tid}#{n}", tid, start + n * headway - first_time[tid]))
            n += 1
    return extra


def build_feed(feed, date, city, routes, route_index, shapes, shape_index, unknown_types, smooth=True,
               grouper=None, modelled=False):
    """Append this feed's routes/shapes/trips to the shared lists; return stats."""
    stats = {"id": feed.id, "trips_in_file": 0, "trips_on_date": 0, "skipped_short": 0, "dropped_clip": 0,
             "forced_monotone": 0, "time_fixes": 0, "shape_from_stops": 0, "blank_filled": 0,
             "spread": 0, "first": None, "last": None, "trips": [], "no_service": None,
             "dropped_type": defaultdict(int)}
    v3 = uses_v3(city)
    include = city.get("include_route_types")
    include = None if include is None else {int(x) for x in include}
    # Trips are kept when they touch the frame itself, not the wider basemap
    # clip: a DRT route in Oshawa is 12 km off the right edge and would only
    # add to the running count. Without a frame the clip box is the test.
    clip = frame_box(city)
    # Output precision in km. Three decimals (metres) is all the renderer can
    # use, but Tsukuba keeps its pre-v2 four: a rounding change shifts every
    # stroke by a fraction of a pixel and the Tsukuba frames must stay as they are.
    decimals = city.get("decimals") or {}
    xy_decimals = int(decimals.get("xy", 3))
    d_decimals = int(decimals.get("d", 3))
    type_to_mode = {rt: m["id"] for m in city["modes"] for rt in m["route_types"]}
    first_mode = city["modes"][0]["id"]

    agency = feed.table("agency")
    info = feed.table("feed_info")
    # A route whose agency_id is blank (allowed when the feed has one agency)
    # or missing from agency.txt is taken to belong to the feed's first agency.
    agency_name = {a.get("agency_id", ""): a.get("agency_name", "") for a in agency}
    first_agency = agency[0].get("agency_name", "") if agency else ""
    stats["name"] = agency[0].get("agency_name", feed.id) if agency else feed.id
    stats["version"] = info[0].get("feed_version", "") if info else ""
    stats["publisher_in_feed"] = info[0].get("feed_publisher_name", "") if info else ""

    stops, stop_xy = {}, {}
    for r in feed.table("stops"):
        try:
            lon, lat = float(r["stop_lon"]), float(r["stop_lat"])
        except (KeyError, ValueError):
            continue
        stops[r["stop_id"]] = (lon, lat, r.get("stop_name", ""))
        stop_xy[r["stop_id"]] = to_km(lon, lat)

    route_rows = {r["route_id"]: r for r in feed.table("routes")}

    def route_type(route_id):
        rt = route_rows.get(route_id, {}).get("route_type", "").strip()
        return int(rt) if rt.isdigit() else None

    active = active_services(feed, date)
    trips = feed.table("trips")
    stats["trips_in_file"] = len(trips)
    trips_by_id = {t["trip_id"]: t for t in trips}
    active_ids = {t["trip_id"] for t in trips if t.get("service_id") in active}
    if not active_ids:
        stats["no_service"] = service_span(feed)
        return stats
    if include is not None:
        # The type filter runs before anything else touches the trips: Toei's
        # train feed carries the subway and the tram, and each belongs in a
        # different video, so the other one must not even reach the counts.
        for tid in list(active_ids):
            rt = route_type(trips_by_id[tid].get("route_id", ""))
            if rt not in include:
                stats["dropped_type"]["blank" if rt is None else rt] += 1
                active_ids.discard(tid)
        if not active_ids:
            return stats

    # Only the active trips' stop rows are kept, which for TTC cuts 5 million
    # rows to the one weekday's worth before any string work happens.
    st = feed.frame("stop_times", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
    st = st[st["trip_id"].str.strip().isin(active_ids)]
    st = st.assign(trip_id=st["trip_id"].str.strip(), stop_id=st["stop_id"].str.strip())
    st = st[st["stop_id"].isin(stops.keys())]
    seq = pd.to_numeric(st["stop_sequence"].str.strip(), errors="coerce").fillna(0).to_numpy(dtype=np.int64)
    perm, st_ranges = grouped(st["trip_id"].to_numpy(), seq)
    st_stop = st["stop_id"].to_numpy()[perm]
    st_arr = parse_times(st["arrival_time"])[perm]
    st_dep = parse_times(st["departure_time"])[perm]
    del st, seq, perm

    def first_time(tid):
        rng = st_ranges.get(tid)
        if rng is None:
            return None
        a = rng[0]
        v = st_dep[a] if not np.isnan(st_dep[a]) else st_arr[a]
        return None if np.isnan(v) else int(v)

    # Frequency-based trips become ordinary trips with shifted times; the trip
    # named in frequencies.txt is only a template and is not scheduled itself.
    freq = [x for x in expand_frequencies(feed, trips_by_id, {t: first_time(t) for t in active_ids})]
    templates = {x[1] for x in freq}
    schedule = [(t["trip_id"], t["trip_id"], 0) for t in trips if t["trip_id"] in active_ids and t["trip_id"] not in templates]
    schedule += freq

    used_shapes = {trips_by_id[tmpl].get("shape_id", "") for _tid, tmpl, _off in schedule}
    sh = feed.frame("shapes", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"])
    sh = sh.assign(shape_id=sh["shape_id"].str.strip())
    sh = sh[sh["shape_id"].isin(used_shapes)]
    sh_seq = pd.to_numeric(sh["shape_pt_sequence"], errors="coerce").to_numpy(dtype=float)
    sh_lon = pd.to_numeric(sh["shape_pt_lon"], errors="coerce").to_numpy(dtype=float)
    sh_lat = pd.to_numeric(sh["shape_pt_lat"], errors="coerce").to_numpy(dtype=float)
    ok = ~(np.isnan(sh_seq) | np.isnan(sh_lon) | np.isnan(sh_lat))
    sh_seq, sh_lon, sh_lat = np.trunc(sh_seq[ok]), sh_lon[ok], sh_lat[ok]
    perm, sh_ranges = grouped(sh["shape_id"].to_numpy()[ok], sh_seq, sh_lon, sh_lat)
    sh_lon, sh_lat = sh_lon[perm], sh_lat[perm]
    del sh, sh_seq, perm
    shape_km = {}
    for sid, (a, b) in sh_ranges.items():
        lonlat = np.column_stack([sh_lon[a:b], sh_lat[a:b]])
        xy, cum = polyline_km(lonlat)
        if len(xy) >= 2:
            bbox = (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()), float(lonlat[:, 0].max()), float(lonlat[:, 1].max()))
            shape_km[sid] = (xy, cum, bbox)

    pattern_cache = {}

    for trip_id, template_id, offset in schedule:
        trip = trips_by_id[template_id]
        rng = st_ranges.get(template_id)
        if rng is None or rng[1] - rng[0] < 2:
            stats["skipped_short"] += 1
            continue
        a, b = rng
        stop_ids = tuple(st_stop[a:b])
        stops_xy = np.array([stop_xy[s] for s in stop_ids])

        shape_id = trip.get("shape_id", "")
        if shape_id in shape_km:
            key = (feed.id, "shape", shape_id)
            xy, cum, bbox = shape_km[shape_id]
        else:
            # No usable shape: the stop sequence itself becomes the polyline.
            key = (feed.id, "stops") + stop_ids
            lonlat = np.array([stops[s][:2] for s in stop_ids])
            xy, cum = polyline_km(lonlat)
            if len(xy) < 2:
                stats["skipped_short"] += 1
                continue
            bbox = (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()), float(lonlat[:, 0].max()), float(lonlat[:, 1].max()))
            stats["shape_from_stops"] += 1
        # A trip that never enters the clip box (GO's Kitchener locals, say)
        # would only cost bytes and inflate the running count.
        if not bbox_touches(bbox, clip):
            stats["dropped_clip"] += 1
            continue
        if key not in shape_index:
            shape_index[key] = len(shapes)
            shapes.append({"xy": np.round(xy.ravel(), xy_decimals).tolist(), "cum": np.round(cum, 4).tolist()})
        s_idx = shape_index[key]

        pkey = (key, stop_ids)
        if pkey not in pattern_cache:
            d, forced = project_stops(xy, cum, stops_xy)
            pattern_cache[pkey] = (d, forced)
        d_stop, forced = pattern_cache[pkey]
        stats["forced_monotone"] += forced

        arr, dep = st_arr[a:b], st_dep[a:b]
        arr = np.where(np.isnan(arr), dep, arr)
        dep = np.where(np.isnan(dep), arr, dep)
        stats["blank_filled"] += int(np.isnan(arr).sum())
        arr_f = fill_times(arr, d_stop)
        dep_f = fill_times(dep, d_stop)
        if arr_f is None or dep_f is None:
            stats["skipped_short"] += 1
            continue
        if smooth:
            arr_f, dep_f, moved = spread_same_minute(arr_f, dep_f, d_stop)
            stats["spread"] += moved

        # A stop where the vehicle waits contributes two entries at the same d.
        t_list, d_list = [], []
        for a_, b_, dd in zip(arr_f, dep_f, d_stop):
            a_, b_ = int(round(a_)) + offset, int(round(b_)) + offset
            t_list.append(a_); d_list.append(dd)
            if b_ > a_:
                t_list.append(b_); d_list.append(dd)
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
            rt = rr.get("route_type", "").strip()
            mode = type_to_mode.get(int(rt)) if rt.isdigit() else None
            if mode is None:
                unknown_types[(feed.id, rt)].append(route_id)
                mode = first_mode
            route_index[rkey] = len(routes)
            route = {"id": rkey, "short": short, "long": long, "color": color, "feed": feed.id, "mode": mode}
            if v3:
                route["agency"] = agency_name.get(rr.get("agency_id", ""), first_agency) or first_agency
                route["modelled"] = bool(modelled)
            if grouper:
                route["group"] = grouper(route["agency"])
            routes.append(route)

        stats["trips"].append({
            "r": route_index[rkey], "s": s_idx,
            "t": t_arr.tolist(), "d": np.round(np.array(d_list), d_decimals).tolist(),
            "_id": trip_id, "_stops": [stops[s][2] for s in stop_ids],
        })
        stats["trips_on_date"] += 1
        t0 = int(t_arr[0])
        stats["first"] = t0 if stats["first"] is None else min(stats["first"], t0)
        stats["last"] = t0 if stats["last"] is None else max(stats["last"], t0)

    return stats


def histogram(trips):
    """Running vehicles per minute: trip is running when t[0] <= T <= t[-1]."""
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


def validate(shapes, trips, clip):
    w, s = to_km(clip[0], clip[1])
    e, n = to_km(clip[2], clip[3])
    for i, sh in enumerate(shapes):
        xy = np.array(sh["xy"]).reshape(-1, 2)
        cum = np.array(sh["cum"])
        assert len(xy) >= 2, f"shape {i} has {len(xy)} points"
        assert len(cum) == len(xy), f"shape {i} cum/xy length mismatch"
        assert np.all(np.diff(cum) >= 0), f"shape {i} cum decreases"
        lo, hi = xy.min(axis=0), xy.max(axis=0)
        assert lo[0] <= e and hi[0] >= w and lo[1] <= n and hi[1] >= s, f"shape {i} lies outside the clip box"
    for i, tr in enumerate(trips):
        t, d = tr["t"], tr["d"]
        assert len(t) == len(d) >= 2, f"trip {i} has {len(t)} times / {len(d)} distances"
        assert all(b >= a for a, b in zip(t, t[1:])), f"trip {i} times decrease"
        assert all(b >= a for a, b in zip(d, d[1:])), f"trip {i} distances decrease"
        end = shapes[tr["s"]]["cum"][-1] + 0.01
        assert all(0 <= v <= end for v in d), f"trip {i} distance outside [0, {end}]"
        assert 0 <= tr["r"] and 0 <= tr["s"] < len(shapes), f"trip {i} bad indices"


def parse_date(s):
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    sys.exit(f"bad date {s!r}; use YYYYMMDD")


def main():
    t_start = time.time()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="tsukuba", help="city id, reads cities/<id>.json (default tsukuba)")
    ap.add_argument("--date", default=None, help="service date YYYYMMDD (default: the config's service_date)")
    ap.add_argument("--feeds", default=None, help="'default' (the config's feeds), 'all' (plus optional_feeds), or comma-separated zip base names")
    ap.add_argument("--out", default=None, help="output path (default <built_dir>/network.json, .gz when the config says gzip)")
    ap.add_argument("--gtfs-dir", default=None, help="directory of GTFS zips (default: the config's gtfs_dir)")
    ap.add_argument("--no-smooth", action="store_true", help="keep raw minute-resolution times for stops that share a minute")
    ap.add_argument("--inspect", default=None, help="print stop-by-stop times and distances for these comma-separated trip_ids")
    args = ap.parse_args()

    city = load_city(args.city)
    set_origin(city["origin"])
    date = parse_date(args.date or city["service_date"])
    gtfs_dir = args.gtfs_dir or os.path.join(ROOT, city["gtfs_dir"])
    zips = sorted(f for f in os.listdir(gtfs_dir) if f.lower().endswith(".zip"))
    all_ids = [os.path.splitext(f)[0] for f in zips]
    configured = {f["id"]: f for f in city["feeds"] + city.get("optional_feeds", [])}
    default_ids = [f["id"] for f in city["feeds"]]
    if args.feeds in (None, "default", city["id"]):
        wanted = default_ids
    elif args.feeds == "all":
        wanted = list(configured)
    else:
        wanted = [f.strip() for f in args.feeds.split(",") if f.strip()]
    missing = [f for f in wanted if f not in all_ids]
    if missing:
        sys.exit(f"no such feed zip in {gtfs_dir}: {missing}")
    # Feeds go in id order whatever the config says, so route and shape indices
    # do not move when a config entry is reordered.
    wanted = sorted(set(wanted), key=all_ids.index)

    routes, route_index, shapes, shape_index = [], {}, [], {}
    unknown_types = defaultdict(list)
    feed_stats = []
    grouper = Grouper(city.get("group_by"))
    v3 = uses_v3(city)
    for fid in wanted:
        t0 = time.time()
        feed = Feed(fid, os.path.join(gtfs_dir, fid + ".zip"))
        st = build_feed(feed, date, city, routes, route_index, shapes, shape_index, unknown_types, smooth=not args.no_smooth,
                        grouper=grouper, modelled=configured.get(fid, {}).get("modelled", False))
        feed_stats.append(st)
        print(f"  {fid}: {st['trips_on_date']} trips in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)

    trips = [t for st in feed_stats for t in st["trips"]]
    trips.sort(key=lambda t: (t["t"][0], t["r"]))
    hist = histogram(trips)
    peak_min = int(np.argmax(hist)) if len(trips) else 0
    mode_ids = [m["id"] for m in city["modes"]]
    hist_by_mode = {m: histogram([t for t in trips if routes[t["r"]]["mode"] == m]) for m in mode_ids}
    group_ids = [g["id"] for g in grouper.groups] if grouper else []
    hist_by_group = {g: histogram([t for t in trips if routes[t["r"]]["group"] == g]) for g in group_ids}

    for want in (args.inspect.split(",") if args.inspect else []):
        found = [t for t in trips if t["_id"] == want]
        if not found:
            print(f"trip {want!r} not on {date}", file=sys.stderr)
        for tr in found:
            route = routes[tr["r"]]
            print(f"\ntrip {tr['_id']}  route {route['short']} {route['long']} ({route['mode']})  shape #{tr['s']} ({shapes[tr['s']]['cum'][-1]:.2f} km)")
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
    validate(shapes, public_trips, city["clip"])

    meta = {
        "service_date": date.isoformat(),
        "title": city["title"],
        "subtitle": city.get("subtitle") or f"{date.strftime('%A')} {ordinal(date.day)} {date.strftime('%B')}",
        "origin": list(ORIGIN),
        "day_start": DAY_START,
        "day_end": DAY_END,
        "frame": city["frame"],
        "modes": city["modes"],
        "attribution": city["attribution"],
        "feeds": [
            {
                "id": st["id"],
                "name": configured.get(st["id"], {}).get("name", st["name"]),
                "publisher": configured.get(st["id"], {}).get("publisher", st["publisher_in_feed"]),
                "license": configured.get(st["id"], {}).get("license", ""),
                "version": st["version"],
                "trips_on_date": st["trips_on_date"],
                **({"modelled": bool(configured.get(st["id"], {}).get("modelled", False))} if v3 else {}),
            }
            for st in feed_stats
        ],
        "trips_total": len(public_trips),
        "peak": {"count": int(hist[peak_min]), "time": peak_min * 60},
        "hist_by_mode": {m: h.tolist() for m, h in hist_by_mode.items()},
    }
    # Pure passthroughs for the page; absent keys stay absent so older cities
    # keep their exact files.
    for key in ("color_by", "theme", "render"):
        if key in city:
            meta[key] = city[key]
    if grouper:
        meta["groups"] = grouper.groups
        meta["hist_by_group"] = {g: h.tolist() for g, h in hist_by_group.items()}
    out = {"meta": meta, "routes": routes, "shapes": shapes, "trips": public_trips, "hist": hist.tolist()}

    if args.out:
        out_path = args.out
    else:
        out_path = os.path.join(ROOT, city["built_dir"], "network.json" + (".gz" if city.get("gzip") else ""))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    raw = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(out_path, "wb") as fh:
        # mtime=0 keeps a no-change rebuild byte-identical in git.
        fh.write(gzip.compress(raw, compresslevel=6, mtime=0) if out_path.endswith(".gz") else raw)

    print(f"\n{city['title']}  service date {meta['service_date']} ({meta['subtitle']})   feeds: {', '.join(wanted)}")
    # Ward feed ids like chiyoda_kazaguruma outgrow the old 11 columns.
    fw = max([11] + [len(st["id"]) + 1 for st in feed_stats])
    print(f"{'feed':<{fw}s}{'name':<36s}{'in file':>8s}{'on date':>8s}{'first':>7s}{'last':>7s}{'peak':>6s}{'at':>7s}  notes")
    for st in feed_stats:
        fh_ = histogram(st["trips"]) if st["trips"] else np.zeros(1, dtype=int)
        pk = int(np.argmax(fh_))
        notes = []
        if st["no_service"]:
            notes.append(f"NO SERVICE on {date.isoformat()}: the feed covers {st['no_service']}")
        if st["dropped_type"]:
            by_type = ", ".join(f"{n} of type {rt}" for rt, n in sorted(st["dropped_type"].items(), key=lambda kv: str(kv[0])))
            notes.append(f"{sum(st['dropped_type'].values())} trips on date left out by include_route_types ({by_type})")
        if configured.get(st["id"], {}).get("modelled"):
            notes.append("modelled")
        if st["dropped_clip"]:
            notes.append(f"{st['dropped_clip']} trips dropped (shape outside clip box)")
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
        print(f"{st['id']:<{fw}s}{st['name'][:34]:<36s}{st['trips_in_file']:>8d}{st['trips_on_date']:>8d}"
              f"{fmt_time(st['first']):>7s}{fmt_time(st['last']):>7s}{int(fh_[pk]):>6d}{fmt_time(pk * 60):>7s}  {'; '.join(notes)}")
    firsts = [st["first"] for st in feed_stats if st["first"] is not None]
    lasts = [st["last"] for st in feed_stats if st["last"] is not None]
    print(f"{'total':<{fw}s}{'':<36s}{sum(s['trips_in_file'] for s in feed_stats):>8d}{len(public_trips):>8d}"
          f"{fmt_time(min(firsts) if firsts else None):>7s}{fmt_time(max(lasts) if lasts else None):>7s}"
          f"{meta['peak']['count']:>6d}{fmt_time(meta['peak']['time']):>7s}")
    if len(mode_ids) > 1:
        print(f"\n{'mode':<11s}{'routes':>7s}{'trips':>8s}{'peak':>6s}{'at':>7s}")
        for m in mode_ids:
            h = hist_by_mode[m]
            pk = int(np.argmax(h))
            n_routes = sum(1 for r in routes if r["mode"] == m)
            n_trips = sum(1 for t in trips if routes[t["r"]]["mode"] == m)
            print(f"{m:<11s}{n_routes:>7d}{n_trips:>8d}{int(h[pk]):>6d}{fmt_time(pk * 60):>7s}")
    if grouper:
        # Peaks per group need not coincide, so the row at the overall peak is
        # what the HUD breakdown shows at its busiest moment.
        print(f"\n{'group':<11s}{'label':<12s}{'routes':>7s}{'trips':>8s}{'peak':>6s}{'at':>7s}{'at overall peak':>17s}")
        for g in grouper.groups:
            h = hist_by_group[g["id"]]
            pk = int(np.argmax(h))
            n_routes = sum(1 for r in routes if r["group"] == g["id"])
            n_trips = sum(1 for t in trips if routes[t["r"]]["group"] == g["id"])
            print(f"{g['id']:<11s}{g['label'][:11]:<12s}{n_routes:>7d}{n_trips:>8d}{int(h[pk]):>6d}{fmt_time(pk * 60):>7s}{int(h[peak_min]):>17d}")
        if grouper.unmatched:
            names = ", ".join(f"{a or '(no agency)'} ({n})" for a, n in sorted(grouper.unmatched.items(), key=lambda kv: (-kv[1], kv[0])))
            print(f"agencies in the default group '{grouper.default}' (routes): {names}")
    if unknown_types:
        for (fid, rt), rids in sorted(unknown_types.items()):
            print(f"route_type {rt or '(blank)'} in {fid} is in no mode's list; {len(rids)} routes ({', '.join(rids[:6])}{'...' if len(rids) > 6 else ''}) fell into '{mode_ids[0]}'")
    else:
        print("every route_type is covered by the mode lists")
    dropped = sum(st["dropped_clip"] for st in feed_stats)
    print(f"routes {len(routes)}, shapes {len(shapes)}, trips {len(public_trips)} ({dropped} dropped by the clip box), "
          f"hist bins {len(hist)} (running at 04:30 {hist[DAY_START // 60]}, at 28:29 {hist[-1]})")
    rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    print(f"wrote {os.path.abspath(out_path)}  {os.path.getsize(out_path) / 1e6:.2f} MB on disk, {len(raw) / 1e6:.2f} MB of JSON, "
          f"{time.time() - t_start:.0f}s wall, peak rss {rss_gb:.1f} GB")


if __name__ == "__main__":
    main()

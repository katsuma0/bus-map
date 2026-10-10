"""Composite dates, signature classes and chunked reading for the v4 area build.

A v4 network shows an average weekday (or an average week) of one month, not
one calendar date. Per feed and day class this module picks the dates that
make the average (A3.1 to A3.3), groups the trips running on them into
timetable-signature classes (A3.4), decides which classes are drawn and how
many times (A3.5, A3.7), and reads the big GTFS tables in chunks so a large
feed never sits in memory whole (A4).

Every set or dict that decides an order is iterated sorted, and every count is
an integer until the final division, so the result does not depend on string
hashing or on the machine.
"""
import collections
import datetime as dt
import hashlib
import math
import os
import sys
import zoneinfo
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# -I leaves the script directory off sys.path; the sibling modules are ours.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import build_network as bn  # noqa: E402

WEEK_CLASSES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_CLASSES = {"day": ["wd"], "week": WEEK_CLASSES}
CAL_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SEP = "\x1f"
UTC = dt.timezone.utc
STOP_TIME_COLUMNS = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]


# ---------------------------------------------------------------- feeds


class Feed(bn.Feed):
    """bn.Feed with its small tables memoised.

    The area build asks for trips, routes and stops several times per feed;
    TTC's trips.txt alone takes seconds to parse. Callers treat the rows as
    read-only.
    """

    def __init__(self, feed_id, path):
        super().__init__(feed_id, path)
        self._tables = {}

    def table(self, name):
        if name not in self._tables:
            self._tables[name] = super().table(name)
        return self._tables[name]


def parse_ymd(s):
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


def iso(d):
    return d.isoformat()


def lower_median(values):
    """The lower of the two middle values for an even count, so medians stay integers."""
    v = sorted(values)
    return v[(len(v) - 1) // 2] if v else None


def day_class(date, kind):
    if kind == "day":
        return "wd" if date.weekday() < 5 else None
    return WEEK_CLASSES[date.weekday()]


def month_dates(month):
    y, m = (int(x) for x in month.split("-"))
    d = dt.date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- calendar


def service_dates(feed):
    """service_id -> set of dates, by the rule of bn.active_services.

    calendar rows add every date of their range whose weekday flag is "1";
    calendar_dates rows then add (1) or remove (2) single dates in file order,
    which is what active_services does one date at a time. A calendar row
    with a blank bound is clamped to the span of the feed's other dates,
    since active_services treats it as open-ended.
    """
    cal = feed.table("calendar")
    cd = feed.table("calendar_dates")
    known = [parse_ymd(r.get(k)) for r in cal for k in ("start_date", "end_date")]
    known += [parse_ymd(r.get("date")) for r in cd]
    known = [d for d in known if d is not None]
    lo_all, hi_all = (min(known), max(known)) if known else (None, None)
    out = collections.defaultdict(set)
    for row in cal:
        sid = row.get("service_id", "")
        flags = [row.get(day) == "1" for day in CAL_DAYS]
        if not any(flags):
            continue
        lo = parse_ymd(row.get("start_date")) or lo_all
        hi = parse_ymd(row.get("end_date")) or hi_all
        if lo is None or hi is None:
            continue
        d = lo
        while d <= hi:
            if flags[d.weekday()]:
                out[sid].add(d)
            d += dt.timedelta(days=1)
    for row in cd:
        d = parse_ymd(row.get("date"))
        if d is None:
            continue
        sid = row.get("service_id", "")
        if row.get("exception_type") == "1":
            out[sid].add(d)
        elif row.get("exception_type") == "2":
            out[sid].discard(d)
    return {k: v for k, v in out.items() if v}


def schedule(feed, first_time=None):
    """(run_id, template_id, offset) for every trip, as build_feed schedules them.

    A frequencies.txt template is replaced by its expanded runs. bn.expand_frequencies
    skips a template whose first time is unknown; without `first_time` every
    template is assumed to have stop times.
    """
    trips = feed.table("trips")
    trips_by_id = {t["trip_id"]: t for t in trips}
    if first_time is None:
        first_time = {t["trip_id"]: 0 for t in trips}
    freq = bn.expand_frequencies(feed, trips_by_id, first_time)
    templates = {x[1] for x in freq}
    out = [(t["trip_id"], t["trip_id"], 0) for t in trips if t["trip_id"] not in templates]
    return out + freq


def trips_per_date(feed, svc, first_time=None):
    """Counter date -> trips running that date; a frequency template counts its runs."""
    trips_by_id = {t["trip_id"]: t for t in feed.table("trips")}
    per_service = collections.Counter()
    for _run, tmpl, _off in schedule(feed, first_time):
        per_service[trips_by_id[tmpl].get("service_id", "")] += 1
    counts = collections.Counter()
    for sid in sorted(per_service):
        for d in svc.get(sid, ()):
            counts[d] += per_service[sid]
    return counts


def feed_timezone(feed, default):
    agency = feed.table("agency")
    tz = (agency[0].get("agency_timezone") or "").strip() if agency else ""
    return tz or default


def dst_change(date, tz, hours):
    """True when the UTC offset at local `date` 00:00 differs from the one `hours` later."""
    z = zoneinfo.ZoneInfo(tz)
    t0 = dt.datetime(date.year, date.month, date.day, tzinfo=z)
    t1 = (t0.astimezone(UTC) + dt.timedelta(hours=hours)).astimezone(z)
    return t0.utcoffset() != t1.utcoffset()


def _exclude(cand, counts, holidays, tz, hours, drop_pct):
    """A3.2 on one candidate list: (kept dates, excluded records)."""
    kept, excluded = [], []
    for d in cand:
        n = counts.get(d, 0)
        if iso(d) in holidays:
            excluded.append({"date": iso(d), "why": "holiday", "trips": n, "median": None, "name": holidays[iso(d)]})
        elif n == 0:
            excluded.append({"date": iso(d), "why": "no service", "trips": 0, "median": None, "name": None})
        elif dst_change(d, tz, hours):
            excluded.append({"date": iso(d), "why": "dst", "trips": n, "median": None, "name": None})
        else:
            kept.append(d)
    med = lower_median([counts[d] for d in kept])
    if med is not None:
        keep = Fraction(100) - Fraction(str(drop_pct))
        low = [d for d in kept if counts[d] * 100 < keep * med]
        for d in low:
            excluded.append({"date": iso(d), "why": "low", "trips": counts[d], "median": med, "name": None})
        kept = [d for d in kept if d not in low]
    excluded.sort(key=lambda e: e["date"])
    return kept, excluded


def select_dates(counts, timeline, tz, max_t1):
    """A3.1 to A3.3 for one feed: {class: {dates, excluded, fallback, month_used, median}}.

    `counts` is trips_per_date, `tz` the feed's timezone, `max_t1` its latest
    stop time in seconds (None counts as 0).
    """
    kind = timeline["kind"]
    holidays = dict(timeline.get("holidays") or {})
    drop_pct = timeline.get("drop_pct", 8)
    fallback_days = int(timeline.get("fallback_days", 28))
    hours = max(30, math.ceil((max_t1 or 0) / 3600))
    valid = sorted(d for d, n in counts.items() if n > 0)
    out = {}
    for k in DAY_CLASSES[kind]:
        rec = {"dates": [], "excluded": [], "fallback": False, "month_used": timeline["month"], "median": None}
        out[k] = rec
        if not valid:
            continue
        first, last = valid[0], valid[-1]
        cand = [d for d in month_dates(timeline["month"]) if day_class(d, kind) == k and first <= d <= last]
        kept, excluded = _exclude(cand, counts, holidays, tz, hours, drop_pct)
        if not kept:
            window = [first + dt.timedelta(days=i) for i in range(fallback_days)]
            cand = [d for d in window if day_class(d, kind) == k]
            kept, more = _exclude(cand, counts, holidays, tz, hours, drop_pct)
            excluded = sorted(excluded + more, key=lambda e: e["date"])
            rec["fallback"] = True
            start = kept[0] if kept else first
            rec["month_used"] = f"{start.year:04d}-{start.month:02d}"
        rec["dates"] = kept
        rec["excluded"] = excluded
        rec["median"] = lower_median([counts[d] for d in kept])
    return out


def feed_month_used(sel, month):
    """The feed's month_used: the month, or the window month of its first fallback class."""
    for k in sorted(sel, key=lambda c: (DAY_CLASSES["week"] + ["wd"]).index(c)):
        if sel[k]["fallback"] and sel[k]["dates"]:
            return sel[k]["month_used"]
    return month


# ---------------------------------------------------------------- reading


def read_frame(feed, name, columns, key, keep, chunksize):
    """The named columns of one member as strings, only rows whose stripped `key` is in `keep`.

    Same parsing options as bn.Feed.frame, but one chunk at a time, so peak
    memory is a chunk plus the kept rows. `keep=None` keeps every row.
    """
    info = feed.members.get(name)
    if info is None:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in columns})
    wanted = set(columns)
    parts = []
    with feed.zf.open(info) as fh:
        reader = pd.read_csv(fh, dtype=str, usecols=lambda c: c.strip() in wanted, keep_default_na=False,
                             na_filter=False, encoding="utf-8-sig", skipinitialspace=True, chunksize=chunksize)
        for chunk in reader:
            chunk.columns = [c.strip() for c in chunk.columns]
            for c in columns:
                if c not in chunk.columns:
                    chunk[c] = ""
            chunk = chunk[list(columns)].fillna("")
            if keep is not None:
                chunk = chunk[chunk[key].str.strip().isin(keep)]
            parts.append(chunk)
    if not parts:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in columns})
    return pd.concat(parts, ignore_index=True)


def parse_times(series):
    """bn.parse_times with a fast path for the common zero-padded HH:MM:SS.

    Cells of exactly that shape are converted with byte arithmetic; every
    other cell goes through bn.parse_times itself, so the result is the same
    array (tests compare the two on every GTA stop_times row).
    """
    s = series.to_numpy(dtype=object)
    out = np.full(len(s), np.nan)
    if len(s) == 0:
        return out
    try:
        b = s.astype("S8")
        lens = np.char.str_len(s.astype(str))
    except (UnicodeEncodeError, ValueError):
        return bn.parse_times(series)
    u = np.frombuffer(b.tobytes(), dtype=np.uint8).reshape(-1, 8).astype(np.int64)
    dig = (u >= 48) & (u <= 57)
    ok = (lens == 8) & dig[:, 0] & dig[:, 1] & (u[:, 2] == 58) & dig[:, 3] & dig[:, 4] & (u[:, 5] == 58) & dig[:, 6] & dig[:, 7]
    v = u - 48
    fast = (v[:, 0] * 10 + v[:, 1]) * 3600 + (v[:, 3] * 10 + v[:, 4]) * 60 + v[:, 6] * 10 + v[:, 7]
    out[ok] = fast[ok]
    rest = np.flatnonzero(~ok)
    if len(rest):
        out[rest] = bn.parse_times(pd.Series(s[rest], dtype=object))
    return out


@dataclass
class StopTimes:
    """The arrays build_feed builds from stop_times (lines 511 to 520), plus raw strings.

    `arr_raw`/`dep_raw` are the cells as read, for the signature; `max_t` is
    the latest parsed time over every row of the member, kept or not.
    """
    ranges: dict
    stop: np.ndarray
    arr: np.ndarray
    dep: np.ndarray
    arr_raw: np.ndarray
    dep_raw: np.ndarray
    max_t: float = 0.0
    rows_read: int = 0

    def __iter__(self):
        return iter((self.ranges, self.stop, self.arr, self.dep))


def valid_stops(feed):
    """stop_id -> (lon, lat, name) for stops with parseable coordinates, as build_feed reads them."""
    stops = {}
    for r in feed.table("stops"):
        try:
            lon, lat = float(r["stop_lon"]), float(r["stop_lat"])
        except (KeyError, ValueError):
            continue
        stops[r["stop_id"]] = (lon, lat, r.get("stop_name", ""))
    return stops


def read_stop_times(feed, trip_ids, chunksize, stops=None):
    """A4: stop_times of `trip_ids`, grouped per trip exactly as build_feed groups them."""
    if stops is None:
        stops = valid_stops(feed)
    keep = set(trip_ids)
    info = feed.members.get("stop_times")
    parts, max_t, rows = [], 0.0, 0
    if info is not None:
        wanted = set(STOP_TIME_COLUMNS)
        with feed.zf.open(info) as fh:
            reader = pd.read_csv(fh, dtype=str, usecols=lambda c: c.strip() in wanted, keep_default_na=False,
                                 na_filter=False, encoding="utf-8-sig", skipinitialspace=True, chunksize=chunksize)
            for chunk in reader:
                chunk.columns = [c.strip() for c in chunk.columns]
                for c in STOP_TIME_COLUMNS:
                    if c not in chunk.columns:
                        chunk[c] = ""
                chunk = chunk[STOP_TIME_COLUMNS].fillna("")
                rows += len(chunk)
                a, d = parse_times(chunk["arrival_time"]), parse_times(chunk["departure_time"])
                m = np.fmax(a, d)
                if len(m) and not np.all(np.isnan(m)):
                    max_t = max(max_t, float(np.nanmax(m)))
                sel = chunk["trip_id"].str.strip().isin(keep).to_numpy()
                chunk = chunk[sel].assign(_arr=a[sel], _dep=d[sel])
                parts.append(chunk)
    if parts:
        st = pd.concat(parts, ignore_index=True)
    else:
        st = pd.DataFrame({c: pd.Series(dtype=object) for c in STOP_TIME_COLUMNS + ["_arr", "_dep"]})
    # The same filters and the same stable grouping as build_feed lines 512 to 519.
    st = st.assign(trip_id=st["trip_id"].str.strip(), stop_id=st["stop_id"].str.strip())
    st = st[st["stop_id"].isin(stops.keys())]
    seq = pd.to_numeric(st["stop_sequence"].str.strip(), errors="coerce").fillna(0).to_numpy(dtype=np.int64)
    perm, ranges = bn.grouped(st["trip_id"].to_numpy(), seq)
    out = StopTimes(
        ranges=ranges,
        stop=st["stop_id"].to_numpy()[perm],
        arr=st["_arr"].to_numpy(dtype=float)[perm],
        dep=st["_dep"].to_numpy(dtype=float)[perm],
        arr_raw=st["arrival_time"].to_numpy()[perm],
        dep_raw=st["departure_time"].to_numpy()[perm],
        max_t=max_t, rows_read=rows)
    return out


def first_times(st):
    """template trip_id -> first time, as build_feed's first_time()."""
    out = {}
    for tid, (a, _b) in st.ranges.items():
        v = st.dep[a] if not np.isnan(st.dep[a]) else st.arr[a]
        out[tid] = None if np.isnan(v) else int(v)
    return out


def frequency_max_t(feed, st):
    """Latest time any expanded frequency run reaches (0 without frequencies)."""
    trips_by_id = {t["trip_id"]: t for t in feed.table("trips")}
    ft = first_times(st)
    best = 0.0
    for _run, tmpl, off in bn.expand_frequencies(feed, trips_by_id, ft):
        a, b = st.ranges[tmpl]
        last = np.fmax(st.arr[a:b], st.dep[a:b])
        if len(last) and not np.all(np.isnan(last)):
            best = max(best, float(np.nanmax(last)) + off)
    return best


# ---------------------------------------------------------------- classes


@dataclass
class TripClass:
    """One timetable-signature class (A3.4)."""
    sha: bytes
    rep: str                 # run id of the representative
    template: str            # its trips.txt trip_id (differs for frequency runs)
    offset: int
    rep_index: int           # position in the feed schedule, the last tie-break
    members: int
    runs: dict               # selected date -> members running that date
    mult: int = 0
    dates_k: dict = field(default_factory=dict)
    runs_k: dict = field(default_factory=dict)
    copies: dict = field(default_factory=dict)   # day class -> copies drawn (A3.5, A3.7)

    @property
    def hex(self):
        return self.sha.hex()


def signature(route, trip, st, template, offset, expanded):
    """The A3.4 fields joined with \\x1f; sequences joined with '|'."""
    rng = st.ranges.get(template)
    if rng is None:
        stops = arr = dep = ""
    else:
        a, b = rng
        stops = "|".join(st.stop[a:b])
        arr = "|".join(st.arr_raw[a:b])
        dep = "|".join(st.dep_raw[a:b])
    return SEP.join([
        route.get("route_short_name", ""), route.get("route_long_name", ""), route.get("route_type", ""),
        trip.get("direction_id", ""), trip.get("shape_id", ""),
        stops, arr, dep, f"@{offset}" if expanded else "",
    ])


def selected_by_class(sel):
    """date -> day class over every selected date of the feed."""
    return {d: k for k in sel for d in sel[k]["dates"]}


def trip_classes(feed, svc, sel, st):
    """A3.4: signature classes of every run whose service runs on a selected date.

    Returns the classes sorted by sha1 with `runs`, `mult`, `dates_k` and
    `runs_k` filled; `copies` is set by apply_guard.
    """
    date_class = selected_by_class(sel)
    trips_by_id = {t["trip_id"]: t for t in feed.table("trips")}
    routes = {r["route_id"]: r for r in feed.table("routes")}
    # Selected dates per service, sorted, computed once per service_id.
    svc_sel = {sid: sorted(d for d in dates if d in date_class) for sid, dates in svc.items()}
    svc_sel = {sid: ds for sid, ds in svc_sel.items() if ds}
    groups = {}
    for idx, (run, tmpl, off) in enumerate(schedule(feed, first_times(st))):
        trip = trips_by_id[tmpl]
        ds = svc_sel.get(trip.get("service_id", ""))
        if not ds:
            continue
        sig = signature(routes.get(trip.get("route_id", ""), {}), trip, st, tmpl, off, run != tmpl)
        sha = hashlib.sha1(sig.encode("utf-8")).digest()
        key = (ds[0], run, idx)
        g = groups.get(sha)
        if g is None:
            groups[sha] = g = {"best": key, "rep": (run, tmpl, off, idx), "members": 0, "runs": collections.Counter()}
        elif key < g["best"]:
            g["best"], g["rep"] = key, (run, tmpl, off, idx)
        g["members"] += 1
        for d in ds:
            g["runs"][d] += 1
    classes = []
    for sha in sorted(groups):
        g = groups[sha]
        run, tmpl, off, idx = g["rep"]
        runs = {d: g["runs"][d] for d in sorted(g["runs"])}
        c = TripClass(sha=sha, rep=run, template=tmpl, offset=off, rep_index=idx, members=g["members"], runs=runs)
        c.mult = max(runs.values())
        for k in sel:
            wk = sel[k]["dates"]
            c.dates_k[k] = sum(1 for d in wk if runs.get(d, 0) >= 1)
            c.runs_k[k] = sum(runs.get(d, 0) for d in wk)
        classes.append(c)
    return classes


def apply_guard(classes, sel, counts, guard):
    """A3.5 and A3.7: per day class, the half rule or, when it misses, the median-date rule.

    Sets `c.copies[k]` on every class and returns {class: {rule, median_date,
    median, ratio_half, drawn, ratio}} with the ratios as floats for the summary.
    """
    low, high = Fraction(str(guard.get("low", 0.90))), Fraction(str(guard.get("high", 1.03)))
    min_dates = int(guard.get("min_dates", 3))
    out = {}
    for k in sel:
        wk = sel[k]["dates"]
        n = len(wk)
        rec = {"rule": "half", "median_date": None, "median": None, "ratio_half": None, "drawn": 0, "ratio": None}
        out[k] = rec
        for c in classes:
            c.copies[k] = c.mult if n and 2 * c.dates_k[k] >= n else 0
        if not n:
            continue
        med = lower_median([counts[d] for d in wk])
        rec["median"] = med
        drawn = sum(c.copies[k] for c in classes)
        ratio = Fraction(drawn, med)
        rec["ratio_half"] = float(ratio)
        if ratio < low or ratio > high or n < min_dates:
            x = min(d for d in wk if counts[d] == med)
            rec["rule"], rec["median_date"] = "median-date", iso(x)
            for c in classes:
                c.copies[k] = c.runs.get(x, 0)
            drawn = sum(c.copies[k] for c in classes)
        rec["drawn"] = drawn
        rec["ratio"] = drawn / med
    return out

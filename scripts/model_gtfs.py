"""Write modelled GTFS for the Japanese areas, where most operators publish no timetable.

Two open national datasets stand in for the missing timetables:

* the gtfs-gis.jp weekday train counts per line section (2026 edition), which
  become `model_rail.zip` and `model_tram.zip`: every counted line touching the
  area's bbox, decomposed into service patterns by count level, timed with
  Toei's real weekday departure profile and fixed speeds by kind of line. Only
  a line's sections that touch the bbox are modelled: 東北線 runs on to
  盛岡, and trains in Utsunomiya would only inflate Tokyo's running count;
* the MLIT N07 bus routes of 2010 with weekday trips per route, which become
  `model_bus.zip`.

The real feeds of the area are copied next to them, and Toei's train feed gets
a shaped copy (its stations are otherwise joined by straight lines).

    python3 scripts/model_gtfs.py --area tokyo|kyoto|osaka|all [--no-verify]

Inputs and per-area choices live in cities/japan_model.json, line colours in
cities/line_colors.json. Output is deterministic: no randomness, sorted rows,
sorted zip entries with a fixed timestamp, so a rerun is byte-identical.
See docs/CONTRACT.md, "v3: Japan".
"""
import argparse
import csv
import datetime as dt
import hashlib
import heapq
import io
import json
import math
import re
import os
import shutil
import struct
import sys
import time
import zipfile
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
import shapely
import shapely.ops

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))

KY = 110.574                 # km per degree of latitude, as in the builders
PROFILE_MINUTES = 1800       # 30 h of one-minute bins, the builder's hist range
PROFILE_SMOOTH_MIN = 15
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
SERVICE_ID = "weekday"
CAL_START, CAL_END = "20260101", "20271231"
SOURCE_URL = "https://gtfs-gis.jp/railway_honsu/"
N07_URL = "https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N07.html"
RAIL_PUBLISHER = "Modelled from gtfs-gis.jp weekday train counts 2026 (A. Nishizawa, CC BY 4.0) by scripts/model_gtfs.py"
BUS_PUBLISHER = "Modelled from MLIT N07 bus routes 2010 (non-commercial) by scripts/model_gtfs.py"

# Speeds by kind of line (contract): stops carry no dwell, so these are
# average speeds including stops, not top speeds.
# 36 km/h for JR and private lines puts the Yamanote at about 45 trains in the
# morning peak, close to the real line; 42 left it at 35.
SPEED_KMH = {"subway": 30.0, "rail": 36.0, "guideway": 27.0, "streetcar": 13.0}
# Last modelled arrival. The Toei profile ends near 24:54 and a long pattern
# leaving then would still be running at 2 am; real last trains are in by
# about 1:15, so late departures are squeezed to fit.
LAST_ARRIVAL_S = 25 * 3600 + 15 * 60
ROUTE_TYPE = {"streetcar": 0, "subway": 1, "rail": 2, "guideway": 2}
BUS_SPEED_KMH = 13.0
BUS_MAX_KM = 60.0
BUS_DEMAND_CLASS = 4
# N07 writes 999.9 where the operator gave no frequency; read as "unknown".
N07_UNKNOWN = 999.0
# Timing points along a bus walk. The builder snaps every stop onto the shape
# with a monotone chain, so points this close keep spurs and loops (where the
# walk passes the same place twice) on the right pass.
BUS_STOP_SPACING_KM = 1.0

SUBWAY_OPERATORS = {"東京地下鉄", "東京都", "横浜市", "大阪市高速電気軌道", "京都市", "北大阪急行電鉄", "埼玉高速鉄道"}
GUIDEWAY_OPERATORS = {"東京モノレール", "多摩都市モノレール", "ゆりかもめ", "埼玉新都市交通", "舞浜リゾートライン",
                      "大阪モノレール", "神戸新交通", "横浜シーサイドライン", "山万", "千葉都市モノレール"}
GUIDEWAY_LINES = {"西武鉄道:山口線", "東京都:日暮里・舎人線", "大阪市高速電気軌道:南港ポートタウン線"}
# Tram lines are slow whichever zip they land in, so the kind does not depend
# on the area's streetcar_lines list (Tokyo leaves Arakawa to the real feed).
KNOWN_STREETCARS = {"東京都:荒川線", "東急電鉄:世田谷線", "京福電気鉄道:嵐山本線", "京福電気鉄道:北野線",
                    "阪堺電気軌道:阪堺線", "阪堺電気軌道:上町線"}

# Bus route pieces closer than this are one junction; gaps up to BUS_JOIN_KM
# between parts of one route are bridged with a straight connector. The widest
# bridged gaps in the 2010 data (0.8 km) are mountain roads and expressway
# ramps; the next ones, 1.2 km and up, are stray pieces of other variants.
BUS_NODE_TOL_KM = 0.02
BUS_JOIN_KM = 1.0

# Snapping Toei's stations onto the counts geometry.
SNAP_SLACK_KM = 0.2
SNAP_CAND_MAX = 8


# ---------------------------------------------------------------- small helpers


def rel(path):
    return os.path.join(ROOT, path)


def load_json(path):
    with open(rel(path), encoding="utf-8") as fh:
        return json.load(fh)


def kx_at(lat):
    return 111.32 * math.cos(math.radians(lat))


def to_xy(lonlat, lat0):
    return np.column_stack([lonlat[:, 0] * kx_at(lat0), lonlat[:, 1] * KY])


def from_xy(xy, lat0):
    return np.column_stack([xy[:, 0] / kx_at(lat0), xy[:, 1] / KY])


def polyline_km(lonlat):
    """Length in km; each polyline is measured at its own mean latitude."""
    if len(lonlat) < 2:
        return 0.0
    xy = to_xy(lonlat, float(lonlat[:, 1].mean()))
    return float(np.hypot(*np.diff(xy, axis=0).T).sum())


def cumulative(xy):
    seg = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.zeros(0)
    return np.concatenate([[0.0], np.cumsum(seg)])


def dedupe_points(lonlat):
    if len(lonlat) < 2:
        return lonlat
    keep = np.ones(len(lonlat), dtype=bool)
    keep[1:] = np.any(np.abs(np.diff(lonlat, axis=0)) > 1e-9, axis=1)
    return lonlat[keep]


def concat_lines(lines):
    """Join polylines end to start, dropping the repeated junction point."""
    out = [lines[0]]
    for ln in lines[1:]:
        if np.allclose(out[-1][-1], ln[0], atol=1e-9):
            ln = ln[1:]
        if len(ln):
            out.append(ln)
    return np.concatenate(out)


def parse_wkt_linestring(s):
    body = s[s.index("(") + 1:s.rindex(")")]
    return np.array([[float(v) for v in p.split()] for p in body.split(",")])


def gtfs_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_gtfs_time(s):
    s = (s or "").strip()
    if not s:
        return None
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def stable_phase(*key):
    """A fraction in [0, 1) that depends only on the key, so lines do not all
    depart together and a rerun gives the same times (Python's hash() is salted)."""
    digest = hashlib.sha256("\t".join(str(k) for k in key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def coord(v):
    return f"{v:.6f}"


def write_zip(path, members):
    """members: {name: bytes}. Sorted entries and a fixed timestamp make the
    archive a pure function of its contents."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, members[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    os.replace(tmp, path)


def zip_member(zf, base):
    for info in zf.infolist():
        b, ext = os.path.splitext(os.path.basename(info.filename))
        if b == base and ext.lower() in (".txt", ".csv"):
            return info
    return None


def zip_table(zf, base):
    info = zip_member(zf, base)
    if info is None:
        return []
    with zf.open(info) as fh:
        rows = list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def bbox_box(bbox):
    return shapely.box(*bbox)


def fmt_hm(sec):
    h, m = divmod(int(sec) // 60, 60)
    return f"{h:02d}:{m:02d}"


def running_hist(spans):
    """Vehicles running per minute, same rule as the builder: a trip runs
    while t0 <= T <= t1."""
    hist = np.zeros(PROFILE_MINUTES, dtype=np.int64)
    for t0, t1 in spans:
        lo, hi = max(math.ceil(t0 / 60), 0), min(math.floor(t1 / 60), PROFILE_MINUTES - 1)
        if hi >= lo:
            hist[lo:hi + 1] += 1
    return hist


def peak_of(hist):
    i = int(np.argmax(hist)) if len(hist) else 0
    return int(hist[i]) if len(hist) else 0, i * 60


# ---------------------------------------------------------------- colours


class Colors:
    def __init__(self, path):
        data = load_json(path)
        self.lines = {k: v.lstrip("#").lower() for k, v in data.get("lines", {}).items()}
        self.operators = {k: v.lstrip("#").lower() for k, v in data.get("operators", {}).items()}

    def lookup(self, op, line):
        """(hex without #, source) with source 'line', 'operator' or 'grey'."""
        key = f"{op}:{line}"
        if key in self.lines:
            return self.lines[key], "line"
        if op in self.operators:
            return self.operators[op], "operator"
        return "808080", "grey"


# ---------------------------------------------------------------- departure profiles


def active_services(zf, ymd):
    weekday = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][
        dt.datetime.strptime(ymd, "%Y%m%d").weekday()]
    active = set()
    for row in zip_table(zf, "calendar"):
        if row.get(weekday) == "1" and row.get("start_date", "") <= ymd <= row.get("end_date", "99999999"):
            active.add(row["service_id"])
    for row in zip_table(zf, "calendar_dates"):
        if row.get("date") == ymd:
            if row.get("exception_type") == "1":
                active.add(row["service_id"])
            elif row.get("exception_type") == "2":
                active.discard(row["service_id"])
    return active


class Profile:
    """The weekday shape of a real operator's day: per-minute first departures,
    smoothed, read as a distribution whose quantiles give modelled departures."""

    def __init__(self, zip_path, ymd):
        zf = zipfile.ZipFile(rel(zip_path))
        active = active_services(zf, ymd)
        trips = {r["trip_id"] for r in zip_table(zf, "trips") if r.get("service_id") in active}
        with zf.open(zip_member(zf, "stop_times")) as fh:
            st = pd.read_csv(fh, dtype=str, usecols=lambda c: c.strip() in ("trip_id", "departure_time", "arrival_time", "stop_sequence"),
                             keep_default_na=False, na_filter=False, encoding="utf-8-sig")
        st.columns = [c.strip() for c in st.columns]
        st = st[st["trip_id"].str.strip().isin(trips)]
        seq = pd.to_numeric(st["stop_sequence"], errors="coerce")
        first = st.assign(seq=seq).sort_values(["trip_id", "seq"], kind="mergesort").groupby("trip_id", sort=True).head(1)
        times = [parse_gtfs_time(d or a) for d, a in zip(first["departure_time"], first["arrival_time"])]
        minutes = np.array([t // 60 for t in times if t is not None], dtype=np.int64)
        minutes = minutes[(minutes >= 0) & (minutes < PROFILE_MINUTES)]
        counts = np.bincount(minutes, minlength=PROFILE_MINUTES).astype(float)
        self.raw = counts
        self.trips = len(minutes)
        self.per_min = np.convolve(counts, np.ones(PROFILE_SMOOTH_MIN) / PROFILE_SMOOTH_MIN, mode="same")
        self.cum = np.concatenate([[0.0], np.cumsum(self.per_min)])
        self.total = float(self.cum[-1])
        self.source = zip_path

    def time_at(self, q):
        """Seconds since midnight where the cumulative share reaches q in [0, 1)."""
        target = min(max(q, 0.0), 1.0 - 1e-12) * self.total
        i = int(np.searchsorted(self.cum, target, side="right")) - 1
        i = min(max(i, 0), len(self.per_min) - 1)
        while self.per_min[i] <= 0 and i + 1 < len(self.per_min):
            i += 1
        frac = (target - self.cum[i]) / self.per_min[i] if self.per_min[i] > 0 else 0.0
        return (i + min(max(frac, 0.0), 1.0)) * 60.0

    def departures(self, n, phase, duration_s=0):
        times = [self.time_at((k + phase) / n) for k in range(n)]
        latest = LAST_ARRIVAL_S - duration_s
        start = self.time_at(0.0)
        end = self.time_at(1.0 - 1e-9)
        if duration_s and end > latest > start:
            # Compress the evening tail linearly so the last train arrives on
            # time while the morning and midday shape stays as measured.
            knee = start + 0.75 * (end - start)
            if latest > knee:
                times = [t if t <= knee else knee + (t - knee) * (latest - knee) / (end - knee) for t in times]
            else:
                times = [start + (t - start) * (latest - start) / (end - start) for t in times]
        return [int(round(t)) for t in times]

    def span(self):
        nz = np.flatnonzero(self.raw)
        return int(nz[0]) * 60, int(nz[-1]) * 60


# ---------------------------------------------------------------- train counts


class Section:
    __slots__ = ("op", "opc", "line", "lnc", "code", "a", "b", "lonlat", "km", "fwd", "rev", "reoriented")


def load_sections(path):
    csv.field_size_limit(1 << 30)
    out = []
    with open(rel(path), encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            s = Section()
            s.op, s.opc = r["事業者名"], r["事業者コード"]
            s.line, s.lnc = r["路線名"], r["路線コード"]
            s.code = int(r["区間コード"])
            s.a, s.b = r["起点駅"], r["終点駅"]
            s.lonlat = dedupe_points(parse_wkt_linestring(r["geometry"]))
            s.km = polyline_km(s.lonlat)
            s.fwd = int(r["順方向運行本数2024"] or 0)
            s.rev = int(r["逆方向運行本数2024"] or 0)
            s.reoriented = False
            out.append(s)
    return out


def load_stations(path):
    pts = defaultdict(list)
    with open(rel(path), encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            g = r["geometry"]
            lon, lat = (float(v) for v in g[g.index("(") + 1:g.rindex(")")].split())
            pts[(r["事業者名"], r["路線名"], r["駅名"])].append((lon, lat))
    return {k: np.array(v) for k, v in pts.items()}


def orient(sec, stations):
    """Make the WKT run 起点駅 -> 終点駅. A loop line lists one station twice
    (Yamanote's 東京), so every point of a name is a candidate."""
    pa = stations.get((sec.op, sec.line, sec.a))
    pb = stations.get((sec.op, sec.line, sec.b))
    if pa is None or pb is None:
        return False
    lat0 = float(sec.lonlat[:, 1].mean())

    def dmin(p, cands):
        return float(np.hypot(*(to_xy(cands, lat0) - to_xy(p[None, :], lat0)).T).min())

    p0, p1 = sec.lonlat[0], sec.lonlat[-1]
    if dmin(p0, pb) + dmin(p1, pa) < dmin(p0, pa) + dmin(p1, pb):
        sec.lonlat = sec.lonlat[::-1].copy()
        sec.reoriented = True
    return True


def chain_sections(secs):
    """Sections of one line in 区間コード order; a section continues the current
    chain when it starts where that chain ends, otherwise (a branch, or a gap
    left by the bbox) it starts a new one."""
    chains = []
    for s in sorted(secs, key=lambda s: s.code):
        if chains and chains[-1][-1].b == s.a:
            chains[-1].append(s)
        else:
            chains.append([s])
    return chains


def decompose(counts):
    """{(i, j): trains}: for every level L, each maximal run of consecutive
    sections with count >= L is one train end to end. Levels between two
    distinct counts share their runs, so only the distinct counts are visited."""
    out = {}
    prev = 0
    for v in sorted({c for c in counts if c > 0}):
        mult = v - prev
        i, m = 0, len(counts)
        while i < m:
            if counts[i] >= v:
                j = i
                while j + 1 < m and counts[j + 1] >= v:
                    j += 1
                out[(i, j)] = out.get((i, j), 0) + mult
                i = j + 1
            else:
                i += 1
        prev = v
    return out


def line_kind(op, line, streetcars):
    key = f"{op}:{line}"
    if key in streetcars or key in KNOWN_STREETCARS:
        return "streetcar"
    if key in GUIDEWAY_LINES or op in GUIDEWAY_OPERATORS:
        return "guideway"
    if op in SUBWAY_OPERATORS:
        return "subway"
    return "rail"


class RailModel:
    """Patterns, trips and GTFS tables for one set of lines."""

    def __init__(self):
        self.agencies = {}
        self.routes = {}
        self.stops = {}
        self.shapes = {}
        self.trips = []        # (trip_id, route_id, shape_id, [(stop_id, t)], direction)
        self.section_use = {}  # (op, line, code, dir) -> (expected, modelled)
        self.line_stats = []

    def add_line(self, op, opc, line, lnc, secs, kind, color, profile):
        route_id = f"{opc}_{lnc}"
        self.agencies[opc] = op
        self.routes[route_id] = (opc, line, ROUTE_TYPE[kind], color)
        speed = SPEED_KMH[kind]
        n_trains = 0
        n_patterns = 0
        for ci, chain in enumerate(chain_sections(secs)):
            m = len(chain)
            closed = chain[0].a == chain[-1].b

            def node_id(k):
                return f"{route_id}_{ci}_{k % m if closed else k}"

            for k in range(m + 1):
                if closed and k == m:
                    continue
                pt = chain[k].lonlat[0] if k < m else chain[m - 1].lonlat[-1]
                name = chain[k].a if k < m else chain[m - 1].b
                self.stops[node_id(k)] = (name, float(pt[1]), float(pt[0]))
            for direction in (0, 1):
                # Travel legs: (section, from node, to node, geometry in travel
                # order, from station, to station, trains in this direction).
                if direction == 0:
                    legs = [(s, k, k + 1, s.lonlat, s.a, s.b, s.fwd) for k, s in enumerate(chain)]
                else:
                    legs = [(s, k + 1, k, s.lonlat[::-1], s.b, s.a, s.rev) for k, s in reversed(list(enumerate(chain)))]
                counts = [leg[6] for leg in legs]
                if closed and len(set(counts)) > 1:
                    # Start a loop just after its least served section, so no run
                    # above the base level is cut in two where the list wraps.
                    r = (int(np.argmin(counts)) + 1) % m
                    legs, counts = legs[r:] + legs[:r], counts[r:] + counts[:r]
                served = [0] * len(legs)
                for (i, j), n in sorted(decompose(counts).items()):
                    run = legs[i:j + 1]
                    shape_id = f"{route_id}_{ci}_{direction}_{run[0][1]}_{run[-1][2]}_{i}_{j}"
                    self.shapes[shape_id] = concat_lines([leg[3] for leg in run])
                    cum = np.concatenate([[0.0], np.cumsum([leg[0].km for leg in run])])
                    stop_ids = [node_id(run[0][1])] + [node_id(leg[2]) for leg in run]
                    phase = stable_phase(op, line, ci, i, j, run[0][4], run[-1][5], direction)
                    run_s = int(round(cum[-1] / speed * 3600))
                    for k, t0 in enumerate(profile.departures(n, phase, run_s)):
                        times = [t0 + int(round(c / speed * 3600)) for c in cum]
                        self.trips.append((f"{shape_id}_{k:03d}", route_id, shape_id, list(zip(stop_ids, times)), direction))
                    for x in range(i, j + 1):
                        served[x] += n
                    n_trains += n
                    n_patterns += 1
                for leg, c, got in zip(legs, counts, served):
                    self.section_use[(op, line, leg[0].code, direction)] = (c, got)
        self.line_stats.append({"op": op, "line": line, "kind": kind, "sections": len(secs),
                                "chains": len(chain_sections(secs)), "patterns": n_patterns, "trains": n_trains})

    def tables(self, publisher, version):
        agency = csv_bytes(["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
                           [[k, v, SOURCE_URL, "Asia/Tokyo", "ja"] for k, v in sorted(self.agencies.items())])
        routes = csv_bytes(["route_id", "agency_id", "route_short_name", "route_long_name", "route_type", "route_color", "route_text_color"],
                           [[rid, a, "", line, rt, color.lower(), "ffffff"] for rid, (a, line, rt, color) in sorted(self.routes.items())])
        stops = csv_bytes(["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"],
                          [[sid, name, coord(lat), coord(lon), "0"] for sid, (name, lat, lon) in sorted(self.stops.items())])
        self.trips.sort(key=lambda t: t[0])
        trips = csv_bytes(["route_id", "service_id", "trip_id", "direction_id", "shape_id"],
                          [[rid, SERVICE_ID, tid, direction, sid] for tid, rid, sid, _, direction in self.trips])
        st_rows = []
        for tid, _rid, _sid, stops_t, _dir in self.trips:
            for seq, (stop_id, t) in enumerate(stops_t, start=1):
                st_rows.append([tid, gtfs_time(t), gtfs_time(t), stop_id, seq])
        stop_times = csv_bytes(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], st_rows)
        return {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops, "trips.txt": trips,
                "stop_times.txt": stop_times, "shapes.txt": shapes_bytes(self.shapes),
                "calendar.txt": calendar_bytes(), "feed_info.txt": feed_info_bytes(publisher, version)}

    def spans(self):
        return [(t[3][0][1], t[3][-1][1]) for t in self.trips]


def shapes_bytes(shapes):
    rows = []
    for sid in sorted(shapes):
        for k, (lon, lat) in enumerate(shapes[sid], start=1):
            rows.append([sid, coord(lat), coord(lon), k])
    return csv_bytes(["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], rows)


def calendar_bytes():
    return csv_bytes(["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"],
                     [[SERVICE_ID, 1, 1, 1, 1, 1, 0, 0, CAL_START, CAL_END]])


def feed_info_bytes(publisher, version):
    return csv_bytes(["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date", "feed_version"],
                     [[publisher, N07_URL if publisher == BUS_PUBLISHER else SOURCE_URL, "ja", CAL_START, CAL_END, version]])


# ---------------------------------------------------------------- snapping (Toei shapes)


def snap_monotone(xy, cum, pts):
    """km along the polyline for each point, non-decreasing, minimising the
    total snap distance; returns (along, err). Toei's 大江戸線 passes 都庁前
    twice, which a plain nearest-point snap would put on the wrong pass."""
    A = xy[:-1]
    AB = xy[1:] - A
    L2 = (AB ** 2).sum(axis=1)
    safe = np.where(L2 > 0, L2, 1.0)
    Q = pts[:, None, :]
    t = np.clip(((Q - A) * AB).sum(axis=2) / safe, 0.0, 1.0)
    foot = A + t[..., None] * AB
    err = np.hypot(*(Q - foot).transpose(2, 0, 1))
    along = cum[:-1] + t * np.sqrt(L2)
    cands = []
    for j in range(len(pts)):
        e = err[j]
        ks = np.flatnonzero(e <= e.min() + SNAP_SLACK_KM)
        runs = np.split(ks, np.flatnonzero(np.diff(ks) > 1) + 1)
        best = sorted((int(run[np.argmin(e[run])]) for run in runs), key=lambda k: e[k])[:SNAP_CAND_MAX]
        cands.append([(float(along[j, k]), float(e[k])) for k in best])
    INF = float("inf")
    cost = [[c[1] + 1e-6 * c[0] for c in cands[0]]]
    back = [[-1] * len(cands[0])]
    for j in range(1, len(cands)):
        row, bp = [], []
        for a, e in cands[j]:
            bi, bc = -1, INF
            for i, (pa, _pe) in enumerate(cands[j - 1]):
                if pa <= a + 1e-9 and cost[j - 1][i] < bc:
                    bi, bc = i, cost[j - 1][i]
            row.append(bc + e + 1e-6 * a)
            bp.append(bi)
        cost.append(row)
        back.append(bp)
    i = int(np.argmin(cost[-1]))
    if cost[-1][i] == INF:
        return None, None
    al, er = np.zeros(len(cands)), np.zeros(len(cands))
    for j in range(len(cands) - 1, -1, -1):
        al[j], er[j] = cands[j][i]
        i = back[j][i]
    return al, er


def cut_polyline(xy, cum, a0, a1):
    """The stretch of the polyline between km a0 and a1, ends interpolated."""
    def at(a):
        k = int(np.clip(np.searchsorted(cum, a, side="right") - 1, 0, len(cum) - 2))
        seg = cum[k + 1] - cum[k]
        f = 0.0 if seg <= 0 else (a - cum[k]) / seg
        return xy[k] + (xy[k + 1] - xy[k]) * min(max(f, 0.0), 1.0)
    inner = xy[(cum > a0 + 1e-6) & (cum < a1 - 1e-6)]
    return np.vstack([at(a0), inner, at(a1)])


def shape_real_feed(name, spec, area_dir, sections_by_line, stations, colors):
    """A copy of a real feed with shapes from the counts geometry. Only
    trips.txt (shape_id), routes.txt (route_color) and the new shapes.txt
    differ; every other member, stop_times included, is copied byte for byte."""
    src = rel(os.path.join("data/japan-src/gtfs", name + ".zip"))
    zf = zipfile.ZipFile(src)
    op = spec["operator"]
    routes = zip_table(zf, "routes")
    stops = {r["stop_id"]: (float(r["stop_lon"]), float(r["stop_lat"]), r.get("stop_name", "")) for r in zip_table(zf, "stops")
             if r.get("stop_lat") and r.get("stop_lon")}
    line_of = {}
    for r in routes:
        for key in (r.get("route_long_name", ""), r.get("route_short_name", "")):
            if key and key in spec["route_names"]:
                line_of[r["route_id"]] = spec["route_names"][key]
                break
    with zf.open(zip_member(zf, "stop_times")) as fh:
        st = pd.read_csv(fh, dtype=str, usecols=lambda c: c.strip() in ("trip_id", "stop_id", "stop_sequence"),
                         keep_default_na=False, na_filter=False, encoding="utf-8-sig")
    st.columns = [c.strip() for c in st.columns]
    st = st.assign(seq=pd.to_numeric(st["stop_sequence"], errors="coerce")).sort_values(["trip_id", "seq"], kind="mergesort")
    seq_of = {tid: tuple(g["stop_id"].str.strip()) for tid, g in st.groupby("trip_id", sort=True)}

    # The line's geometry: every chain of its sections, both ways round.
    lat0 = float(np.mean([p[1] for p in stops.values()]))
    geoms = {}
    for line in sorted(set(line_of.values())):
        secs = sections_by_line.get((op, line), [])
        for s in secs:
            orient(s, stations)
        for ci, chain in enumerate(chain_sections(secs)):
            ll = concat_lines([s.lonlat for s in chain])
            for o, g in ((0, ll), (1, ll[::-1])):
                xy = to_xy(g, lat0)
                geoms.setdefault(line, []).append((ci, o, xy, cumulative(xy)))

    trips_rows = zip_table(zf, "trips")
    with zf.open(zip_member(zf, "trips")) as fh:
        header = next(csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")))
    header = [h.strip() for h in header]
    if "shape_id" not in header:
        header.append("shape_id")
    route_of = {t["trip_id"]: t["route_id"] for t in trips_rows}

    shapes, shape_key_to_id, pattern_shape = {}, {}, {}
    worst = {}
    unmatched = Counter()
    for tid in sorted(seq_of):
        rid = route_of.get(tid)
        seq = tuple(s for s in seq_of[tid] if s in stops)
        if rid not in line_of or len(seq) < 2:
            unmatched[rid] += 1
            continue
        pkey = (rid, seq)
        if pkey in pattern_shape:
            continue
        pts = to_xy(np.array([stops[s][:2] for s in seq]), lat0)
        best = None
        for ci, o, xy, cum in geoms.get(line_of[rid], []):
            al, er = snap_monotone(xy, cum, pts)
            if al is None or al[-1] - al[0] <= 0:
                continue
            score = float(er.sum())
            if best is None or score < best[0]:
                best = (score, ci, o, xy, cum, al, er)
        if best is None:
            unmatched[rid] += 1
            continue
        _score, ci, o, xy, cum, al, er = best
        skey = (rid, ci, o, round(al[0], 3), round(al[-1], 3))
        if skey not in shape_key_to_id:
            base = f"{rid}_{o}_{seq[0]}_{seq[-1]}"
            sid, n = base, 2
            while sid in shapes:
                sid, n = f"{base}_{n}", n + 1
            shape_key_to_id[skey] = sid
            cut = cut_polyline(xy, cum, al[0], al[-1])
            # Where the counts geometry stops short of a terminus (浅草線 ends
            # 87 m before Toei's 押上), carry the shape on to the station.
            if al[0] <= 1e-6 and er[0] > 0.01:
                cut = np.vstack([pts[:1], cut])
            if al[-1] >= cum[-1] - 1e-6 and er[-1] > 0.01:
                cut = np.vstack([cut, pts[-1:]])
            shapes[sid] = from_xy(cut, lat0)
        pattern_shape[pkey] = shape_key_to_id[skey]
        w = worst.get(rid, (0.0, ""))
        k = int(np.argmax(er))
        if er[k] > w[0]:
            worst[rid] = (float(er[k]), stops[seq[k]][2])

    rows = []
    with_shape = 0
    for t in trips_rows:
        seq = tuple(s for s in seq_of.get(t["trip_id"], ()) if s in stops)
        sid = pattern_shape.get((t["route_id"], seq), t.get("shape_id", ""))
        with_shape += bool(sid)
        t = dict(t, shape_id=sid)
        rows.append([t.get(h, "") for h in header])

    route_rows = []
    with zf.open(zip_member(zf, "routes")) as fh:
        rheader = [h.strip() for h in next(csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")))]
    if "route_color" not in rheader:
        rheader.append("route_color")
    recolor = []
    for r in routes:
        if r["route_id"] in line_of:
            color, src_kind = colors.lookup(op, line_of[r["route_id"]])
            if src_kind == "line" and color.upper() != r.get("route_color", "").upper():
                recolor.append((r.get("route_long_name") or r.get("route_short_name"), r.get("route_color", "") or "(blank)", color.upper()))
                r = dict(r, route_color=color.upper())
        route_rows.append([r.get(h, "") for h in rheader])

    members = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = os.path.splitext(os.path.basename(info.filename))[0]
        if base in ("trips", "routes", "shapes"):
            continue
        members[info.filename] = zf.read(info)
    members["trips.txt"] = csv_bytes(header, rows)
    members["routes.txt"] = csv_bytes(rheader, route_rows)
    members["shapes.txt"] = shapes_bytes(shapes)
    out = os.path.join(area_dir, spec["out"] + ".zip")
    write_zip(out, members)
    return {"out": out, "trips": len(trips_rows), "with_shape": with_shape, "shapes": len(shapes),
            "patterns": len(pattern_shape), "worst": worst, "unmatched": dict(unmatched), "recolor": recolor,
            "routes": {rid: line_of[rid] for rid in sorted(line_of)}}


# ---------------------------------------------------------------- N07 bus routes


def read_dbf(data, encoding="cp932"):
    """Minimal dBASE III reader: the N07 attribute table needs nothing more."""
    n_rec, hdr_len, rec_len = struct.unpack("<IHH", data[4:12])
    fields, pos = [], 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii")
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fields.append((name, ftype, flen))
        pos += 32
    out = []
    for i in range(n_rec):
        rec = data[hdr_len + i * rec_len: hdr_len + (i + 1) * rec_len]
        off, row = 1, {}
        for name, ftype, flen in fields:
            raw = rec[off:off + flen].decode(encoding, errors="replace").strip()
            off += flen
            if ftype in "NF":
                try:
                    row[name] = float(raw)
                except ValueError:
                    row[name] = None
            else:
                row[name] = raw
        out.append((rec[:1] == b"*", row))
    return out


def read_shp_polylines(data):
    """Minimal ESRI shapefile reader for PolyLine (3) and PolyLineM (23):
    each record becomes a list of (n, 2) lon/lat parts."""
    pos, out = 100, []
    while pos + 8 <= len(data):
        _num, clen = struct.unpack(">ii", data[pos:pos + 8])
        body = data[pos + 8: pos + 8 + clen * 2]
        pos += 8 + clen * 2
        stype = struct.unpack("<i", body[:4])[0]
        if stype == 0:
            out.append([])
            continue
        if stype not in (3, 13, 23):
            raise ValueError(f"shape type {stype} is not a polyline")
        n_parts, n_pts = struct.unpack("<ii", body[36:44])
        parts = list(struct.unpack(f"<{n_parts}i", body[44:44 + 4 * n_parts])) + [n_pts]
        pts = np.frombuffer(body, dtype="<f8", count=2 * n_pts, offset=44 + 4 * n_parts).reshape(-1, 2)
        out.append([pts[parts[k]:parts[k + 1]].copy() for k in range(n_parts)])
    return out


def load_n07(path):
    zf = zipfile.ZipFile(rel(path))
    shp = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    if not shp:
        raise SystemExit(f"{path}: no shapefile inside")
    stem = os.path.splitext(shp[0])[0]
    recs = read_dbf(zf.read(stem + ".dbf"))
    geoms = read_shp_polylines(zf.read(stem + ".shp"))
    if len(recs) != len(geoms):
        raise SystemExit(f"{path}: {len(recs)} attribute rows but {len(geoms)} shapes")
    return [(i, row, parts) for i, ((deleted, row), parts) in enumerate(zip(recs, geoms)) if not deleted]


class Graph:
    def __init__(self):
        self.edges = []   # [u, v, xy]
        self.nodes = []   # xy

    def add_node(self, p):
        self.nodes.append(np.asarray(p, dtype=float))
        return len(self.nodes) - 1

    def adjacency(self):
        adj = defaultdict(list)
        for eid, (u, v, xy) in enumerate(self.edges):
            ln = float(np.hypot(*np.diff(xy, axis=0).T).sum())
            adj[u].append((eid, v, True, ln))
            adj[v].append((eid, u, False, ln))
        return adj


def dijkstra(adj, src):
    dist, prev = {src: 0.0}, {}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, float("inf")):
            continue
        for eid, v, _fwd, ln in adj[u]:
            nd = d + ln
            if nd < dist.get(v, float("inf")) - 1e-12:
                dist[v], prev[v] = nd, (u, eid)
                heapq.heappush(heap, (nd, v))
    return dist, prev


def postman_ends(adj, odd, deg):
    """Ends and doubled pairs of an open Chinese-postman walk.

    Every odd node but the two ends must be paired up and the shortest path of
    each pair driven twice; the ends are the pair whose removal leaves the
    cheapest pairing, so a route with a terminal loop is not driven out and back
    end to end. Exact over subsets up to 12 odd nodes, greedy beyond. Ties go
    to the pair farther apart, then to dead ends. Returns (s, t, pairs, prev)."""
    dists = {n: dijkstra(adj, n) for n in odd}
    D = {(a, b): dists[a][0].get(b, float("inf")) for a in odd for b in odd}
    prev = {n: dists[n][1] for n in odd}
    n = len(odd)
    if n == 2:
        return odd[0], odd[1], [], prev
    if n <= 12:
        memo = {0: (0.0, ())}

        def match(mask):
            if mask in memo:
                return memo[mask]
            i = (mask & -mask).bit_length() - 1
            best = None
            rest = mask & ~(1 << i)
            j = rest
            while j:
                k = (j & -j).bit_length() - 1
                j &= j - 1
                c, pairs = match(rest & ~(1 << k))
                c += D[(odd[i], odd[k])]
                if best is None or c < best[0] - 1e-9:
                    best = (c, ((odd[i], odd[k]),) + pairs)
            memo[mask] = best
            return best

        full = (1 << n) - 1
        cands = []
        for i in range(n):
            for k in range(i + 1, n):
                c, pairs = match(full & ~(1 << i) & ~(1 << k))
                leaves = (deg[odd[i]] == 1) + (deg[odd[k]] == 1)
                cands.append((round(c, 6), -round(D[(odd[i], odd[k])], 6), -leaves, i, k, pairs))
        c, _d, _l, i, k, pairs = min(cands)
        return odd[i], odd[k], list(pairs), prev
    leaves = [x for x in odd if deg[x] == 1]
    pool = leaves if len(leaves) >= 2 else odd
    s, t = max(((a, b) for a in pool for b in pool if a < b), key=lambda ab: (D[ab], -ab[0], -ab[1]))
    rest = [x for x in odd if x not in (s, t)]
    used, pairs = set(), []
    for _d, a, b in sorted((D[(a, b)], a, b) for a in rest for b in rest if a < b):
        if a not in used and b not in used:
            used.update((a, b))
            pairs.append((a, b))
    return s, t, pairs, prev


def bus_walk(parts_lonlat):
    """One drivable polyline for a route given as unordered pieces.

    N07's shapefile stores a route as a multi-line: pieces split at junctions,
    with spurs into terminals, loops and the odd gap. The pieces become a
    graph (ends merged within BUS_NODE_TOL_KM, an end touching another piece's
    middle splits it there, separate parts joined where they cross or come
    within BUS_JOIN_KM, anything farther dropped), and the walk is an open
    Chinese-postman tour: it covers every piece, driving twice only what it
    must (a spur into a terminal), between the two ends that need the least
    doubling. Returns (lonlat walk, info)."""
    lat0 = float(np.mean([p[:, 1].mean() for p in parts_lonlat]))
    pieces = []
    for p in parts_lonlat:
        p = dedupe_points(p)
        if len(p) >= 2:
            pieces.append(to_xy(p, lat0))
    info = {"pieces": len(pieces), "dropped_km": 0.0, "joins": 0}
    if not pieces:
        return None, info
    geom_km = sum(float(cumulative(p)[-1]) for p in pieces)
    info["geom_km"] = geom_km
    tol = BUS_NODE_TOL_KM

    # T-junctions: an end lying on another piece's interior splits that piece.
    cuts = defaultdict(list)
    for i, p in enumerate(pieces):
        for which, e in ((0, p[0]), (1, p[-1])):
            for j, q in enumerate(pieces):
                cq = cumulative(q)
                A, AB = q[:-1], np.diff(q, axis=0)
                L2 = (AB ** 2).sum(axis=1)
                t = np.clip(((e - A) * AB).sum(axis=1) / np.where(L2 > 0, L2, 1.0), 0, 1)
                foot = A + t[:, None] * AB
                d = np.hypot(*(foot - e).T)
                along = cq[:-1] + t * np.sqrt(L2)
                ok = (d < tol) & (along > tol) & (along < cq[-1] - tol)
                if i == j:
                    own = 0.0 if which == 0 else cq[-1]
                    ok &= np.abs(along - own) > 3 * tol
                if ok.any():
                    k = int(np.flatnonzero(ok)[np.argmin(d[ok])])
                    cuts[j].append(float(along[k]))
    split = []
    for j, q in enumerate(pieces):
        if not cuts[j]:
            split.append(q)
            continue
        cq = cumulative(q)
        marks = []
        for a in sorted(cuts[j]):
            if not marks or a - marks[-1] > tol:
                marks.append(a)
        bounds = [0.0] + marks + [cq[-1]]
        for a0, a1 in zip(bounds, bounds[1:]):
            split.append(cut_polyline(q, cq, a0, a1))
    pieces = [p for p in split if cumulative(p)[-1] > 1e-6]

    # Merge ends into nodes.
    ends = [p[0] for p in pieces] + [p[-1] for p in pieces]
    parent = list(range(len(ends)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    E = np.array(ends)
    for a in range(len(ends)):
        close = np.flatnonzero(np.hypot(*(E[a + 1:] - E[a]).T) < tol) + a + 1
        for b in close:
            parent[find(int(b))] = find(a)
    g = Graph()
    node_of = {}
    for a in range(len(ends)):
        r = find(a)
        if r not in node_of:
            node_of[r] = g.add_node(E[r])
    n_p = len(pieces)
    for k, p in enumerate(pieces):
        u, v = node_of[find(k)], node_of[find(k + n_p)]
        p = p.copy()
        p[0], p[-1] = g.nodes[u], g.nodes[v]
        g.edges.append([u, v, p])

    # Components: bridge small gaps to the largest one, drop the rest.
    def components():
        par = list(range(len(g.nodes)))

        def f(a):
            while par[a] != a:
                par[a] = par[par[a]]
                a = par[a]
            return a
        for u, v, _xy in g.edges:
            par[f(u)] = f(v)
        comp = defaultdict(lambda: [set(), [], 0.0])
        for eid, (u, v, xy) in enumerate(g.edges):
            c = comp[f(u)]
            c[0].update((u, v))
            c[1].append(eid)
            c[2] += float(cumulative(xy)[-1])
        return sorted(comp.values(), key=lambda c: (-c[2], min(c[0])))

    def node_at(edge_ids, point):
        """The node at `point` on one of these edges, splitting the edge there
        unless the point is within the node tolerance of one of its ends."""
        best = min(edge_ids, key=lambda e: (shapely.distance(shapely.linestrings(g.edges[e][2]), point), e))
        u, v, xy = g.edges[best]
        line = shapely.linestrings(xy)
        along = float(shapely.line_locate_point(line, point))
        cum = cumulative(xy)
        if along <= tol:
            return u, None
        if along >= cum[-1] - tol:
            return v, None
        node = g.add_node(np.array([point.x, point.y]))
        first, second = cut_polyline(xy, cum, 0.0, along), cut_polyline(xy, cum, along, cum[-1])
        first[-1] = second[0] = g.nodes[node]
        g.edges[best] = [u, node, first]
        g.edges.append([node, v, second])
        return node, len(g.edges) - 1

    while True:
        comps = components()
        if len(comps) == 1:
            break
        main = comps[0]
        main_geom = shapely.multilinestrings([shapely.linestrings(g.edges[e][2]) for e in main[1]])
        best = None
        for other in comps[1:]:
            geom = shapely.multilinestrings([shapely.linestrings(g.edges[e][2]) for e in other[1]])
            d = float(shapely.distance(main_geom, geom))
            if best is None or d < best[0]:
                best = (d, other, geom)
        d, other, geom = best
        if d > BUS_JOIN_KM:
            for c in comps[1:]:
                info["dropped_km"] += c[2]
            keep = set(main[1])
            g.edges = [e for i, e in enumerate(g.edges) if i in keep]
            break
        # Nearest points rather than nearest ends, so a piece that crosses the
        # route (and only touches it mid-way) joins it where they cross.
        pa, pb = shapely.ops.nearest_points(geom, main_geom)
        na, _ = node_at(other[1], pa)
        nb, _ = node_at(main[1], pb)
        if d > tol:
            g.edges.append([na, nb, np.vstack([g.nodes[na], g.nodes[nb]])])
        else:
            for e in g.edges:
                if e[0] == na:
                    e[0] = nb
                    e[2][0] = g.nodes[nb]
                if e[1] == na:
                    e[1] = nb
                    e[2][-1] = g.nodes[nb]
        if d > tol:
            info["joins"] += 1
        else:
            info["crossings"] = info.get("crossings", 0) + 1
        info["join_km"] = max(info.get("join_km", 0.0), d)

    adj = g.adjacency()
    deg = {n: len(a) for n, a in adj.items()}
    odd = sorted(n for n, d in deg.items() if d % 2)
    dup = []
    if odd:
        s, t, pairs, prev = postman_ends(adj, odd, deg)
        for a_, b_ in pairs:
            # Walk the shortest path back from b to a, doubling its edges.
            node = b_
            while node != a_:
                pu, eid = prev[a_][node]
                dup.append(eid)
                node = pu
    else:
        s = t = min(min(e[0], e[1]) for e in g.edges)
    for eid in dup:
        u, v, xy = g.edges[eid]
        g.edges.append([u, v, xy])
    adj = g.adjacency()

    # Hierholzer: an Euler path from s exists now that only s and t are odd.
    for n in adj:
        adj[n].sort(key=lambda a: (a[0], a[2]))
    used = [False] * len(g.edges)
    ptr = defaultdict(int)
    stack, path = [(s, None)], []
    while stack:
        v, via = stack[-1]
        lst = adj[v]
        while ptr[v] < len(lst) and used[lst[ptr[v]][0]]:
            ptr[v] += 1
        if ptr[v] == len(lst):
            path.append(stack.pop())
        else:
            eid, w, fwd, _ln = lst[ptr[v]]
            used[eid] = True
            stack.append((w, (eid, fwd)))
    path.reverse()
    lines = []
    for _node, via in path[1:]:
        eid, fwd = via
        xy = g.edges[eid][2]
        lines.append(xy if fwd else xy[::-1])
    walk = concat_lines(lines)
    info["walk_km"] = float(cumulative(walk)[-1])
    info["closed"] = bool(s == t)
    return from_xy(walk, lat0), info


def model_bus(spec, bbox, profile):
    """GTFS tables for the N07 routes touching bbox; returns (members, stats)."""
    excluded_ops = set(spec.get("exclude_operators") or [])
    highway_name = re.compile(spec.get("highway_route_pattern") or r"(?!)")
    highway_op = re.compile(spec.get("highway_operator_pattern") or r"(?!)")
    area = bbox_box(bbox)
    feats = load_n07(spec["source"])
    stats = Counter()
    unknown = []
    keep = []
    for idx, row, parts in feats:
        cls = int(row.get("N07_001") or 0)
        op, name = row.get("N07_002", ""), row.get("N07_003", "")
        if not parts:
            stats["no_geometry"] += 1
            continue
        if cls == BUS_DEMAND_CLASS:
            stats["demand"] += 1
            continue
        if op in excluded_ops:
            stats["excluded_operator"] += 1
            continue
        # N07 files are cut at the prefecture line, so a Takamatsu to Kyoto
        # coach shows up as a short in-prefecture leg and slips past the
        # length test; at city-bus speed it would crawl along the expressway
        # for hours. Names and coach operators catch them.
        if highway_name.search(name) or highway_op.search(op):
            stats["highway_or_airport"] += 1
            continue
        if not any(shapely.intersects(shapely.linestrings(p), area) for p in parts if len(p) >= 2):
            stats["outside_bbox"] += 1
            continue
        km = sum(polyline_km(p) for p in parts)
        if km > BUS_MAX_KM:
            stats["over_60km"] += 1
            continue
        keep.append((idx, row, parts, km))

    route_ids = {}
    for idx, row, _parts, _km in sorted(keep, key=lambda f: (f[1]["N07_002"], f[1]["N07_003"], f[0])):
        key = (row["N07_002"], row["N07_003"])
        if key not in route_ids:
            route_ids[key] = f"b{len(route_ids) + 1:04d}"
    agency_ids = {op: f"op{k + 1:03d}" for k, op in enumerate(sorted({k[0] for k in route_ids}))}

    shapes, stops, trips = {}, {}, []
    walk_ratio, dropped_km, joins, crossings = [], 0.0, 0, 0
    for idx, row, parts, km in sorted(keep, key=lambda f: f[0]):
        op, name = row["N07_002"], row["N07_003"]
        rid = route_ids[(op, name)]
        walk, winfo = bus_walk(parts)
        if walk is None or len(walk) < 2:
            stats["degenerate"] += 1
            continue
        dropped_km += winfo["dropped_km"]
        joins += winfo["joins"]
        crossings += winfo.get("crossings", 0)
        walk_ratio.append(winfo["walk_km"] / max(winfo["geom_km"], 1e-9))
        lat0 = float(walk[:, 1].mean())
        cum = cumulative(to_xy(walk, lat0))
        total = float(cum[-1])
        # Timing points at walk vertices, about every BUS_STOP_SPACING_KM.
        marks = np.linspace(0.0, total, max(2, int(math.ceil(total / BUS_STOP_SPACING_KM)) + 1))
        vidx = sorted({int(np.argmin(np.abs(cum - m))) for m in marks} | {0, len(walk) - 1})
        base = f"{rid}_{idx}"
        stop_ids = []
        for k, vi in enumerate(vidx):
            sid = f"{base}_{k}"
            stops[sid] = (f"{name} {k + 1}", float(walk[vi, 1]), float(walk[vi, 0]))
            stop_ids.append(sid)
        raw = row.get("N07_004")
        if raw is None or raw >= N07_UNKNOWN:
            unknown.append((op, name, raw))
            n = 1
        else:
            n = max(1, int(math.floor(raw + 0.5)))
        per_dir = (n - n // 2, n // 2)
        for direction in (0, 1):
            if per_dir[direction] == 0:
                continue
            if direction == 0:
                shape_pts, seq, d = walk, stop_ids, [float(cum[v]) for v in vidx]
            else:
                shape_pts, seq, d = walk[::-1], stop_ids[::-1], [total - float(cum[v]) for v in reversed(vidx)]
            shape_id = f"{base}_{direction}"
            shapes[shape_id] = shape_pts
            phase = stable_phase(op, name, idx, direction)
            run_s = int(round(total / BUS_SPEED_KMH * 3600))
            for k, t0 in enumerate(profile.departures(per_dir[direction], phase, run_s)):
                times = [t0 + int(round(x / BUS_SPEED_KMH * 3600)) for x in d]
                trips.append((f"{shape_id}_{k:03d}", rid, shape_id, direction, list(zip(seq, times))))
        stats["routes_kept_features"] += 1

    trips.sort(key=lambda t: t[0])
    agency = csv_bytes(["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
                       [[aid, op, N07_URL, "Asia/Tokyo", "ja"] for op, aid in sorted(agency_ids.items(), key=lambda kv: kv[1])])
    routes = csv_bytes(["route_id", "agency_id", "route_short_name", "route_long_name", "route_type", "route_color", "route_text_color"],
                       [[rid, agency_ids[op], name if len(name) <= 6 else "", name if len(name) > 6 else "", 3, "ffffff", "000000"]
                        for (op, name), rid in sorted(route_ids.items(), key=lambda kv: kv[1])])
    stops_b = csv_bytes(["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"],
                        [[sid, nm, coord(lat), coord(lon), "0"] for sid, (nm, lat, lon) in sorted(stops.items())])
    trips_b = csv_bytes(["route_id", "service_id", "trip_id", "direction_id", "shape_id"],
                        [[rid, SERVICE_ID, tid, direction, sid] for tid, rid, sid, direction, _ in trips])
    st_rows = []
    for tid, _rid, _sid, _dir, seq in trips:
        for k, (sid, t) in enumerate(seq, start=1):
            st_rows.append([tid, gtfs_time(t), gtfs_time(t), sid, k])
    members = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops_b, "trips.txt": trips_b,
               "stop_times.txt": csv_bytes(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], st_rows),
               "shapes.txt": shapes_bytes(shapes), "calendar.txt": calendar_bytes(),
               "feed_info.txt": feed_info_bytes(BUS_PUBLISHER, "model-n07-2011")}
    hist = running_hist([(t[4][0][1], t[4][-1][1]) for t in trips])
    by_op = Counter()
    rid_op = {rid: op for (op, _n), rid in route_ids.items()}
    for t in trips:
        by_op[rid_op[t[1]]] += 1
    out = {"features_in_file": len(feats), "routes": len(route_ids), "features": stats["routes_kept_features"],
           "trips": len(trips), "operators": len(agency_ids), "peak": peak_of(hist), "drops": dict(stats),
           "unknown": unknown, "walk_ratio": walk_ratio, "dropped_km": dropped_km, "joins": joins, "crossings": crossings,
           "by_op": by_op}
    return members, out


# ---------------------------------------------------------------- verification


def read_feed(path):
    zf = zipfile.ZipFile(path)
    tables = {b: zip_table(zf, b) for b in ("agency", "routes", "trips", "stops", "shapes", "stop_times")}
    return tables


def verify_model(path, sections_by_key, label):
    """Checks on the written zip itself, independent of the model's own bookkeeping:
    (1) per line section and direction, trips crossing it equal its count;
    (2) every shape starts at its trip's first stop and ends at its last (50 m);
    (4) trip ids are unique."""
    t = read_feed(path)
    agency = {a["agency_id"]: a["agency_name"] for a in t["agency"]}
    route_line = {r["route_id"]: (agency[r["agency_id"]], r["route_long_name"]) for r in t["routes"]}
    stops = {s["stop_id"]: (s["stop_name"], float(s["stop_lon"]), float(s["stop_lat"])) for s in t["stops"]}
    shapes = defaultdict(list)
    for p in t["shapes"]:
        shapes[p["shape_id"]].append((int(p["shape_pt_sequence"]), float(p["shape_pt_lon"]), float(p["shape_pt_lat"])))
    shapes = {k: np.array([q[1:] for q in sorted(v)]) for k, v in shapes.items()}
    seqs = defaultdict(list)
    for r in t["stop_times"]:
        seqs[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"]))
    trip_ids = [r["trip_id"] for r in t["trips"]]
    dup_ids = len(trip_ids) - len(set(trip_ids))
    worst_end = 0.0
    counted = Counter()
    unmapped = 0
    for tr in t["trips"]:
        seq = [s for _k, s in sorted(seqs[tr["trip_id"]])]
        sh = shapes[tr["shape_id"]]
        lat0 = float(sh[:, 1].mean())
        for stop, pt in ((seq[0], sh[0]), (seq[-1], sh[-1])):
            p = np.array([stops[stop][1:]])
            worst_end = max(worst_end, float(np.hypot(*(to_xy(p, lat0) - to_xy(pt[None, :], lat0)).ravel())))
        if sections_by_key is None:
            continue
        op, line = route_line[tr["route_id"]]
        for a, b in zip(seq, seq[1:]):
            na, nb = stops[a][0], stops[b][0]
            if (op, line, na, nb) in sections_by_key:
                counted[(op, line, sections_by_key[(op, line, na, nb)], 0)] += 1
            elif (op, line, nb, na) in sections_by_key:
                counted[(op, line, sections_by_key[(op, line, nb, na)], 1)] += 1
            else:
                unmapped += 1
    return {"label": label, "trips": len(trip_ids), "dup_ids": dup_ids, "worst_end_m": worst_end * 1000,
            "counted": counted, "unmapped": unmapped}


def verify_shaped(path):
    """(3) every trip of the shaped feed has a shape and every stop lies within
    150 m of it (plain nearest distance); also (2) for its shape ends."""
    t = read_feed(path)
    stops = {s["stop_id"]: (float(s["stop_lon"]), float(s["stop_lat"])) for s in t["stops"]}
    shapes = defaultdict(list)
    for p in t["shapes"]:
        shapes[p["shape_id"]].append((int(p["shape_pt_sequence"]), float(p["shape_pt_lon"]), float(p["shape_pt_lat"])))
    shapes = {k: np.array([q[1:] for q in sorted(v)]) for k, v in shapes.items()}
    seqs, order = defaultdict(set), defaultdict(list)
    for r in t["stop_times"]:
        seqs[r["trip_id"]].add(r["stop_id"])
        order[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"]))
    missing = sum(1 for tr in t["trips"] if tr.get("shape_id", "") not in shapes)
    worst, worst_end = 0.0, 0.0
    for tr in t["trips"]:
        sh = shapes.get(tr.get("shape_id", ""))
        if sh is None or not order[tr["trip_id"]]:
            continue
        seq = [sid for _k, sid in sorted(order[tr["trip_id"]])]
        lat0 = float(sh[:, 1].mean())
        for stop, pt in ((seq[0], sh[0]), (seq[-1], sh[-1])):
            p = np.array([stops[stop]])
            worst_end = max(worst_end, float(np.hypot(*(to_xy(p, lat0) - to_xy(pt[None, :], lat0)).ravel())))
    pairs = defaultdict(set)
    for tr in t["trips"]:
        if tr.get("shape_id") in shapes:
            pairs[tr["shape_id"]].update(seqs[tr["trip_id"]])
    for sid, ss in pairs.items():
        sh = shapes[sid]
        lat0 = float(sh[:, 1].mean())
        geom = shapely.linestrings(to_xy(sh, lat0))
        pts = shapely.points(to_xy(np.array([stops[s] for s in sorted(ss)]), lat0))
        worst = max(worst, float(shapely.distance(geom, pts).max()))
    ids = [tr["trip_id"] for tr in t["trips"]]
    return {"trips": len(ids), "missing_shape": missing, "worst_stop_m": worst * 1000, "worst_end_m": worst_end * 1000,
            "dup_ids": len(ids) - len(set(ids))}


# ---------------------------------------------------------------- driver


def run_area(name, area, cfg, sections, stations, colors, profiles, verify):
    t_start = time.time()
    out_dir = rel(area["gtfs_dir"])
    os.makedirs(out_dir, exist_ok=True)
    bbox = area["bbox"]
    box = bbox_box(bbox)
    written = []

    for feed in area.get("real_feeds", []):
        dst = os.path.join(out_dir, feed + ".zip")
        shutil.copyfile(rel(os.path.join("data/japan-src/gtfs", feed + ".zip")), dst)
        written.append(dst)

    by_line = defaultdict(list)
    for s in sections:
        by_line[(s.op, s.line)].append(s)

    shaped = {}
    for feed, spec in sorted(area.get("shape_real_feeds", {}).items()):
        res = shape_real_feed(feed, spec, out_dir, by_line, stations, colors)
        shaped[feed] = res
        written.append(res["out"])

    rail_cfg = area.get("rail") or {}
    exclude = set(rail_cfg.get("exclude_operators") or [])
    streetcars = set(rail_cfg.get("streetcar_lines") or [])
    rail, tram = RailModel(), RailModel()
    used_lines, uncoloured, grey, skipped_ops = [], [], [], Counter()
    sections_by_key = {}
    touching = {}
    for s in sections:
        if shapely.intersects(shapely.linestrings(s.lonlat), box):
            touching.setdefault((s.op, s.line), []).append(s)
    ambiguous = []
    for (op, line) in sorted(touching, key=lambda k: (int(touching[k][0].opc), int(touching[k][0].lnc), k)):
        secs = touching[(op, line)]
        key = f"{op}:{line}"
        color, src = colors.lookup(op, line)
        if src != "line":
            uncoloured.append(key)
        if src == "grey":
            grey.append(key)
        if op in exclude:
            skipped_ops[op] += 1
            continue
        for s in secs:
            orient(s, stations)
            if (op, line, s.a, s.b) in sections_by_key or (op, line, s.b, s.a) in sections_by_key:
                ambiguous.append((op, line, s.a, s.b))
            sections_by_key[(op, line, s.a, s.b)] = s.code
        kind = line_kind(op, line, streetcars)
        model = tram if key in streetcars else rail
        model.add_line(op, secs[0].opc, line, secs[0].lnc, secs, kind, color, profiles["rail"])
        used_lines.append((op, line, len(secs), len(by_line[(op, line)])))

    files = {}
    for label, model in (("model_rail", rail), ("model_tram", tram)):
        if model.trips:
            p = os.path.join(out_dir, label + ".zip")
            write_zip(p, model.tables(RAIL_PUBLISHER, "model-honsu-2026"))
            files[label] = p
            written.append(p)
    bus_stats = None
    if area.get("bus"):
        members, bus_stats = model_bus(area["bus"], bbox, profiles["bus"])
        p = os.path.join(out_dir, "model_bus.zip")
        write_zip(p, members)
        files["model_bus"] = p
        written.append(p)

    # ------------------------------------------------ summary
    print(f"\n=== {name}  bbox {bbox}  -> {area['gtfs_dir']}")
    n_used = sum(u[2] for u in used_lines)
    n_all = sum(u[3] for u in used_lines)
    print(f"counted lines touching the bbox: {len(touching)}; modelled {len(used_lines)} lines, "
          f"{n_used} of their {n_all} sections (the rest lie outside the bbox)"
          + (f"; excluded operators {dict(skipped_ops)}" if skipped_ops else ""))
    for label, model in (("rail", rail), ("streetcar", tram)):
        if not model.trips:
            continue
        hist = running_hist(model.spans())
        pk, at = peak_of(hist)
        by_op = Counter()
        for st in model.line_stats:
            by_op[st["op"]] += st["trains"]
        kinds = Counter()
        for st in model.line_stats:
            kinds[st["kind"]] += st["trains"]
        print(f"{label}: {len(model.line_stats)} lines, {sum(s['patterns'] for s in model.line_stats)} patterns, "
              f"{len(model.trips)} trains per weekday, peak {pk} running at {fmt_hm(at)}; by kind {dict(sorted(kinds.items()))}")
        if label == "streetcar":
            for st in model.line_stats:
                print(f"    {st['op']}:{st['line']}  {st['sections']} sections, {st['patterns']} patterns, {st['trains']} trains")
        else:
            print("    trains per weekday by operator: " + ", ".join(f"{op} {n}" for op, n in sorted(by_op.items(), key=lambda kv: -kv[1])))
    for feed, res in shaped.items():
        w = ", ".join(f"{res['routes'][rid]} {km * 1000:.0f} m ({nm})" for rid, (km, nm) in sorted(res["worst"].items()))
        print(f"{feed} -> {os.path.basename(res['out'])}: {res['with_shape']} of {res['trips']} trips shaped, {res['patterns']} stop patterns, "
              f"{res['shapes']} shapes; worst monotone snap per line: {w}")
        if res["unmatched"]:
            print(f"    trips left without a shape by route: {res['unmatched']}")
        for nm, old, new in res["recolor"]:
            print(f"    route_color {nm}: {old} -> {new} (cities/line_colors.json)")
    if bus_stats:
        b = bus_stats
        pk, at = b["peak"]
        wr = np.array(b["walk_ratio"]) if b["walk_ratio"] else np.ones(1)
        print(f"bus: {b['features_in_file']} N07 routes in file; kept {b['routes']} routes ({b['features']} features) of {b['operators']} operators, "
              f"{b['trips']} trips per weekday, peak {pk} running at {fmt_hm(at)}")
        print(f"    dropped: {b['drops'].get('demand', 0)} demand (class 4), {b['drops'].get('over_60km', 0)} over {BUS_MAX_KM:.0f} km, "
              f"{b['drops'].get('outside_bbox', 0)} outside the bbox, {b['drops'].get('excluded_operator', 0)} excluded operator, "
              f"{b['drops'].get('degenerate', 0)} degenerate")
        print(f"    unknown frequency (999.9) read as 1 trip: {len(b['unknown'])}" + (f" ({', '.join(f'{o} {n}' for o, n, _ in b['unknown'][:4])}{'...' if len(b['unknown']) > 4 else ''})" if b["unknown"] else ""))
        print(f"    walk length / geometry length: median {np.median(wr):.2f}, 90th pct {np.percentile(wr, 90):.2f}, max {wr.max():.2f}; "
              f"{b['crossings']} separate parts joined where they cross, {b['joins']} gaps up to {BUS_JOIN_KM:.0f} km bridged, "
              f"{b['dropped_km']:.1f} km of parts farther away dropped")
        print("    trips by operator (top 8): " + ", ".join(f"{op} {n}" for op, n in b["by_op"].most_common(8)))
    if ambiguous:
        print(f"sections sharing their end stations (verification may mis-map them): {ambiguous}")
    print(f"counted lines in the bbox without their own colour in line_colors.json: {uncoloured if uncoloured else 'none'}"
          + (f"; grey (no operator colour either): {grey}" if grey else ""))
    for p in written:
        print(f"    {os.path.relpath(p, ROOT):<40s} {os.path.getsize(p) / 1e6:8.2f} MB")

    if verify:
        for label in ("model_rail", "model_tram", "model_bus"):
            if label not in files:
                continue
            res = verify_model(files[label], sections_by_key if label != "model_bus" else None, label)
            line = f"verify {label}: {res['trips']} trips, duplicate ids {res['dup_ids']}, worst shape end vs stop {res['worst_end_m']:.1f} m"
            if label != "model_bus":
                model = rail if label == "model_rail" else tram
                bad = []
                checked = 0
                for key, (exp, _got) in model.section_use.items():
                    checked += 1
                    if res["counted"].get(key, 0) != exp:
                        bad.append((key, exp, res["counted"].get(key, 0)))
                line += f", sections x directions checked {checked}, count mismatches {len(bad)}, unmapped stop pairs {res['unmapped']}"
                yam = [(k, e) for k, (e, _g) in model.section_use.items() if k[1] == "山手線"]
                if yam:
                    ok = all(res["counted"].get(k, 0) == e for k, e in yam)
                    line += f"; 山手線 {'matches' if ok else 'DIFFERS'} on {len(yam)} section-directions"
                for b_ in bad[:5]:
                    line += f"\n    mismatch {b_}"
            print(line)
        for feed, res in shaped.items():
            v = verify_shaped(res["out"])
            print(f"verify {os.path.basename(res['out'])}: {v['trips']} trips, without shape {v['missing_shape']}, "
                  f"worst stop distance to its shape {v['worst_stop_m']:.1f} m, worst shape end vs stop {v['worst_end_m']:.1f} m, "
                  f"duplicate ids {v['dup_ids']}")
    print(f"{name} done in {time.time() - t_start:.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", default="all", help="tokyo, kyoto, osaka or all")
    ap.add_argument("--config", default="cities/japan_model.json")
    ap.add_argument("--no-verify", action="store_true", help="skip re-reading the written zips for the sanity checks")
    args = ap.parse_args()
    cfg = load_json(args.config)
    areas = list(cfg["areas"]) if args.area == "all" else [a.strip() for a in args.area.split(",")]
    unknown = [a for a in areas if a not in cfg["areas"]]
    if unknown:
        sys.exit(f"unknown area {unknown}; have {list(cfg['areas'])}")
    t0 = time.time()
    colors = Colors(cfg["line_colors"])
    stations = load_stations(cfg["rail_stations"])
    sections = load_sections(cfg["rail_counts"])
    prof = cfg["profiles"]
    profiles = {"rail": Profile(prof["rail"], prof["profile_date"])}
    if any(cfg["areas"][a].get("bus") for a in areas):
        profiles["bus"] = Profile(prof["bus"], prof["profile_date"])
    for k, p in profiles.items():
        a, b = p.span()
        print(f"{k} profile: {p.trips} first departures on {prof['profile_date']} in {p.source}, {fmt_hm(a)} to {fmt_hm(b)}")
    print(f"counts: {len(sections)} sections in {cfg['rail_counts']}")
    for a in areas:
        run_area(a, cfg["areas"][a], cfg, sections, stations, colors, profiles, not args.no_verify)
    print(f"\nall done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

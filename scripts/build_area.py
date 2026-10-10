"""The v4 area build: every feed of one area, one composite month, one timeline.

    python3 -I scripts/build_area.py --config build/areas/gta/area.day.json --out build/areas/gta/day [--key HEX]

The config is the area config of spec 2.7 (written by make.py). For each feed
the build picks the composite dates per day class, groups the trips running
on them into timetable-signature classes, decides which classes are drawn and
how many times, and builds one representative trip per class with exactly
the steps build_network.build_feed uses for one date. The result is the
columnar store of area_store.py, which every city trim of the area reads.

Feed statistics are computed before the area_box filter, and trips are
ordered by string keys, so neither depends on which cities use the area.
A summary per feed goes to stderr.
"""
import argparse
import datetime as dt
import gc
import json
import os
import resource
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
# -I leaves the script directory off sys.path; the sibling modules are ours.
sys.path.insert(0, HERE)
import area_store  # noqa: E402
import build_network as bn  # noqa: E402
import composite  # noqa: E402

DEFAULT_CHUNK = 2_000_000


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def repo_path(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


@dataclass
class BuiltTrip:
    cls: composite.TripClass
    feed: str
    route_key: str
    shape_key: str
    t: np.ndarray       # int64 seconds
    d: np.ndarray       # float64 km along the shape
    feed_index: int = 0


def build_trips_v4(feed, classes, stop_times, area_box, modes, chunksize=DEFAULT_CHUNK, smooth=True):
    """A5: the representative trip of every class, built as build_feed builds one date's trips.

    Returns (trips, shapes, routes, stats): BuiltTrip per class whose
    representative survives, shapes as key -> (xy km, cum km, bbox degrees),
    routes as "<feed>:<route_id>" -> record with the legacy fields plus
    color_raw, agency and type.
    """
    stats = {"skipped_short": 0, "dropped_box": 0, "forced_monotone": 0, "time_fixes": 0,
             "shape_from_stops": 0, "blank_filled": 0, "spread": 0, "unknown_types": defaultdict(list)}
    type_to_mode = {rt: m["id"] for m in modes for rt in m["route_types"]}
    first_mode = modes[0]["id"]

    agency = feed.table("agency")
    agency_name = {a.get("agency_id", ""): a.get("agency_name", "") for a in agency}
    first_agency = agency[0].get("agency_name", "") if agency else ""
    stops = composite.valid_stops(feed)
    stop_xy = {sid: bn.to_km(lon, lat) for sid, (lon, lat, _name) in stops.items()}
    route_rows = {r["route_id"]: r for r in feed.table("routes")}
    trips_by_id = {t["trip_id"]: t for t in feed.table("trips")}
    st_ranges, st_stop, st_arr, st_dep = stop_times

    # Shapes: only those of representatives, read in chunks, then exactly
    # build_feed's parsing (lines 538 to 555).
    used_shapes = {trips_by_id[c.template].get("shape_id", "") for c in classes}
    sh = composite.read_frame(feed, "shapes", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
                              "shape_id", used_shapes, chunksize)
    sh = sh.assign(shape_id=sh["shape_id"].str.strip())
    sh = sh[sh["shape_id"].isin(used_shapes)]
    sh_seq = pd.to_numeric(sh["shape_pt_sequence"], errors="coerce").to_numpy(dtype=float)
    sh_lon = pd.to_numeric(sh["shape_pt_lon"], errors="coerce").to_numpy(dtype=float)
    sh_lat = pd.to_numeric(sh["shape_pt_lat"], errors="coerce").to_numpy(dtype=float)
    ok = ~(np.isnan(sh_seq) | np.isnan(sh_lon) | np.isnan(sh_lat))
    sh_seq, sh_lon, sh_lat = np.trunc(sh_seq[ok]), sh_lon[ok], sh_lat[ok]
    perm, sh_ranges = bn.grouped(sh["shape_id"].to_numpy()[ok], sh_seq, sh_lon, sh_lat)
    sh_lon, sh_lat = sh_lon[perm], sh_lat[perm]
    del sh, sh_seq, perm
    shape_km = {}
    for sid, (a, b) in sh_ranges.items():
        lonlat = np.column_stack([sh_lon[a:b], sh_lat[a:b]])
        xy, cum = bn.polyline_km(lonlat)
        if len(xy) >= 2:
            bbox = (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()), float(lonlat[:, 0].max()), float(lonlat[:, 1].max()))
            shape_km[sid] = (xy, cum, bbox)

    shapes, routes, out = {}, {}, []
    pattern_cache = {}
    for c in classes:
        trip = trips_by_id[c.template]
        offset = c.offset
        rng = st_ranges.get(c.template)
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
            xy, cum = bn.polyline_km(lonlat)
            if len(xy) < 2:
                stats["skipped_short"] += 1
                continue
            bbox = (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()), float(lonlat[:, 0].max()), float(lonlat[:, 1].max()))
            stats["shape_from_stops"] += 1
        if not bn.bbox_touches(bbox, area_box):
            stats["dropped_box"] += 1
            continue
        skey = "\x1f".join(key)
        if skey not in shapes:
            shapes[skey] = (xy, cum)

        pkey = (key, stop_ids)
        if pkey not in pattern_cache:
            pattern_cache[pkey] = bn.project_stops(xy, cum, stops_xy)
        d_stop, forced = pattern_cache[pkey]
        stats["forced_monotone"] += forced

        arr, dep = st_arr[a:b], st_dep[a:b]
        arr = np.where(np.isnan(arr), dep, arr)
        dep = np.where(np.isnan(dep), arr, dep)
        stats["blank_filled"] += int(np.isnan(arr).sum())
        arr_f = bn.fill_times(arr, d_stop)
        dep_f = bn.fill_times(dep, d_stop)
        if arr_f is None or dep_f is None:
            stats["skipped_short"] += 1
            continue
        if smooth:
            arr_f, dep_f, moved = bn.spread_same_minute(arr_f, dep_f, d_stop)
            stats["spread"] += moved

        # A stop where the vehicle waits contributes two entries at the same d.
        t_list, d_list = [], []
        for a_, b_, dd in zip(arr_f, dep_f, d_stop):
            a_, b_ = int(round(a_)) + offset, int(round(b_)) + offset
            t_list.append(a_)
            d_list.append(dd)
            if b_ > a_:
                t_list.append(b_)
                d_list.append(dd)
        t_arr = np.array(t_list, dtype=np.int64)
        fixed = np.maximum.accumulate(t_arr)
        stats["time_fixes"] += int((fixed != t_arr).sum())
        t_arr = fixed

        route_id = trip.get("route_id", "")
        rkey = f"{feed.id}:{route_id}"
        if rkey not in routes:
            rr = route_rows.get(route_id, {})
            short, long = bn.split_route_name(rr.get("route_short_name", ""), rr.get("route_long_name", "") or route_id)
            # build_feed writes 2f6bff for a blank colour; color_raw keeps the blank.
            color = (rr.get("route_color") or "2f6bff").lower()
            rt = rr.get("route_type", "").strip()
            mode = type_to_mode.get(int(rt)) if rt.isdigit() else None
            if mode is None:
                stats["unknown_types"][rt].append(route_id)
                mode = first_mode
            routes[rkey] = {"id": rkey, "short": short, "long": long, "color": color, "feed": feed.id, "mode": mode,
                            "agency": agency_name.get(rr.get("agency_id", ""), first_agency) or first_agency,
                            "type": int(rt) if rt.isdigit() else -1,
                            "color_raw": (rr.get("route_color") or "").strip()}
        out.append(BuiltTrip(cls=c, feed=feed.id, route_key=rkey, shape_key=skey, t=t_arr, d=np.array(d_list, dtype=float)))
    stats["unknown_types"] = {k: sorted(v) for k, v in sorted(stats["unknown_types"].items())}
    return out, shapes, routes, stats


def sha256_check(path, want):
    got = area_store.sha256_file(path)
    if want and got != want:
        sys.exit(f"{path}: sha256 {got} does not match the config's {want}")
    return got


def candidate_trips(feed, svc, timeline, counts):
    """trip_ids whose service runs on a date select_dates could pick: the month or the fallback window."""
    valid = sorted(d for d, n in counts.items() if n > 0)
    if not valid:
        return set()
    window = set(composite.month_dates(timeline["month"]))
    window |= {valid[0] + dt.timedelta(days=i) for i in range(int(timeline.get("fallback_days", 28)))}
    services = {sid for sid, dates in svc.items() if dates & window}
    return {t["trip_id"] for t in feed.table("trips") if t.get("service_id", "") in services}


def feed_summary(fid, rec, kinds):
    parts = []
    for k in kinds:
        r = rec["by_class"][k]
        ds = r["dates"]
        dates = f"{len(ds)} dates {ds[0][5:]}..{ds[-1][5:]}" if ds else "no dates"
        rule = r["rule"] + (f" {r['median_date'][5:]}" if r["median_date"] else "")
        ratio = f"{r['ratio']:.3f}" if r["ratio"] is not None else "-"
        fb = f" fallback {r['month_used']}" if r["fallback"] else ""
        parts.append(f"{k}: {dates}{fb}, {rule}, drawn {r['drawn']} / median {r['median']} = {ratio}, mean {r['mean_trips_per_day']}")
    excl = ", ".join(f"{e['date'][5:]} {e['why']}" for e in rec["excluded"]) or "none"
    log(f"  {fid}: {rec['classes']} classes, stored {rec['stored']}; excluded {excl}")
    for p in parts:
        log(f"      {p}")


def main():
    t_start = time.time()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="area config (spec 2.7), schema 4")
    ap.add_argument("--out", required=True, help="store directory, e.g. build/areas/gta/day")
    ap.add_argument("--key", default=None, help="step key from make.py, written to stamp.json")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if cfg.get("schema") != 4 or cfg.get("kind") != "area":
        sys.exit(f"{args.config}: not a schema 4 area config")
    bn.set_origin(cfg["origin"])
    timeline = cfg["timeline"]
    kind = timeline["kind"]
    kinds = composite.DAY_CLASSES[kind]
    area_box = tuple(float(v) for v in cfg["area_box"])
    chunk = int(cfg.get("stop_times_chunk", DEFAULT_CHUNK))
    gtfs_dir = repo_path(cfg["gtfs_dir"])
    modes = cfg["modes"]

    all_trips, all_shapes, all_routes = [], {}, {}
    feed_recs, timeline_feeds, n_dates = [], {}, {}
    log(f"area {cfg['id']} {kind} {timeline['month']}: {len(cfg['feeds'])} feeds, box {list(area_box)}")
    for fi, fcfg in enumerate(cfg["feeds"]):
        t0 = time.time()
        fid = fcfg["id"]
        path = os.path.join(gtfs_dir, f"{fid}.zip")
        sha = sha256_check(path, fcfg.get("sha256"))
        feed = composite.Feed(fid, path)
        svc = composite.service_dates(feed)
        counts0 = composite.trips_per_date(feed, svc)
        stops = composite.valid_stops(feed)
        st = composite.read_stop_times(feed, candidate_trips(feed, svc, timeline, counts0), chunk, stops)
        max_t1 = max(st.max_t, composite.frequency_max_t(feed, st))
        counts = composite.trips_per_date(feed, svc, composite.first_times(st))
        tz = composite.feed_timezone(feed, timeline["timezone"])
        sel = composite.select_dates(counts, timeline, tz, max_t1)
        classes = composite.trip_classes(feed, svc, sel, st)
        guard = composite.apply_guard(classes, sel, counts, timeline.get("guard", {}))
        built, shapes, routes, bstats = build_trips_v4(feed, classes, st, area_box, modes, chunk)
        del st
        gc.collect()

        info = feed.table("feed_info")
        by_class = {}
        for k in kinds:
            ds = sel[k]["dates"]
            g = guard[k]
            by_class[k] = {
                "dates": [composite.iso(d) for d in ds],
                "rule": g["rule"], "median_date": g["median_date"], "median": g["median"],
                "drawn": g["drawn"], "ratio": None if g["ratio"] is None else round(g["ratio"], 6),
                "ratio_half": None if g["ratio_half"] is None else round(g["ratio_half"], 6),
                "mean_trips_per_day": round(sum(counts[d] for d in ds) / len(ds), 2) if ds else 0.0,
                "fallback": sel[k]["fallback"], "month_used": sel[k]["month_used"],
                "in_month": sum(1 for d in ds if composite.iso(d)[:7] == timeline["month"] and not sel[k]["fallback"]),
            }
            n_dates.setdefault(fid, {})[k] = len(ds)
        excluded = sorted((e for k in kinds for e in sel[k]["excluded"]), key=lambda e: e["date"])
        rec = {
            "id": fid, "name": fcfg.get("name", fid), "publisher": fcfg.get("publisher", ""),
            "licence_id": fcfg.get("licence_id", ""), "licence_text": fcfg.get("licence_text", ""),
            "version": info[0].get("feed_version", "") if info else "", "sha256": sha,
            "timezone": tz, "max_t1": int(max_t1),
            "month_used": composite.feed_month_used(sel, timeline["month"]),
            "classes": len(classes), "stored": len(built), "excluded": excluded, "by_class": by_class,
            "build": {k: v for k, v in bstats.items()},
        }
        feed_recs.append(rec)
        timeline_feeds[fid] = {"dates": {k: by_class[k]["dates"] for k in kinds}, "excluded": excluded,
                               "rule": {k: by_class[k]["rule"] for k in kinds}}
        for bt in built:
            bt.feed_index = fi
        all_trips += built
        for k, v in shapes.items():
            all_shapes.setdefault(k, v)
        all_routes.update(routes)
        log(f"  {fid}: {sum(counts.values())} trip-dates, {len(classes)} classes, {len(built)} stored "
            f"({bstats['dropped_box']} outside the area box, {bstats['skipped_short']} short) in {time.time() - t0:.0f}s")
        feed_summary(fid, rec, kinds)
        if bstats["unknown_types"]:
            for rt, rids in bstats["unknown_types"].items():
                log(f"      route_type {rt or '(blank)'} is in no mode's list: {len(rids)} routes fell into '{modes[0]['id']}'")

    # String keys only, so one city's trip order never depends on other feeds or cities.
    all_trips.sort(key=lambda bt: (int(bt.t[0]), bt.route_key, bt.cls.hex))
    route_ids = sorted({bt.route_key for bt in all_trips})
    route_index = {r: i for i, r in enumerate(route_ids)}
    shape_index, shape_order = {}, []
    for bt in all_trips:
        if bt.shape_key not in shape_index:
            shape_index[bt.shape_key] = len(shape_order)
            shape_order.append(bt.shape_key)

    n, s = len(all_trips), len(shape_order)
    K = len(kinds)
    shape_off = np.zeros(s + 1, dtype=np.int64)
    xy_parts, cum_parts, bbox = [], [], np.zeros((s, 4), dtype=np.int64)
    for i, skey in enumerate(shape_order):
        xy, cum = all_shapes[skey]
        xi = np.rint(xy.ravel() * 1000.0).astype(np.int64)
        ci = np.rint(cum * 10000.0).astype(np.int64)
        xy_parts.append(xi)
        cum_parts.append(ci)
        shape_off[i + 1] = shape_off[i] + len(ci)
        p = xi.reshape(-1, 2)
        bbox[i] = [p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()]
    trip_off = np.zeros(n + 1, dtype=np.int64)
    t_parts, d_parts = [], []
    arrays = {k: np.zeros(n, dtype=np.int64) for k in ("trip_r", "trip_s", "trip_feed", "trip_mult", "trip_draw")}
    sha = np.zeros((n, 20), dtype=np.uint8)
    dates_k = np.zeros((n, K), dtype=np.int64)
    runs_k = np.zeros((n, K), dtype=np.int64)
    copies = np.zeros((n, K), dtype=np.int64)
    for i, bt in enumerate(all_trips):
        t_parts.append(bt.t)
        d_parts.append(np.rint(bt.d * 1000.0).astype(np.int64))
        trip_off[i + 1] = trip_off[i] + len(bt.t)
        arrays["trip_r"][i] = route_index[bt.route_key]
        arrays["trip_s"][i] = shape_index[bt.shape_key]
        arrays["trip_feed"][i] = bt.feed_index
        arrays["trip_mult"][i] = bt.cls.mult
        sha[i] = np.frombuffer(bt.cls.sha, dtype=np.uint8)
        draw = 0
        for j, k in enumerate(kinds):
            dates_k[i, j] = bt.cls.dates_k[k]
            runs_k[i, j] = bt.cls.runs_k[k]
            copies[i, j] = bt.cls.copies[k]
            if bt.cls.copies[k] > 0:
                draw |= 1 << j
        arrays["trip_draw"][i] = draw

    def cat(parts, dtype):
        return np.concatenate(parts).astype(dtype) if parts else np.zeros(0, dtype=dtype)

    limits = {"trip_mult": arrays["trip_mult"], "trip_dates": dates_k, "trip_copies": copies}
    for name, arr in limits.items():
        if arr.size and arr.max() > np.iinfo(np.int16).max:
            sys.exit(f"{name} overflows int16 ({arr.max()})")
    arrays.update({
        "shape_off": shape_off, "shape_xy": cat(xy_parts, np.int32), "shape_cum": cat(cum_parts, np.int32),
        "shape_bbox": bbox, "trip_off": trip_off, "trip_t": cat(t_parts, np.int32), "trip_d": cat(d_parts, np.int32),
        "trip_sha": sha, "trip_dates": dates_k, "trip_runs": runs_k, "trip_copies": copies,
    })
    max_t1 = int(arrays["trip_t"].max()) if n else 0
    meta = {
        "schema": 4, "kind": "area_store", "area": cfg["id"], "origin": list(cfg["origin"]), "area_box": list(area_box),
        "timeline": dict(timeline, feeds=timeline_feeds),
        "day_classes": kinds, "n_dates": n_dates,
        "feeds": feed_recs, "modes": modes,
        "routes": [all_routes[r] for r in route_ids],
        "max_t1": max_t1, "n_trips": n, "n_shapes": s,
    }
    area_store.write(args.out, meta, arrays, key=args.key)
    rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    log(f"wrote {args.out}: {n} trips, {s} shapes, {len(route_ids)} routes, latest time {bn.fmt_time(max_t1)}; "
        f"{time.time() - t_start:.0f}s wall, peak rss {rss_gb:.2f} GB")


if __name__ == "__main__":
    main()

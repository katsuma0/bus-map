"""City trim: one v4 network (spec 2.9) out of an area store.

    python3 -I scripts/trim_network.py --config build/gta-markham/city.day.json --area build/areas/gta/day \
            --out build/gta-markham/day/network.json.gz [--key HEX]
    python3 -I scripts/trim_network.py --config build/gta-markham/city.week.json --area build/areas/gta/week \
            --day-network build/gta-markham/day/network.json.gz --out build/gta-markham/week/network.json.gz

The trim keeps the drawn classes whose shapes touch the city's trim box, each
as many times as it is drawn, and computes every number the video shows from
the mean running counts inside the city boundary: the minute histogram, the
peaks, the brand and group shares, the credit line. It also places the
automatic rush frame and picks the HUD panel side.

Exit codes: 0 ok, 1 error, 3 (week only) the city is not eligible for a week
video; then one JSON line {"eligible": false, "why": [...]} goes to stdout.
A summary goes to stderr.
"""
import argparse
import collections
import gzip
import hashlib
import json
import math
import os
import re
import resource
import sys
import time

import numpy as np
import shapely
from shapely.geometry import shape as geo_shape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
# -I leaves the script directory off sys.path; the sibling modules are ours.
sys.path.insert(0, HERE)
import area_store  # noqa: E402
import build_network as bn  # noqa: E402

WEEK_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
FRAME_W, FRAME_H = 1080, 1920
SAFE_X1 = 880
AM_RANGE, PM_RANGE = (300, 630), (870, 1170)
CREDIT_FONT = os.path.join(ROOT, "web", "fonts", "InterX.woff2")
CREDIT_PX, CREDIT_WIDTH, CREDIT_LINES = 22, 504, 2
# The page measures with InterX and applies kerning; a small allowance keeps
# a line A accepts from needing a third line in the browser.
CREDIT_SLACK = 1.02
T_BITS = 22   # trip times fit in 22 bits (48 days), so (rank << 22) | t sorts per trip


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def repo_path(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


class Fail(Exception):
    pass


# ---------------------------------------------------------------- colour


def _s2l(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def oklab(hexs):
    r, g, b = (_s2l(int(hexs[i:i + 2], 16)) for i in (0, 2, 4))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (x ** (1 / 3) for x in (l, m, s))
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def informative(hexs):
    """6 hex digits, not white or black, OKLCH chroma at least 0.03 (B5's rule)."""
    h = (hexs or "").strip().lower().lstrip("#")
    if not re.fullmatch(r"[0-9a-f]{6}", h) or h in ("ffffff", "000000"):
        return False
    _L, a, b = oklab(h)
    return math.hypot(a, b) >= 0.03


def hexnorm(h):
    return (h or "").strip().lower().lstrip("#")


# ---------------------------------------------------------------- text width


_FONT = {}


def text_width(text, px):
    """Advance width in px of `text` in InterX at weight 400 (its default instance)."""
    if "cmap" not in _FONT:
        from fontTools.ttLib import TTFont
        f = TTFont(CREDIT_FONT)
        _FONT["cmap"], _FONT["hmtx"], _FONT["upm"] = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm
        _FONT["fallback"] = f["hmtx"][_FONT["cmap"].get(ord("n"), ".notdef")][0]
    cmap, hmtx = _FONT["cmap"], _FONT["hmtx"]
    w = 0
    for ch in text:
        g = cmap.get(ord(ch))
        w += hmtx[g][0] if g is not None else _FONT["fallback"]
    return w / _FONT["upm"] * px


def wrap_lines(text, px, width):
    """Greedy wrap at spaces; a word wider than a line counts as one line too many."""
    lines, cur = [], None
    for w in text.split(" "):
        cand = w if cur is None else cur + " " + w
        if text_width(cand, px) * CREDIT_SLACK <= width:
            cur = cand
            continue
        if cur is not None:
            lines.append(cur)
        cur = w
        if text_width(w, px) * CREDIT_SLACK > width:
            lines.append(w)
            cur = None
    if cur is not None:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- boundary


def load_feature(path):
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if obj.get("type") == "FeatureCollection":
        if len(obj["features"]) != 1:
            raise Fail(f"{path}: a FeatureCollection boundary must hold exactly one Feature")
        obj = obj["features"][0]
    if obj.get("type") != "Feature":
        raise Fail(f"{path}: not a GeoJSON Feature")
    return obj


def project(geom):
    return shapely.transform(geom, lambda c: np.column_stack(bn.to_km(c[:, 0], c[:, 1])))


def polygons(geom):
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [p for g in geom.geoms for p in polygons(g)]
    return []


def flat3(coords):
    out = []
    for x, y in coords:
        out.append(round(float(x), 3))
        out.append(round(float(y), 3))
    return out


def build_mask(poly, bbox, cell):
    """A8.2: cells over the bbox grown by 0.1 km, inside when contains_xy holds at the centre.

    Rows run south to north (row j covers y0 + j cell .. y0 + (j + 1) cell),
    each row as run lengths that start with an outside run and sum to nx.
    """
    x0 = round(bbox[0] - 0.1, 3)
    y0 = round(bbox[1] - 0.1, 3)
    nx = int(math.ceil(round((bbox[2] + 0.1 - x0) / cell, 9)))
    ny = int(math.ceil(round((bbox[3] + 0.1 - y0) / cell, 9)))
    cx = x0 + (np.arange(nx) + 0.5) * cell
    cy = y0 + (np.arange(ny) + 0.5) * cell
    gx, gy = np.meshgrid(cx, cy)
    inside = shapely.contains_xy(poly, gx.ravel(), gy.ravel()).reshape(ny, nx)
    rle = []
    for row in inside:
        change = np.flatnonzero(np.diff(row.astype(np.int8))) + 1
        bounds = np.concatenate([[0], change, [nx]])
        runs = np.diff(bounds).tolist()
        if row[0]:
            runs = [0] + runs
        rle += runs
    return {"cell_km": cell, "x0": x0, "y0": y0, "nx": nx, "ny": ny, "rle": rle}, inside


def decode_mask(mask):
    """The page's reading of the mask, for tests: (ny, nx) bools, row 0 the southmost."""
    nx, ny = mask["nx"], mask["ny"]
    out = np.zeros(ny * nx, dtype=bool)
    pos, inside, row_left, i = 0, False, nx, 0
    rle = mask["rle"]
    while pos < ny * nx:
        n = rle[i]
        i += 1
        if inside:
            out[pos:pos + n] = True
        pos += n
        row_left -= n
        inside = not inside
        if row_left == 0:
            row_left, inside = nx, False
    return out.reshape(ny, nx)


# ---------------------------------------------------------------- positions


def positions(store, trips, q_rank, q_t):
    """x, y (km) of store trips `trips[q_rank]` at times `q_t`, by the CONTRACT position rule.

    Every query must satisfy t[0] <= T <= t[last] of its trip. The stop
    search is exact on integer keys and the shape search runs per shape on
    the same floats the page reads (metres / 1000, 0.1 m / 10000).
    """
    trips = np.asarray(trips, dtype=np.int64)
    off = np.asarray(store.trip_off)
    starts, ends = off[trips], off[trips + 1]
    lens = ends - starts
    seg0 = np.concatenate([[0], np.cumsum(lens)[:-1]])
    idx = np.repeat(starts - seg0, lens) + np.arange(int(lens.sum()))
    tt = np.asarray(store.trip_t[idx], dtype=np.int64)
    dd = np.asarray(store.trip_d[idx], dtype=np.int64) / 1000.0
    keys = (np.repeat(np.arange(len(trips), dtype=np.int64), lens) << T_BITS) | tt
    q_rank = np.asarray(q_rank, dtype=np.int64)
    q_t = np.asarray(q_t, dtype=np.int64)
    pos = np.searchsorted(keys, (q_rank << T_BITS) | q_t, side="right") - 1
    last = seg0[q_rank] + lens[q_rank] - 1
    at_end = pos >= last
    nxt = np.minimum(pos + 1, last)
    ti, tn = tt[pos], tt[nxt]
    di, dn = dd[pos], dd[nxt]
    span = np.where(at_end, 1, tn - ti)
    dist = np.where(at_end, dd[last], di + (q_t - ti) / span * (dn - di))

    shp = np.asarray(store.trip_s)[trips][q_rank]
    x = np.empty(len(q_t))
    y = np.empty(len(q_t))
    order = np.argsort(shp, kind="stable")
    sorted_shp = shp[order]
    cuts = np.flatnonzero(np.diff(sorted_shp)) + 1
    for a, b in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(order)]])):
        if a == b:
            continue
        q = order[a:b]
        xy, cum = store.shape_arrays(int(sorted_shp[a]))
        dq = dist[q]
        k = np.clip(np.searchsorted(cum, dq, side="right") - 1, 0, len(cum) - 2)
        f = np.clip((dq - cum[k]) / (cum[k + 1] - cum[k]), 0.0, 1.0)
        x[q] = xy[k, 0] + f * (xy[k + 1, 0] - xy[k, 0])
        y[q] = xy[k, 1] + f * (xy[k + 1, 1] - xy[k, 1])
    return x, y


def inside_counts(store, poly, bbox_km, route_local, n_local, batch=20000):
    """A9: cnt[k, route, minute] = sum of runs_k over inside minutes, integers.

    Every store trip whose shape bbox touches the boundary bbox counts, drawn
    or not, at every minute m from ceil(t0 / 60) to floor(t1 / 60).
    """
    K = len(store.day_classes)
    M = int(math.ceil(store.meta["max_t1"] / 60)) + 1
    cnt = np.zeros((K, n_local, M), dtype=np.int64)
    touch = store.touching(bbox_km)
    t0_all, t1_all = store.trip_t0_t1()
    runs = np.asarray(store.trip_runs)
    trip_r = np.asarray(store.trip_r)
    for s in range(0, len(touch), batch):
        trips = touch[s:s + batch]
        lo = -(-t0_all[trips] // 60)
        hi = t1_all[trips] // 60
        n = np.maximum(hi - lo + 1, 0)
        total = int(n.sum())
        if total == 0:
            continue
        q_rank = np.repeat(np.arange(len(trips)), n)
        start = np.concatenate([[0], np.cumsum(n)[:-1]])
        q_m = lo[q_rank] + (np.arange(total) - start[q_rank])
        x, y = positions(store, trips, q_rank, q_m * 60)
        ins = shapely.contains_xy(poly, x, y)
        qr, qm = q_rank[ins], q_m[ins]
        rl = route_local[trip_r[trips[qr]]]
        flat = rl * M + qm
        for kj in range(K):
            w = runs[trips[qr], kj]
            if not w.any():
                continue
            c = np.bincount(flat, weights=w, minlength=n_local * M)
            cnt[kj] += np.rint(c).astype(np.int64).reshape(n_local, M)
    return cnt, len(touch)


class Folder:
    """Folds integer counts into mean histograms: hist[(o_k + m) mod P] += cnt_fk[m] / |W_fk|."""

    def __init__(self, store, cnt, route_feed):
        self.cnt = cnt
        self.route_feed = route_feed
        self.kinds = store.day_classes
        self.feeds = [f["id"] for f in store.meta["feeds"]]
        self.n_dates = store.meta["n_dates"]
        week = store.meta["timeline"]["kind"] == "week"
        self.P = 10080 if week else 1440
        M = cnt.shape[2]
        self.idx = [(1440 * (WEEK_DAYS.index(k) if week else 0) + np.arange(M)) % self.P for k in self.kinds]

    def __call__(self, rsel):
        h = np.zeros(self.P)
        for fi, fid in enumerate(self.feeds):
            rs = rsel & (self.route_feed == fi)
            if not rs.any():
                continue
            for kj, k in enumerate(self.kinds):
                w = self.n_dates[fid][k]
                if w == 0:
                    continue
                np.add.at(h, self.idx[kj], self.cnt[kj][rs].sum(axis=0) / w)
        return h


def first_argmax(h2, minutes):
    """First maximum of the rounded hist over `minutes` (absolute minute numbers, wrapped).

    The count is Python's round() of the 2-decimal value in the file, which is
    what the page's count line reads (an exact half goes to the even number).
    """
    P = len(h2)
    vals = h2[np.asarray(minutes) % P]
    j = int(np.argmax(vals))
    m = int(minutes[j])
    return {"count": int(round(float(h2[m % P]))), "time": m * 60}


# ---------------------------------------------------------------- brands


def match_entry(route, entries):
    for e in entries:
        if route["feed"] in e.get("feeds", []) and ("agency_names" not in e or route["agency"] in e["agency_names"]):
            return e
    return None


def match_rule(route, entry):
    rid = route["id"].split(":", 1)[1]
    for rule in entry.get("rules") or []:
        if "short" in rule and re.search(rule["short"], route["short"]):
            return rule
        if "route_ids" in rule and rid in rule["route_ids"]:
            return rule
    return None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "agency"


def assign_brands(routes, entries, modes):
    """A10 steps 1 to 4 for every store route: (brand id per route, brand definitions)."""
    mode_label = {m["id"]: m.get("label", m["id"]) for m in modes}
    out, defs, loose = [None] * len(routes), {}, collections.defaultdict(list)
    for i, r in enumerate(routes):
        e = match_entry(r, entries)
        raw = hexnorm(r.get("color_raw"))
        if e is None:
            loose[(r["feed"], r["agency"])].append(i)
            continue
        ecol = hexnorm(e.get("color"))
        lines = e.get("lines", "none")
        if (lines == "all" or (lines == "rail" and r["mode"] != "bus")) and informative(raw) and raw != ecol:
            bid = f"{e['id']}:line:{raw}"
            defs.setdefault(bid, {"label": r["short"] or r["long"], "hex": raw, "kind": "line", "entry": e["id"],
                                  "verified": True, "alt": None,
                                  "source": f"{r['feed']}.zip routes.txt route_color {raw.upper()} on {r['short'] or r['id']}"})
        else:
            rule = match_rule(r, e)
            if rule is not None:
                bid = f"{e['id']}:{rule['id']}"
                defs.setdefault(bid, {"label": rule.get("label", rule["id"]), "hex": hexnorm(rule.get("color")),
                                      "kind": "rule", "entry": e["id"], "verified": bool(rule.get("verified", False)),
                                      "alt": hexnorm(rule.get("alt")) or None, "source": rule.get("source", "")})
            else:
                bid = e["id"]
        out[i] = bid
    for e in entries:
        defs.setdefault(e["id"], {"label": e.get("label", e["id"]), "hex": hexnorm(e.get("color")), "kind": "agency",
                                  "entry": e["id"], "verified": bool(e.get("verified", False)),
                                  "alt": hexnorm(e.get("alt")) or None, "source": e.get("source", "")})
    for (feed, agency), idxs in sorted(loose.items()):
        colours = collections.Counter(hexnorm(routes[i].get("color_raw")) for i in idxs
                                      if informative(routes[i].get("color_raw")))
        if colours:
            best = sorted(colours.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            bid = f"{feed}:{slug(agency)}"
            defs[bid] = {"label": agency or feed, "hex": best, "kind": "gtfs", "entry": None, "verified": False,
                         "alt": None, "source": f"{feed}.zip routes.txt: {best.upper()} on {colours[best]} of {len(idxs)} routes of {agency}"}
            for i in idxs:
                out[i] = bid
        else:
            for i in idxs:
                mode = routes[i]["mode"]
                bid = f"mode:{mode}"
                defs.setdefault(bid, {"label": mode_label.get(mode, mode), "hex": "", "kind": "mode", "entry": None,
                                      "verified": False, "alt": None,
                                      "source": "no informative route_color; the theme's mode colour"})
                out[i] = bid
    return out, defs


def verified_warnings(routes, entries, brand_of, defs):
    warns = []
    for e in entries:
        if not e.get("verified"):
            continue
        cols = collections.Counter(hexnorm(r.get("color_raw")) for i, r in enumerate(routes)
                                   if defs[brand_of[i]].get("entry") == e["id"] and informative(r.get("color_raw")))
        if not cols:
            continue
        top = sorted(cols.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if top[0] != hexnorm(e.get("color")):
            warns.append(f"brand {e['id']} is verified as {hexnorm(e.get('color'))} but its most common informative "
                         f"route_color is {top[0]} ({top[1]} routes)")
    return warns


# ---------------------------------------------------------------- frames


def frame_box(frame):
    kv = float(frame["km_vertical"])
    cx, cy = frame["center_km"]
    return [cx - kv * 9 / 16 / 2, cy - kv / 2, cx + kv * 9 / 16 / 2, cy + kv / 2]


def inside_box(inner, outer):
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def to_screen(x, y, frame):
    s = FRAME_H / float(frame["km_vertical"])
    cx, cy = frame["center_km"]
    return FRAME_W / 2 + (x - cx) * s, FRAME_H / 2 - (y - cy) * s


def auto_rush_frame(vx, vy, day_frame, trim_box, fit_box, zoom, share):
    """A8.6: the closest zoom whose best 0.25 km grid centre holds `share` of the inside vehicles."""
    kv = float(day_frame["km_vertical"])
    s = FRAME_H / kv
    fx0, fx1 = (fit_box[0] - FRAME_W / 2) / s, (fit_box[2] - FRAME_W / 2) / s
    fy0, fy1 = (FRAME_H / 2 - fit_box[3]) / s, (FRAME_H / 2 - fit_box[1]) / s
    n_in = len(vx)
    zs = [k / 10 for k in range(int(round(zoom[1] * 10)), int(round(zoom[0] * 10)) - 1, -1)]
    order = np.argsort(vx, kind="stable")
    sx, sy = vx[order], vy[order]
    best = None
    for z in zs:
        hw, hh = kv / z * 9 / 32, kv / z / 2
        # 0.1 km of slack inside the trim box absorbs the rounding of the centre.
        gx = np.arange(math.ceil((trim_box[0] + hw + 0.1) / 0.25), math.floor((trim_box[2] - hw - 0.1) / 0.25) + 1) * 0.25
        gy = np.arange(math.ceil((trim_box[1] + hh + 0.1) / 0.25), math.floor((trim_box[3] - hh - 0.1) / 0.25) + 1) * 0.25
        if len(gx) == 0 or len(gy) == 0:
            continue
        counts = np.zeros((len(gy), len(gx)), dtype=np.int64)
        for j, cy in enumerate(gy):
            sel = (sy >= cy + fy0 / z) & (sy <= cy + fy1 / z)
            xs = sx[sel]
            counts[j] = np.searchsorted(xs, gx + fx1 / z, side="right") - np.searchsorted(xs, gx + fx0 / z, side="left")
        top = int(counts.max())
        jj, ii = np.nonzero(counts == top)
        # Ties: the centre whose fit box is best centred on the vehicles it holds.
        cand = []
        for j, i in zip(jj, ii):
            cx, cy = gx[i], gy[j]
            bx, by = cx + (fx0 + fx1) / 2 / z, cy + (fy0 + fy1) / 2 / z
            if top:
                m = (vx >= cx + fx0 / z) & (vx <= cx + fx1 / z) & (vy >= cy + fy0 / z) & (vy <= cy + fy1 / z)
                d = math.hypot(float(vx[m].mean()) - bx, float(vy[m].mean()) - by)
            else:
                d = math.hypot(bx - day_frame["center_km"][0], by - day_frame["center_km"][1])
            cand.append((d, cx, cy))
        cand.sort()
        _d, cx, cy = cand[0]
        best = (z, top, cx, cy)
        if n_in and top >= share * n_in:
            break
    if best is None:
        return None
    z, top, cx, cy = best
    return {"zoom": z, "count": top, "inside": n_in,
            "frame": {"km_vertical": round(kv / z, 1), "center_km": [round(float(cx), 1), round(float(cy), 1)]}}


def panel_side(vx, vy, frame, panel):
    rect = panel.get("rect", [60, 1140, 620, 1500])
    tie = float(panel.get("tie", 0.10))
    pref = panel.get("preferred", "left")
    sx, sy = to_screen(vx, vy, frame)
    w = rect[2] - rect[0]
    under = {}
    for side, (x0, x1) in (("left", (rect[0], rect[2])), ("right", (SAFE_X1 - w, SAFE_X1))):
        under[side] = int(np.count_nonzero((sx >= x0) & (sx <= x1) & (sy >= rect[1]) & (sy <= rect[3])))
    l, r = under["left"], under["right"]
    if l == r or abs(l - r) < tie * max(l, r):
        return {"side": pref, "inside_under": under,
                "why": f"within {round(tie * 100)}% of each other: the recipe's preference"}
    return {"side": "left" if l < r else "right", "inside_under": under, "why": "fewer inside vehicles under the panel"}


# ---------------------------------------------------------------- output


def sha256_file(path):
    return area_store.sha256_file(path)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def local_key(cfg_path, store, boundary_path, brands_path):
    """A stand-in trim key when make.py passes none: the D2 inputs of the trim step."""
    code = {n: sha256_file(os.path.join(HERE, n)) for n in ("trim_network.py", "area_store.py")}
    obj = {"step": "trim", "config": sha256_file(cfg_path), "area": store.key or sha256_file(os.path.join(store.path, "meta.json")),
           "boundary": sha256_file(boundary_path), "brands": sha256_file(brands_path), "code": code}
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def write_gz(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        # mtime=0 keeps a no-change rebuild byte-identical.
        fh.write(gzip.compress(raw, compresslevel=6, mtime=0) if path.endswith(".gz") else raw)
    os.replace(tmp, path)
    return len(raw)


def manifest_path_for(out):
    d = os.path.dirname(os.path.abspath(out))
    return os.path.join(os.path.dirname(d) if os.path.basename(d) in ("day", "week") else d, "manifest.json")


def validate_v4(store, emit_trips, shape_ids, box):
    """The asserts of bn.validate on the emitted trips and shapes, with the trim box in km."""
    x0, y0, x1, y1 = box
    bb = np.asarray(store.shape_bbox)[shape_ids] / 1000.0
    assert np.all((bb[:, 0] <= x1) & (bb[:, 2] >= x0) & (bb[:, 1] <= y1) & (bb[:, 3] >= y0)), "a shape lies outside the trim box"
    soff = np.asarray(store.shape_off)
    for s in shape_ids:
        a, b = int(soff[s]), int(soff[s + 1])
        assert b - a >= 2, f"shape {s} has {b - a} points"
        assert np.all(np.diff(np.asarray(store.shape_cum[a:b])) >= 0), f"shape {s} cum decreases"
    toff = np.asarray(store.trip_off)
    cum_end = np.asarray(store.shape_cum)[soff[1:] - 1] / 10000.0
    for i in emit_trips:
        a, b = int(toff[i]), int(toff[i + 1])
        t = np.asarray(store.trip_t[a:b])
        d = np.asarray(store.trip_d[a:b]) / 1000.0
        assert b - a >= 2, f"trip {i} has {b - a} stops"
        assert np.all(np.diff(t) >= 0), f"trip {i} times decrease"
        assert np.all(np.diff(d) >= 0), f"trip {i} distances decrease"
        end = cum_end[store.trip_s[i]] + 0.01
        assert d.min() >= 0 and d.max() <= end, f"trip {i} distance outside [0, {end}]"


# ---------------------------------------------------------------- main


def week_eligibility(store, day_meta, min_dates, month):
    """A3.6: every major feed of the day network needs min_dates month dates in every day class."""
    majors = {f["id"]: f.get("inside_share", 0) for f in day_meta.get("feeds", []) if f.get("major")}
    why = []
    for f in store.meta["feeds"]:
        if f["id"] not in majors:
            continue
        short = {k: f["by_class"][k]["in_month"] for k in store.day_classes if f["by_class"][k]["in_month"] < min_dates}
        if short:
            why.append(f"{f['id']} ({f['name']}) is major in the day network (inside share {majors[f['id']]}) but has fewer "
                       f"than {min_dates} {month} dates in " + ", ".join(f"{k} ({n})" for k, n in short.items()))
    return why


def emit(store, trim_box):
    """A8.4: (store index, w mask or None) per emitted trip, in store order, copies in a row."""
    touch = store.touching(trim_box)
    copies = np.asarray(store.trip_copies)[touch]
    out = []
    week = copies.shape[1] > 1
    for i, c in zip(touch.tolist(), copies.tolist()):
        n = max(c)
        for j in range(n):
            if week:
                w = sum(1 << k for k, ck in enumerate(c) if ck > j)
                out.append((i, w))
            else:
                out.append((i, None))
    return out


def vehicles_at(store, emitted, T, period):
    """Store trips of the emitted set running at absolute time T, one entry per drawn copy."""
    t0, t1 = store.trip_t0_t1()
    trips, times = [], []
    for i, w in emitted:
        if w is None:
            if t0[i] <= T <= t1[i]:
                trips.append(i)
                times.append(T)
            continue
        for k in range(7):
            if w >> k & 1:
                tl = (T - 86400 * k) % period
                if t0[i] <= tl <= t1[i]:
                    trips.append(i)
                    times.append(tl)
    if not trips:
        return np.zeros(0), np.zeros(0)
    uniq, rank = np.unique(np.asarray(trips, dtype=np.int64), return_inverse=True)
    return positions(store, uniq, rank, np.asarray(times, dtype=np.int64))


def main():
    t_start = time.time()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="city config (spec 2.7), schema 4")
    ap.add_argument("--area", required=True, help="area store directory of the same timeline")
    ap.add_argument("--out", required=True, help="network path, .json.gz")
    ap.add_argument("--day-network", default=None, help="week only: the city's day network, for the major feeds")
    ap.add_argument("--key", default=None, help="trim step key from make.py (meta.build_key)")
    ap.add_argument("--manifest", default=None, help="default: build/<id>/manifest.json next to the output")
    args = ap.parse_args()
    try:
        rc = trim(args, t_start)
    except Fail as e:
        log(f"trim: {e}")
        rc = 1
    sys.exit(rc)


def trim(args, t_start):
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if cfg.get("schema") != 4 or cfg.get("kind") != "city":
        raise Fail(f"{args.config}: not a schema 4 city config")
    store = area_store.Store(args.area)
    sm = store.meta
    if [float(v) for v in sm["origin"]] != [float(v) for v in cfg["origin"]]:
        raise Fail(f"origin {cfg['origin']} differs from the area store's {sm['origin']}")
    bn.set_origin(sm["origin"])
    tl = sm["timeline"]
    week = tl["kind"] == "week"
    P = 10080 if week else 1440
    month = tl["month"]
    manifest = args.manifest or manifest_path_for(args.out)

    # Week eligibility first: it needs no geometry.
    if week:
        min_dates = int(tl.get("min_week_dates", 2))
        if args.day_network:
            with gzip.open(args.day_network, "rt", encoding="utf-8") if args.day_network.endswith(".gz") \
                    else open(args.day_network, encoding="utf-8") as fh:
                day_meta = json.load(fh)["meta"]
        else:
            log("warning: no --day-network; week eligibility uses no major feeds")
            day_meta = {"feeds": []}
        why = week_eligibility(store, day_meta, min_dates, month)
        if why:
            print(json.dumps({"eligible": False, "why": why}, ensure_ascii=False))
            log(f"{cfg['id']} week not eligible: " + "; ".join(why))
            if os.path.exists(args.out):
                os.remove(args.out)
            if os.path.exists(manifest):
                area_store.update_manifest(manifest, {}, drop=[area_store.rel(args.out)])
            return 3

    # A8.1, A8.2: boundary polygon in km, simplified, and its mask.
    bcfg = cfg["boundary"]
    bpath = repo_path(bcfg["file"])
    feat = load_feature(bpath)
    poly = project(geo_shape(feat["geometry"])).simplify(float(bcfg.get("simplify_km", 0.02)), preserve_topology=True)
    parts = polygons(poly)
    if not parts:
        raise Fail(f"{bpath}: no polygon in the boundary")
    bbox_km = [round(float(v), 3) for v in poly.bounds]
    shapely.prepare(poly)
    mask, _grid = build_mask(poly, bbox_km, float(bcfg.get("mask_km", 0.025)))
    props = feat.get("properties") or {}
    source = props.get("source") or (f"Overture {props['release']} division_area {props['id']}"
                                     if props.get("release") and props.get("id") else f"file {area_store.rel(bpath)}")
    area_km2 = props.get("area_km2")
    if area_km2 is None:
        area_km2 = round(float(poly.area), 2)

    # A8.3: trim box; the boundary and a pinned rush frame must lie inside it.
    frame = cfg["frame"]
    kv = float(frame["km_vertical"])
    cx, cy = frame["center_km"]
    scale = float(cfg.get("trim_scale", 1.25))
    hw, hh = kv * 9 / 16 / 2 * scale + 1, kv / 2 * scale + 1
    trim_box = [cx - hw, cy - hh, cx + hw, cy + hh]
    if not inside_box(bbox_km, trim_box):
        raise Fail(f"boundary bbox {bbox_km} is not inside the trim box {[round(v, 3) for v in trim_box]}")
    variants_cfg = cfg.get("variants") or {}
    rush_cfg = dict(cfg.get("rush") or {})
    pinned = None
    if "rush" in variants_cfg:
        pinned = (variants_cfg["rush"] or {}).get("frame") or rush_cfg.get("frame")
        if pinned and not inside_box(frame_box(pinned), trim_box):
            raise Fail(f"pinned rush frame {pinned} is not inside the trim box {[round(v, 3) for v in trim_box]}")

    # A10 brand per store route (cheap, and needed before any per-brand count).
    routes = sm["routes"]
    brands_path = repo_path(cfg.get("brands", "cities/brands.json"))
    with open(brands_path, encoding="utf-8") as fh:
        entries = json.load(fh).get("agencies", [])
    brand_of, bdefs = assign_brands(routes, entries, sm["modes"])
    warnings = verified_warnings(routes, entries, brand_of, bdefs)

    # A8.4: emitted trips.
    emitted = emit(store, trim_box)
    emit_ids = sorted({i for i, _w in emitted})

    # A9: inside counts per day class, route and minute.
    t_counts = time.time()
    trip_r_all = np.asarray(store.trip_r).astype(np.int64)
    used = np.unique(np.concatenate([trip_r_all[store.touching(bbox_km)], trip_r_all[np.asarray(emit_ids, dtype=np.int64)]]))
    route_local = np.full(len(routes), -1, dtype=np.int64)
    route_local[used] = np.arange(len(used))
    feed_idx = {f["id"]: i for i, f in enumerate(sm["feeds"])}
    route_feed = np.array([feed_idx[routes[r]["feed"]] for r in used], dtype=np.int64)
    cnt, n_counted = inside_counts(store, poly, bbox_km, route_local, len(used))
    fold = Folder(store, cnt, route_feed)
    all_sel = np.ones(len(used), dtype=bool)
    hist_raw = fold(all_sel)
    h2 = np.round(hist_raw, 2)
    t_counts = time.time() - t_counts

    am = first_argmax(h2, np.arange(AM_RANGE[0], AM_RANGE[1] + 1))
    pm = first_argmax(h2, np.arange(PM_RANGE[0], PM_RANGE[1] + 1))
    m_am = am["time"] // 60
    total_am = float(hist_raw[m_am])

    def share_at_am(h):
        return float(h[m_am]) / total_am if total_am > 0 else 0.0

    # Brands of the city: every route drawn or counted here.
    city_brand = [brand_of[r] for r in used]
    brand_ids = sorted(set(city_brand))
    brand_hist = {b: fold(np.array([cb == b for cb in city_brand])) for b in brand_ids}

    # A10 groups: one candidate per entry; gtfs and mode brands are their own.
    def candidate(bid):
        d = bdefs[bid]
        return d["entry"] if d["kind"] in ("agency", "rule", "line") else bid

    city_cand = [candidate(b) for b in city_brand]
    cand_ids = sorted(set(city_cand))
    cand_hist = {c: fold(np.array([cc == c for cc in city_cand])) for c in cand_ids}
    cand_share = {c: share_at_am(cand_hist[c]) for c in cand_ids}
    gb = cfg.get("group_by") or {"field": "agency-auto", "min_share": 0.03, "max_groups": 3}
    if gb.get("field") != "agency-auto":
        raise Fail(f"group_by field {gb.get('field')!r} is not implemented; only 'agency-auto' is")
    ranked = sorted((c for c in cand_ids if cand_share[c] >= float(gb.get("min_share", 0.03))),
                    key=lambda c: (-cand_share[c], c))[:int(gb.get("max_groups", 3))]
    shown = set(ranked)
    groups = []
    for c in ranked:
        groups.append({"id": c, "label": bdefs[c]["label"], "brand": c, "share": round(cand_share[c], 4)})
    other_sel = np.array([cc not in shown for cc in city_cand])
    other_hist = fold(other_sel) if other_sel.any() else np.zeros(P)
    # "other" is listed whenever it has any inside vehicle-minutes, not only at
    # the am peak, so the chips add up to the count at every minute.
    if other_hist.sum() > 0:
        groups.append({"id": "other", "label": "other", "brand": None, "share": round(share_at_am(other_hist), 4)})
    group_hist = {g["id"]: (cand_hist[g["id"]] if g["id"] != "other" else other_hist) for g in groups}
    route_group = {int(r): (cc if cc in shown else "other") for r, cc in zip(used, city_cand)}

    # Brands listed: the city's, plus each shown group's entry brand.
    listed = set(brand_ids) | {g["brand"] for g in groups if g["brand"]}
    for b in listed - set(brand_hist):
        brand_hist[b] = np.zeros(P)
    vm_brand = {b: share_at_am(brand_hist[b]) for b in listed}
    brand_order = sorted(listed, key=lambda b: (-vm_brand[b], b))
    brand_index = {b: i for i, b in enumerate(brand_order)}

    def brand_rail(b):
        rs = [routes[r] for r, cb in zip(used, city_brand) if cb == b]
        if bdefs[b]["kind"] == "line":
            return any(r["mode"] != "bus" for r in rs)
        return bool(rs) and 2 * sum(1 for r in rs if r["mode"] != "bus") > len(rs)

    meta_brands = [{"id": b, "label": bdefs[b]["label"], "hex": bdefs[b]["hex"], "kind": bdefs[b]["kind"],
                    "entry": bdefs[b]["entry"], "rail": brand_rail(b), "share": round(vm_brand[b], 4),
                    "verified": bdefs[b]["verified"], "alt": bdefs[b]["alt"], "source": bdefs[b]["source"]}
                   for b in brand_order]

    # Per feed: inside vehicle-minutes, share, major.
    feed_hist = {f["id"]: fold(route_feed == i) for i, f in enumerate(sm["feeds"])}
    vm_total = float(hist_raw.sum())
    major_share = float(cfg.get("major_share", 0.05))
    feed_vm = {fid: float(h.sum()) for fid, h in feed_hist.items()}

    # Month label: the fallback month's name when a fallback feed is major here.
    fallback = [f for f in sm["feeds"] if f["month_used"] != month]
    major_fb = sorted((f for f in fallback if vm_total > 0 and feed_vm[f["id"]] / vm_total >= major_share),
                      key=lambda f: -feed_vm[f["id"]])
    label_month = major_fb[0]["month_used"] if major_fb else month
    month_label = MONTHS[int(label_month[5:7]) - 1]

    def feed_entry(f):
        bc = f["by_class"]
        kinds = store.day_classes
        share = feed_vm[f["id"]] / vm_total if vm_total > 0 else 0.0
        e = {"id": f["id"], "name": f["name"], "publisher": f["publisher"], "licence_id": f["licence_id"],
             "licence_text": f["licence_text"], "version": f["version"], "sha256": f["sha256"],
             "month_used": f["month_used"]}
        if week:
            e["dates"] = {k: bc[k]["dates"] for k in kinds}
            e["rule"] = {k: bc[k]["rule"] for k in kinds}
            md = {k: bc[k]["median_date"] for k in kinds if bc[k]["rule"] == "median-date"}
            if md:
                e["median_date"] = md
        else:
            e["dates"] = bc["wd"]["dates"]
            e["rule"] = bc["wd"]["rule"]
            if bc["wd"]["rule"] == "median-date":
                e["median_date"] = bc["wd"]["median_date"]
        e["excluded"] = f["excluded"]
        e["classes"] = f["classes"]
        e["drawn"] = {k: bc[k]["drawn"] for k in kinds} if week else bc["wd"]["drawn"]
        e["mean_trips_per_day"] = {k: bc[k]["mean_trips_per_day"] for k in kinds} if week else bc["wd"]["mean_trips_per_day"]
        e["inside_vehicle_minutes"] = round(feed_vm[f["id"]], 2)
        e["inside_share"] = round(share, 4)
        e["major"] = share >= major_share
        return e

    meta_feeds = [feed_entry(f) for f in sm["feeds"]]

    # Credit: agencies with inside vehicle-minutes, by share. An entry is one
    # agency (Zum counts with Brampton); a route without an entry counts as its feed.
    feed_name = {f["id"]: f["name"] for f in sm["feeds"]}
    unit_of, unit_label = [], {}
    for r, cb in zip(used, city_brand):
        e = bdefs[cb]["entry"]
        u = "entry:" + e if e else "feed:" + routes[r]["feed"]
        unit_label[u] = bdefs[e]["label"] if e else feed_name[routes[r]["feed"]]
        unit_of.append(u)
    unit_vm = {u: float(fold(np.array([x == u for x in unit_of])).sum()) for u in sorted(unit_label)}
    agencies = [unit_label[u] for u in sorted((u for u in unit_vm if unit_vm[u] > 0), key=lambda u: (-unit_vm[u], unit_label[u]))]
    template = cfg.get("credit_template", "Data: {agencies} · Map: Overture, OSM")
    credit = template.replace("{agencies}", ", ".join(agencies))
    if len(wrap_lines(credit, CREDIT_PX, CREDIT_WIDTH)) > CREDIT_LINES:
        credit = cfg.get("credit_fallback", "Data: {n} transit agencies · Map: Overture, OSM").replace("{n}", str(len(agencies)))

    # Vehicles at the am peak: rush frame (A8.6) and panel side (A8.7).
    period_s = P * 60
    vx, vy = vehicles_at(store, emitted, am["time"], period_s)
    ins = shapely.contains_xy(poly, vx, vy) if len(vx) else np.zeros(0, dtype=bool)
    ix, iy = vx[ins], vy[ins]
    panel = panel_side(ix, iy, frame, cfg.get("panel") or {})

    # Variants (2.9).
    variants, rush_info = {}, None
    for v, vc in variants_cfg.items():
        V = dict(vc or {})
        start = V.get("start", "am_peak")
        start = am["time"] if start == "am_peak" else int(start)
        end = V.get("end")
        end = start + period_s if end in (None, "auto") else int(end)
        V["start"], V["end"] = start, end
        V["label"] = (V.get("label") or "").replace("{month}", month_label)
        render = dict(V.get("render") or {})
        V["frame"] = V.get("frame")
        if v == "rush":
            base_tm = render.get("TRAIL_MINUTES", 8)
            if pinned:
                z = kv / float(pinned["km_vertical"])
                V["frame"] = pinned
                render["TRAIL_MINUTES"] = min(10, max(5, round(base_tm * 1.6 / z)))
            elif rush_cfg.get("auto", True):
                fit_box = cfg.get("fit_box", [50, 390, 870, 1300])
                rush_info = auto_rush_frame(ix, iy, frame, trim_box, fit_box, rush_cfg.get("zoom", [1.4, 2.2]),
                                            float(rush_cfg.get("share", 0.6)))
                if rush_info:
                    V["frame"] = rush_info["frame"]
                    render["TRAIL_MINUTES"] = min(10, max(5, round(base_tm * 1.6 / rush_info["zoom"])))
            else:
                V["frame"] = None
        mins = np.arange(-(-start // 60), -(-end // 60))
        V["peak"] = first_argmax(h2, mins)
        V["render"] = render
        variants[v] = {k: V[k] for k in ("start", "end", "label", "frame", "peak", "render")} | \
                      {k: V[k] for k in V if k not in ("start", "end", "label", "frame", "peak", "render")}
    first = next(iter(variants.values())) if variants else {"start": 0, "end": period_s, "label": "", "peak": am}

    # Trips and shapes, re-indexed in first-use order.
    shape_index, route_index, shapes_out, routes_out, trips_out = {}, {}, [], [], []
    toff, soff = np.asarray(store.trip_off), np.asarray(store.shape_off)
    trip_r, trip_s = np.asarray(store.trip_r), np.asarray(store.trip_s)
    cache = {}
    for i, w in emitted:
        if i not in cache:
            s = int(trip_s[i])
            if s not in shape_index:
                shape_index[s] = len(shapes_out)
                a, b = int(soff[s]), int(soff[s + 1])
                shapes_out.append({"xy": (np.asarray(store.shape_xy[2 * a:2 * b], dtype=np.int64) / 1000.0).tolist(),
                                   "cum": (np.asarray(store.shape_cum[a:b], dtype=np.int64) / 10000.0).tolist()})
            r = int(trip_r[i])
            if r not in route_index:
                route_index[r] = len(routes_out)
                rr = routes[r]
                routes_out.append({"id": rr["id"], "short": rr["short"], "long": rr["long"], "color": rr["color"],
                                   "feed": rr["feed"], "mode": rr["mode"], "agency": rr["agency"], "type": rr["type"],
                                   "color_raw": rr["color_raw"], "brand": brand_index[brand_of[r]],
                                   "group": route_group.get(r, "other")})
            a, b = int(toff[i]), int(toff[i + 1])
            cache[i] = {"r": route_index[r], "s": shape_index[s],
                        "t": np.asarray(store.trip_t[a:b], dtype=np.int64).tolist(),
                        "d": (np.asarray(store.trip_d[a:b], dtype=np.int64) / 1000.0).tolist()}
        tr = cache[i]
        trips_out.append(dict(tr, w=w) if w is not None else tr)
    validate_v4(store, emit_ids, sorted(shape_index), trim_box)

    build_key = args.key or local_key(args.config, store, bpath, brands_path)
    modes_meta = [{k: v for k, v in m.items() if k not in ("color", "trail")} for m in sm["modes"]]
    meta = {
        "schema": 4, "kind": "city", "id": cfg["id"], "batch": cfg["batch"], "area": cfg["area"], "place": cfg["place"],
        "title": cfg.get("title") or cfg["place"].upper(), "subtitle": first.get("label", ""),
        "service_date": month, "origin": list(sm["origin"]),
        "day_start": first["start"], "day_end": first["end"],
        "frame": frame, "trim": {"scale": scale, "box_km": [round(v, 3) for v in trim_box]},
        "modes": modes_meta, "attribution": [credit], "credit": credit, "build_key": build_key,
        "feeds": meta_feeds, "trips_total": len(trips_out),
        "timeline": {"kind": tl["kind"], "period": period_s, "basis": "average-week" if week else "average-weekday",
                     "month": month, "month_label": month_label, "fallback_feeds": [f["id"] for f in fallback]},
        "hist_period": P, "am_peak": am, "pm_peak": pm, "peak": first.get("peak", am),
        "hist_by_mode": {m["id"]: np.round(fold(np.array([routes[r]["mode"] == m["id"] for r in used])), 2).tolist()
                         for m in sm["modes"]},
        "groups": groups,
        "hist_by_group": {g: np.round(h, 2).tolist() for g, h in group_hist.items()},
        "boundary": {"name": bcfg.get("name", cfg["place"]),
                     "rings": [flat3(p.exterior.coords) for p in parts],
                     "holes": [flat3(r.coords) for p in parts for r in p.interiors],
                     "area_km2": area_km2, "bbox_km": bbox_km, "source": source, "mask": mask},
        "panel": panel, "brands": meta_brands, "color_by": "brand",
        "preset": cfg.get("preset", "shorts"), "theme": cfg.get("theme", {}), "render": cfg.get("render", {}),
        "variants": variants, "card": cfg.get("card", {}),
    }
    out = {"meta": meta, "routes": routes_out, "shapes": shapes_out, "trips": trips_out, "hist": h2.tolist()}
    n_json = write_gz(args.out, out)
    # The day network's key is the city's build key; a week trim leaves it as it is.
    area_store.update_manifest(manifest, {area_store.rel(args.out): sha256_file(args.out),
                                          area_store.rel(bpath): sha256_file(bpath)},
                               build_key=None if week else build_key)

    # Summary (A12).
    log(f"{cfg['id']} {tl['kind']}: {len(trips_out)} trips ({len(emit_ids)} classes), {len(shapes_out)} shapes, "
        f"{len(routes_out)} routes; {n_counted} trips counted inside the boundary in {t_counts:.0f}s")
    log(f"  inside peak {int(h2.max())} at {bn.fmt_time(int(np.argmax(h2)) * 60)}; am peak {am['count']} at "
        f"{bn.fmt_time(am['time'])}, pm peak {pm['count']} at {bn.fmt_time(pm['time'])}; month label {month_label}")
    log("  feeds by inside share: " + ", ".join(f"{e['id']} {e['inside_share']:.3f}{' major' if e['major'] else ''}"
                                               for e in sorted(meta_feeds, key=lambda e: -e["inside_share"]) if e["inside_share"] > 0))
    log("  brands: " + ", ".join(f"{b['id']} {b['share']:.3f}" for b in meta_brands))
    log("  groups: " + ", ".join(f"{g['id']} {g['share']:.3f}" for g in groups))
    for v, V in variants.items():
        log(f"  {v}: {bn.fmt_time(V['start'])} to {bn.fmt_time(V['end'])}, peak {V['peak']['count']} at "
            f"{bn.fmt_time(V['peak']['time'])}, frame {V['frame']}, {V['label']!r}")
    if rush_info:
        log(f"  automatic rush frame: zoom {rush_info['zoom']}, {rush_info['count']} of {rush_info['inside']} inside "
            f"vehicles in the fit box, {rush_info['frame']}")
    log(f"  panel {panel['side']} ({panel['inside_under']}, {panel['why']}); credit {credit!r}")
    if week:
        log("  week eligible")
    for w_ in warnings:
        log(f"  warning: {w_}")
    rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    log(f"wrote {args.out} {os.path.getsize(args.out) / 1e6:.2f} MB ({n_json / 1e6:.1f} MB JSON) in "
        f"{time.time() - t_start:.0f}s, peak rss {rss_gb:.2f} GB")
    return 0


if __name__ == "__main__":
    main()

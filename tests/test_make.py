#!/usr/bin/env python3
"""Tests for scripts/make.py (spec F3 D-2 to D-7, D-10) on the test batch and the GTA fixtures.

Part A's builders and part C's render script are not in this tree yet, so the
pipeline runs in a scratch repository with stubs in their place: the stubs
honour the command lines and file formats of spec 2.7, 2.9, 2.12 and A2 and log
every call, which is what the caching and --no-upstream tests count. A fake `gh`
keeps a draft release in a JSON file for the Actions tests.

    python3 tests/test_make.py [-v] [Test.test_name]
"""

import csv
import gzip
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import make  # noqa: E402

D_FILES = ["scripts/make.py", "scripts/shorts_meta.py", "scripts/tune.py", "scripts/release.py",
           "cities/defaults.json", "cities/licences.json", "cities/templates/shorts_en.json",
           "cities/batches/test.json", "cities/recipes/test-centre.json", "cities/recipes/test-north.json",
           "tests/fixtures/test/centre.geojson", "tests/fixtures/test/north.geojson"]
HOLIDAYS = {"CA-ON": {"2026-10-12": "Thanksgiving", "2026-12-25": "Christmas Day"},
            "JP": {"2026-10-12": "Sports Day", "2026-11-03": "Culture Day"}}

# ------------------------------------------------------------------ stubs

STUB_COMMON = r'''
import json, os, sys, gzip, hashlib
def log(*parts):
    p = os.environ.get("STUB_LOG")
    if p:
        with open(p, "a") as fh:
            fh.write(" ".join(str(x) for x in parts) + "\n")
def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default
def write_gz(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(gzip.compress(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode(), mtime=0))
'''

STUB_BUILD_AREA = STUB_COMMON + r'''
import calendar, datetime
cfg = json.load(open(arg("--config")))
out = arg("--out")
log("build_area", cfg["id"], cfg["timeline"]["kind"])
tl = cfg["timeline"]
y, m = int(tl["month"][:4]), int(tl["month"][5:7])
days = [datetime.date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
hol = tl["holidays"]
keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
feeds = []
for f in cfg["feeds"]:
    exc = [{"date": d.isoformat(), "why": "holiday", "name": hol[d.isoformat()], "trips": 10, "median": 100}
           for d in days if d.isoformat() in hol and d.weekday() < 5]
    if tl["kind"] == "day":
        dates = [d.isoformat() for d in days if d.weekday() < 5 and d.isoformat() not in hol]
        rule = "half"
    else:
        dates = {k: [d.isoformat() for d in days if d.weekday() == i and d.isoformat() not in hol] for i, k in enumerate(keys)}
        rule = {k: "half" for k in keys}
    feeds.append({"id": f["id"], "dates": dates, "rule": rule, "excluded": exc, "month_used": tl["month"],
                  "classes": 10, "drawn": 10, "mean_trips_per_day": 100.0})
os.makedirs(out, exist_ok=True)
json.dump({"schema": 4, "area": cfg["id"], "origin": cfg["origin"], "timeline": tl, "feeds": feeds},
          open(os.path.join(out, "meta.json"), "w"), sort_keys=True)
open(os.path.join(out, "trip_t.npy"), "wb").write(hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).digest())
'''

STUB_TRIM = STUB_COMMON + r'''
import math
cfg = json.load(open(arg("--config")))
area = json.load(open(os.path.join(arg("--area"), "meta.json")))
week = "--day-network" in sys.argv
log("trim", cfg["id"], "week" if week else "day")
if week and cfg["id"] in os.environ.get("STUB_NOWEEK", "").split(","):
    print(json.dumps({"eligible": False, "why": ["stub: tsukubus has no Saturday dates"]}))
    sys.exit(3)
P = 604800 if week else 86400
H = P // 60
def curve(i):
    m = i % 1440
    base = 20 + 80 * math.exp(-((m - 480) / 90) ** 2) + 60 * math.exp(-((m - 1050) / 120) ** 2)
    return round(base * (0.6 if (week and i // 1440 >= 5) else 1.0), 2)
hist = [curve(i) for i in range(H)]
def argmax(lo, hi):
    best = max(range(lo, hi), key=lambda i: (hist[i], -i))
    return {"count": round(hist[best]), "time": best * 60}
am, pm = argmax(300, 631), argmax(870, 1171)
variants = {}
for v, V in cfg["variants"].items():
    start = am["time"] if V["start"] == "am_peak" else V["start"]
    end = V["end"] if V.get("end") is not None else start + P
    pk = max(range(start // 60, end // 60), key=lambda i: (hist[i % H], -i))
    variants[v] = {"start": start, "end": end, "label": V["label"].replace("{month}", "October"),
                   "frame": V.get("frame"), "peak": {"count": round(hist[pk % H]), "time": pk * 60}, "render": V["render"]}
kv, (cx, cy) = cfg["frame"]["km_vertical"], cfg["frame"]["center_km"]
hw, hh = kv * 9 / 32 * cfg["trim_scale"] + 1, kv / 2 * cfg["trim_scale"] + 1
feeds = []
for i, f in enumerate(area["feeds"]):
    feeds.append(dict(f, name=f["id"].title(), publisher="Tsukuba City", licence_id="cc-by-4.0", licence_text="CC BY 4.0",
                      version="", sha256="0" * 64, inside_vehicle_minutes=1000.0 / (i + 1), inside_share=round(0.9 if i == 0 else 0.1, 4),
                      major=True))
meta = {"schema": 4, "kind": "city", "id": cfg["id"], "batch": cfg["batch"], "area": cfg["area"], "place": cfg["place"],
        "title": cfg["title"], "service_date": "2026-10", "origin": cfg["origin"], "frame": cfg["frame"],
        "trim": {"scale": cfg["trim_scale"], "box_km": [cx - hw, cy - hh, cx + hw, cy + hh]},
        "modes": [{"id": "bus", "label": "buses", "singular": "bus"}], "credit": "Data: Tsukubus · Map: Overture, OSM",
        "build_key": arg("--key", ""), "feeds": feeds, "trips_total": 300,
        "timeline": {"kind": "week" if week else "day", "period": P, "basis": "average-week" if week else "average-weekday",
                     "month": "2026-10", "month_label": "October", "fallback_feeds": []},
        "hist_period": H, "am_peak": am, "pm_peak": pm, "hist_by_mode": {"bus": hist},
        "groups": [{"id": "tsukubus", "label": "Tsukubus", "brand": "tsukubus", "share": 0.9},
                   {"id": "other", "label": "other", "brand": None, "share": 0.1}],
        "variants": variants, "card": cfg["card"], "preset": cfg["preset"], "theme": cfg["theme"], "render": cfg["render"],
        "color_by": "brand", "panel": {"side": cfg["panel"]["preferred"]}, "brands": []}
write_gz(arg("--out"), {"meta": meta, "routes": [], "shapes": [], "trips": [], "hist": hist})
'''

STUB_BASEMAP = STUB_COMMON + r'''
cfg = json.load(open(arg("--config")))
log("basemap", cfg["id"])
write_gz(os.path.join(cfg["built_dir"], "basemap.json.gz"), {"origin": cfg["origin"], "clip": cfg["clip"],
         "params": cfg["basemap"], "roads": {"major": [], "minor": [], "rail": []}, "water": {"poly": [], "line": []}})
'''

STUB_FETCH_BOUNDARY = STUB_COMMON + r'''
cmd = sys.argv[1]
log("fetch_boundary", cmd)
if cmd == "fetch":
    # The test squares stand in for Overture division areas, named as the fixtures name them.
    import glob
    os.makedirs(os.path.dirname(arg("--out")), exist_ok=True)
    feats = []
    for i, p in enumerate(sorted(glob.glob("tests/fixtures/test/*.geojson"))):
        f = json.load(open(p))
        f["properties"] = {"id": f"area-{i}", "division_id": f"div-{i}", "name": f["properties"]["name"],
                           "subtype": "locality", "class": "land", "area_km2": 36.0 if "north" in p else 64.0}
        feats.append(f)
    json.dump({"type": "FeatureCollection", "features": feats}, open(arg("--out"), "w"), sort_keys=True)
else:
    fc = json.load(open(arg("--in")))
    subs = arg("--subtypes").split(",")
    hits = [f for f in fc["features"] if f["properties"]["name"] == arg("--name") and f["properties"]["subtype"] in subs]
    if not hits:
        sys.exit(1)
    json.dump(hits[0], open(arg("--out"), "w"), sort_keys=True)
'''

STUB_FETCH_OVERTURE = STUB_COMMON + r'''
out = os.environ["OUT"]
log("fetch_overture", os.environ["BBOX"])
os.makedirs(out, exist_ok=True)
for what in sys.argv[1:]:
    json.dump({"type": "FeatureCollection", "features": [], "bbox": os.environ["BBOX"], "what": what},
              open(os.path.join(out, what + ".geojson"), "w"), sort_keys=True)
'''

STUB_RENDER_SHIM = r'''#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const r = spawnSync(process.env.STUB_PYTHON || 'python3', ['-I', path.join(here, 'stub_render.py'), ...process.argv.slice(2)], { stdio: 'inherit' });
process.exit(r.status === null ? 1 : r.status);
'''

STUB_RENDER = STUB_COMMON + r'''
import math
from PIL import Image, ImageDraw
args = sys.argv[1:]
tier = arg("--tier")
log("render", tier, arg("--variant"), arg("--name") or arg("--out-dir"))
with gzip.open(arg("--data"), "rt") as fh:
    meta = json.load(fh)["meta"]
V = meta["variants"][arg("--variant")]
cfg = {"FRAME_ZOOM": 1, "FRAME_DX_KM": 0, "FRAME_DY_KM": 0, "TRAIL_MINUTES": 40, "TRAIL_CORE_W": 2.5,
       "TRAIL_SHOULDER_W": 9, "TRAIL_SHOULDER_ALPHA": 0.35, "TRAIL_ALPHA": 0.8, "TRAIL_LAYER_ALPHA": 0.85,
       "BUS_CORE_R": 2.4, "BUS_HALO_R": 11, "BUS_HALO_ALPHA": 0.35, "ROUTE_ALPHA": 0.3, "BASE_ROADS_GAIN": 1,
       "BASE_WATER_GAIN": 1, "TIME_WARP_GAMMA": 1, "TIME_WARP_FLOOR": 0.15, "CARD_TITLE_MAX": 132, "CARD_LINES": 0,
       "CARD_SCRIM": 0.25, "CARD_CENTER_Y": 620, "PANEL_ALPHA": 1}
cfg.update(V["render"])
cfg.update(json.loads(arg("--render-json") or "{}"))
cfg["KM_VERTICAL"] = (V.get("frame") or meta["frame"])["km_vertical"] / cfg["FRAME_ZOOM"]
N = cfg.get("HOLD_START", 0) + cfg["DURATION_FRAMES"] + cfg.get("HOLD_END", 0)
week = arg("--variant") == "week"
def hhmm(t):
    t = int(t)
    if week:
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][(t // 86400) % 7] + f"-{t % 86400 // 3600:02d}{t % 3600 // 60:02d}"
    return f"{t // 3600 % 24:02d}{t % 3600 // 60:02d}"
def picture(w, h, t, text=True, white=False):
    im = Image.new("RGB", (w, h), (255, 255, 255) if white else (14, 12, 10))
    d = ImageDraw.Draw(im)
    k = w / 1080
    lum = min(255, int(60 + cfg["TRAIL_MINUTES"] * 3))
    for i in range(12):
        x = int((100 + i * 75) * k)
        d.line([(x, int(500 * k)), (x + int(40 * k), int(1050 * k))], fill=(lum, lum // 2, 40), width=max(1, int(cfg["TRAIL_CORE_W"] * k)))
    if text:
        d.rectangle([int(72 * k), int(260 * k), int(600 * k), int(330 * k)], fill=(245, 239, 231))
    return im
def vehicles(t):
    z = cfg["FRAME_ZOOM"]
    out = []
    for i in range(120):
        x = 540 + (((i * 37) % 700) - 350) * z
        y = 900 + (((i * 53) % 900) - 450) * z
        out += [round(x, 1), round(y, 1), 1 if i % 4 else 0]
    return out
BOXES = [{"name": "title", "x0": 72, "y0": 260, "x1": 600, "y1": 330, "color": "#f5efe7", "size": 64, "font": "MontserratX"},
         {"name": "clock", "x0": 88, "y0": 1160, "x1": 400, "y1": 1240, "color": "#fffaf3", "size": 88, "font": "MontserratXTnum"},
         {"name": "count", "x0": 88, "y0": 1250, "x1": 500, "y1": 1290, "color": "#f2a48c", "size": 40, "font": "MontserratXTnum"},
         {"name": "credit", "x0": 88, "y0": 1430, "x1": 560, "y1": 1480, "color": "#9c9285", "size": 22, "font": "InterX"}]
CARD = [{"name": "card_title", "x0": 72, "y0": 500, "x1": 800, "y1": 620, "color": "#f5efe7", "size": cfg["CARD_TITLE_MAX"], "font": "MontserratX"},
        {"name": "card_line0", "x0": 72, "y0": 660, "x1": 860, "y1": 710, "color": "#f5efe7", "size": 44, "font": "InterX"}]
def save(im, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    im.save(p)
if tier == "final":
    out = arg("--out")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "wb").write(("MP4 " + arg("--key", "")).encode())
    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    side = {"name": arg("--name"), "variant": arg("--variant"), "tier": "final", "frames": N, "fps": 30, "width": 1080,
            "height": 1920, "duration_s": N / 30, "crf": 18, "kbps": 9000, "bitrate_floor_met": True,
            "capture": arg("--capture"), "ms_per_frame": 100, "query": "", "network_sha256": sha(arg("--data")),
            "basemap_sha256": sha(arg("--basemap")), "key": arg("--key")}
    json.dump(side, open(out[:-4] + ".json", "w"), sort_keys=True)
    rd = arg("--review-dir")
    if rd:
        for f in (0, 300, N - 1):
            save(picture(108, 192, 0), os.path.join(rd, f"f{f:04d}.png"))
        picture(270, 480, 0).save(os.path.join(rd, "thumb-f0300.jpg"), quality=85)
    print("SIDECAR " + json.dumps(side, sort_keys=True))
elif tier == "preview":
    open(arg("--out"), "wb").write(b"PREVIEW")
elif tier == "stills":
    od, stem = arg("--out-dir"), arg("--name")
    for t in [x for x in (arg("--times") or "none").split(",") if x != "none"]:
        save(picture(108, 192, t), os.path.join(od, f"{stem}-{hhmm(t)}.png"))
    for f in [x for x in (arg("--frames") or "").split(",") if x]:
        f = N - 1 if f == "last" else int(f)
        save(picture(108, 192, 0), os.path.join(od, f"{stem}-f{f:04d}.png"))
elif tier == "tune":
    od = arg("--out-dir")
    os.makedirs(od, exist_ok=True)
    times = [x for x in (arg("--times") or "none").split(",") if x != "none"]
    white = os.environ.get("STUB_WHITE") == "1"
    for t in times:
        save(picture(1080, 1920, t, white=white), os.path.join(od, f"still-{hhmm(t)}.png"))
        save(picture(1080, 1920, t, text=False, white=white), os.path.join(od, f"bg-{hhmm(t)}.png"))
        json.dump(vehicles(t), open(os.path.join(od, f"vehicles-{hhmm(t)}.json"), "w"))
        json.dump(BOXES, open(os.path.join(od, f"boxes-{hhmm(t)}.json"), "w"))
    if times and "--roundtrip" in args:
        save(picture(720, 1280, times[0], white=white), os.path.join(od, f"rt-still-{hhmm(times[0])}.png"))
    for f in [x for x in (arg("--frames") or "").split(",") if x]:
        f = N - 1 if f == "last" else int(f)
        save(picture(1080, 1920, 0), os.path.join(od, f"frame-{f:04d}.png"))
        save(picture(1080, 1920, 0, text=False), os.path.join(od, f"bgframe-{f:04d}.png"))
        json.dump(CARD if f in (0, 15) else BOXES, open(os.path.join(od, f"boxes-f{f:04d}.json"), "w"))
        json.dump(vehicles(0), open(os.path.join(od, f"vehicles-f{f:04d}.json"), "w"))
    nclip = int(arg("--clip-frames", "90"))
    for i in range(nclip):
        im = Image.new("RGB", (54, 96), (14, 12, 10))
        ImageDraw.Draw(im).rectangle([i % 40, 40, i % 40 + 4, 44], fill=(200, 200, 200))
        save(im, os.path.join(od, f"clip-{i:03d}.png"))
        if "--roundtrip" in args:
            save(im.resize((72, 128)), os.path.join(od, f"rt-clip-{i:03d}.png"))
    json.dump({"ms_per_frame": 120}, open(os.path.join(od, "timing.json"), "w"))
    json.dump(cfg, open(os.path.join(od, "config.json"), "w"), sort_keys=True)
else:
    sys.exit(f"stub: unknown tier {tier}")
'''

FAKE_GH = r'''#!/usr/bin/env python3
"""A fake `gh api` over a JSON file: releases, assets, git refs. FAKE_GH_FAIL=rename makes asset renames fail."""
import json, os, re, shutil, sys, urllib.parse
state_p = os.environ["FAKE_GH_STATE"]
st = json.load(open(state_p)) if os.path.exists(state_p) else {"releases": [], "assets": {}, "refs": [], "next": 1}
a = sys.argv[1:]
assert a[0] == "api", a
a = a[1:]
method, fields, headers, inp, path = "GET", {}, [], None, None
i = 0
while i < len(a):
    if a[i] == "-X": method = a[i + 1]; i += 2
    elif a[i] in ("-f", "-F"): k, v = a[i + 1].split("=", 1); fields[k] = v; i += 2
    elif a[i] == "-H": headers.append(a[i + 1]); i += 2
    elif a[i] == "--input": inp = a[i + 1]; i += 2
    elif a[i] == "--paginate": i += 1
    else: path = a[i]; i += 1
def save():
    json.dump(st, open(state_p, "w"), indent=1)
def nid():
    st["next"] += 1
    return st["next"]
def out(obj):
    sys.stdout.write(json.dumps(obj))
u = urllib.parse.urlparse(path)
p, q = u.path.lstrip("/"), dict(urllib.parse.parse_qsl(u.query))
store = os.path.join(os.path.dirname(state_p), "assets")
os.makedirs(store, exist_ok=True)
log = os.environ.get("FAKE_GH_LOG")
if log:
    open(log, "a").write(f"{method} {p} {json.dumps(fields, sort_keys=True)}\n")
if m := re.fullmatch(r"repos/[^/]+/[^/]+/releases", p):
    if method == "GET":
        out(st["releases"])
    else:
        r = {"id": nid(), "tag_name": fields["tag_name"], "name": fields["name"], "draft": fields.get("draft") == "true", "body": ""}
        st["releases"].append(r); st["assets"][str(r["id"])] = []; save(); out(r)
elif m := re.fullmatch(r"repos/[^/]+/[^/]+/releases/(\d+)/assets", p):
    if method == "GET":
        out(st["assets"][m.group(1)])
    else:
        lst = st["assets"][m.group(1)]
        if os.environ.get("FAKE_GH_FAIL") == "upload:" + q["name"]:
            sys.stderr.write("simulated cancel"); sys.exit(1)
        if any(x["name"] == q["name"] for x in lst):
            sys.stderr.write("422 already_exists"); sys.exit(1)
        aid = nid()
        shutil.copyfile(inp, os.path.join(store, str(aid)))
        x = {"id": aid, "name": q["name"], "label": q.get("label", ""), "size": os.path.getsize(inp)}
        lst.append(x); save(); out(x)
elif m := re.fullmatch(r"repos/[^/]+/[^/]+/releases/assets/(\d+)", p):
    aid = int(m.group(1))
    lst = next(l for l in st["assets"].values() if any(x["id"] == aid for x in l))
    x = next(x for x in lst if x["id"] == aid)
    if method == "DELETE":
        lst.remove(x); save()
    elif method == "PATCH":
        if os.environ.get("FAKE_GH_FAIL") == "rename":
            sys.stderr.write("simulated cancel"); sys.exit(1)
        x["name"], x["label"] = fields["name"], fields["label"]; save(); out(x)
    else:
        sys.stdout.buffer.write(open(os.path.join(store, str(aid)), "rb").read())
elif m := re.fullmatch(r"repos/[^/]+/[^/]+/releases/(\d+)", p):
    r = next(r for r in st["releases"] if r["id"] == int(m.group(1)))
    r["body"] = json.load(open(inp))["body"]; save(); out(r)
elif re.fullmatch(r"repos/[^/]+/[^/]+/git/refs", p):
    st["refs"].append({"ref": fields["ref"], "sha": fields["sha"]}); save(); out({"ref": fields["ref"]})
else:
    sys.stderr.write(f"fake gh: no route for {method} {p}"); sys.exit(1)
'''


def feed_zip(name, start="20260401", end="20271231"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for member, text in (("agency.txt", "agency_id,agency_name\n1,Tsukuba City\n"),
                             ("feed_info.txt", f"feed_publisher_name,feed_version\nTsukuba,{name}_v1\n"),
                             ("calendar.txt", f"service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                                              f"wd,1,1,1,1,1,0,0,{start},{end}\n")):
            info = zipfile.ZipInfo(member, date_time=(2026, 10, 1, 0, 0, 0))
            zf.writestr(info, text)
    return buf.getvalue()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check)


class FakeRepo:
    """A scratch repository with D's files, the test batch and stubs for parts A and C."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.root = os.path.join(tmp, "repo")
        self.log = os.path.join(tmp, "calls.log")
        os.makedirs(self.root)
        for rel in D_FILES:
            dst = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(REPO, rel), dst)
        self.write("cities/holidays.json", json.dumps(HOLIDAYS, indent=1) + "\n")
        self.write("cities/brands.json", json.dumps({"version": 1, "agencies": [
            {"id": "tsukubus", "feeds": ["tsukubus"], "label": "Tsukubus", "color": "#1e5aa8", "verified": False,
             "source": "test", "lines": "none", "alt": None, "rules": []}]}, indent=1) + "\n")
        for name in ("tsukubus", "tsukubane"):
            self.write(f"data/gtfs/{name}.zip", feed_zip(name), binary=True)
        for name, body in (("build_area.py", STUB_BUILD_AREA), ("trim_network.py", STUB_TRIM),
                           ("basemap_v4.py", STUB_BASEMAP), ("fetch_boundary.py", STUB_FETCH_BOUNDARY),
                           ("fetch_overture.py", STUB_FETCH_OVERTURE), ("stub_render.py", STUB_RENDER),
                           ("render_video.mjs", STUB_RENDER_SHIM)):
            self.write(f"scripts/{name}", body)
        for name in ("composite.py", "area_store.py", "build_network.py", "build_basemap.py"):
            self.write(f"scripts/{name}", f"# stub {name}\n")
        self.write("web/app.js", "// stub page\n")
        self.write("web/index.html", "<!doctype html>\n")
        from importlib import metadata
        self.write("requirements.txt", f"numpy=={metadata.version('numpy')} \\\n    --hash=sha256:{'0' * 64}\n")
        self.write("package-lock.json", "{}\n")
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "gh"), "w") as fh:
            fh.write(FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        os.chmod(os.path.join(self.bin, "gh"), 0o755)
        self.origin = os.path.join(tmp, "origin.git")
        git(tmp, "init", "-q", "--bare", self.origin)
        git(self.root, "init", "-q", "-b", "batch/test")
        self.write(".gitignore", "build/\ncache/\nout/shorts/\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "test repo")
        git(self.root, "remote", "add", "origin", self.origin)
        git(self.root, "push", "-q", "origin", "batch/test")

    def write(self, rel, data, binary=False):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb" if binary else "w") as fh:
            fh.write(data)

    def read_json(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
            return json.load(fh)

    def write_json(self, rel, obj):
        self.write(rel, json.dumps(obj, indent=1, ensure_ascii=False) + "\n")

    def env(self, extra=None):
        env = dict(os.environ, STUB_LOG=self.log, STUB_PYTHON=sys.executable, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   FAKE_GH_STATE=os.path.join(self.tmp, "gh", "state.json"))
        os.makedirs(os.path.join(self.tmp, "gh"), exist_ok=True)
        for k in ("GH_TOKEN", "GH_REPO", "GITHUB_ACTIONS", "GITHUB_OUTPUT"):
            env.pop(k, None)
        env.update(extra or {})
        return env

    def run(self, *args, env=None, check=True, root=None):
        res = subprocess.run([sys.executable, "-I", os.path.join(root or self.root, "scripts", "make.py"), *args],
                             cwd=root or self.root, env=self.env(env), capture_output=True, text=True)
        if check and res.returncode != 0:
            raise AssertionError(f"make.py {' '.join(args)} exited {res.returncode}\n{res.stdout}\n{res.stderr}")
        return res

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def clear_calls(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def show(self, target):
        return json.loads(self.run("show", target).stdout)

    def commit(self, msg="update"):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", msg, check=False)


def gh_env(repo):
    return {"GH_TOKEN": "x", "GH_REPO": "owner/repo", "GITHUB_SHA": "f" * 40, "FAKE_GH_LOG": os.path.join(repo.tmp, "gh.log")}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-make-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ------------------------------------------------------------------ D-2 validation

class Validation(unittest.TestCase):
    def setUp(self):
        self.pl = make.Pipeline(REPO, "tests/fixtures/gta")
        with open(os.path.join(REPO, "tests/fixtures/gta/batches/gta.json")) as fh:
            self.batch = json.load(fh)
        with open(os.path.join(REPO, "tests/fixtures/gta/recipes/gta-markham.json")) as fh:
            self.recipe = json.load(fh)

    def recipe_errors(self, r):
        errors = []
        make.validate_recipe(r, self.batch, self.pl.defaults(), REPO, errors, file_stem=r.get("id"))
        return errors

    def batch_errors(self, b):
        errors = []
        make.validate_batch(b, "gta", self.pl.licences(), None, errors)
        return errors

    def test_fixtures_validate(self):
        self.assertEqual(self.batch_errors(self.batch), [])
        for rid in self.batch["cities"]:
            with open(os.path.join(REPO, f"tests/fixtures/gta/recipes/{rid}.json")) as fh:
                self.assertEqual(self.recipe_errors(json.load(fh)), [], rid)
        b = make.Pipeline(REPO).load_batch("test")
        self.assertEqual(b["cities"], ["test-centre", "test-north"])

    def test_unknown_keys(self):
        r = dict(self.recipe, colour="red")
        self.assertTrue(any("unknown key 'colour'" in e for e in self.recipe_errors(r)))
        r = json.loads(json.dumps(self.recipe))
        r["variety"]["side"] = "left"
        self.assertTrue(any("variety: unknown key 'side'" in e for e in self.recipe_errors(r)))
        b = dict(self.batch, theme_extra=1)
        self.assertTrue(any("unknown key 'theme_extra'" in e for e in self.batch_errors(b)))
        b = json.loads(json.dumps(self.batch))
        b["areas"][0]["feeds"][0]["source"]["branch"] = "x"
        self.assertTrue(any("source: unknown key 'branch'" in e for e in self.batch_errors(b)))
        errors = []
        make.validate_lock({"batch": "gta", "areas": {}, "boundaries": {}, "extra": 1}, self.batch, errors)
        self.assertTrue(any("unknown key 'extra'" in e for e in errors))

    def test_review_videos_key_is_accepted(self):
        self.assertIn("review_videos", self.batch)
        self.assertEqual(self.batch_errors(self.batch), [])

    def test_bad_ids(self):
        for bad in ("Gta-markham", "-gta", "g", "gta_markham", "x" * 41):
            r = dict(self.recipe, id=bad)
            self.assertTrue(any("id must match" in e for e in self.recipe_errors(r)), bad)

    def test_override_render_key_of_a_variant_block(self):
        r = json.loads(json.dumps(self.recipe))
        r["override"]["render"] = {"TRAIL_MINUTES": 12}
        self.assertTrue(any("TRAIL_MINUTES is set by a defaults.variants" in e for e in self.recipe_errors(r)))
        r["override"]["render"] = {"BUS_HALO_R": 12}
        r["override"]["variant_render"] = {"day": {"TRAIL_MINUTES": 10}}
        self.assertEqual(self.recipe_errors(r), [])
        r["override"]["variant_render"] = {"night": {"TRAIL_MINUTES": 10}}
        self.assertTrue(any("unknown variant 'night'" in e for e in self.recipe_errors(r)))

    def test_unknown_licence(self):
        b = json.loads(json.dumps(self.batch))
        b["areas"][0]["feeds"][3]["licence_id"] = "ogl-yorkshire"
        self.assertTrue(any("unknown licence id 'ogl-yorkshire'" in e for e in self.batch_errors(b)))

    def test_other_errors(self):
        r = json.loads(json.dumps(self.recipe))
        r["variants"] = ["day", "night"]
        r["variety"]["card_line"] = 3
        r["override"]["brand_colors"] = {"yrt": "blue"}
        errs = self.recipe_errors(r)
        for want in ("variants: must be", "card_line: must be", "brand_colors: must map"):
            self.assertTrue(any(want in e for e in errs), want)
        b = json.loads(json.dumps(self.batch))
        b["areas"][0]["feeds"][0]["source"] = {"tag": "feeds/gta 2026", "path": "gtfs/ttc.zip"}
        b["theme"] = "neon"
        errs = self.batch_errors(b)
        self.assertTrue(any("source.tag: must match" in e for e in errs))
        self.assertTrue(any("theme: must be one of" in e for e in errs))


# ------------------------------------------------------------------ D-3 frames

E2 = {  # id: (subtype, expected area, km_vertical, center_km)
    "gta-toronto": ("county", 661.7, 99.0, [11.7, -15.1]),
    "gta-mississauga": ("locality", 295.8, 60.5, [-13.9, -24.9]),
    "gta-brampton": ("locality", 267.8, 57.5, [-20.9, -11.7]),
    "gta-markham": ("locality", 211.4, 49.0, [15.7, 6.0]),
    "gta-vaughan": ("locality", 272.4, 55.0, [-5.4, 0.8]),
    "gta-oakville": ("locality", 153.5, 38.5, [-18.1, -41.4]),
    "gta-richmond-hill": ("locality", 101.9, 35.0, [4.8, 9.3]),
    "gta-burlington": ("locality", 198.7, 46.5, [-27.5, -49.1]),
    "gta-oshawa": ("locality", 161.6, 50.5, [50.0, 12.4]),
    "gta-whitby": ("locality", 167.0, 49.5, [43.0, 10.6]),
}


class Labels(unittest.TestCase):
    def test_asset_labels(self):
        self.assertEqual(make.asset_label("gta-toronto-day.mp4", "0123456789abcdef" * 4),
                         "gta-toronto-day.mp4 key:0123456789abcdef")
        self.assertEqual(make.label_key("gta-toronto-day.mp4 key:0123456789abcdef"), "0123456789abcdef")
        self.assertEqual(make.label_key("key:0123456789abcdef"), "0123456789abcdef")  # labels of earlier runs
        self.assertEqual(make.label_key(""), "")
        self.assertEqual(make.label_key(None), "")


class Modes(unittest.TestCase):
    MODES = [{"id": "bus"}, {"id": "rail"}]

    def test_modes_over_the_window(self):
        """B9: a mode is named when half a vehicle of it is inside in some minute of [start, end)."""
        rail = [0.0] * 1440
        by = {"bus": [5.0] * 1440, "rail": rail}
        rail[400] = 1.0  # 6:40 am: inside the rush window, far from its peak
        self.assertEqual(make.modes_in_window(self.MODES, by, 23400, 34200), ["bus", "rail"])
        self.assertEqual(make.modes_in_window(self.MODES, by, 24060, 34200), ["bus"])
        self.assertEqual(make.modes_in_window(self.MODES, by, 20000, 24000), ["bus"])  # the end minute is out
        rail[400] = 0.49
        self.assertEqual(make.modes_in_window(self.MODES, by, 23400, 34200), ["bus"])
        rail[30] = 0.5  # 0:30 am, reached by a day window that wraps past midnight
        self.assertEqual(make.modes_in_window(self.MODES, by, 26520, 112920), ["bus", "rail"])
        self.assertEqual(make.modes_in_window(self.MODES, {"bus": []}, 0, 86400), [])


class Frames(unittest.TestCase):
    DIV = os.path.join(REPO, "data/gta/basemap/divisions.geojson")

    @unittest.skipUnless(os.path.exists(DIV), "data/gta/basemap/divisions.geojson is not in this checkout")
    def test_frames_equal_e2(self):
        """D-3 on the unnamed 0.1.9 polygons, matched by subtype and area until A's named boundaries land."""
        pl = make.Pipeline(REPO, "tests/fixtures/gta")
        batch = pl.load_batch("gta")
        area = batch["areas"][0]
        with open(self.DIV) as fh:
            feats = [f for f in json.load(fh)["features"] if f["properties"].get("subtype") in ("county", "locality")]
        for rid, (sub, want_area, kv, ctr) in E2.items():
            cands = [f for f in feats if f["properties"]["subtype"] == sub
                     and abs(make.geometry_area_km2(f["geometry"]) - want_area) < 0.6]
            self.assertEqual(len(cands), 1, rid)
            fr = pl.frame_of_boundary(area, pl.recipe(rid, batch), cands[0])
            self.assertLessEqual(abs(fr["km_vertical"] - kv), 0.5, rid)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(fr["center_km"], ctr)), 0.1 + 1e-9, rid)
            # The trim box must lie inside the batch's explicit area box (E1).
            self.assertTrue(make.box_inside(pl.trim_box_deg(area, fr), area["area_box"]), rid)

    def test_fit_frame_rule(self):
        fr = make.fit_frame([0, 0, 8, 8], [50, 390, 870, 1300])
        s = 1920 / fr["km_vertical"]
        self.assertEqual(fr["km_vertical"], 19.0)
        self.assertEqual(fr["center_km"], [round(4 + 80 / s, 1), round(4 - 115 / s, 1)])

    def test_clip_rounds_outward(self):
        pl = make.Pipeline(REPO, "tests/fixtures/gta")
        area = pl.load_batch("gta")["areas"][0]
        clip = pl.clip_of(area, {"km_vertical": 49.0, "center_km": [15.7, 6.0]})
        self.assertEqual(clip, [-79.53, 43.55, -79.02, 44.16])  # the 2.3 example


# ------------------------------------------------------------------ derivation

class Derivation(Scratch):
    def setUp(self):
        super().setUp()
        self.repo = FakeRepo(self.tmp)
        self.repo.run("lock", "test")
        self.pl = make.Pipeline(self.repo.root)
        self.batch = self.pl.load_batch("test")
        self.lock = self.pl.load_lock(self.batch)

    def test_lock_contents(self):
        lock = self.lock
        self.assertEqual(list(lock), ["batch", "locked_at", "areas", "boundaries", "tools"])
        f = lock["areas"]["tsukuba"]["feeds"]["tsukubus"]
        self.assertEqual(f["source"], "path")
        self.assertEqual(f["sha256"], make.sha256_file(os.path.join(self.repo.root, "data/gtfs/tsukubus.zip")))
        self.assertEqual(f["valid"], ["2026-04-01", "2027-12-31"])
        self.assertEqual(f["feed_version"], "tsukubus_v1")
        b = lock["boundaries"]["test-centre"]
        self.assertEqual(b["frame"], {"km_vertical": 19.0, "center_km": [3.0, -2.2]})
        self.assertEqual(b["area_km2"], 64.0)
        ov = lock["areas"]["tsukuba"]["overture"]
        self.assertEqual(ov["data_tag"], "overture/tsukuba-2026-09-23.1")
        # Both test boundaries are files, so no Overture divisions are fetched or pinned.
        self.assertEqual(lock["areas"]["tsukuba"]["divisions"], {"key": "JP-08-tsukuba", "sha256": None,
                                                                 "bbox": [139.95, 35.88, 140.29, 36.33]})
        self.assertFalse([c for c in self.repo.calls() if c.startswith("fetch_boundary")])
        clips = [lock["boundaries"][c]["clip"] for c in ("test-centre", "test-north")]
        self.assertTrue(all(make.box_inside(c, ov["bbox"]) for c in clips))
        # A second lock with nothing changed keeps the file byte for byte.
        before = read_bytes(self.pl.lock_path("test"))
        self.repo.run("lock", "test")
        self.assertEqual(read_bytes(self.pl.lock_path("test")), before)

    def test_show(self):
        """D-2: show prints the derived configs, the keys and the area_box suggestion."""
        info = self.repo.show("test")
        a = info["areas"]["tsukuba"]
        self.assertEqual(a["area_box_suggestion"], [140.01, 35.93, 140.23, 36.28])
        self.assertEqual(a["area_box_suggestion"], self.batch["areas"][0]["area_box"])
        self.assertEqual(set(a["keys"]), {"area_day", "area_week", "overture"})
        self.assertEqual(a["config_day"]["kind"], "area")
        self.assertEqual(sorted(info["videos"]["test-centre"]), ["day", "rush", "week"])
        self.assertEqual(info["publish_order"], ["test-centre-day", "test-north-day", "test-centre-rush",
                                                 "test-north-rush", "test-centre-week"])
        city = self.repo.show("test-north")
        self.assertEqual(city["city.day.json"]["frame"], {"km_vertical": 14.5, "center_km": [2.0, 8.0]})
        self.assertNotIn("city.week.json", city)
        self.assertEqual(city["queries"]["day"]["render"], {"CARD_LINES": 1, "FRAME_ZOOM": 1.05})
        self.assertEqual(len(city["keys"]["day"]["render_key"]), 64)

    def test_no_brands_file_yet(self):
        """Until a batch branch adds cities/brands.json, trims get an empty entry list."""
        os.remove(os.path.join(self.repo.root, "cities/brands.json"))
        city = self.repo.show("test-north")
        self.assertEqual(city["city.day.json"]["brands"], "build/brands.empty.json")
        self.assertEqual(self.repo.read_json("build/brands.empty.json"), {"version": 1, "agencies": []})

    def test_overture_boundary(self):
        """A recipe without boundary.file selects its Overture division; the lock pins the divisions file."""
        r = self.repo.read_json("cities/recipes/test-north.json")
        del r["boundary"]["file"]
        r["boundary"]["subtypes"] = ["county", "locality"]
        self.repo.write_json("cities/recipes/test-north.json", r)
        self.repo.clear_calls()
        self.repo.run("lock", "test")
        self.assertEqual(self.repo.calls(), ["fetch_boundary fetch", "fetch_boundary select"])
        lock = self.repo.read_json("cities/locks/test.lock.json")
        div = lock["areas"]["tsukuba"]["divisions"]
        self.assertEqual(div["sha256"], make.sha256_file(os.path.join(
            self.repo.root, "cache/overture/2026-09-23.1/divisions/JP-08-tsukuba.geojson")))
        b = lock["boundaries"]["test-north"]
        self.assertEqual((b["id"], b["division_id"], b["subtype"]), ("area-1", "div-1", "locality"))
        self.assertEqual(b["frame"], self.lock["boundaries"]["test-north"]["frame"])
        self.repo.clear_calls()
        self.repo.run("lock", "test")
        self.assertEqual(self.repo.calls(), [], "a second lock reuses the pinned divisions and the boundary stamp")
        self.repo.run("build", "test", "--city", "test-north")
        self.assertIn("test-north boundary: up to date", self.repo.run("build", "test", "--city", "test-north").stdout)

    def test_derived_configs(self):
        recipe = self.pl.recipe("test-north", self.batch)
        ac = self.pl.area_config(self.batch, self.batch["areas"][0], "week", self.lock)
        self.assertEqual(set(ac), {"schema", "kind", "id", "origin", "area_box", "gtfs_dir", "feeds", "modes",
                                   "timeline", "stop_times_chunk"})
        self.assertEqual(ac["timeline"]["holidays"], HOLIDAYS["JP"])
        self.assertEqual(ac["feeds"][0]["sha256"], self.lock["areas"]["tsukuba"]["feeds"]["tsukubus"]["sha256"])
        cc = self.pl.city_config(self.batch, recipe, "day", self.lock)
        self.assertEqual(set(cc), {"schema", "kind", "id", "batch", "area", "place", "title", "origin", "frame",
                                   "trim_scale", "boundary", "brands", "group_by", "credit_template", "credit_fallback",
                                   "preset", "theme", "render", "variants", "rush", "fit_box", "panel", "card", "major_share"})
        self.assertEqual(cc["title"], "NORTH TSUKUBA")
        self.assertEqual(list(cc["variants"]), ["day", "rush"])
        self.assertEqual(cc["variants"]["rush"]["render"]["DURATION_FRAMES"], 750)
        self.assertEqual(cc["variants"]["day"]["label"], "An average {month} weekday")
        self.assertEqual(cc["panel"]["preferred"], "right")
        bc = self.pl.basemap_config(self.batch, recipe, self.lock)
        self.assertEqual(set(bc), {"schema", "id", "origin", "clip", "basemap_dir", "basemap", "gzip", "boundary", "built_dir"})
        p = 14.5 / 1920 / 1.1
        self.assertAlmostEqual(bc["basemap"]["min_road_km"], 1.2 * p, places=6)
        self.assertAlmostEqual(bc["basemap"]["min_water_area_km2"], 14 * p * p, places=6)

    def test_render_query(self):
        r = self.pl.recipe("test-north", self.batch)
        r = json.loads(json.dumps(r))
        r["override"]["render"] = {"BUS_HALO_R": 12}
        r["override"]["variant_render"] = {"day": {"FRAME_ZOOM": 0.9, "TRAIL_MINUTES": 10}, "rush": {"FRAME_ZOOM": 1.2}}
        r["override"]["brand_colors"] = {"brampton:zum": "#E31837", "yrt": "0058a9"}
        rj, bh = self.pl.render_query(r, "day")
        self.assertEqual(rj, {"CARD_LINES": 1, "BUS_HALO_R": 12, "FRAME_ZOOM": 0.945, "TRAIL_MINUTES": 10})
        rj, _ = self.pl.render_query(r, "rush")
        self.assertEqual(rj["FRAME_ZOOM"], 1.2)
        self.assertEqual(bh, "brampton:zum:e31837;yrt:0058a9")

    def test_render_key_scope(self):
        recipe = self.pl.recipe("test-centre", self.batch)
        k = self.pl.render_key(self.batch, recipe, "day", self.lock)
        r2 = json.loads(json.dumps(recipe))
        r2["override"]["_why"] = {"BUS_HALO_R": "a reason changes no pixel"}
        self.assertEqual(self.pl.render_key(self.batch, r2, "day", self.lock), k)
        b2 = dict(self.batch, title="Another title", hashtags=["#x"], category="Education")
        self.assertEqual(self.pl.render_key(b2, recipe, "day", self.lock), k)
        b3 = dict(self.batch, render_epoch=1)
        self.assertNotEqual(self.pl.render_key(b3, recipe, "day", self.lock), k)
        self.assertNotEqual(self.pl.render_key(self.batch, recipe, "rush", self.lock), k)


# ------------------------------------------------------------------ D-4 caching, D-5 frozen

class Caching(Scratch):
    def setUp(self):
        super().setUp()
        self.repo = FakeRepo(self.tmp)
        self.repo.run("lock", "test")

    def keys(self):
        info = self.repo.show("test")
        return {f"{c}-{v}": e["render_key"] for c, vs in info["videos"].items() for v, e in vs.items() if "render_key" in e}

    def test_second_build_runs_nothing(self):
        self.repo.run("build", "test")
        calls = self.repo.calls()
        self.assertEqual(sum(c.startswith("build_area") for c in calls), 2)
        self.assertEqual(sum(c.startswith("trim test-centre") for c in calls), 2)
        self.assertEqual(sum(c.startswith("trim test-north") for c in calls), 1)
        self.assertEqual(sum(c.startswith("basemap") for c in calls), 2)
        self.assertTrue(any(c.startswith("fetch_overture") for c in calls))
        self.repo.clear_calls()
        out = self.repo.run("build", "test").stdout
        self.assertEqual(self.repo.calls(), [])
        self.assertIn("up to date", out)
        man = self.repo.read_json("build/test-centre/manifest.json")
        self.assertEqual(sorted(man["keys"]), ["basemap", "boundary", "trim-day", "trim-week"])
        self.assertEqual(man["build_key"], man["keys"]["trim-day"])
        net = make.Pipeline(self.repo.root).network_meta(os.path.join(self.repo.root, "build/test-centre/day/network.json.gz"))
        self.assertEqual(net["build_key"], man["keys"]["trim-day"])
        lock = self.repo.read_json("cities/locks/test.lock.json")
        self.assertIn("tsukubus", lock["areas"]["tsukuba"]["dates"]["day"])
        self.assertEqual(lock["areas"]["tsukuba"]["rules"]["week"]["tsukubus"]["mon"], "half")
        self.assertTrue(lock["areas"]["tsukuba"]["overture"]["files"]["segments"])

    def test_override_reruns_only_that_city(self):
        self.repo.run("build", "test")
        before = self.keys()
        r = self.repo.read_json("cities/recipes/test-north.json")
        r["override"]["render"]["BUS_HALO_R"] = 12
        self.repo.write_json("cities/recipes/test-north.json", r)
        self.repo.clear_calls()
        self.repo.run("build", "test")
        self.assertEqual(self.repo.calls(), [], "an override is a query change, never a rebuild")
        after = self.keys()
        for stem in before:
            if stem.startswith("test-north"):
                self.assertNotEqual(before[stem], after[stem], stem)
            else:
                self.assertEqual(before[stem], after[stem], stem)

    def test_adding_a_city_changes_no_other_key(self):
        self.repo.run("build", "test")
        before = self.keys()
        r = self.repo.read_json("cities/recipes/test-north.json")
        r.update(id="test-west", place="West Tsukuba")
        self.repo.write_json("cities/recipes/test-west.json", r)
        b = self.repo.read_json("cities/batches/test.json")
        b["cities"].append("test-west")
        self.repo.write_json("cities/batches/test.json", b)
        self.repo.run("lock", "test")
        self.repo.clear_calls()
        self.repo.run("build", "test")
        self.assertEqual(sorted(c for c in self.repo.calls() if c.startswith(("trim", "build_area"))),
                         ["trim test-west day"])
        after = self.keys()
        for stem, k in before.items():
            self.assertEqual(after[stem], k, stem)

    def test_frozen(self):
        self.repo.run("build", "test")
        self.repo.run("fetch", "test", "--frozen")
        # D-5: a changed zip at the source; a damaged cache copy is simply replaced
        cache = os.path.join(self.repo.root, "cache/feeds/tsukuba/tsukubus.zip")
        src = os.path.join(self.repo.root, "data/gtfs/tsukubus.zip")
        good_zip = read_bytes(src)
        os.remove(cache)
        with open(cache, "wb") as fh:
            fh.write(b"damaged")
        self.repo.run("fetch", "test", "--frozen")
        self.assertEqual(read_bytes(cache), good_zip)
        os.remove(cache)
        with open(src, "wb") as fh:
            fh.write(feed_zip("tsukubus", end="20271230"))
        res = self.repo.run("fetch", "test", "--frozen", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("bytes changed", res.stderr)
        with open(src, "wb") as fh:
            fh.write(good_zip)
        self.repo.run("fetch", "test", "--frozen")
        # changed dates
        lock = self.repo.read_json("cities/locks/test.lock.json")
        good = json.loads(json.dumps(lock))
        lock["areas"]["tsukuba"]["dates"]["day"]["tsukubus"] = lock["areas"]["tsukuba"]["dates"]["day"]["tsukubus"][1:]
        self.repo.write_json("cities/locks/test.lock.json", lock)
        res = self.repo.run("build", "test", "--frozen", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("dates", res.stderr)
        self.repo.write_json("cities/locks/test.lock.json", good)
        # a different Python patch only warns
        good["tools"]["python"] = "3.13.0"
        self.repo.write_json("cities/locks/test.lock.json", good)
        res = self.repo.run("build", "test", "--frozen")
        self.assertIn("warning: python is", res.stderr)
        # a changed pinned package fails
        self.repo.write("requirements.txt", "numpy==0.0.1 \\\n    --hash=sha256:" + "0" * 64 + "\n")
        res = self.repo.run("build", "test", "--frozen", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("numpy is", res.stderr)

    def test_no_upstream(self):
        self.repo.run("build", "test")
        self.repo.clear_calls()
        self.repo.run("build", "test", "--city", "test-centre", "--frozen", "--no-upstream")
        self.assertEqual(self.repo.calls(), [])
        self.repo.run("render", "test-centre", "--variant", "day", "--no-upstream")
        # A changed network fails --no-upstream instead of being rebuilt.
        p = os.path.join(self.repo.root, "build/test-centre/day/network.json.gz")
        with open(p, "ab") as fh:
            fh.write(b"\0")
        res = self.repo.run("render", "test-centre", "--variant", "rush", "--no-upstream", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("differs from build/test-centre/manifest.json", res.stderr)
        # Without the week area store, the build job skips the week trim (no week render pending).
        shutil.rmtree(os.path.join(self.repo.root, "build/areas/tsukuba/week"))
        os.remove(p)
        self.repo.run("build", "test", "--city", "test-centre")
        shutil.rmtree(os.path.join(self.repo.root, "build/areas/tsukuba/week"))
        res = self.repo.run("build", "test", "--city", "test-centre", "--no-upstream")
        self.assertIn("skipping the week trim", res.stdout)
        res = self.repo.run("build", "test", "--city", "test-centre", "--area-only", "--no-upstream", check=False)
        self.assertNotEqual(res.returncode, 0)

    def test_week_not_eligible(self):
        res = self.repo.run("build", "test", env={"STUB_NOWEEK": "test-centre"})
        self.assertIn("week not eligible", res.stdout)
        lock = self.repo.read_json("cities/locks/test.lock.json")
        self.assertIs(lock["boundaries"]["test-centre"]["week_eligible"], False)
        res = self.repo.run("render", "test-centre", "--variant", "week")
        self.assertIn("not eligible", res.stdout)
        self.assertNotIn("test-centre-week", self.repo.show("test")["publish_order"])


# ------------------------------------------------------------------ renders, meta, Actions (D-6 partly, D-7, D-10)

class Actions(Scratch):
    def setUp(self):
        super().setUp()
        self.repo = FakeRepo(self.tmp)
        self.repo.run("lock", "test")
        self.repo.run("build", "test")
        self.repo.commit("lock")

    def render_all(self, review=None):
        for stem in self.repo.show("test")["publish_order"]:
            city, v = stem.rsplit("-", 1)
            extra = ["--review-dir", review] if review else []
            self.repo.run("render", city, "--variant", v, "--no-upstream", *extra)
            self.repo.run("meta", "test", "--only", stem, "--no-upstream", *extra)

    def plan(self, env=None):
        out = os.path.join(self.tmp, "gh_output")
        if os.path.exists(out):
            os.remove(out)
        self.repo.run("plan", "--from-branch", "batch/test", "--github-output", out, env=env)
        res = {}
        for ln in read_text(out).splitlines():
            k, v = ln.split("=", 1)
            res[k] = v if k in ("batch", "feed_refspecs", "publish") else json.loads(v)
        return res

    def test_render_outputs(self):
        self.repo.run("render", "test-centre", "--variant", "day", "--review-dir", "review")
        o = os.path.join(self.repo.root, "out/shorts/test")
        side = load(os.path.join(o, "test-centre-day.json"))
        nm = load(os.path.join(o, "test-centre-day.netmeta.json"))
        self.assertEqual(side["key"], self.repo.show("test")["videos"]["test-centre"]["day"]["render_key"])
        self.assertEqual(set(nm) >= {"id", "variant", "place", "title", "label", "month_label", "peak", "am_peak",
                                     "pm_peak", "feeds", "modes_present", "groups", "credit", "seconds", "build_key"}, True)
        self.assertEqual(nm["seconds"], 50)
        self.assertEqual(nm["modes_present"], ["bus"])
        self.assertTrue(os.path.exists(os.path.join(self.repo.root, "review/test-centre-day/f0300.png")))
        self.repo.clear_calls()
        self.assertIn("up to date", self.repo.run("render", "test-centre", "--variant", "day").stdout)
        self.assertEqual([c for c in self.repo.calls() if c.startswith("render")], [])

    def test_render_command_line(self):
        res = self.repo.run("render", "test-north", "--variant", "day")
        line = next(ln for ln in res.stdout.splitlines() if "render_video.mjs" in ln)
        # The capture method is one line in defaults.json (C3), so the test follows it.
        d = load(os.path.join(self.repo.root, "cities/defaults.json"))["render"]
        capture = [f"--capture {d['capture']}"] + ([f"--jobs {d['jobs']}"] if d["capture"] == "raw" else [])
        for want in ["--tier final", "--data build/test-north/day/network.json.gz", "--basemap build/test-north/basemap.json.gz",
                     "--variant day", "--render-json", "--min-kbps 8000", "--crf-ladder 18,16,14,12,10",
                     "--keep-frames 0,300,last", "--key "] + capture:
            self.assertIn(want, line)
        if d["capture"] != "raw":
            self.assertNotIn("--jobs", line)
        self.assertIn('"FRAME_ZOOM":1.05', line.replace(" ", "").replace('\\"', '"'))

    def test_meta_and_csv(self):
        self.repo.run("meta", "test")
        o = os.path.join(self.repo.root, "out/shorts/test")
        with open(os.path.join(o, "test-youtube.csv"), "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual([r["file"] for r in rows], ["test-centre-day.mp4", "test-north-day.mp4", "test-centre-rush.mp4",
                                                      "test-north-rush.mp4", "test-centre-week.mp4"])
        self.assertEqual(rows[0]["made_for_kids"], "no")
        self.assertEqual(rows[0]["visibility"], "private")
        m = load(os.path.join(o, "test-centre-day.meta.json"))
        self.assertEqual(m["title"], "Every bus in Tsukuba in 24 hours")
        self.assertEqual(m["hashtags"][0], "#shorts")
        self.assertIn("Sports Day (October 12)", m["description"])

    def test_plan_on_a_fresh_clone_equals_show(self):
        """D-10: plan computes the keys from inputs only, on a clone with no build/ and no cache/."""
        keys = {f"{c}-{v}": e["render_key"] for c, vs in self.repo.show("test")["videos"].items() for v, e in vs.items()}
        clone = os.path.join(self.tmp, "clone")
        git(self.tmp, "clone", "-q", "-b", "batch/test", self.repo.root, clone)
        out = os.path.join(self.tmp, "out.txt")
        self.repo.run("plan", "--from-branch", "batch/test", "--github-output", out, root=clone)
        plan = dict(ln.split("=", 1) for ln in read_text(out).splitlines())
        renders = json.loads(plan["renders"])["include"]
        self.assertEqual({f"{r['city']}-{r['variant']}": r["key"] for r in renders}, keys)
        self.assertFalse(os.path.exists(os.path.join(clone, "build")))
        areas = json.loads(plan["areas"])["include"]
        self.assertEqual({(a["area"], a["timeline"]) for a in areas}, {("tsukuba", "day"), ("tsukuba", "week")})
        builds = json.loads(plan["builds"])["include"]
        self.assertEqual([b["city"] for b in builds], ["test-centre", "test-north"])
        self.assertEqual(builds[1]["week_key"], "")
        self.assertEqual(plan["publish"], "true")
        self.assertEqual(plan["feed_refspecs"], "")

    def test_release_flow(self):
        """D-7 on a fake GitHub: skipping by label, epoch bump, template-only change, cancelled swap, review branch."""
        env = gh_env(self.repo)
        p = self.plan(env)
        self.assertEqual(len(p["renders"]["include"]), 5)
        self.render_all(review="review")
        for stem in self.repo.show("test")["publish_order"]:
            self.repo.run("release", "test", "--upload", stem, env=env)
        p = self.plan(env)
        self.assertEqual(p["renders"]["include"], [], "a push with no change renders nothing")
        self.assertEqual(p["publish"], "true", "the CSV is not published yet")
        self.repo.run("release", "test", "--publish", env=env)
        st = load(os.path.join(self.tmp, "gh", "state.json"))
        names = sorted(a["name"] for a in st["assets"][str(st["releases"][0]["id"])])
        self.assertEqual(len([n for n in names if n.endswith(".mp4")]), 5)
        self.assertIn("test-youtube.csv", names)
        self.assertEqual(len([n for n in names if n.endswith(".netmeta.json")]), 5)
        # GitHub lists the label in place of the file name, so it leads with the name.
        self.assertTrue(all(re.fullmatch(re.escape(a["name"]) + r" key:[0-9a-f]{16}", a["label"])
                            for a in st["assets"][str(st["releases"][0]["id"])]))
        self.assertIn("| 1 | test-centre-day |", st["releases"][0]["body"])
        p = self.plan(env)
        self.assertEqual((p["renders"]["include"], p["publish"]), ([], "false"))
        # template-only change: publish, no render
        t = self.repo.read_json("cities/templates/shorts_en.json")
        t["title"]["day"] = "Every {modes_singular} in {place}, one day"
        self.repo.write_json("cities/templates/shorts_en.json", t)
        p = self.plan(env)
        self.assertEqual((p["renders"]["include"], p["publish"]), ([], "true"))
        # render_epoch bump: everything renders
        b = self.repo.read_json("cities/batches/test.json")
        b["render_epoch"] = 1
        self.repo.write_json("cities/batches/test.json", b)
        p = self.plan(env)
        self.assertEqual(len(p["renders"]["include"]), 5)
        # A job cancelled inside a swap leaves the old asset or a .part, never a half-replaced file; the next plan
        # deletes the .part and renders again whatever lost its MP4.
        self.repo.run("render", "test-centre", "--variant", "day")
        self.repo.run("meta", "test", "--only", "test-centre-day")
        state = os.path.join(self.tmp, "gh", "state.json")
        res = self.repo.run("release", "test", "--upload", "test-centre-day", env=dict(env, FAKE_GH_FAIL="rename"), check=False)
        self.assertNotEqual(res.returncode, 0)
        st = load(state)
        names = [a["name"] for a in st["assets"][str(st["releases"][0]["id"])]]
        self.assertIn("test-centre-day.json.part", names)
        self.assertNotIn("test-centre-day.json", names)
        p = self.plan(env)
        st = load(state)
        names = [a["name"] for a in st["assets"][str(st["releases"][0]["id"])]]
        self.assertFalse([n for n in names if n.endswith(".part")])
        self.assertIn({"city": "test-centre", "variant": "day"},
                      [{k: r[k] for k in ("city", "variant")} for r in p["renders"]["include"]])
        # Stopped after the sidecars, before the MP4: the MP4 is uploaded last, and
        # plan wants the key on the MP4 and both sidecars, so the video stays pending.
        self.render_all()
        for stem in self.repo.show("test")["publish_order"]:
            self.repo.run("release", "test", "--upload", stem, env=env)
        b["render_epoch"] = 2
        self.repo.write_json("cities/batches/test.json", b)
        self.repo.run("render", "test-centre", "--variant", "day")
        self.repo.run("meta", "test", "--only", "test-centre-day")
        res = self.repo.run("release", "test", "--upload", "test-centre-day",
                            env=dict(env, FAKE_GH_FAIL="upload:test-centre-day.mp4.part"), check=False)
        self.assertNotEqual(res.returncode, 0)
        st = load(state)
        lab = {a["name"]: a["label"] for a in st["assets"][str(st["releases"][0]["id"])]}
        self.assertNotEqual(make.label_key(lab["test-centre-day.json"]), make.label_key(lab["test-centre-day.mp4"]))
        p = self.plan(env)
        self.assertIn({"city": "test-centre", "variant": "day"},
                      [{k: r[k] for k in ("city", "variant")} for r in p["renders"]["include"]])
        # review branch: one commit, under the cap, frames only for the review videos
        self.repo.run("review-push", "test", "--from", "review", env=env)
        tree = git(self.repo.origin, "ls-tree", "-r", "-l", "review/test").stdout.split("\n")
        files = [ln.split("\t")[1] for ln in tree if ln]
        self.assertIn("test-centre-day/f0300.png", files)
        self.assertIn("test-north-day/thumb-f0300.jpg", files)
        self.assertNotIn("test-north-day/f0300.png", files)
        self.assertIn("test-north-rush/test-north-rush.meta.json", files)
        self.assertLess(sum(int(ln.split()[3]) for ln in tree if ln), 40e6)
        # A later run that rendered one video keeps the other videos' files from the branch.
        only = os.path.join(self.tmp, "review2")
        shutil.copytree(os.path.join(self.repo.root, "review", "test-north-rush"), os.path.join(only, "test-north-rush"))
        self.repo.run("review-push", "test", "--from", only, env=env)
        files2 = [ln.split("\t")[1] for ln in git(self.repo.origin, "ls-tree", "-r", "review/test").stdout.split("\n") if ln]
        self.assertEqual(sorted(files2), sorted(files))
        index = json.loads(git(self.repo.origin, "show", "review/test:index.json").stdout)
        self.assertEqual(index["rendered_now"], ["test-north-rush"])

    def test_feed_tags_are_created(self):
        """plan creates a missing feed tag from the lock's commit (D4)."""
        tag_commit = git(self.repo.root, "rev-parse", "HEAD").stdout.strip()
        git(self.repo.root, "tag", "feeds/test-1", tag_commit)
        b = self.repo.read_json("cities/batches/test.json")
        b["areas"][0]["feeds"][0]["source"] = {"tag": "feeds/test-1", "path": "data/gtfs/tsukubus.zip"}
        self.repo.write_json("cities/batches/test.json", b)
        self.repo.run("lock", "test")
        lock = self.repo.read_json("cities/locks/test.lock.json")
        self.assertEqual(lock["areas"]["tsukuba"]["feeds"]["tsukubus"]["commit"], tag_commit)
        p = self.plan(gh_env(self.repo))
        self.assertEqual(p["feed_refspecs"], "refs/tags/feeds/test-1:refs/tags/feeds/test-1")
        st = load(os.path.join(self.tmp, "gh", "state.json"))
        self.assertEqual(st["refs"], [{"ref": "refs/tags/feeds/test-1", "sha": tag_commit}])
        # fetch extracts the zip from the tag with git archive when no local copy matches
        shutil.rmtree(os.path.join(self.repo.root, "cache/feeds"))
        os.remove(os.path.join(self.repo.root, "data/gtfs/tsukubus.zip"))
        res = self.repo.run("fetch", "test", "--feeds-only")
        self.assertIn("feed tsukubus: git archive feeds/test-1", res.stdout)

    def test_url_sources(self):
        """A URL feed is downloaded once by lock, checked against the batch's sha256, and cached for fetch."""
        z = os.path.join(self.tmp, "mirror", "tsukubane.zip")
        os.makedirs(os.path.dirname(z))
        with open(z, "wb") as fh:
            fh.write(feed_zip("tsukubane"))
        sha = make.sha256_file(z)
        b = self.repo.read_json("cities/batches/test.json")
        b["areas"][0]["feeds"][1]["source"] = {"url": "file://" + z, "sha256": "0" * 64}
        self.repo.write_json("cities/batches/test.json", b)
        res = self.repo.run("lock", "test", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("the batch pins", res.stderr)
        b["areas"][0]["feeds"][1]["source"]["sha256"] = sha
        self.repo.write_json("cities/batches/test.json", b)
        self.repo.run("lock", "test")
        fe = self.repo.read_json("cities/locks/test.lock.json")["areas"]["tsukuba"]["feeds"]["tsukubane"]
        self.assertEqual((fe["source"], fe["url"], fe["sha256"]), ("url", "file://" + z, sha))
        os.remove(z)
        self.repo.run("fetch", "test", "--feeds-only", "--frozen")

    def test_data_tag_round_trip(self):
        # fetch --push-data-tag pushes the cached, locked extract when origin lacks the tag, and not again.
        res = self.repo.run("fetch", "test", "--overture-only", "--push-data-tag")
        self.assertIn("pushed overture/tsukuba-2026-09-23.1", res.stdout)
        res = self.repo.run("fetch", "test", "--overture-only", "--push-data-tag")
        self.assertIn("is already on origin", res.stdout)
        tree = git(self.repo.origin, "ls-tree", "-r", "--name-only", "overture/tsukuba-2026-09-23.1").stdout.split()
        self.assertEqual(sorted(tree), ["2026-09-23.1/tsukuba/segments.geojson.gz.000",
                                        "2026-09-23.1/tsukuba/water.geojson.gz.000", "MANIFEST.json", "README.md"])
        pl = make.Pipeline(self.repo.root)
        rels = ["cache/overture/2026-09-23.1/tsukuba/segments.geojson", "cache/overture/2026-09-23.1/tsukuba/water.geojson"]
        want = {r: pl.sha(r) for r in rels}
        pl.push_data_tag("tsukuba", "2026-09-23.1", rels)
        self.assertIn("overture/tsukuba-2026-09-23.1", git(self.repo.origin, "tag").stdout)
        clone = os.path.join(self.tmp, "clone")
        git(self.tmp, "clone", "-q", "-b", "batch/test", self.repo.origin, clone)
        pc = make.Pipeline(clone)
        self.assertTrue(pc.restore_data_tag("overture/tsukuba-2026-09-23.1", want))
        for r, s in want.items():
            self.assertEqual(make.sha256_file(os.path.join(clone, r)), s)


if __name__ == "__main__":
    unittest.main()

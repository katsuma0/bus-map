"""Shared helpers of the part A tests: configs as make.py derives them, and running the builders.

The city and area configs below are a stand-in for D's derivation (spec 2.7
and D3) so A's builders can be tested before make.py exists: same keys, the
defaults of cities/defaults.json, the GTA batch, recipes and brands from
tests/fixtures/gta/.
"""
import gzip
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
GTA_FIX = os.path.join(ROOT, "tests", "fixtures", "gta")
GTA_GTFS = os.path.join(ROOT, "data", "gta", "gtfs")
DIVISIONS = os.path.join(ROOT, "cache", "overture", "2026-09-23.1", "divisions", "CA-ON.geojson")
TEST_BUILD = os.path.join(ROOT, "build", "test_a")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

MODES = [
    {"id": "bus", "label": "buses", "singular": "bus", "route_types": [3, 700, 701, 702, 704, 11]},
    {"id": "streetcar", "label": "streetcars", "singular": "streetcar", "route_types": [0, 5, 900]},
    {"id": "rail", "label": "trains", "singular": "train", "route_types": [1, 2, 100, 109, 400]},
]
TIMELINE = {"drop_pct": 8, "min_week_dates": 2, "fallback_days": 28, "guard": {"low": 0.90, "high": 1.03, "min_dates": 3}}
LABELS = {"day": "An average {month} weekday", "rush": "Morning rush, an average {month} weekday", "week": "An average {month} week"}
VARIANTS = {
    "day": {"frames": 1500, "start": "am_peak", "render": {"LOOP": "wrap", "TIME_WARP_MODE": "activity", "SPARK_SMOOTH_MIN": 35}},
    "rush": {"frames": 750, "start": 23400, "end": 34200,
             "render": {"LOOP": "xfade", "TIME_WARP_MODE": "linear", "SPARK_SMOOTH_MIN": 15, "TRAIL_MINUTES": 8}},
    "week": {"frames": 1800, "start": "am_peak",
             "render": {"LOOP": "wrap", "TIME_WARP_MODE": "activity-daily", "TIME_WARP_FLOOR": 0.3, "SPARK_SMOOTH_MIN": 60,
                        "TRAIL_MINUTES": 30, "BUS_HALO_ALPHA": 0.1, "BUS_CORE_R": 0, "WEEKEND_BAND": True, "CLOCK_ROUND": 60}},
}
CARD = {v: [["Every {modes_singular} in {place}", "Busiest at {peak_time} with {peak_count} vehicles"]] for v in VARIANTS}
GTA_HOLIDAYS = {"2026-10-12": "Thanksgiving", "2026-12-25": "Christmas Day", "2026-12-28": "Boxing Day (observed)"}


def load_json(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def area_config(area_id, origin, area_box, gtfs_dir, feeds, kind, month="2026-10", holidays=None,
                timezone="America/Toronto", chunk=2_000_000, modes=MODES):
    return {"schema": 4, "kind": "area", "id": area_id, "origin": list(origin), "area_box": list(area_box),
            "gtfs_dir": gtfs_dir, "feeds": feeds, "modes": modes,
            "timeline": dict(TIMELINE, kind=kind, month=month, timezone=timezone,
                             holidays=dict(holidays if holidays is not None else {"2026-10-12": "Thanksgiving"})),
            "stop_times_chunk": chunk}


def fit_frame(bbox_km, fit_box=(50, 390, 870, 1300)):
    """D3.2."""
    x0, y0, x1, y1 = bbox_km
    s = min((fit_box[2] - fit_box[0]) / (x1 - x0), (fit_box[3] - fit_box[1]) / (y1 - y0))
    kv = math.ceil(1920 / s / 0.5) * 0.5
    s = 1920 / kv
    return {"km_vertical": kv, "center_km": [round((x0 + x1) / 2 + 80 / s, 1), round((y0 + y1) / 2 - 115 / s, 1)]}


def boundary_bbox_km(path, origin):
    import numpy as np
    import shapely
    from shapely.geometry import shape
    import build_network as bn
    bn.set_origin(origin)
    feat = load_json(path)
    if feat.get("type") == "FeatureCollection":
        feat = feat["features"][0]
    g = shapely.transform(shape(feat["geometry"]), lambda c: np.column_stack(bn.to_km(c[:, 0], c[:, 1])))
    return list(g.bounds)


def city_config(cid, place, origin, boundary_file, frame, variants, kind, batch="gta", area="gta",
                brands=os.path.join(GTA_FIX, "brands.json"), panel_side="left", rush=None):
    """City config of spec 2.7 for one timeline; `variants` lists the variants of that timeline."""
    V = {}
    for v in variants:
        d = VARIANTS[v]
        r = dict(d["render"], DURATION_FRAMES=d["frames"], HOLD_START=0, HOLD_END=0)
        V[v] = {"start": d["start"], "label": LABELS[v], "frame": None, "render": r}
        if "end" in d:
            V[v]["end"] = d["end"]
        if v == "rush" and rush and rush.get("frame"):
            V[v]["frame"] = rush["frame"]
    return {"schema": 4, "kind": "city", "id": cid, "batch": batch, "area": area, "place": place, "title": place.upper(),
            "origin": list(origin), "frame": frame, "trim_scale": 1.25,
            "boundary": {"file": boundary_file, "name": place, "simplify_km": 0.02, "mask_km": 0.025},
            "brands": brands, "group_by": {"field": "agency-auto", "min_share": 0.03, "max_groups": 3},
            "credit_template": "Data: {agencies} · Map: Overture, OSM",
            "credit_fallback": "Data: {n} transit agencies · Map: Overture, OSM",
            "preset": "shorts", "theme": {"batch": "lake"}, "render": {}, "variants": V,
            "rush": {"auto": (rush or {}).get("auto", True), "zoom": [1.4, 2.2], "share": 0.6},
            "panel": {"preferred": panel_side, "tie": 0.10, "rect": [60, 1140, 620, 1500]},
            "card": {"title": place.upper(), "templates": {v: CARD[v] for v in variants}}, "major_share": 0.05}


def run(script, *args, seed="0", check=True):
    """Run one builder as make.py does: sys.executable -I, PYTHONHASHSEED set. Returns (proc, seconds, peak GB)."""
    env = dict(os.environ, PYTHONHASHSEED=seed)
    probe = ("import resource, subprocess, sys\n"
             "p = subprocess.run(sys.argv[1:])\n"
             "print('PEAK_KB', resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, file=sys.stderr)\n"
             "sys.exit(p.returncode)\n")
    t0 = time.time()
    p = subprocess.run([sys.executable, "-I", "-c", probe, sys.executable, "-I", os.path.join(SCRIPTS, script), *args],
                       env=env, capture_output=True, text=True)
    secs = time.time() - t0
    peak = 0.0
    lines = []
    for ln in p.stderr.splitlines():
        if ln.startswith("PEAK_KB "):
            peak = int(ln.split()[1]) / 1e6
        else:
            lines.append(ln)
    p.stderr = "\n".join(lines)
    if check and p.returncode != 0:
        raise RuntimeError(f"{script} {' '.join(args)} exited {p.returncode}:\n{p.stderr[-3000:]}")
    return p, secs, peak


def gta_batch():
    return load_json(os.path.join(GTA_FIX, "batches", "gta.json"))


def gta_recipe(cid):
    return load_json(os.path.join(GTA_FIX, "recipes", f"{cid}.json"))


def gta_area_config(kind):
    b = gta_batch()
    a = b["areas"][0]
    feeds = [{k: v for k, v in f.items() if k != "source"} for f in a["feeds"]]
    return area_config(a["id"], a["origin"], a["area_box"], GTA_GTFS, feeds, kind, month=b["month"],
                       holidays=GTA_HOLIDAYS, timezone=a["timezone"], modes=b["modes"])


def ensure_divisions():
    if not os.path.exists(DIVISIONS):
        run("fetch_boundary.py", "fetch", "--release", "2026-09-23.1", "--country", "CA", "--region", "CA-ON",
            "--bbox", "-80.12,43.01,-78.52,44.30", "--out", DIVISIONS)
    return DIVISIONS


def gta_city(cid, out_root):
    """Select the boundary and write city.day.json / city.week.json for one GTA recipe; returns their paths."""
    b, rec = gta_batch(), gta_recipe(cid)
    a = b["areas"][0]
    bfile = os.path.join(out_root, cid, "boundary.geojson")
    run("fetch_boundary.py", "select", "--in", ensure_divisions(), "--name", rec["boundary"]["name"],
        "--subtypes", ",".join(rec["boundary"].get("subtypes", ["locality", "localadmin", "county"])),
        "--area-km2", str(rec["boundary"]["area_km2"]), "--out", bfile)
    frame = rec.get("frame") or fit_frame(boundary_bbox_km(bfile, a["origin"]))
    variants = rec.get("variants") or b["variants_default"]
    paths = {}
    for kind, vs in (("day", [v for v in variants if v in ("day", "rush")]), ("week", [v for v in variants if v == "week"])):
        if not vs:
            continue
        cfg = city_config(cid, rec["place"], a["origin"], bfile, frame, vs, kind,
                          panel_side=rec.get("variety", {}).get("panel_side", "left"), rush=rec.get("rush"))
        paths[kind] = os.path.join(out_root, cid, f"city.{kind}.json")
        dump_json(paths[kind], cfg)
    return paths


def gta_store(kind):
    """build/test_a/areas/gta/<kind>, built once per config and reused while the config is unchanged."""
    import area_store
    out = os.path.join(TEST_BUILD, "areas", "gta", kind)
    cfg_path = os.path.join(TEST_BUILD, "areas", "gta", f"area.{kind}.json")
    cfg = gta_area_config(kind)
    fresh = os.path.exists(os.path.join(out, "stamp.json")) and os.path.exists(cfg_path) and load_json(cfg_path) == cfg
    if not fresh:
        dump_json(cfg_path, cfg)
        p, secs, peak = run("build_area.py", "--config", cfg_path, "--out", out)
        dump_json(os.path.join(TEST_BUILD, "areas", "gta", f"perf.{kind}.json"), {"seconds": round(secs, 1), "peak_gb": round(peak, 3)})
        sys.stderr.write(p.stderr + "\n")
    return area_store.Store(out)

#!/usr/bin/env python3
"""Shorts pipeline driver: recipes in, cached builds, renders and metadata out.

    python3 scripts/make.py lock <batch> [--refresh]
    python3 scripts/make.py fetch <batch> [--area A] [--feeds-only | --overture-only] [--push-data-tag] [--frozen]
    python3 scripts/make.py build <batch> [--area A] [--city ID] [--area-only] [--timeline day|week] [--frozen] [--no-upstream]
    python3 scripts/make.py stills <id> [--variant V] [--extra-query Q]
    python3 scripts/make.py preview <id> --variant V
    python3 scripts/make.py render <id> --variant V [--tier final] [--no-upstream] [--review-dir DIR]
    python3 scripts/make.py tune <id> [--variant V] [--knob K | --next] [--pick K=a|b|c --why TEXT] [--auto] [--apply]
    python3 scripts/make.py meta <batch> [--only ID-VARIANT] [--from-netmeta DIR] [--no-upstream] [--allow-nc]
    python3 scripts/make.py plan --from-branch batch/<batch> --github-output FILE
    python3 scripts/make.py release <batch> --upload ID-VARIANT | --publish
    python3 scripts/make.py review-push <batch> --from DIR
    python3 scripts/make.py check-legacy [--record]
    python3 scripts/make.py show <batch> | <id>

A global `--cities DIR` (before the command) reads batches, recipes, locks and
brands.json from DIR instead of cities/ (tests/fixtures/gta is laid out that
way); defaults, licences, templates and holidays fall back to cities/.

Every step's key is the sha256 of canonical JSON of its code, inputs and
parameters. Stamps sit next to their outputs (build/areas/<area>/<tl>/stamp.json,
build/<id>/stamps/, build/<id>/manifest.json, the render sidecar), so a cache
or an artifact carries its own proof of what made it. A step whose stamp key
matches and whose outputs still hash to the stamp is skipped.

Subprocesses run as `sys.executable -I scripts/<tool>.py` or
`node scripts/render_video.mjs` with argument lists and PYTHONHASHSEED=0.
"""

import argparse
import copy
import datetime
import glob
import gzip
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
# `python3 -I` leaves the script's own directory off sys.path.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VARIANTS = ("day", "rush", "week")
TIMELINE_OF = {"day": "day", "rush": "day", "week": "week"}
THEMES = ("lake", "ink", "sodium", "slate", "teal")
SUBTYPES = ("locality", "localadmin", "county")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
FEED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TAG_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# Bumped when a derivation rule in this file changes what a video looks like,
# because render keys are computed from inputs and would not notice.
DERIVE_VERSION = 1
KY = 110.574
FPS = 30
WIDTH, HEIGHT = 1080, 1920

# Code whose bytes decide outputs (F1). Every file must exist: one hashed as
# "missing" would keep its key steady while a renamed copy changed.
A_FILES = ["scripts/build_area.py", "scripts/composite.py", "scripts/area_store.py", "scripts/trim_network.py",
           "scripts/fetch_boundary.py", "scripts/basemap_v4.py"]
LEGACY_CODE = ["scripts/build_network.py", "scripts/build_basemap.py", "scripts/fetch_overture.py"]
C_GLOBS = ["scripts/render_video.mjs", "scripts/capture/**"]
B_GLOBS = ["web/**"]
LOCKED_DEPS = ["requirements.txt", "package-lock.json"]
AREA_CODE = ["scripts/build_area.py", "scripts/composite.py", "scripts/area_store.py", "scripts/build_network.py",
             "requirements.txt"]
# The trim measures the credit with InterX, so the font file is part of its key.
TRIM_CODE = ["scripts/trim_network.py", "scripts/area_store.py", "scripts/composite.py", "scripts/build_network.py",
             "web/fonts/InterX.woff2", "requirements.txt"]
BOUNDARY_CODE = ["scripts/fetch_boundary.py"]
BASEMAP_CODE = ["scripts/basemap_v4.py", "scripts/build_basemap.py", "requirements.txt"]


class MakeError(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ JSON and hashes

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj):
    return sha256_bytes(canonical(obj).encode("utf-8"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(obj):
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def write_text_if_changed(path, text):
    """Write only when the content differs, so mtimes (and the hash memo) stay put."""
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return True


def write_json(path, obj):
    return write_text_if_changed(path, dump_json(obj))


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class HashMemo:
    """sha256 of files, memoised in cache/hashes.json by (path, size, mtime_ns)."""

    def __init__(self, root):
        self.root = root
        self.path = os.path.join(root, "cache", "hashes.json")
        self.dirty = False
        try:
            self.table = read_json(self.path)
        except (FileNotFoundError, ValueError):
            self.table = {}

    def key(self, path):
        ap = os.path.abspath(path)
        rel = os.path.relpath(ap, self.root)
        return ap if rel.startswith("..") else rel

    def sha(self, path):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return "missing"
        k = self.key(path)
        hit = self.table.get(k)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2]
        digest = sha256_file(path)
        self.table[k] = [st.st_size, st.st_mtime_ns, digest]
        self.dirty = True
        return digest

    def save(self):
        if not self.dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.table, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, self.path)
        self.dirty = False


# ------------------------------------------------------------------ geometry

def kx_of(origin):
    return 111.32 * math.cos(math.radians(origin[1]))


def to_km(origin, lon, lat):
    """The projection of build_network.to_km about an area origin."""
    return (lon - origin[0]) * kx_of(origin), (lat - origin[1]) * KY


def to_deg(origin, x, y):
    return origin[0] + x / kx_of(origin), origin[1] + y / KY


def floor_to(v, step=0.01):
    return round(math.floor(round(v / step, 9)) * step, 6)


def ceil_to(v, step=0.01):
    return round(math.ceil(round(v / step, 9)) * step, 6)


def box_deg_outward(origin, box_km, step=0.01):
    """km box about the origin to degrees, rounded outward to `step`."""
    w, s = to_deg(origin, box_km[0], box_km[1])
    e, n = to_deg(origin, box_km[2], box_km[3])
    return [floor_to(w, step), floor_to(s, step), ceil_to(e, step), ceil_to(n, step)]


def grow_km(box, km):
    return [box[0] - km, box[1] - km, box[2] + km, box[3] + km]


def grow_deg(origin, box, km):
    """Grow a degree box by `km` on every side (x scale at the area origin)."""
    dx, dy = km / kx_of(origin), km / KY
    return [box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy]


def box_inside(inner, outer, eps=1e-9):
    return (inner[0] >= outer[0] - eps and inner[1] >= outer[1] - eps
            and inner[2] <= outer[2] + eps and inner[3] <= outer[3] + eps)


def union_box(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def geometry_rings(geom):
    """[(exterior, [holes])] of a Polygon or MultiPolygon GeoJSON geometry."""
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        raise MakeError(f"boundary geometry must be a Polygon or MultiPolygon, got {geom['type']}")
    return [(p[0], p[1:]) for p in polys]


def geometry_bbox(geom):
    xs, ys = [], []
    for ext, _holes in geometry_rings(geom):
        xs += [c[0] for c in ext]
        ys += [c[1] for c in ext]
    return [min(xs), min(ys), max(xs), max(ys)]


def ring_area(ring, kx):
    s = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        s += (x0 * kx) * (y1 * KY) - (x1 * kx) * (y0 * KY)
    return abs(s) / 2


def geometry_area_km2(geom):
    """Equirectangular area at the bbox-centre latitude, as fetch_boundary (A7)."""
    b = geometry_bbox(geom)
    kx = 111.32 * math.cos(math.radians((b[1] + b[3]) / 2))
    total = 0.0
    for ext, holes in geometry_rings(geom):
        total += ring_area([tuple(c[:2]) for c in ext], kx) - sum(ring_area([tuple(c[:2]) for c in h], kx) for h in holes)
    return total


def fit_frame(bbox_km, fit_box):
    """D3.2: the day frame that puts the boundary bbox into the fit box."""
    x0, y0, x1, y1 = bbox_km
    fw, fh = fit_box[2] - fit_box[0], fit_box[3] - fit_box[1]
    fcx, fcy = (fit_box[0] + fit_box[2]) / 2, (fit_box[1] + fit_box[3]) / 2
    s = min(fw / (x1 - x0), fh / (y1 - y0))
    km_vertical = math.ceil(round(HEIGHT / s / 0.5, 9)) * 0.5
    s = HEIGHT / km_vertical
    cx = (x0 + x1) / 2 + (WIDTH / 2 - fcx) / s
    cy = (y0 + y1) / 2 - (HEIGHT / 2 - fcy) / s
    return {"km_vertical": float(km_vertical), "center_km": [round(cx, 1), round(cy, 1)]}


def trim_box(frame, trim_scale, bbox_km=None):
    """A8.3: frame box scaled by trim_scale plus 1 km, in km about the origin.

    With the boundary's bbox, a box that does not hold it grows to the bbox plus
    1 km, as trim_network does: a pinned frame may show only part of a region
    (the GTA trains video) while the counts still cover all of it.
    """
    kv = frame["km_vertical"]
    cx, cy = frame["center_km"]
    hw = kv * 9 / 16 / 2 * trim_scale + 1
    hh = kv / 2 * trim_scale + 1
    box = [cx - hw, cy - hh, cx + hw, cy + hh]
    if bbox_km is not None and not box_inside(bbox_km, box):
        box = union_box([box, grow_km(bbox_km, 1.0)])
    return box


def frame_box(frame, zoom=1.0, dx=0.0, dy=0.0):
    kv = frame["km_vertical"] / zoom
    cx, cy = frame["center_km"][0] + dx, frame["center_km"][1] + dy
    return [cx - kv * 9 / 32, cy - kv / 2, cx + kv * 9 / 32, cy + kv / 2]


def r6(v):
    return round(float(v), 6)


def camera_path(rid):
    """A city's own camera path (B18): FNV-1a of its id, as web/app.js cameraPathFor() picks it
    for CAMERA_PATH 'auto'. camera_paths() starts from it to spread a batch."""
    h = 2166136261
    for ch in rid.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return CAMERA_PATHS[h % len(CAMERA_PATHS)]


def camera_paths(ids, chosen=None):
    """The camera path of each city of a batch, walked in batch order (B18). A recipe's own
    variety.camera (chosen) is kept; any other city takes camera_path(id) unless more cities
    before it have that path than have the least used one, and then the next least used path in
    CAMERA_PATHS order. The id hash alone put three of the ten GTA cities on the sway and none on
    pull-out-east; this way every prefix of a batch is as even as it can be, and a city appended
    to the batch never moves one before it."""
    chosen = chosen or {}
    count = dict.fromkeys(CAMERA_PATHS, 0)
    n = len(CAMERA_PATHS)
    out = {}
    for rid in ids:
        p = chosen.get(rid)
        if p is None:
            p = camera_path(rid)
            low = min(count.values())
            if count[p] > low:
                i = CAMERA_PATHS.index(p)
                p = next(CAMERA_PATHS[(i + k) % n] for k in range(1, n) if count[CAMERA_PATHS[(i + k) % n]] == low)
        if p in count:
            count[p] += 1
        out[rid] = p
    return out


# ------------------------------------------------------------------ validation (2.1 to 2.3)

RECIPE_KEYS = {"id", "batch", "area", "place", "boundary", "modes", "center", "frame", "variants", "rush", "variety",
               "override"}
BOUNDARY_KEYS = {"name", "subtypes", "area_km2", "file"}
FRAME_KEYS = {"km_vertical", "center_km"}
RUSH_KEYS = {"frame", "auto"}
VARIETY_KEYS = {"panel_side", "card_line", "zoom", "camera"}
# The camera paths of the page (B18), in the order camera_path() picks from:
# web/app.js CAMERA_NAMES lists the same names in the same order.
CAMERA_PATHS = ("pull-out-east", "pull-out-north", "pull-out-west", "pull-out-south", "drift-orbit", "drift-sway")
OVERRIDE_KEYS = {"render", "variant_render", "brand_colors", "_why"}
BATCH_KEYS = {"batch", "title", "theme", "month", "overture_release", "render_epoch", "review_videos", "areas", "modes",
              "cities", "variants_default", "hashtags", "category", "release"}
BATCH_REQUIRED = BATCH_KEYS - {"review_videos"}
AREA_KEYS = {"id", "origin", "timezone", "country", "region", "holidays", "area_box", "feeds", "month"}
AREA_REQUIRED = AREA_KEYS - {"month"}
FEED_KEYS = {"id", "name", "publisher", "licence_id", "licence_text", "allow_nc", "source"}
FEED_REQUIRED = FEED_KEYS - {"allow_nc"}
MODE_KEYS = {"id", "label", "singular", "route_types", "routes"}
MODE_REQUIRED = MODE_KEYS - {"routes"}
# A route rule moves the routes it names into its mode whatever their route_type
# (TTC's Line 5 and Line 6 are light rail with route_type 0, the streetcars' type).
MODE_ROUTE_KEYS = {"feed", "short", "route_id", "why"}
# Source kinds of 2.2, plus "path": a file committed on this branch, used by the
# fixture batch so it needs no tag, URL or key.
SOURCE_KINDS = {
    "tag": ({"tag", "path"}, set()),
    "url": ({"url", "sha256"}, set()),
    "secret": ({"secret", "url"}, {"sha256"}),
    "model": ({"model", "inputs"}, set()),
    "path": ({"path"}, set()),
}
LOCK_KEYS = {"batch", "locked_at", "areas", "boundaries", "tools"}
LOCK_AREA_KEYS = {"feeds", "overture", "divisions", "dates", "rules"}
LOCK_FEED_KEYS = {"source", "tag", "commit", "path", "url", "secret", "sha256", "bytes", "feed_version", "valid", "inputs"}
LOCK_OVERTURE_KEYS = {"release", "bbox", "data_tag", "files"}
LOCK_DIVISIONS_KEYS = {"key", "sha256", "bbox"}
LOCK_BOUNDARY_KEYS = {"id", "division_id", "name", "subtype", "area_km2", "frame", "clip", "file", "sha256", "parts",
                      "week_eligible", "week_why"}


def source_kind(src):
    if not isinstance(src, dict):
        return None
    for kind in ("secret", "model", "tag", "url", "path"):
        if kind in src:
            return kind
    return None


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_pair(v):
    return isinstance(v, list) and len(v) == 2 and all(is_num(x) for x in v)


def is_box(v):
    return isinstance(v, list) and len(v) == 4 and all(is_num(x) for x in v) and v[0] < v[2] and v[1] < v[3]


def unknown(obj, allowed, where, errors):
    for k in sorted(set(obj) - set(allowed)):
        errors.append(f"{where}: unknown key {k!r}")


def missing(obj, required, where, errors):
    for k in sorted(set(required) - set(obj)):
        errors.append(f"{where}: missing key {k!r}")


def validate_frame(fr, where, errors):
    if not isinstance(fr, dict):
        errors.append(f"{where}: must be an object")
        return
    unknown(fr, FRAME_KEYS, where, errors)
    missing(fr, FRAME_KEYS, where, errors)
    if "km_vertical" in fr and not (is_num(fr["km_vertical"]) and fr["km_vertical"] > 0):
        errors.append(f"{where}.km_vertical: must be a positive number")
    if "center_km" in fr and not is_pair(fr["center_km"]):
        errors.append(f"{where}.center_km: must be [x, y]")


def validate_boundary_part(b, where, root, errors):
    if not isinstance(b, dict):
        errors.append(f"{where}: must be an object")
        return
    unknown(b, BOUNDARY_KEYS, where, errors)
    missing(b, {"name", "area_km2"}, where, errors)
    if not isinstance(b.get("name", ""), str) or not b.get("name", "x"):
        errors.append(f"{where}.name: must be a non-empty string")
    if "area_km2" in b and not (is_num(b["area_km2"]) and b["area_km2"] > 0):
        errors.append(f"{where}.area_km2: must be a positive number")
    st = b.get("subtypes", list(SUBTYPES))
    if not (isinstance(st, list) and st and all(s in SUBTYPES for s in st)):
        errors.append(f"{where}.subtypes: must be a non-empty list of {', '.join(SUBTYPES)}")
    if "file" in b and (not isinstance(b["file"], str) or not os.path.isfile(os.path.join(root, b["file"]))):
        errors.append(f"{where}.file: {b.get('file')!r} is not a file in the repo")


def validate_mode_routes(rules, where, feed_ids, errors):
    if not isinstance(rules, list):
        errors.append(f"{where}: must be a list of route rules")
        return
    for j, rule in enumerate(rules):
        rw = f"{where}[{j}]"
        if not isinstance(rule, dict):
            errors.append(f"{rw}: must be an object")
            continue
        unknown(rule, MODE_ROUTE_KEYS, rw, errors)
        if rule.get("feed") not in feed_ids:
            errors.append(f"{rw}.feed: must be a feed id of the batch, got {rule.get('feed')!r}")
        if not ({"short", "route_id"} & set(rule)):
            errors.append(f"{rw}: needs short (a regex on route_short_name) or route_id (a regex on route_id)")
        for k in ("short", "route_id"):
            if k not in rule:
                continue
            if not (isinstance(rule[k], str) and rule[k]):
                errors.append(f"{rw}.{k}: must be a non-empty regular expression")
                continue
            try:
                re.compile(rule[k])
            except re.error as e:
                errors.append(f"{rw}.{k}: not a regular expression ({e})")
        if "why" in rule and not isinstance(rule["why"], str):
            errors.append(f"{rw}.why: must be a string")


def boundary_parts(recipe):
    """The divisions of a recipe's boundary: one object, or the list whose union is the boundary."""
    b = recipe["boundary"]
    return list(b) if isinstance(b, list) else [b]


def boundary_name(recipe):
    """meta.boundary.name and the lock's name: the division's name, or every division's joined with " + "."""
    return " + ".join(p["name"] for p in boundary_parts(recipe))


def recipe_modes(recipe, batch):
    """The mode ids a video keeps, in batch order: the recipe's `modes`, else every mode of the batch."""
    keep = recipe.get("modes")
    return [m["id"] for m in batch["modes"] if keep is None or m["id"] in keep]


def variant_block_keys(defaults):
    keys = set()
    for v in defaults.get("variants", {}).values():
        keys |= set(v.get("render", {}))
    return keys


def validate_recipe(recipe, batch, defaults, root, errors, file_stem=None):
    where = f"recipe {recipe.get('id', file_stem)}"
    if not isinstance(recipe, dict):
        errors.append(f"{where}: must be an object")
        return
    unknown(recipe, RECIPE_KEYS, where, errors)
    missing(recipe, {"id", "batch", "place", "boundary"}, where, errors)
    rid = recipe.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        errors.append(f"{where}: id must match {ID_RE.pattern}")
    elif file_stem is not None and rid != file_stem:
        errors.append(f"{where}: id differs from its file name {file_stem}.json")
    if batch is not None:
        if recipe.get("batch") != batch.get("batch"):
            errors.append(f"{where}: batch is {recipe.get('batch')!r}, expected {batch.get('batch')!r}")
        if rid not in batch.get("cities", []):
            errors.append(f"{where}: not listed in cities/batches/{batch.get('batch')}.json")
        area_ids = [a.get("id") for a in batch.get("areas", [])]
        if "area" in recipe:
            if recipe["area"] not in area_ids:
                errors.append(f"{where}: area {recipe['area']!r} is not an area of the batch")
        elif len(area_ids) > 1:
            errors.append(f"{where}: area is required because the batch has {len(area_ids)} areas")
    if not isinstance(recipe.get("place", ""), str) or not recipe.get("place", "x").strip():
        errors.append(f"{where}: place must be a non-empty string")
    b = recipe.get("boundary")
    if b is not None:
        if isinstance(b, list):
            # Several divisions: the boundary is their union (the GTA is Toronto and four regions).
            if not b:
                errors.append(f"{where}.boundary: a list must name at least one division")
            for i, part in enumerate(b):
                validate_boundary_part(part, f"{where}.boundary[{i}]", root, errors)
            names = [str(p.get("name")) for p in b if isinstance(p, dict)]
            if len(set(names)) != len(names):
                errors.append(f"{where}.boundary: a division is named twice")
        else:
            validate_boundary_part(b, f"{where}.boundary", root, errors)
    if "modes" in recipe:
        ms = recipe["modes"]
        ids = [m.get("id") for m in (batch or {}).get("modes", []) if isinstance(m, dict)]
        if not (isinstance(ms, list) and ms and all(isinstance(m, str) for m in ms) and len(set(ms)) == len(ms)):
            errors.append(f"{where}.modes: must be a non-empty list of distinct mode ids")
        elif batch is not None:
            for m in ms:
                if m not in ids:
                    errors.append(f"{where}.modes: {m!r} is not a mode of the batch ({', '.join(map(str, ids))})")
    if "center" in recipe and not is_pair(recipe["center"]):
        errors.append(f"{where}.center: must be [lon, lat]")
    if "frame" in recipe:
        validate_frame(recipe["frame"], f"{where}.frame", errors)
    if "variants" in recipe:
        vs = recipe["variants"]
        if not (isinstance(vs, list) and vs and all(v in VARIANTS for v in vs) and len(set(vs)) == len(vs)):
            errors.append(f"{where}.variants: must be a non-empty list of distinct {', '.join(VARIANTS)}")
    rush = recipe.get("rush", {})
    if not isinstance(rush, dict):
        errors.append(f"{where}.rush: must be an object")
    else:
        unknown(rush, RUSH_KEYS, f"{where}.rush", errors)
        if rush.get("frame") is not None:
            validate_frame(rush["frame"], f"{where}.rush.frame", errors)
        if "auto" in rush and not isinstance(rush["auto"], bool):
            errors.append(f"{where}.rush.auto: must be true or false")
    var = recipe.get("variety", {})
    if not isinstance(var, dict):
        errors.append(f"{where}.variety: must be an object")
    else:
        unknown(var, VARIETY_KEYS, f"{where}.variety", errors)
        if var.get("panel_side", "left") not in ("left", "right"):
            errors.append(f"{where}.variety.panel_side: must be left or right")
        cl = var.get("card_line", 0)
        if not (isinstance(cl, int) and not isinstance(cl, bool) and 0 <= cl <= 2):
            errors.append(f"{where}.variety.card_line: must be 0, 1 or 2")
        z = var.get("zoom", 1.0)
        if not (is_num(z) and 0.5 <= z <= 2.0):
            errors.append(f"{where}.variety.zoom: must be a number between 0.5 and 2")
        cam = var.get("camera")
        if cam is not None and cam not in CAMERA_PATHS + ("off",):
            errors.append(f"{where}.variety.camera: must be off or one of {', '.join(CAMERA_PATHS)}")
    ov = recipe.get("override", {})
    if not isinstance(ov, dict):
        errors.append(f"{where}.override: must be an object")
        return
    unknown(ov, OVERRIDE_KEYS, f"{where}.override", errors)
    blocked = variant_block_keys(defaults)
    render = ov.get("render", {})
    if not isinstance(render, dict):
        errors.append(f"{where}.override.render: must be an object")
    else:
        for k in sorted(render):
            if not CONFIG_KEY_RE.match(k):
                errors.append(f"{where}.override.render: {k!r} is not a CONFIG key")
            elif k in blocked:
                errors.append(f"{where}.override.render: {k} is set by a defaults.variants render block; "
                              f"put it in override.variant_render.<variant>")
    vr = ov.get("variant_render", {})
    if not isinstance(vr, dict):
        errors.append(f"{where}.override.variant_render: must be an object")
    else:
        for v, block in sorted(vr.items()):
            if v not in VARIANTS:
                errors.append(f"{where}.override.variant_render: unknown variant {v!r}")
            elif not isinstance(block, dict) or not all(CONFIG_KEY_RE.match(k) for k in block):
                errors.append(f"{where}.override.variant_render.{v}: must be an object of CONFIG keys")
    bc = ov.get("brand_colors", {})
    if not isinstance(bc, dict) or not all(isinstance(k, str) and isinstance(v, str) and HEX_RE.match(v)
                                           for k, v in bc.items()):
        errors.append(f"{where}.override.brand_colors: must map brand ids to #rrggbb")
    why = ov.get("_why", {})
    if not isinstance(why, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in why.items()):
        errors.append(f"{where}.override._why: must map KEY or <variant>.KEY to one line")


def validate_batch(batch, name, licences, holidays, errors):
    where = f"batch {name}"
    if not isinstance(batch, dict):
        errors.append(f"{where}: must be an object")
        return
    unknown(batch, BATCH_KEYS, where, errors)
    missing(batch, BATCH_REQUIRED, where, errors)
    if batch.get("batch") != name:
        errors.append(f"{where}: batch is {batch.get('batch')!r}, expected the file name {name!r}")
    if not ID_RE.match(name):
        errors.append(f"{where}: batch name must match {ID_RE.pattern}")
    if "theme" in batch and batch["theme"] not in THEMES:
        errors.append(f"{where}.theme: must be one of {', '.join(THEMES)}")
    if "month" in batch and not (isinstance(batch["month"], str) and MONTH_RE.match(batch["month"])):
        errors.append(f"{where}.month: must be YYYY-MM")
    re_ = batch.get("render_epoch", 0)
    if not (isinstance(re_, int) and not isinstance(re_, bool) and re_ >= 0):
        errors.append(f"{where}.render_epoch: must be an integer >= 0")
    cities = batch.get("cities", [])
    if not (isinstance(cities, list) and cities and all(isinstance(c, str) and ID_RE.match(c) for c in cities)):
        errors.append(f"{where}.cities: must be a non-empty list of recipe ids")
    elif len(set(cities)) != len(cities):
        errors.append(f"{where}.cities: duplicate ids")
    vd = batch.get("variants_default", list(VARIANTS))
    if not (isinstance(vd, list) and vd and all(v in VARIANTS for v in vd)):
        errors.append(f"{where}.variants_default: must be a non-empty list of {', '.join(VARIANTS)}")
    tags = batch.get("hashtags", [])
    if not (isinstance(tags, list) and all(isinstance(t, str) and re.match(r"^#\w+$", t) for t in tags)):
        errors.append(f"{where}.hashtags: must be a list of #words")
    rel = batch.get("release", {})
    if not (isinstance(rel, dict) and set(rel) == {"tag", "name"} and all(isinstance(v, str) for v in rel.values())):
        errors.append(f"{where}.release: must be {{\"tag\", \"name\"}}")
    elif not TAG_RE.match(rel["tag"]):
        errors.append(f"{where}.release.tag: must match {TAG_RE.pattern}")
    rv = batch.get("review_videos", [])
    if not (isinstance(rv, list) and all(isinstance(s, str) for s in rv)):
        errors.append(f"{where}.review_videos: must be a list of <id>-<variant> names")
    modes = batch.get("modes", [])
    feed_ids = {f.get("id") for a in batch.get("areas", []) if isinstance(a, dict)
                for f in (a.get("feeds") or []) if isinstance(f, dict)}
    if not (isinstance(modes, list) and modes):
        errors.append(f"{where}.modes: must be a non-empty list")
    else:
        seen_modes = set()
        for i, m in enumerate(modes):
            if not isinstance(m, dict):
                errors.append(f"{where}.modes[{i}]: must be an object")
                continue
            unknown(m, MODE_KEYS, f"{where}.modes[{i}]", errors)
            missing(m, MODE_REQUIRED, f"{where}.modes[{i}]", errors)
            if m.get("id") in seen_modes:
                errors.append(f"{where}.modes[{i}]: duplicate mode id {m.get('id')!r}")
            seen_modes.add(m.get("id"))
            if "routes" in m:
                validate_mode_routes(m["routes"], f"{where}.modes[{i}].routes", feed_ids, errors)
    areas = batch.get("areas", [])
    if not (isinstance(areas, list) and areas):
        errors.append(f"{where}.areas: must be a non-empty list")
        return
    seen = set()
    for i, a in enumerate(areas):
        aw = f"{where}.areas[{a.get('id', i) if isinstance(a, dict) else i}]"
        if not isinstance(a, dict):
            errors.append(f"{aw}: must be an object")
            continue
        unknown(a, AREA_KEYS, aw, errors)
        missing(a, AREA_REQUIRED, aw, errors)
        if a.get("id") in seen:
            errors.append(f"{aw}: duplicate area id")
        seen.add(a.get("id"))
        if not (isinstance(a.get("id"), str) and ID_RE.match(a.get("id", ""))):
            errors.append(f"{aw}.id: must match {ID_RE.pattern}")
        if "origin" in a and not is_pair(a["origin"]):
            errors.append(f"{aw}.origin: must be [lon, lat]")
        if "area_box" in a and not is_box(a["area_box"]):
            errors.append(f"{aw}.area_box: must be [w, s, e, n] with w < e and s < n")
        if "month" in a and not (isinstance(a["month"], str) and MONTH_RE.match(a["month"])):
            errors.append(f"{aw}.month: must be YYYY-MM")
        if holidays is not None and "holidays" in a and a["holidays"] not in holidays:
            errors.append(f"{aw}.holidays: {a['holidays']!r} is not a key of cities/holidays.json")
        feeds = a.get("feeds", [])
        if not (isinstance(feeds, list) and feeds):
            errors.append(f"{aw}.feeds: must be a non-empty list")
            continue
        fids = set()
        for f in feeds:
            fw = f"{aw}.feeds[{f.get('id') if isinstance(f, dict) else '?'}]"
            if not isinstance(f, dict):
                errors.append(f"{fw}: must be an object")
                continue
            unknown(f, FEED_KEYS, fw, errors)
            missing(f, FEED_REQUIRED, fw, errors)
            if not (isinstance(f.get("id"), str) and FEED_ID_RE.match(f.get("id", ""))):
                errors.append(f"{fw}.id: must match {FEED_ID_RE.pattern}")
            if f.get("id") in fids:
                errors.append(f"{fw}: duplicate feed id")
            fids.add(f.get("id"))
            if "licence_id" in f and f["licence_id"] not in licences.get("licences", {}):
                errors.append(f"{fw}.licence_id: unknown licence id {f['licence_id']!r} (cities/licences.json)")
            if "allow_nc" in f and not (isinstance(f["allow_nc"], str) and f["allow_nc"].strip()):
                errors.append(f"{fw}.allow_nc: must be a reason string")
            src = f.get("source")
            kind = source_kind(src)
            if kind is None:
                errors.append(f"{fw}.source: must be one of {{tag, path}}, {{url, sha256}}, {{secret, url}}, "
                              f"{{model, inputs}} or {{path}}")
                continue
            req, opt = SOURCE_KINDS[kind]
            unknown(src, req | opt, f"{fw}.source", errors)
            missing(src, req, f"{fw}.source", errors)
            if kind == "tag" and not TAG_RE.match(str(src.get("tag", ""))):
                errors.append(f"{fw}.source.tag: must match {TAG_RE.pattern}")
            if kind == "url" and not SHA_RE.match(str(src.get("sha256", ""))):
                errors.append(f"{fw}.source.sha256: must be 64 lowercase hex digits")
            if kind == "secret" and "{key}" not in str(src.get("url", "")):
                errors.append(f"{fw}.source.url: a secret source needs {{key}} in its URL")


def validate_lock(lock, batch, errors):
    where = f"lock {batch.get('batch')}"
    if not isinstance(lock, dict):
        errors.append(f"{where}: must be an object")
        return
    unknown(lock, LOCK_KEYS, where, errors)
    missing(lock, {"batch", "areas", "boundaries"}, where, errors)
    if lock.get("batch") != batch.get("batch"):
        errors.append(f"{where}: batch is {lock.get('batch')!r}")
    for aid, entry in sorted(lock.get("areas", {}).items()):
        aw = f"{where}.areas.{aid}"
        if not isinstance(entry, dict):
            errors.append(f"{aw}: must be an object")
            continue
        unknown(entry, LOCK_AREA_KEYS, aw, errors)
        for fid, fe in sorted(entry.get("feeds", {}).items()):
            unknown(fe, LOCK_FEED_KEYS, f"{aw}.feeds.{fid}", errors)
            if not SHA_RE.match(str(fe.get("sha256", ""))):
                errors.append(f"{aw}.feeds.{fid}.sha256: must be 64 lowercase hex digits")
        unknown(entry.get("overture", {}), LOCK_OVERTURE_KEYS, f"{aw}.overture", errors)
        unknown(entry.get("divisions", {}), LOCK_DIVISIONS_KEYS, f"{aw}.divisions", errors)
    for cid, be in sorted(lock.get("boundaries", {}).items()):
        unknown(be, LOCK_BOUNDARY_KEYS, f"{where}.boundaries.{cid}", errors)
        if "frame" in be:
            validate_frame(be["frame"], f"{where}.boundaries.{cid}.frame", errors)
        if "clip" in be and not is_box(be["clip"]):
            errors.append(f"{where}.boundaries.{cid}.clip: must be [w, s, e, n]")


# ------------------------------------------------------------------ the pipeline

def stem_of(city, variant):
    return f"{city}-{variant}"


def split_stem(stem, cities):
    for v in VARIANTS:
        if stem.endswith("-" + v) and stem[: -len(v) - 1] in cities:
            return stem[: -len(v) - 1], v
    raise MakeError(f"{stem!r} is not <city>-<variant> of this batch")


def recipe_variants(recipe, batch):
    return [v for v in VARIANTS if v in recipe.get("variants", batch.get("variants_default", list(VARIANTS)))]


# The page's MODE_PRESENT_MIN (B9): a mode is named when at least half a
# vehicle of it is inside in some minute of the window V.peak is taken over.
MODE_PRESENT_MIN = 0.5


def modes_in_window(modes, by_mode, start, end):
    """Ids of the modes the page names for a variant window [start, end) in seconds."""
    m0, m1 = -(-start // 60), -(-end // 60)
    out = []
    for m in modes:
        a = by_mode.get(m["id"]) or []
        if a and any((a[t % len(a)] or 0) >= MODE_PRESENT_MIN for t in range(int(m0), int(m1))):
            out.append(m["id"])
    return out


def asset_label(name, key):
    """A release asset's label: GitHub lists the label in place of the file name, so it leads with the name."""
    return f"{name} key:{key[:16]}"


def label_key(label):
    """The 16 hex key of an asset label, also from the bare "key:<hex>" labels of earlier runs."""
    label = label or ""
    return label.rsplit("key:", 1)[-1] if "key:" in label else ""


def strip_why(recipe):
    r = copy.deepcopy(recipe)
    r.get("override", {}).pop("_why", None)
    return r


class Pipeline:
    def __init__(self, root=ROOT, cities="cities"):
        self.root = os.path.abspath(root)
        self.cities_dir = cities if os.path.isabs(cities) else os.path.join(self.root, cities)
        self.memo = HashMemo(self.root)
        self._cache = {}
        self._render_code = None
        self.fetched_from_overture = set()

    # ---------------------------------------------------------- paths and files

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def rel(self, path):
        return os.path.relpath(os.path.abspath(path), self.root)

    def cfile(self, name, required=True):
        """A config file: from --cities DIR when there, else from cities/."""
        for base in (self.cities_dir, self.path("cities")):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
        if required:
            raise MakeError(f"{os.path.join(self.rel(self.cities_dir), name)} is missing")
        return None

    def load(self, path):
        if path not in self._cache:
            try:
                self._cache[path] = read_json(path)
            except ValueError as e:
                raise MakeError(f"{self.rel(path)}: {e}")
        return self._cache[path]

    def defaults(self):
        return self.load(self.cfile("defaults.json"))

    def licences(self):
        return self.load(self.cfile("licences.json"))

    def templates(self):
        return self.load(self.cfile(os.path.join("templates", "shorts_en.json")))

    def holidays(self, required=True):
        p = self.cfile("holidays.json", required=False)
        if p is None:
            if required:
                raise MakeError("cities/holidays.json is missing")
            return None
        return self.load(p)

    def brands_path(self):
        p = self.cfile("brands.json", required=False)
        if p:
            return p
        # cities/brands.json arrives with the first batch branch; until then every route takes its GTFS or
        # mode colour (A10 rule 4), which an empty entry list gives without a special case in the trim.
        p = self.path("build", "brands.empty.json")
        write_json(p, {"version": 1, "agencies": []})
        return p

    def brands(self):
        return self.load(self.brands_path())

    def batch_path(self, name):
        return os.path.join(self.cities_dir, "batches", f"{name}.json")

    def recipe_path(self, rid):
        return os.path.join(self.cities_dir, "recipes", f"{rid}.json")

    def lock_path(self, name):
        return os.path.join(self.cities_dir, "locks", f"{name}.lock.json")

    def sha(self, rel_or_abs):
        p = rel_or_abs if os.path.isabs(rel_or_abs) else self.path(rel_or_abs)
        return self.memo.sha(p)

    def code_shas(self, files):
        gone = [f for f in files if not os.path.isfile(self.path(f))]
        if gone:
            raise MakeError(f"code in the step keys is missing: {', '.join(gone)}")
        return {f: self.sha(f) for f in files}

    def glob_files(self, patterns):
        out = set()
        for pat in patterns:
            for p in glob.glob(self.path(pat), recursive=True):
                if os.path.isfile(p):
                    out.add(self.rel(p))
        return sorted(out)

    def render_code(self):
        if self._render_code is None:
            files = sorted(set(A_FILES + LEGACY_CODE + LOCKED_DEPS + self.glob_files(B_GLOBS + C_GLOBS)))
            self._render_code = self.code_shas(files)
        return self._render_code

    # ---------------------------------------------------------- loading with validation

    def load_batch(self, name, recipes=True):
        p = self.batch_path(name)
        if not os.path.exists(p):
            raise MakeError(f"no batch file {self.rel(p)}")
        batch = self.load(p)
        errors = []
        validate_batch(batch, name, self.licences(), self.holidays(required=False), errors)
        if errors:
            raise MakeError("invalid batch:\n  " + "\n  ".join(errors))
        if recipes:
            for rid in batch["cities"]:
                self.recipe(rid, batch)
            for stem in batch.get("review_videos", []):
                city, variant = split_stem(stem, batch["cities"])
                if variant not in recipe_variants(self.recipe(city, batch), batch):
                    raise MakeError(f"batch {name}.review_videos: {city} has no {variant} variant")
        return batch

    def recipe(self, rid, batch=None):
        p = self.recipe_path(rid)
        if not os.path.exists(p):
            raise MakeError(f"no recipe {self.rel(p)}")
        recipe = self.load(p)
        if batch is None:
            batch = self.load_batch(recipe.get("batch", ""), recipes=False)
        errors = []
        validate_recipe(recipe, batch, self.defaults(), self.root, errors, file_stem=rid)
        if errors:
            raise MakeError("invalid recipe:\n  " + "\n  ".join(errors))
        return recipe

    def load_lock(self, batch, required=True):
        p = self.lock_path(batch["batch"])
        if not os.path.exists(p):
            if required:
                raise MakeError(f"no lock {self.rel(p)}; run make.py lock {batch['batch']}")
            return None
        lock = self.load(p)
        errors = []
        validate_lock(lock, batch, errors)
        if errors:
            raise MakeError("invalid lock:\n  " + "\n  ".join(errors))
        return lock

    def save_lock(self, batch, lock):
        p = self.lock_path(batch["batch"])
        write_json(p, lock)
        self._cache[p] = lock

    def area_of(self, batch, recipe):
        aid = recipe.get("area") or batch["areas"][0]["id"]
        for a in batch["areas"]:
            if a["id"] == aid:
                return a
        raise MakeError(f"{recipe['id']}: area {aid!r} is not in the batch")

    def city_lock(self, lock, rid):
        be = lock.get("boundaries", {}).get(rid)
        if not be or "frame" not in be or "clip" not in be:
            raise MakeError(f"the lock has no boundary, frame and clip for {rid}; run make.py lock")
        return be

    def videos(self, batch, lock=None):
        """(city, variant) in publish order: every day video in city order, then rush, then week."""
        out = []
        for v in VARIANTS:
            for rid in batch["cities"]:
                recipe = self.recipe(rid, batch)
                if v not in recipe_variants(recipe, batch):
                    continue
                if v == "week" and lock and lock.get("boundaries", {}).get(rid, {}).get("week_eligible") is False:
                    continue
                out.append((rid, v))
        return out

    # ---------------------------------------------------------- derivation (D3, 2.7)

    def area_month(self, batch, area):
        return area.get("month", batch["month"])

    def area_config(self, batch, area, timeline, lock):
        d = self.defaults()
        lk = lock["areas"].get(area["id"])
        if not lk:
            raise MakeError(f"the lock has no area {area['id']}; run make.py lock {batch['batch']}")
        feeds = []
        for f in area["feeds"]:
            if f["id"] not in lk["feeds"]:
                raise MakeError(f"the lock has no feed {f['id']} in area {area['id']}; run make.py lock")
            entry = {k: v for k, v in f.items() if k != "source"}
            entry["sha256"] = lk["feeds"][f["id"]]["sha256"]
            feeds.append(entry)
        tl = d["timeline"]
        return {
            "schema": 4, "kind": "area", "id": area["id"],
            "origin": area["origin"],
            "area_box": area["area_box"],
            "gtfs_dir": f"cache/feeds/{area['id']}",
            "feeds": feeds,
            "modes": batch["modes"],
            "timeline": {
                "kind": timeline,
                "month": self.area_month(batch, area),
                "timezone": area["timezone"],
                "holidays": dict(sorted(self.holidays()[area["holidays"]].items())),
                "drop_pct": tl["drop_pct"],
                "min_week_dates": tl["min_week_dates"],
                "fallback_days": tl["fallback_days"],
                "guard": tl["guard"],
            },
            "stop_times_chunk": 2000000,
        }

    def variant_block(self, variant, recipe):
        d = self.defaults()["variants"][variant]
        t = self.templates()["labels"][variant]
        render = dict(d["render"])
        render.update({"DURATION_FRAMES": d["frames"], "HOLD_START": 0, "HOLD_END": 0})
        frame = None
        if variant == "rush":
            frame = (recipe.get("rush") or {}).get("frame")
        return {"start": d["start"], "end": d.get("end"), "label": t, "frame": frame, "render": render}

    def city_config(self, batch, recipe, timeline, lock):
        d = self.defaults()
        area = self.area_of(batch, recipe)
        be = self.city_lock(lock, recipe["id"])
        vs = recipe_variants(recipe, batch)
        if timeline == "day":
            names = [v for v in vs if v in ("day", "rush")] or ["day"]
        else:
            names = ["week"]
        rush = recipe.get("rush") or {}
        variety = recipe.get("variety") or {}
        place = recipe["place"]
        # The trim fills {agencies} and {n}; {author} is the channel's name, the same one the
        # description's "Made by" line uses (D5).
        if not d.get("author") and any("{author}" in d[k] for k in ("credit_template", "credit_fallback")):
            raise MakeError("cities/defaults.json: the credit names {author} but author is empty")
        credit = {k: d[k].replace("{author}", d.get("author") or "") for k in ("credit_template", "credit_fallback")}
        return {
            "schema": 4, "kind": "city",
            "id": recipe["id"], "batch": batch["batch"], "area": area["id"], "place": place,
            "title": place.upper(),
            "origin": area["origin"],
            "frame": be["frame"],
            "trim_scale": d["trim_scale"],
            "boundary": {"file": f"build/{recipe['id']}/boundary.geojson", "name": boundary_name(recipe),
                         "simplify_km": d["boundary"]["simplify_km"], "mask_km": d["boundary"]["mask_km"]},
            # The trim keeps only the trips of these modes; the area store holds every mode, so recipes that
            # split one city by mode share its area build.
            "modes": recipe_modes(recipe, batch),
            "brands": self.rel(self.brands_path()),
            "group_by": d["group_by"],
            "credit_template": credit["credit_template"],
            "credit_fallback": credit["credit_fallback"],
            "preset": d["preset"],
            "theme": {"batch": batch["theme"]},
            "render": {},
            "variants": {v: self.variant_block(v, recipe) for v in names},
            "rush": {"auto": bool(rush.get("auto", d["rush"]["auto"])) and rush.get("frame") is None,
                     "zoom": d["rush"]["zoom"], "share": d["rush"]["share"]},
            "fit_box": d["fit_box"],
            "panel": {"preferred": variety.get("panel_side", "left"), "tie": d["panel"]["tie"], "rect": d["panel"]["rect"]},
            "card": {"title": place.upper(), "templates": self.templates()["card"]},
            "major_share": d["major_share"],
        }

    def basemap_params(self, km_vertical):
        rule = self.defaults()["basemap_rule"]
        # One frame pixel at a 1.1 zoom arm (A11), so tuning never sees a blurrier map.
        p = km_vertical / HEIGHT / 1.1
        return {
            "simplify_km": {k: r6(rule[k] * p) for k in ("major", "minor", "rail", "water", "water_line")},
            "min_water_area_km2": r6(rule["min_water_px2"] * p * p),
            "min_road_km": r6(rule["min_road_px"] * p),
            "water_polys": rule["water_polys"],
            "water_lines": rule["water_lines"],
            "min_stream_km": r6(rule["min_stream_px"] * p),
        }

    def basemap_config(self, batch, recipe, lock):
        area = self.area_of(batch, recipe)
        be = self.city_lock(lock, recipe["id"])
        rel = lock["areas"][area["id"]]["overture"]["release"]
        return {
            "schema": 4,
            "id": recipe["id"],
            "origin": area["origin"],
            "clip": be["clip"],
            "basemap_dir": f"cache/overture/{rel}/{area['id']}",
            "basemap": self.basemap_params(be["frame"]["km_vertical"]),
            "gzip": True,
            "boundary": None,
            "built_dir": f"build/{recipe['id']}",
        }

    def camera_pick(self, recipe):
        """B18: the camera path of a recipe without variety.camera, camera_paths() over the cities
        of its batch up to it; a recipe its batch does not list keeps camera_path(id). The other
        recipes are read without validation, so a broken neighbour never stops this render."""
        rid = recipe["id"]
        p = self.batch_path(recipe.get("batch", ""))
        ids = list(self.load(p).get("cities", [])) if os.path.exists(p) else []
        if rid not in ids:
            return camera_path(rid)
        ids = ids[:ids.index(rid) + 1]
        chosen = {}
        for other in ids[:-1]:
            rp = self.recipe_path(other)
            cam = (self.load(rp).get("variety") or {}).get("camera") if os.path.exists(rp) else None
            if cam in CAMERA_PATHS + ("off",):
                chosen[other] = cam
        return camera_paths(ids, chosen)[rid]

    def render_query(self, recipe, variant):
        """D3.8: the render= JSON and brandhex= string for one video."""
        variety = recipe.get("variety") or {}
        ov = recipe.get("override") or {}
        rj = {"CARD_LINES": variety.get("card_line", 0)}
        # The preset turns the camera on; the recipe only names the path (or turns it off), the
        # same for every variant, so the rush close-up and the week move like the day.
        cam = variety.get("camera")
        if cam == "off":
            rj["CAMERA"] = False
        else:
            rj["CAMERA_PATH"] = cam or self.camera_pick(recipe)
        rj.update(ov.get("render", {}))
        rj.update(ov.get("variant_render", {}).get(variant, {}))
        if variant in ("day", "week"):
            # FRAME_ZOOM is the one multiplicative key; tune --apply stores it divided by variety.zoom.
            rj["FRAME_ZOOM"] = r6(variety.get("zoom", 1.0) * rj.get("FRAME_ZOOM", 1))
        bh = ";".join(f"{k}:{v.lstrip('#').lower()}" for k, v in sorted(ov.get("brand_colors", {}).items()))
        return rj, bh

    def frame_of_boundary(self, area, recipe, feature):
        """The lock frame: pinned in the recipe, else fitted to the boundary bbox (D3.2)."""
        if recipe.get("frame"):
            fr = recipe["frame"]
            return {"km_vertical": float(fr["km_vertical"]), "center_km": [float(fr["center_km"][0]), float(fr["center_km"][1])]}
        b = geometry_bbox(feature["geometry"])
        x0, y0 = to_km(area["origin"], b[0], b[1])
        x1, y1 = to_km(area["origin"], b[2], b[3])
        return fit_frame([x0, y0, x1, y1], self.defaults()["fit_box"])

    def clip_of(self, area, frame):
        """D3.4: trim box plus 2 km, in degrees rounded outward to 0.01."""
        return box_deg_outward(area["origin"], grow_km(trim_box(frame, self.defaults()["trim_scale"]), 2.0))

    def boundary_bbox_km(self, area, feature):
        b = geometry_bbox(feature["geometry"])
        x0, y0 = to_km(area["origin"], b[0], b[1])
        x1, y1 = to_km(area["origin"], b[2], b[3])
        return [x0, y0, x1, y1]

    def trim_box_deg(self, area, frame, feature=None):
        """The trim box in degrees; given the boundary, grown to hold it as the trim does."""
        tb = trim_box(frame, self.defaults()["trim_scale"],
                      self.boundary_bbox_km(area, feature) if feature is not None else None)
        w, s = to_deg(area["origin"], tb[0], tb[1])
        e, n = to_deg(area["origin"], tb[2], tb[3])
        return [w, s, e, n]

    def overture_bbox(self, area, clips):
        """D3.4: union of the area's clips plus 5 km, rounded outward to 0.01 degree."""
        u = grow_deg(area["origin"], union_box(clips), 5.0)
        return [floor_to(u[0]), floor_to(u[1]), ceil_to(u[2]), ceil_to(u[3])]

    def divisions_bbox(self, area):
        """Divisions are fetched before any frame exists, so they use the explicit area box plus 5 km."""
        u = grow_deg(area["origin"], area["area_box"], 5.0)
        return [floor_to(u[0]), floor_to(u[1]), ceil_to(u[2]), ceil_to(u[3])]

    @staticmethod
    def data_tag_of(area, release, bbox, old):
        """overture/<area>-<release>, the lock's tag for the area's Overture extract and divisions.

        A lock that moves the extract bbox within a release gets 8 hex of the new bbox
        appended: the tag already on origin holds the old bytes, and restoring from it
        fails on their sha256 while pushing over it breaks the lock that still pins them.
        """
        tag = f"overture/{area['id']}-{release}"
        if old.get("release") != release:
            return tag
        if old.get("bbox") == bbox:
            return old.get("data_tag") or tag
        return f"{tag}-{hashlib.sha256(canonical(bbox).encode()).hexdigest()[:8]}"

    def divisions_key(self, area):
        # One file per area (A7: one fetch per area); a region code alone cannot
        # tell two areas of one state apart (Dallas and Houston are both US-TX).
        return f"{area['region']}-{area['id']}"

    def divisions_path(self, release, key):
        return self.path("cache", "overture", release, "divisions", f"{key}.geojson")

    def overture_dir(self, release, area_id):
        return self.path("cache", "overture", release, area_id)

    # ---------------------------------------------------------- keys (D2)

    def brands_for(self, area):
        """The brand entries that can match a route of this area's feeds."""
        fids = {f["id"] for f in area["feeds"]}
        b = self.brands()
        return {"version": b.get("version"),
                "agencies": [e for e in b.get("agencies", []) if fids & set(e.get("feeds", []))]}

    def area_key(self, batch, area, timeline, lock):
        cfg = self.area_config(batch, area, timeline, lock)
        return sha256_json({"step": "area", "code": self.code_shas(AREA_CODE), "config": cfg})

    def boundary_key(self, batch, recipe, lock):
        area = self.area_of(batch, recipe)
        b = recipe["boundary"]
        files = [self.sha(p["file"]) if p.get("file") else None for p in boundary_parts(recipe)]
        payload = {"step": "boundary", "code": self.code_shas(BOUNDARY_CODE),
                   "divisions": lock["areas"][area["id"]]["divisions"], "boundary": b,
                   "file": files if isinstance(b, list) else files[0]}
        return sha256_json(payload)

    def trim_key(self, batch, recipe, timeline, lock, boundary_sha, day_network_sha=None):
        area = self.area_of(batch, recipe)
        # The brands file's path follows --cities while its content is keyed below, so a build from a
        # scratch cities dir gets the key, and the network bytes, of the checkout's (D-8).
        payload = {"step": f"trim-{timeline}", "code": self.code_shas(TRIM_CODE),
                   "config": dict(self.city_config(batch, recipe, timeline, lock), brands=None),
                   "area": self.area_key(batch, area, timeline, lock),
                   "boundary": boundary_sha, "brands": self.brands_for(area)}
        if timeline == "week":
            payload["day_network"] = day_network_sha
        return sha256_json(payload)

    def basemap_key(self, batch, recipe, lock):
        area = self.area_of(batch, recipe)
        ov = lock["areas"][area["id"]]["overture"]
        return sha256_json({"step": "basemap", "code": self.code_shas(BASEMAP_CODE),
                            "config": self.basemap_config(batch, recipe, lock),
                            "overture": {"release": ov["release"], "files": ov.get("files", {})}})

    def overture_key(self, area, lock):
        lk = lock["areas"][area["id"]]
        ov = lk["overture"]
        return sha256_json({"release": ov["release"], "bbox": ov["bbox"], "files": ov.get("files", {}),
                            "divisions": lk["divisions"]})

    def render_key(self, batch, recipe, variant, lock, tier="final"):
        """From inputs only, so plan can compute it on a fresh clone."""
        area = self.area_of(batch, recipe)
        lk = lock["areas"][area["id"]]
        be = {k: v for k, v in self.city_lock(lock, recipe["id"]).items() if k not in ("week_eligible", "week_why")}
        b = {k: v for k, v in batch.items() if k not in ("title", "cities", "hashtags", "release", "category",
                                                         "review_videos")}
        b["areas"] = [area]
        payload = {
            "step": "render", "derive": DERIVE_VERSION,
            "lock": {"feeds": {f: e["sha256"] for f, e in sorted(lk["feeds"].items())},
                     "overture_release": lk["overture"]["release"],
                     "divisions": lk["divisions"],
                     "dates": lk.get("dates", {}), "rules": lk.get("rules", {}),
                     "boundary": be},
            "batch": b,
            # A reason line from the judge changes no pixel.
            "recipe": strip_why(recipe),
            "defaults": self.defaults(),
            "brands": self.brands_for(area),
            "holidays": self.holidays().get(area["holidays"], {}),
            "code": self.render_code(),
            "variant": variant, "tier": tier, "render_epoch": batch.get("render_epoch", 0),
        }
        return sha256_json(payload)

    def publish_order(self, batch, rid, variant, lock=None):
        for i, (c, v) in enumerate(self.videos(batch, lock), 1):
            if (c, v) == (rid, variant):
                return i
        raise MakeError(f"{rid} has no {variant} video in batch {batch['batch']}")

    def meta_key(self, batch, recipe, variant, lock, render_key=None):
        rk = render_key or self.render_key(batch, recipe, variant, lock)
        return sha256_json({"render_key": rk, "templates": self.templates(), "licences": self.licences(),
                            "shorts_meta": self.sha("scripts/shorts_meta.py"),
                            "hashtags": batch.get("hashtags", []), "category": batch.get("category"),
                            "cities": batch["cities"],
                            "publish_order": self.publish_order(batch, recipe["id"], variant, lock)})

    def csv_key(self, meta_keys):
        return sha256_json(sorted(meta_keys))

    # ---------------------------------------------------------- processes

    def env(self, extra=None):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = "0"
        env.update(extra or {})
        return env

    def tool_path(self, script):
        p = self.path("scripts", script)
        if not os.path.exists(p):
            raise MakeError(f"scripts/{script} is missing")
        return p

    def show_cmd(self, cmd):
        def short(a):
            a = str(a)
            if a.startswith(self.root + os.sep):
                a = os.path.relpath(a, self.root)
            return a if re.match(r"^[\w./:=,@%+-]+$", a) else json.dumps(a, ensure_ascii=False)
        say("  $ " + " ".join(short(a) for a in cmd))

    def run_py(self, script, args, env=None, ok_codes=(0,), capture=False):
        cmd = [sys.executable, "-I", self.tool_path(script), *[str(a) for a in args]]
        self.show_cmd(cmd)
        res = subprocess.run(cmd, cwd=self.root, env=self.env(env), text=True,
                             stdout=subprocess.PIPE if capture else None)
        if res.returncode not in ok_codes:
            raise MakeError(f"scripts/{script} exited with {res.returncode}")
        return res

    def run_node(self, args, env=None):
        """render_video.mjs with streamed output; returns the parsed SIDECAR line, if any."""
        cmd = ["node", self.tool_path("render_video.mjs"), *[str(a) for a in args]]
        self.show_cmd(cmd)
        proc = subprocess.Popen(cmd, cwd=self.root, env=self.env(env), text=True, stdout=subprocess.PIPE)
        sidecar = None
        for line in proc.stdout:
            sys.stdout.write(line)
            if line.startswith("SIDECAR "):
                try:
                    sidecar = json.loads(line[len("SIDECAR "):])
                except ValueError:
                    warn("could not parse the SIDECAR line")
        sys.stdout.flush()
        if proc.wait() != 0:
            raise MakeError(f"render_video.mjs exited with {proc.returncode}")
        return sidecar

    def git(self, *args, check=True, binary=False, input=None, env=None):
        res = subprocess.run(["git", *args], cwd=self.root, capture_output=True, input=input,
                             text=not binary, env=self.env(env))
        if check and res.returncode != 0:
            err = res.stderr.decode(errors="replace") if binary else res.stderr
            raise MakeError(f"git {' '.join(args[:3])}: {err.strip()}")
        return res

    # ---------------------------------------------------------- the step runner

    def stamp_ok(self, stamp):
        for rel, digest in stamp.get("outputs", {}).items():
            if self.sha(rel) != digest:
                return False
        return True

    def step_current(self, key, stamp_path):
        """True when `step` would find the stamp at `stamp_path` up to date for `key`."""
        try:
            stamp = read_json(stamp_path)
        except (FileNotFoundError, ValueError):
            return False
        return stamp.get("key") == key and self.stamp_ok(stamp)

    def step(self, name, key, stamp_path, fn, outputs=None, out_dir=None, no_upstream=False):
        """Run `fn` unless the stamp next to the outputs carries `key` and the outputs still hash to it."""
        try:
            stamp = read_json(stamp_path)
        except (FileNotFoundError, ValueError):
            stamp = None
        if stamp and stamp.get("key") == key and self.stamp_ok(stamp):
            say(f"  {name}: up to date ({key[:12]})")
            return stamp, False
        if no_upstream:
            why = "no stamp" if not stamp else ("key mismatch" if stamp.get("key") != key else "outputs changed")
            raise MakeError(f"{name}: {why} at {self.rel(stamp_path)}, and --no-upstream runs only the step asked for")
        say(f"  {name}: building ({key[:12]})")
        t0 = time.time()
        extra = fn() or {}
        files = list(outputs or [])
        if out_dir:
            for dirpath, _dirs, names in os.walk(out_dir):
                for n in names:
                    p = os.path.join(dirpath, n)
                    if os.path.abspath(p) != os.path.abspath(stamp_path):
                        files.append(p)
        outs = {}
        for p in sorted(files, key=self.rel):
            if extra.get("eligible") is False and not os.path.exists(p):
                continue
            if not os.path.exists(p):
                raise MakeError(f"{name}: expected output {self.rel(p)} was not written")
            outs[self.rel(p)] = self.sha(p)
        stamp = {"step": name, "key": key, "outputs": outs, "seconds": round(time.time() - t0, 1)}
        stamp.update(extra)
        write_json(stamp_path, stamp)
        return stamp, True

    # ---------------------------------------------------------- frozen checks (2.3)

    def pinned_packages(self):
        p = self.path("requirements.txt")
        if not os.path.exists(p):
            raise MakeError("requirements.txt is missing")
        pins = {}
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line.strip())
                if m:
                    pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
        return pins

    def installed_versions(self, names):
        """Versions as the builders see them: `python3 -I` skips user site-packages that this process may load."""
        code = ("import json, sys\nfrom importlib import metadata\nout = {}\n"
                "for n in json.loads(sys.argv[1]):\n"
                "    try:\n        out[n] = metadata.version(n)\n"
                "    except metadata.PackageNotFoundError:\n        out[n] = None\n"
                "print(json.dumps(out))")
        res = subprocess.run([sys.executable, "-I", "-c", code, json.dumps(sorted(names))], capture_output=True,
                             text=True, env=self.env(), check=True)
        return json.loads(res.stdout)

    def check_frozen(self, lock):
        bad = []
        pins = self.pinned_packages()
        have_all = self.installed_versions(pins)
        for name, want in sorted(pins.items()):
            have = have_all.get(name)
            if have is None:
                bad.append(f"{name}=={want} is not installed")
            elif have != want:
                bad.append(f"{name} is {have}, requirements.txt pins {want}")
        if bad:
            raise MakeError("--frozen: installed packages differ from requirements.txt:\n  " + "\n  ".join(bad))
        tools = (lock or {}).get("tools", {})
        have = self.tool_versions()
        for k in ("python", "chromium", "ffmpeg"):
            if tools.get(k) and have.get(k) and tools[k] != have[k]:
                warn(f"{k} is {have[k]}, the lock was made with {tools[k]} (networks and basemaps depend on the "
                     f"pinned packages, not on this; D-8 compares the outputs)")

    def tool_versions(self):
        out = {"python": platform.python_version(), "chromium": "", "ffmpeg": ""}
        bj = self.path("node_modules", "playwright-core", "browsers.json")
        try:
            for b in read_json(bj)["browsers"]:
                if b["name"] == "chromium":
                    out["chromium"] = str(b["revision"])
        except (OSError, ValueError, KeyError):
            pass
        ff = shutil.which("ffmpeg")
        if ff:
            try:
                first = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=30).stdout.split("\n")[0]
                m = re.match(r"ffmpeg version (\S+)", first)
                out["ffmpeg"] = m.group(1) if m else ""
            except (OSError, subprocess.SubprocessError):
                pass
        return out

    # ---------------------------------------------------------- lock (D4)

    def feed_facts(self, data):
        """feed_version and the first and last date with any service, from the zip's bytes."""
        version, dates = "", []
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise MakeError("feed is not a zip file")
        members = {}
        for n in zf.namelist():
            base = os.path.basename(n)
            stem, ext = os.path.splitext(base)
            if ext in (".txt", ".csv") and stem not in members:
                members[stem] = n

        def rows(stem):
            if stem not in members:
                return []
            import csv
            text = zf.read(members[stem]).decode("utf-8-sig", errors="replace")
            return list(csv.DictReader(io.StringIO(text), skipinitialspace=True))

        for r in rows("feed_info"):
            version = (r.get("feed_version") or "").strip()
        for r in rows("calendar"):
            for k in ("start_date", "end_date"):
                if (r.get(k) or "").strip():
                    dates.append(r[k].strip())
        for r in rows("calendar_dates"):
            if (r.get("exception_type") or "").strip() == "1" and (r.get("date") or "").strip():
                dates.append(r["date"].strip())
        valid = []
        if dates:
            lo, hi = min(dates), max(dates)
            valid = [f"{lo[:4]}-{lo[4:6]}-{lo[6:8]}", f"{hi[:4]}-{hi[4:6]}-{hi[6:8]}"]
        return version, valid

    def tag_commit(self, tag):
        res = self.git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False)
        if res.returncode != 0:
            say(f"  fetching tag {tag} from origin")
            self.git("fetch", "--no-tags", "--depth=1", "origin", f"refs/tags/{tag}:refs/tags/{tag}", check=False)
            res = self.git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False)
        if res.returncode != 0:
            raise MakeError(f"tag {tag} is not in this repository or on origin")
        return res.stdout.strip()

    def download(self, url, dest, shown=None):
        say(f"  downloading {shown or url}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "bus-map-make/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh, 1 << 20)
        os.replace(tmp, dest)

    def lock_feed(self, area, f, old, refresh):
        src = f["source"]
        kind = source_kind(src)
        if kind == "tag":
            commit = self.tag_commit(src["tag"])
            data = self.git("show", f"{commit}:{src['path']}", binary=True).stdout
            version, valid = self.feed_facts(data)
            return {"source": "tag", "tag": src["tag"], "commit": commit, "path": src["path"],
                    "sha256": sha256_bytes(data), "bytes": len(data), "feed_version": version, "valid": valid}
        if kind == "path":
            p = self.path(src["path"])
            if self.git("ls-files", "--error-unmatch", src["path"], check=False).returncode != 0:
                raise MakeError(f"feed {f['id']}: {src['path']} is not committed on this branch")
            with open(p, "rb") as fh:
                data = fh.read()
            version, valid = self.feed_facts(data)
            return {"source": "path", "path": src["path"], "sha256": sha256_bytes(data), "bytes": len(data),
                    "feed_version": version, "valid": valid}
        if kind in ("url", "secret"):
            dest = self.path("cache", "feeds", area["id"], f"{f['id']}.zip")
            want = src.get("sha256")
            if old and not refresh and old.get("url") == src["url"] and (not want or old.get("sha256") == want) \
                    and self.sha(dest) == old.get("sha256"):
                return old
            if kind == "secret":
                key = os.environ.get(src["secret"])
                if not key:
                    raise MakeError(f"feed {f['id']}: ${src['secret']} is not set")
                self.download(src["url"].replace("{key}", key), dest, shown=src["url"])
            else:
                self.download(src["url"], dest)
            digest = sha256_file(dest)
            if want and digest != want:
                raise MakeError(f"feed {f['id']}: {src['url']} has sha256 {digest}, the batch pins {want}")
            with open(dest, "rb") as fh:
                version, valid = self.feed_facts(fh.read())
            entry = {"source": kind, "url": src["url"], "sha256": digest, "bytes": os.path.getsize(dest),
                     "feed_version": version, "valid": valid}
            if kind == "secret":
                entry["secret"] = src["secret"]
            return entry
        raise MakeError(f"feed {f['id']}: model sources are added with the Japan model PR (model_gtfs.py areas)")

    def boundary_part(self, recipe, b, divisions_file, out):
        """One division as a GeoJSON Feature at `out` (the recipe's own file, or A7 select) and returns it."""
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if b.get("file"):
            obj = read_json(self.path(b["file"]))
            feat = obj["features"][0] if obj.get("type") == "FeatureCollection" and len(obj.get("features", [])) == 1 else obj
            if feat.get("type") != "Feature":
                raise MakeError(f"{b['file']}: must be a GeoJSON Feature (or a collection of one)")
            area = geometry_area_km2(feat["geometry"])
            if abs(area - b["area_km2"]) / b["area_km2"] > 0.20:
                raise MakeError(f"{recipe['id']}: {b['file']} has {area:.1f} km2, the recipe says {b['area_km2']}")
            feat = {"type": "Feature",
                    "properties": {"id": f"file:{b['file']}", "division_id": None, "name": b["name"],
                                   "subtype": "file", "area_km2": round(area, 2), "source": f"file {b['file']}"},
                    "geometry": feat["geometry"]}
            write_text_if_changed(out, json.dumps(feat, ensure_ascii=False, separators=(",", ":")) + "\n")
            return feat
        self.run_py("fetch_boundary.py", ["select", "--in", self.rel(divisions_file), "--name", b["name"],
                                          "--subtypes", ",".join(b.get("subtypes", list(SUBTYPES))),
                                          "--area-km2", b["area_km2"], "--out", self.rel(out)])
        obj = read_json(out)
        if obj.get("type") == "FeatureCollection":
            obj = obj["features"][0]
        return obj

    def select_boundary(self, batch, recipe, divisions_file):
        """Writes build/<id>/boundary.geojson and returns the Feature: one division, or the union of several."""
        parts = boundary_parts(recipe)
        out = self.path("build", recipe["id"], "boundary.geojson")
        if len(parts) == 1:
            return self.boundary_part(recipe, parts[0], divisions_file, out)
        pdir = self.path("build", recipe["id"], "boundary.parts")
        if os.path.isdir(pdir):
            shutil.rmtree(pdir)
        args = ["union"]
        for i, b in enumerate(parts):
            p = os.path.join(pdir, f"{i}.geojson")
            self.boundary_part(recipe, b, divisions_file, p)
            args += ["--in", self.rel(p)]
        self.run_py("fetch_boundary.py", args + ["--out", self.rel(out)])
        return read_json(out)

    def boundary_entry(self, batch, recipe, feat):
        area = self.area_of(batch, recipe)
        props = feat.get("properties", {})
        frame = self.frame_of_boundary(area, recipe, feat)
        be = {"id": props.get("id"), "division_id": props.get("division_id"), "name": boundary_name(recipe),
              "subtype": props.get("subtype"), "area_km2": props.get("area_km2"),
              "frame": frame, "clip": self.clip_of(area, frame)}
        parts = boundary_parts(recipe)
        if len(parts) > 1:
            got = props.get("parts") or []
            if len(got) != len(parts):
                raise MakeError(f"{recipe['id']}: the boundary union has {len(got)} parts, the recipe names {len(parts)}")
            be["parts"] = []
            for b, pp in zip(parts, got):
                pe = {k: pp.get(k) for k in ("id", "division_id", "name", "subtype", "area_km2")}
                if b.get("file"):
                    pe.update(file=b["file"], sha256=self.sha(b["file"]))
                be["parts"].append(pe)
        elif parts[0].get("file"):
            be["file"] = parts[0]["file"]
            be["sha256"] = self.sha(parts[0]["file"])
        rush_frame = (recipe.get("rush") or {}).get("frame")
        if rush_frame and not box_inside(frame_box(rush_frame), trim_box(frame, self.defaults()["trim_scale"])):
            raise MakeError(f"{recipe['id']}: rush.frame does not lie inside the day trim box")
        return be

    def fetch_divisions(self, batch, area, want=None, bbox=None, refresh=False, frozen=False, tag=None):
        """cache/overture/<release>/divisions/<key>.geojson: the cache, else the data tag, else Overture (A7 fetch)."""
        rel = batch["overture_release"]
        tag = tag or f"overture/{area['id']}-{rel}"
        p = self.divisions_path(rel, self.divisions_key(area))
        bbox = bbox or self.divisions_bbox(area)
        info = p + ".json"
        if os.path.exists(p) and not refresh:
            if want and self.sha(p) == want:
                return p
            if not want:
                try:
                    if read_json(info).get("bbox") == bbox:
                        return p
                except (FileNotFoundError, ValueError):
                    pass
        if want and self.restore_data_tag(tag, {self.rel(p): want}):
            return p
        if frozen and not want:
            raise MakeError("--frozen: the lock has no divisions entry; run make.py lock")
        self.run_py("fetch_boundary.py", ["fetch", "--release", rel, "--country", area["country"],
                                          "--region", area["region"], "--bbox", ",".join(str(v) for v in bbox),
                                          "--out", self.rel(p)])
        write_json(info, {"release": rel, "bbox": bbox})
        if want and self.sha(p) != want:
            raise MakeError(f"{self.rel(p)} has sha256 {self.sha(p)}, the lock pins {want}")
        self.fetched_from_overture.add(self.rel(p))
        return p

    def cmd_lock(self, name, refresh=False):
        batch = self.load_batch(name)
        old = self.load_lock(batch, required=False) or {}
        lock = {"batch": name, "locked_at": old.get("locked_at"), "areas": {}, "boundaries": {}, "tools": {}}
        for area in batch["areas"]:
            say(f"area {area['id']}")
            olda = old.get("areas", {}).get(area["id"], {})
            feeds = {}
            for f in area["feeds"]:
                feeds[f["id"]] = self.lock_feed(area, f, olda.get("feeds", {}).get(f["id"]), refresh)
                say(f"  feed {f['id']}: {feeds[f['id']]['sha256'][:12]} {feeds[f['id']]['bytes']} bytes")
            dkey = self.divisions_key(area)
            dbbox = self.divisions_bbox(area)
            olddiv = olda.get("divisions", {})
            keep = olddiv.get("sha256") if olddiv.get("bbox") == dbbox and olddiv.get("key") == dkey else None
            mine = [self.recipe(rid, batch) for rid in batch["cities"]
                    if self.area_of(batch, self.recipe(rid, batch))["id"] == area["id"]]
            if any(not b.get("file") for r in mine for b in boundary_parts(r)):
                dpath = self.fetch_divisions(batch, area, want=keep, bbox=dbbox, refresh=refresh,
                                             tag=olda.get("overture", {}).get("data_tag"))
                divisions = {"key": dkey, "sha256": self.sha(dpath), "bbox": dbbox}
            else:
                # Every boundary of the area is a file in the repo: no Overture divisions to fetch or pin.
                dpath, divisions = None, {"key": dkey, "sha256": None, "bbox": dbbox}
            partial = {"areas": {area["id"]: {"divisions": divisions}}}
            clips = []
            for rid in batch["cities"]:
                recipe = self.recipe(rid, batch)
                if self.area_of(batch, recipe)["id"] != area["id"]:
                    continue
                stamp, _ = self.step(f"{rid} boundary", self.boundary_key(batch, recipe, partial),
                                     self.path("build", rid, "stamps", "boundary.json"),
                                     lambda: (self.select_boundary(batch, recipe, dpath), None)[1],
                                     outputs=[self.path("build", rid, "boundary.geojson")])
                feat = read_json(self.path("build", rid, "boundary.geojson"))
                be = self.boundary_entry(batch, recipe, feat)
                oldb = old.get("boundaries", {}).get(rid, {})
                for k in ("week_eligible", "week_why"):
                    if k in oldb and not refresh:
                        be[k] = oldb[k]
                lock["boundaries"][rid] = be
                clips.append(be["clip"])
                say(f"  {rid}: {be['subtype']} {be['area_km2']} km2, frame {be['frame']['km_vertical']} km "
                    f"at {be['frame']['center_km']}")
            rel = batch["overture_release"]
            obbox = self.overture_bbox(area, clips) if clips else self.divisions_bbox(area)
            oldo = olda.get("overture", {})
            files = oldo.get("files", {}) if (not refresh and oldo.get("release") == rel and oldo.get("bbox") == obbox) else {}
            entry = {"feeds": feeds,
                     "overture": {"release": rel, "bbox": obbox, "data_tag": self.data_tag_of(area, rel, obbox, oldo),
                                  "files": files},
                     "divisions": divisions}
            same_feeds = {k: v["sha256"] for k, v in olda.get("feeds", {}).items()} == {k: v["sha256"] for k, v in feeds.items()}
            if not refresh and same_feeds:
                for k in ("dates", "rules"):
                    if k in olda:
                        entry[k] = olda[k]
            lock["areas"][area["id"]] = entry
        lock["tools"] = self.tool_versions()
        body = {k: v for k, v in lock.items() if k not in ("locked_at", "tools")}
        old_body = {k: v for k, v in old.items() if k not in ("locked_at", "tools")}
        if body != old_body or not lock["locked_at"]:
            lock["locked_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if body == old_body and old.get("tools"):
            lock["tools"] = old["tools"]
        self.save_lock(batch, lock)
        say(f"wrote {self.rel(self.lock_path(name))}")
        return lock

    # ---------------------------------------------------------- fetch (D4)

    def link_or_copy(self, src, dest):
        tmp = dest + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        try:
            os.link(os.path.realpath(src), tmp)
        except OSError:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)

    def fetch_feed(self, area, fid, fe):
        dest = self.path("cache", "feeds", area["id"], f"{fid}.zip")
        want = fe["sha256"]
        if self.sha(dest) == want:
            return "cached"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        local = self.path("data", area["id"], "gtfs", f"{fid}.zip")
        how = None
        if os.path.exists(local) and self.sha(local) == want:
            self.link_or_copy(local, dest)
            how = "linked"
        elif fe["source"] == "tag":
            ref = fe.get("tag")
            res = self.git("archive", "--format=tar", ref, fe["path"], binary=True, check=False)
            if res.returncode != 0:
                self.tag_commit(ref)
                res = self.git("archive", "--format=tar", fe.get("commit") or ref, fe["path"], binary=True)
            # Read the one member by name; nothing from the archive is unpacked to disk.
            with tarfile.open(fileobj=io.BytesIO(res.stdout)) as tf:
                data = tf.extractfile(tf.getmember(fe["path"])).read()
            with open(dest + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(dest + ".tmp", dest)
            how = f"git archive {ref}"
        elif fe["source"] == "path":
            self.link_or_copy(self.path(fe["path"]), dest)
            how = "copied"
        elif fe["source"] in ("url", "secret"):
            url = fe["url"]
            if fe["source"] == "secret":
                key = os.environ.get(fe["secret"])
                if not key:
                    raise MakeError(f"feed {fid}: ${fe['secret']} is not set")
                url = url.replace("{key}", key)
            self.download(url, dest, shown=fe["url"])
            how = "downloaded"
        else:
            raise MakeError(f"feed {fid}: cannot fetch a {fe['source']} source")
        got = self.sha(dest)
        if got != want:
            raise MakeError(f"feed {fid}: bytes changed: {self.rel(dest)} has sha256 {got}, the lock pins {want}")
        say(f"  feed {fid}: {how}")
        return how

    def restore_data_tag(self, tag, wanted):
        """Fill cache files {repo path: sha256} from the gzipped parts under a data tag. False when unavailable."""
        if self.git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False).returncode != 0:
            res = self.git("fetch", "--no-tags", "--depth=1", "origin", f"refs/tags/{tag}:refs/tags/{tag}", check=False)
            if res.returncode != 0:
                return False
        res = self.git("show", f"refs/tags/{tag}:MANIFEST.json", check=False)
        if res.returncode != 0:
            return False
        manifest = json.loads(res.stdout)
        for rel, want in sorted(wanted.items()):
            arc = os.path.relpath(rel, os.path.join("cache", "overture"))
            ent = manifest.get("files", {}).get(arc)
            if not ent:
                return False
            data = b"".join(self.git("show", f"refs/tags/{tag}:{part}", binary=True).stdout for part in ent["parts"])
            raw = gzip.decompress(data)
            if sha256_bytes(raw) != want:
                raise MakeError(f"data tag {tag}: {arc} has sha256 {sha256_bytes(raw)}, the lock pins {want}")
            dest = self.path(rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".tmp", "wb") as fh:
                fh.write(raw)
            os.replace(dest + ".tmp", dest)
        say(f"  restored {len(wanted)} file(s) from data tag {tag}")
        return True

    def bot_env(self):
        env = {}
        for who in ("AUTHOR", "COMMITTER"):
            env.setdefault(f"GIT_{who}_NAME", os.environ.get(f"GIT_{who}_NAME", "github-actions[bot]"))
            env.setdefault(f"GIT_{who}_EMAIL", os.environ.get(f"GIT_{who}_EMAIL",
                                                             "41898282+github-actions[bot]@users.noreply.github.com"))
        return env

    def commit_files(self, files, message):
        """A parentless commit holding {path in tree: bytes, or ("blob", sha) of an object already in the repo}."""
        fd, index = tempfile.mkstemp(prefix="make-index-")
        os.close(fd)
        os.remove(index)
        env = dict(self.bot_env(), GIT_INDEX_FILE=index)
        try:
            self.git("read-tree", "--empty", env=env)
            for arc, data in sorted(files.items()):
                if isinstance(data, tuple):
                    blob = data[1]
                else:
                    blob = self.git("hash-object", "-w", "--stdin", input=data, binary=True, env=env).stdout.decode().strip()
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{arc}", env=env)
            tree = self.git("write-tree", env=env).stdout.strip()
            return self.git("commit-tree", tree, "-m", message, env=env).stdout.strip()
        finally:
            if os.path.exists(index):
                os.remove(index)

    def push_data_tag(self, area_id, release, rels, tag=None):
        tag = tag or f"overture/{area_id}-{release}"
        manifest = {"release": release, "area": area_id, "files": {}}
        files = {}
        for rel in sorted(rels):
            arc = os.path.relpath(rel, os.path.join("cache", "overture"))
            raw = read_bytes(self.path(rel))
            data = gzip.compress(raw, compresslevel=6, mtime=0)
            # GitHub refuses blobs over 100 MB.
            step = 95_000_000
            parts = []
            for i in range(0, max(len(data), 1), step):
                name = f"{arc}.gz.{i // step:03d}"
                files[name] = data[i:i + step]
                parts.append(name)
            manifest["files"][arc] = {"sha256": sha256_bytes(raw), "bytes": len(raw), "parts": parts}
        files["MANIFEST.json"] = dump_json(manifest).encode()
        files["README.md"] = (f"Overture {release} extract for area {area_id}, pushed by render.yml (make.py fetch "
                              f"--push-data-tag). make.py fetch joins the parts, gunzips them and checks the sha256s "
                              f"in MANIFEST.json against the batch lock.\n").encode()
        commit = self.commit_files(files, f"chore: overture extract {area_id} {release}")
        # Forced: a tag left by a push that lacked a file is replaced; make.py checks every byte by sha256 anyway.
        self.git("push", "origin", f"+{commit}:refs/heads/data/overture-{area_id}", f"+{commit}:refs/tags/{tag}")
        say(f"  pushed {tag} ({commit[:12]})")

    def fetch_area_overture(self, batch, area, lock, frozen=False, push=False, check_only=False):
        lk = lock["areas"][area["id"]]
        ov = lk["overture"]
        rel = ov["release"]
        d = self.overture_dir(rel, area["id"])
        paths = {n: os.path.join(d, f"{n}.geojson") for n in ("segments", "water")}
        want = ov.get("files", {})
        dpath = self.divisions_path(rel, lk["divisions"]["key"]) if lk["divisions"].get("sha256") else None
        if check_only:
            for n, p in list(paths.items()) + ([("divisions", dpath)] if dpath else []):
                w = want.get(n) if n != "divisions" else lk["divisions"]["sha256"]
                if not w or self.sha(p) != w:
                    raise MakeError(f"{self.rel(p)} is missing or differs from the lock, and --no-upstream fetches nothing")
            return
        if dpath:
            self.fetch_divisions(batch, area, want=lk["divisions"]["sha256"], bbox=lk["divisions"].get("bbox"),
                                 frozen=frozen, tag=ov["data_tag"])
        ok = all(want.get(n) and self.sha(p) == want[n] for n, p in paths.items())
        if not ok and all(want.get(n) for n in paths):
            ok = self.restore_data_tag(ov["data_tag"], {self.rel(p): want[n] for n, p in paths.items()})
        if not ok:
            info = os.path.join(d, "fetch.json")
            try:
                same = read_json(info) == {"release": rel, "bbox": ov["bbox"]}
            except (FileNotFoundError, ValueError):
                same = False
            if not (same and all(os.path.exists(p) for p in paths.values())):
                if frozen and not all(want.get(n) for n in paths):
                    raise MakeError("--frozen: the lock has no Overture file sha256s; run make.py fetch locally")
                self.run_py("fetch_overture.py", ["segments", "water"],
                            env={"BBOX": ",".join(str(v) for v in ov["bbox"]), "OUT": d, "OVERTURE_RELEASE": rel})
                write_json(info, {"release": rel, "bbox": ov["bbox"]})
                for p in paths.values():
                    self.fetched_from_overture.add(self.rel(p))
            got = {n: self.sha(p) for n, p in paths.items()}
            if all(want.get(n) for n in paths):
                bad = [n for n in paths if got[n] != want[n]]
                if bad:
                    raise MakeError(f"Overture {rel} {', '.join(bad)} for {area['id']} differ from the lock "
                                    f"(lock --refresh moves to new bytes)")
            elif frozen:
                raise MakeError("--frozen: the lock has no Overture file sha256s")
            else:
                ov["files"] = got
                self.save_lock(batch, lock)
                say(f"  recorded the Overture file sha256s in {self.rel(self.lock_path(batch['batch']))}")
        if push:
            # Pushed by whoever fetched from Overture's bucket, or by anyone holding the locked bytes while origin
            # lacks the tag: then a runner that cannot reproduce the extract byte for byte still gets it.
            on_origin = self.git("ls-remote", "--tags", "origin", f"refs/tags/{ov['data_tag']}", check=False).stdout.strip()
            if self.fetched_from_overture or not on_origin:
                self.push_data_tag(area["id"], rel, sorted(set(self.rel(p) for p in paths.values())
                                                           | ({self.rel(dpath)} if dpath else set())), tag=ov["data_tag"])
            else:
                say(f"  data tag {ov['data_tag']} is already on origin")

    def areas_selected(self, batch, area=None):
        out = [a for a in batch["areas"] if area in (None, a["id"])]
        if not out:
            raise MakeError(f"batch {batch['batch']} has no area {area!r}")
        return out

    def cmd_fetch(self, name, area=None, feeds_only=False, overture_only=False, push_data_tag=False, frozen=False):
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        if frozen:
            self.check_frozen(lock)
        for a in self.areas_selected(batch, area):
            say(f"area {a['id']}")
            if not overture_only:
                for fid, fe in sorted(lock["areas"][a["id"]]["feeds"].items()):
                    self.fetch_feed(a, fid, fe)
            if not feeds_only:
                self.fetch_area_overture(batch, a, lock, frozen=frozen, push=push_data_tag)

    # ---------------------------------------------------------- build (A2 command lines)

    def store_dates(self, store_dir):
        """Dates and rules per feed from an area store's meta.json, in the shapes of 2.9 meta.feeds[].

        build_area.py keeps them per day class in feeds[].by_class (wd for a day
        store, mon..sun for a week store); the lock and the render keys want 2.9's.
        """
        meta = read_json(os.path.join(store_dir, "meta.json"))
        dates, rules = {}, {}
        for f in meta.get("feeds") or []:
            flat = self.flatten_store_feed(f["by_class"])
            fid, rule, med = f["id"], flat["rule"], flat["median_date"]
            dates[fid] = flat["dates"]
            if isinstance(rule, dict):
                rules[fid] = {k: (f"median-date {med[k]}" if r == "median-date" else r) for k, r in sorted(rule.items())}
            else:
                rules[fid] = f"median-date {med}" if rule == "median-date" else rule
        return dates, rules

    @staticmethod
    def flatten_store_feed(by_class):
        """One store feed's dates and rules in the shapes of 2.9: a day store's `wd` is flat, a week's per day."""
        if set(by_class) == {"wd"}:
            c = by_class["wd"]
            return {"dates": c["dates"], "rule": c["rule"], "median_date": c.get("median_date")}
        md = {k: c["median_date"] for k, c in sorted(by_class.items()) if c.get("rule") == "median-date"}
        return {"dates": {k: c["dates"] for k, c in sorted(by_class.items())},
                "rule": {k: c["rule"] for k, c in sorted(by_class.items())}, "median_date": md}

    def record_dates(self, batch, area, tl, lock, frozen, store_dir):
        dates, rules = self.store_dates(store_dir)
        lk = lock["areas"][area["id"]]
        had_d, had_r = lk.get("dates", {}).get(tl), lk.get("rules", {}).get(tl)
        if had_d == dates and had_r == rules:
            return
        if frozen:
            what = "has no" if had_d is None else "differs from the area build's"
            raise MakeError(f"--frozen: the lock {what} {tl} dates for area {area['id']}; "
                            f"run make.py build locally and commit the lock")
        if had_d is not None:
            changed = sorted(f for f in set(dates) | set(had_d) if dates.get(f) != had_d.get(f))
            warn(f"area {area['id']} {tl}: dates changed for {', '.join(changed) or 'rules only'}; the lock is updated")
        lk.setdefault("dates", {})[tl] = dates
        lk.setdefault("rules", {})[tl] = rules
        self.save_lock(batch, lock)
        say(f"  recorded the {tl} dates of area {area['id']} in the lock (render keys include them)")

    def area_store(self, batch, area, tl, lock, frozen=False, no_upstream=False):
        key = self.area_key(batch, area, tl, lock)
        out = self.path("build", "areas", area["id"], tl)
        cfg_path = self.path("build", "areas", area["id"], f"area.{tl}.json")
        cfg = self.area_config(batch, area, tl, lock)
        if not no_upstream:
            write_json(cfg_path, cfg)

        def run():
            for fid, fe in sorted(lock["areas"][area["id"]]["feeds"].items()):
                self.fetch_feed(area, fid, fe)
            if os.path.isdir(out):
                shutil.rmtree(out)
            os.makedirs(out)
            self.run_py("build_area.py", ["--config", self.rel(cfg_path), "--out", self.rel(out), "--key", key])

        self.step(f"area {area['id']} {tl}", key, os.path.join(out, "stamp.json"), run, out_dir=out,
                  no_upstream=no_upstream)
        self.record_dates(batch, area, tl, lock, frozen, out)
        return key

    def set_week_eligibility(self, batch, lock, rid, eligible, why, frozen):
        be = lock["boundaries"][rid]
        now = (be.get("week_eligible"), be.get("week_why"))
        want = (None, None) if eligible else (False, why)
        if now == want:
            return
        if frozen:
            warn(f"{rid}: week eligibility is {eligible} but the lock says otherwise; the render job will skip it")
            return
        be.pop("week_eligible", None)
        be.pop("week_why", None)
        if not eligible:
            be["week_eligible"], be["week_why"] = False, why
        self.save_lock(batch, lock)

    def build_city(self, batch, recipe, lock, frozen=False, no_upstream=False, timeline=None):
        rid = recipe["id"]
        area = self.area_of(batch, recipe)
        be = self.city_lock(lock, rid)
        bdir = self.path("build", rid)
        has_week = "week" in recipe_variants(recipe, batch)
        trims = {"day": timeline in (None, "day"), "week": has_week and timeline in (None, "week")}
        say(f"city {rid}")
        # Area stores: under --no-upstream only checked; a week store that the plan did not restore means no
        # week render is pending for this city.
        for tl in ("day", "week"):
            if not trims[tl]:
                continue
            if no_upstream:
                key = self.area_key(batch, area, tl, lock)
                sp = self.path("build", "areas", area["id"], tl, "stamp.json")
                if tl == "week" and not os.path.exists(sp):
                    say("  week area store not present: skipping the week trim")
                    trims["week"] = False
                    continue
                self.step(f"area {area['id']} {tl}", key, sp, None, no_upstream=True)
                self.record_dates(batch, area, tl, lock, frozen, os.path.dirname(sp))
            else:
                self.area_store(batch, area, tl, lock, frozen=frozen)
        lk = lock["areas"][area["id"]]
        if no_upstream:
            self.fetch_area_overture(batch, area, lock, check_only=True)
        elif (self.step_current(self.boundary_key(batch, recipe, lock), os.path.join(bdir, "stamps", "boundary.json"))
              and self.step_current(self.basemap_key(batch, recipe, lock), os.path.join(bdir, "stamps", "basemap.json"))):
            # Only the boundary and the basemap read Overture. A city whose two
            # steps are current needs no extract, so a cache emptied to save disk
            # (a New York extract is 485 MB) is not pulled again for a trim.
            say(f"  {rid}: boundary and basemap up to date, Overture extracts not needed")
        else:
            self.fetch_area_overture(batch, area, lock, frozen=frozen)
        dpath = self.divisions_path(lk["overture"]["release"], lk["divisions"]["key"])
        bpath = os.path.join(bdir, "boundary.geojson")
        self.step(f"{rid} boundary", self.boundary_key(batch, recipe, lock), os.path.join(bdir, "stamps", "boundary.json"),
                  lambda: (self.select_boundary(batch, recipe, dpath), None)[1], outputs=[bpath])
        frame = self.frame_of_boundary(area, recipe, read_json(bpath))
        if frame != be["frame"]:
            msg = f"{rid}: the boundary gives frame {frame}, the lock has {be['frame']}"
            if frozen:
                raise MakeError("--frozen: " + msg)
            warn(msg + "; keeping the lock's (make.py lock recomputes it)")
        tb = self.trim_box_deg(area, be["frame"], read_json(bpath))
        if not box_inside(tb, area["area_box"]):
            raise MakeError(f"{rid}: the trim box {[round(v, 3) for v in tb]} is not "
                            f"inside area_box {area['area_box']} (make.py show {batch['batch']} suggests one)")
        if not box_inside(be["clip"], lk["overture"]["bbox"]):
            raise MakeError(f"{rid}: clip {be['clip']} is not inside the Overture extract bbox {lk['overture']['bbox']}")
        bsha = self.sha(bpath)
        day_net = os.path.join(bdir, "day", "network.json.gz")
        week_net = os.path.join(bdir, "week", "network.json.gz")
        store = lambda tl: self.rel(self.path("build", "areas", area["id"], tl))
        if trims["day"]:
            cfg_path = os.path.join(bdir, "city.day.json")
            write_json(cfg_path, self.city_config(batch, recipe, "day", lock))
            key = self.trim_key(batch, recipe, "day", lock, bsha)
            self.step(f"{rid} trim day", key, os.path.join(bdir, "stamps", "trim-day.json"),
                      lambda: (self.run_py("trim_network.py", ["--config", self.rel(cfg_path), "--area", store("day"),
                                                               "--out", self.rel(day_net), "--key", key]), None)[1],
                      outputs=[day_net])
        if trims["week"]:
            if not os.path.exists(day_net):
                raise MakeError(f"{rid}: the week trim needs {self.rel(day_net)}")
            cfg_path = os.path.join(bdir, "city.week.json")
            write_json(cfg_path, self.city_config(batch, recipe, "week", lock))
            key = self.trim_key(batch, recipe, "week", lock, bsha, self.sha(day_net))

            def week():
                if os.path.exists(week_net):
                    os.remove(week_net)
                res = self.run_py("trim_network.py", ["--config", self.rel(cfg_path), "--area", store("week"),
                                                      "--day-network", self.rel(day_net), "--out", self.rel(week_net),
                                                      "--key", key],
                                  ok_codes=(0, 3), capture=True)
                sys.stdout.write(res.stdout)
                if res.returncode == 3:
                    line = [ln for ln in res.stdout.strip().split("\n") if ln.startswith("{")]
                    info = json.loads(line[-1]) if line else {"why": ["no reason printed"]}
                    return {"eligible": False, "why": info.get("why", [])}
                return {"eligible": True}

            stamp, _ = self.step(f"{rid} trim week", key, os.path.join(bdir, "stamps", "trim-week.json"), week,
                                 outputs=[week_net])
            if stamp.get("eligible") is False:
                say(f"  {rid}: week not eligible: {'; '.join(stamp.get('why', []))}")
            self.set_week_eligibility(batch, lock, rid, stamp.get("eligible", True), stamp.get("why", []), frozen)
        cfg_path = os.path.join(bdir, "basemap.config.json")
        write_json(cfg_path, self.basemap_config(batch, recipe, lock))
        key = self.basemap_key(batch, recipe, lock)
        out = os.path.join(bdir, "basemap.json.gz")
        self.step(f"{rid} basemap", key, os.path.join(bdir, "stamps", "basemap.json"),
                  lambda: (self.run_py("basemap_v4.py", ["--config", self.rel(cfg_path), "--key", key]), None)[1],
                  outputs=[out])
        write_text_if_changed(out + ".key", key + "\n")
        self.write_manifest(rid)

    def write_manifest(self, rid):
        """build/<id>/manifest.json: every stamped output of the city with its step key."""
        bdir = self.path("build", rid)
        man = {"build_key": None, "keys": {}, "files": {}, "week": None}
        for step in ("boundary", "trim-day", "trim-week", "basemap"):
            try:
                st = read_json(os.path.join(bdir, "stamps", f"{step}.json"))
            except (FileNotFoundError, ValueError):
                continue
            if not self.stamp_ok(st):
                continue
            man["keys"][step] = st["key"]
            man["files"].update(st.get("outputs", {}))
            if step == "trim-week":
                man["week"] = {"eligible": st.get("eligible", True), "why": st.get("why", [])}
        man["build_key"] = man["keys"].get("trim-day")
        write_json(os.path.join(bdir, "manifest.json"), man)
        return man

    def cmd_build(self, name, area=None, city=None, area_only=False, timeline=None, frozen=False, no_upstream=False):
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        if frozen:
            self.check_frozen(lock)
        if city and city not in batch["cities"]:
            raise MakeError(f"{city} is not a city of batch {name}")
        cities = [c for c in ([city] if city else batch["cities"])
                  if area in (None, self.area_of(batch, self.recipe(c, batch))["id"])]
        if area_only:
            for a in self.areas_selected(batch, area):
                mine = [self.recipe(c, batch) for c in batch["cities"] if self.area_of(batch, self.recipe(c, batch))["id"] == a["id"]]
                tls = ["day"] + (["week"] if any("week" in recipe_variants(r, batch) for r in mine) else [])
                for tl in tls:
                    if timeline in (None, tl):
                        self.area_store(batch, a, tl, lock, frozen=frozen, no_upstream=no_upstream)
            return
        for c in cities:
            self.build_city(batch, self.recipe(c, batch), lock, frozen=frozen, no_upstream=no_upstream, timeline=timeline)

    # ---------------------------------------------------------- networks and netmeta

    def network_path(self, rid, variant):
        return self.path("build", rid, TIMELINE_OF[variant], "network.json.gz")

    def network_meta(self, path):
        """meta of a network file without parsing its trips: meta comes first in the builders' output."""
        dec = json.JSONDecoder()
        buf = ""
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            while True:
                chunk = fh.read(1 << 20)
                buf += chunk
                i = buf.find('"meta":')
                if i >= 0:
                    j = buf.index("{", i) if "{" in buf[i:] else -1
                    if j >= 0:
                        try:
                            meta, _ = dec.raw_decode(buf, j)
                            return meta
                        except json.JSONDecodeError:
                            pass
                if not chunk:
                    break
                if len(buf) > (64 << 20):
                    break
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)["meta"]

    def effective_render(self, recipe, variant, meta):
        V = meta["variants"][variant]
        rj, bh = self.render_query(recipe, variant)
        r = dict(V.get("render", {}))
        r.update(rj)
        frames = int(r.get("HOLD_START", 0)) + int(r.get("DURATION_FRAMES", 0)) + int(r.get("HOLD_END", 0))
        return rj, bh, r, frames

    def netmeta(self, batch, recipe, variant, meta, build_key):
        """2.13 network metadata: all that make.py meta needs, so metadata can be rebuilt without the network."""
        V = meta["variants"][variant]
        _rj, _bh, _r, frames = self.effective_render(recipe, variant, meta)
        seconds = frames / FPS
        seconds = int(seconds) if seconds == int(seconds) else round(seconds, 1)
        keep = recipe_modes(recipe, batch)
        modes = [{k: v for k, v in m.items() if k != "routes"} for m in batch["modes"] if m["id"] in keep]
        modes_present = modes_in_window(modes, meta.get("hist_by_mode", {}), V["start"], V["end"])
        keys = ("id", "name", "publisher", "licence_id", "licence_text", "dates", "rule", "median_date", "excluded",
                "inside_share", "inside_vehicle_minutes", "major", "month_used")
        feeds = [{k: f.get(k) for k in keys if k in f} for f in meta.get("feeds", [])]
        tl = meta.get("timeline", {})
        return {
            "id": recipe["id"], "variant": variant, "place": meta.get("place", recipe["place"]),
            "title": meta.get("title", recipe["place"].upper()), "label": V.get("label"),
            "month_label": tl.get("month_label"), "month": tl.get("month"),
            "timeline": {k: tl.get(k) for k in ("kind", "period", "basis", "fallback_feeds") if k in tl},
            "peak": V["peak"], "am_peak": meta.get("am_peak"), "pm_peak": meta.get("pm_peak"),
            "window": [V.get("start"), V.get("end")],
            "feeds": feeds, "modes_present": modes_present, "modes": modes,
            "groups": meta.get("groups", []), "credit": meta.get("credit"),
            "seconds": seconds, "frames": frames, "trips_total": meta.get("trips_total"), "build_key": build_key,
        }

    def write_netmeta(self, batch, recipe, variant, out_dir=None):
        net = self.network_path(recipe["id"], variant)
        meta = self.network_meta(net)
        if variant not in meta.get("variants", {}):
            raise MakeError(f"{self.rel(net)} has no variant {variant}")
        man = self.read_manifest(recipe["id"])
        nm = self.netmeta(batch, recipe, variant, meta, (man or {}).get("keys", {}).get(f"trim-{TIMELINE_OF[variant]}")
                          or meta.get("build_key"))
        p = os.path.join(out_dir or self.out_dir(batch), f"{stem_of(recipe['id'], variant)}.netmeta.json")
        write_json(p, nm)
        return p, meta

    def read_manifest(self, rid):
        try:
            return read_json(self.path("build", rid, "manifest.json"))
        except (FileNotFoundError, ValueError):
            return None

    def out_dir(self, batch):
        return self.path("out", "shorts", batch["batch"])

    def week_skipped(self, rid, variant, lock):
        if variant != "week":
            return None
        be = lock.get("boundaries", {}).get(rid, {})
        if be.get("week_eligible") is False:
            return be.get("week_why") or ["not eligible"]
        man = self.read_manifest(rid)
        if man and man.get("week") and man["week"].get("eligible") is False:
            return man["week"].get("why") or ["not eligible"]
        return None

    def check_upstream(self, batch, recipe, variant, lock):
        """--no-upstream: build/<id>/manifest.json must list the inputs with matching hashes and step keys."""
        rid = recipe["id"]
        man = self.read_manifest(rid)
        if not man:
            raise MakeError(f"build/{rid}/manifest.json is missing, and --no-upstream builds nothing")
        tl = TIMELINE_OF[variant]
        bpath = f"build/{rid}/boundary.geojson"
        need = [bpath, f"build/{rid}/{tl}/network.json.gz", f"build/{rid}/basemap.json.gz"]
        files = man.get("files") or {}
        for rel in need:
            if files.get(rel) is None or self.sha(rel) != files[rel]:
                raise MakeError(f"{rel} is missing or differs from build/{rid}/manifest.json")
        day_sha = self.sha(f"build/{rid}/day/network.json.gz") if tl == "week" else None
        want = {f"trim-{tl}": self.trim_key(batch, recipe, tl, lock, self.sha(bpath), day_sha),
                "basemap": self.basemap_key(batch, recipe, lock)}
        keys = man.get("keys") or {}
        for step, key in want.items():
            if step not in keys:
                raise MakeError(f"build/{rid}/manifest.json has no {step} key: the last build of {rid} did not finish")
            if keys[step] != key:
                raise MakeError(f"build/{rid}: the {step} key in manifest.json does not match this checkout's inputs")

    def ensure_city(self, batch, recipe, lock, no_upstream=False, variant=None):
        if no_upstream:
            self.check_upstream(batch, recipe, variant or "day", lock)
        else:
            self.build_city(batch, recipe, lock)
        self.memo.save()

    def common_render_args(self, recipe, variant, meta):
        rj, bh, _r, _frames = self.effective_render(recipe, variant, meta)
        args = ["--data", self.rel(self.network_path(recipe["id"], variant)),
                "--basemap", f"build/{recipe['id']}/basemap.json.gz",
                "--variant", variant, "--render-json", canonical(rj)]
        if bh:
            args += ["--brandhex", bh]
        return args

    # ---------------------------------------------------------- stills, preview, render

    def still_times(self, meta, variant):
        """busmap.stillTimes() of B15, from the network meta, as absolute seconds inside the window."""
        V = meta["variants"][variant]
        w0 = V["start"]
        period = (meta.get("timeline") or {}).get("period") or (86400 if variant != "week" else 604800)
        am, pm = meta["am_peak"]["time"], meta["pm_peak"]["time"]
        if variant == "day":
            ts = [am, 45000, pm, 81000, 9000]
        elif variant == "rush":
            ts = [V["peak"]["time"], 24300, 28800, 33300]
        else:
            ts = [am, 2 * 86400 + 45000, 4 * 86400 + pm, 5 * 86400 + 46800, 6 * 86400 + 82800]
        return [int(t + period if t < w0 else t) for t in ts]

    @staticmethod
    def still_frames(variant, n):
        if variant == "week":
            return [0, n - 1]
        return [0, 15, 45, n - 31, n - 1]

    @staticmethod
    def still_names(stem, t, week):
        h, m = int(t // 3600), int(t % 3600 // 60)
        if week:
            ddd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][(int(t) // 86400) % 7]
            d = int(t) % 86400
            return [f"{stem}-{ddd}-{d // 3600:02d}{d % 3600 // 60:02d}"]
        return [f"{stem}-{h:02d}{m:02d}", f"{stem}-{h % 24:02d}{m:02d}"]

    def find_still(self, out_dir, stem, t, week):
        for n in self.still_names(stem, t, week):
            p = os.path.join(out_dir, n + ".png")
            if os.path.exists(p):
                return p
        return None

    def roundtrip_png(self, src, dest):
        """What a phone shows after YouTube's transcode: 720p VP9 at 1.5 Mb/s and back (C5's filter)."""
        ff = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        with tempfile.TemporaryDirectory() as td:
            webm = os.path.join(td, "rt.webm")
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vf", "scale=720:1280:flags=area",
                            "-c:v", "libvpx-vp9", "-b:v", "1500k", "-row-mt", "1", "-frames:v", "1", webm], check=True)
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", webm, "-frames:v", "1", dest], check=True)

    def cmd_stills(self, rid, variant=None, extra_query=None):
        recipe = self.recipe(rid)
        batch = self.load_batch(recipe["batch"])
        lock = self.load_lock(batch)
        self.ensure_city(batch, recipe, lock)
        import tune
        sheets = []
        for v in ([variant] if variant else recipe_variants(recipe, batch)):
            if self.week_skipped(rid, v, lock):
                say(f"{rid} {v}: not eligible, no stills")
                continue
            meta = self.network_meta(self.network_path(rid, v))
            _rj, _bh, _r, n = self.effective_render(recipe, v, meta)
            slug = ("-" + re.sub(r"[^a-z0-9]+", "-", extra_query.lower()).strip("-")) if extra_query else ""
            out = self.path("build", rid, "stills", v + slug)
            os.makedirs(out, exist_ok=True)
            stem = stem_of(rid, v)
            times = self.still_times(meta, v)
            frames = self.still_frames(v, n)
            base = self.common_render_args(recipe, v, meta)
            q = extra_query or ""
            self.run_node(base + ["--tier", "stills", "--out-dir", self.rel(out), "--name", stem,
                                  "--times", ",".join(str(t) for t in times), "--frames", ",".join(str(f) for f in frames)]
                          + (["--query", q] if q else []))
            tiles = []
            for t in times:
                p = self.find_still(out, stem, t, v == "week")
                if p:
                    tiles.append((p, tune.clock_label(t, v == "week")))
            for f in frames:
                p = os.path.join(out, f"{stem}-f{f:04d}.png")
                if os.path.exists(p):
                    tiles.append((p, f"frame {f}"))
            if v == "day":
                self.run_node(base + ["--tier", "stills", "--out-dir", self.rel(out), "--name", stem + "-safe",
                                      "--times", str(times[0]), "--query", "&".join(x for x in ("safe=1", q) if x)])
                p = self.find_still(out, stem + "-safe", times[0], False)
                if p:
                    tiles.append((p, "safe=1"))
            am = self.find_still(out, stem, times[0], v == "week")
            if am:
                rt = os.path.join(out, f"{stem}-rt.png")
                self.roundtrip_png(am, rt)
                tiles.append((rt, "am peak, VP9 720p"))
            sheet = os.path.join(out, f"{stem}.sheet.jpg")
            tune.tile_sheet([("", tiles)], sheet, cols=5)
            sheets.append(sheet)
            say(f"sheet {self.rel(sheet)}")
        return sheets

    def capture_args(self):
        """defaults.json's capture method, and its page count for raw: render_video's own default is
        min(3, CPUs - 1) pages, which oversubscribes the 4 vCPUs C3 measured 2 pages on."""
        d = self.defaults()["render"]
        return ["--capture", d["capture"]] + (["--jobs", d["jobs"]] if d["capture"] == "raw" else [])

    def cmd_preview(self, rid, variant):
        recipe = self.recipe(rid)
        batch = self.load_batch(recipe["batch"])
        lock = self.load_lock(batch)
        self.ensure_city(batch, recipe, lock)
        if self.week_skipped(rid, variant, lock):
            raise MakeError(f"{rid} has no eligible {variant}")
        meta = self.network_meta(self.network_path(rid, variant))
        stem = stem_of(rid, variant)
        out = os.path.join(self.out_dir(batch), f"{stem}.preview.mp4")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        self.run_node(self.common_render_args(recipe, variant, meta) + ["--tier", "preview", "--out", self.rel(out),
                                                                        "--name", stem] + self.capture_args())
        say(f"preview {self.rel(out)}")
        return out

    def cmd_render(self, rid, variant, tier="final", no_upstream=False, review_dir=None):
        recipe = self.recipe(rid)
        batch = self.load_batch(recipe["batch"])
        lock = self.load_lock(batch)
        if variant not in recipe_variants(recipe, batch):
            raise MakeError(f"{rid} has no {variant} variant")
        skip = self.week_skipped(rid, variant, lock)
        if not skip:
            self.ensure_city(batch, recipe, lock, no_upstream=no_upstream, variant=variant)
            skip = self.week_skipped(rid, variant, lock)
        if skip:
            say(f"{rid} week: not eligible ({'; '.join(skip)}); nothing to render")
            return None
        stem = stem_of(rid, variant)
        out_dir = self.out_dir(batch)
        os.makedirs(out_dir, exist_ok=True)
        mp4, side = os.path.join(out_dir, f"{stem}.mp4"), os.path.join(out_dir, f"{stem}.json")
        key = self.render_key(batch, recipe, variant, lock, tier)
        _p, meta = self.write_netmeta(batch, recipe, variant)
        rdir = os.path.join(os.path.abspath(review_dir), stem) if review_dir else None
        try:
            sc = read_json(side)
        except (FileNotFoundError, ValueError):
            sc = None
        if sc and sc.get("key") == key and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            say(f"{stem}: up to date ({key[:16]})")
        else:
            d = self.defaults()["render"]
            args = self.common_render_args(recipe, variant, meta) + [
                "--tier", tier, "--out", self.rel(mp4), "--name", stem, "--key", key] + self.capture_args()
            args += ["--min-kbps", d["min_kbps"], "--crf-ladder", ",".join(str(c) for c in d["crf_ladder"]),
                     "--keep-frames", ",".join(str(f) for f in d["keep_frames"])]
            if rdir:
                os.makedirs(rdir, exist_ok=True)
                args += ["--review-dir", rdir]
            sc_line = self.run_node(args)
            if not os.path.exists(side) and sc_line:
                write_json(side, sc_line)
            if not os.path.exists(mp4) or not os.path.exists(side):
                raise MakeError(f"render_video.mjs did not write {self.rel(mp4)} and its sidecar")
            sc = read_json(side)
            if sc.get("key") != key:
                sc["key"] = key
                write_json(side, sc)
        if rdir:
            os.makedirs(rdir, exist_ok=True)
            shutil.copyfile(side, os.path.join(rdir, f"{stem}.json"))
        say(f"{self.rel(mp4)}  key:{key[:16]}")
        return mp4

    # ---------------------------------------------------------- metadata (D5)

    def meta_for(self, batch, lock, rid, variant, netmeta, sidecar=None, allow_nc=False):
        import shorts_meta
        recipe = self.recipe(rid, batch)
        rk = self.render_key(batch, recipe, variant, lock)
        return shorts_meta.build_meta(
            batch=batch, recipe=recipe, netmeta=netmeta, templates=self.templates(), licences=self.licences(),
            defaults=self.defaults(), publish_order=self.publish_order(batch, rid, variant, lock),
            meta_key=self.meta_key(batch, recipe, variant, lock, rk), sidecar=sidecar, allow_nc=allow_nc)

    def cmd_meta(self, name, only=None, from_netmeta=None, no_upstream=False, allow_nc=False, review_dir=None,
                 out_dir=None):
        import shorts_meta
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        out = out_dir or self.out_dir(batch)
        os.makedirs(out, exist_ok=True)
        src = os.path.abspath(from_netmeta) if from_netmeta else out
        videos = self.videos(batch, lock)
        if only:
            pick = split_stem(only, batch["cities"])
            if pick not in videos:
                raise MakeError(f"{only} is not a video of batch {name}")
            videos = [pick]
        metas, failures = [], []
        for rid, v in videos:
            stem = stem_of(rid, v)
            nm_path = os.path.join(src, f"{stem}.netmeta.json")
            if not os.path.exists(nm_path):
                if from_netmeta:
                    say(f"  {stem}: no netmeta in {from_netmeta}, skipped")
                    continue
                if no_upstream:
                    raise MakeError(f"{self.rel(nm_path)} is missing, and --no-upstream builds nothing")
                recipe = self.recipe(rid, batch)
                if self.week_skipped(rid, v, lock) is None:
                    self.build_city(batch, recipe, lock)
                if self.week_skipped(rid, v, lock):
                    say(f"  {stem}: week not eligible, skipped")
                    continue
                self.write_netmeta(batch, recipe, v, out_dir=src)
            netmeta = read_json(nm_path)
            side = os.path.join(src, f"{stem}.json")
            sidecar = read_json(side) if os.path.exists(side) else None
            try:
                m = self.meta_for(batch, lock, rid, v, netmeta, sidecar=sidecar, allow_nc=allow_nc)
            except shorts_meta.MetaError as e:
                failures.append(f"{stem}: {e}")
                continue
            p = os.path.join(out, f"{stem}.meta.json")
            write_json(p, m)
            if review_dir:
                rdir = os.path.join(os.path.abspath(review_dir), stem)
                os.makedirs(rdir, exist_ok=True)
                shutil.copyfile(p, os.path.join(rdir, f"{stem}.meta.json"))
            metas.append(m)
            say(f"  {stem}: {m['title']}")
        if failures:
            raise MakeError("metadata checks failed:\n  " + "\n  ".join(failures))
        if not only:
            csv_path = os.path.join(out, f"{name}-youtube.csv")
            shorts_meta.write_csv(csv_path, metas)
            say(f"wrote {self.rel(csv_path)} ({len(metas)} rows)")
        return metas

    # ---------------------------------------------------------- show (D3.3)

    def boundary_feature(self, rid):
        """build/<id>/boundary.geojson once lock or build selected it, else None."""
        try:
            return read_json(self.path("build", rid, "boundary.geojson"))
        except FileNotFoundError:
            return None

    def area_box_suggestion(self, batch, area, lock):
        boxes = []
        for rid in batch["cities"]:
            recipe = self.recipe(rid, batch)
            if self.area_of(batch, recipe)["id"] != area["id"] or rid not in lock.get("boundaries", {}):
                continue
            boxes.append(grow_deg(area["origin"], self.trim_box_deg(area, lock["boundaries"][rid]["frame"],
                                                                    self.boundary_feature(rid)), 2.0))
        if not boxes:
            return None
        u = union_box(boxes)
        return [floor_to(u[0]), floor_to(u[1]), ceil_to(u[2]), ceil_to(u[3])]

    def keys_of(self, batch, rid, lock):
        recipe = self.recipe(rid, batch)
        out = {}
        for v in recipe_variants(recipe, batch):
            if self.week_skipped(rid, v, lock):
                out[v] = {"skipped": "week not eligible"}
                continue
            rk = self.render_key(batch, recipe, v, lock)
            out[v] = {"render_key": rk, "meta_key": self.meta_key(batch, recipe, v, lock, rk)}
        return out

    def cmd_show(self, target):
        if os.path.exists(self.batch_path(target)):
            batch = self.load_batch(target)
            lock = self.load_lock(batch, required=False)
            info = {"batch": target, "cities": batch["cities"], "areas": {}}
            if lock:
                for a in batch["areas"]:
                    tls = {tl: self.area_key(batch, a, tl, lock) for tl in ("day", "week")}
                    info["areas"][a["id"]] = {
                        "area_box": a["area_box"], "area_box_suggestion": self.area_box_suggestion(batch, a, lock),
                        "overture_bbox": lock["areas"][a["id"]]["overture"]["bbox"],
                        "keys": {"area_day": tls["day"], "area_week": tls["week"],
                                 "overture": self.overture_key(a, lock)},
                        "config_day": self.area_config(batch, a, "day", lock)}
                info["videos"] = {rid: self.keys_of(batch, rid, lock) for rid in batch["cities"]}
                info["publish_order"] = [stem_of(c, v) for c, v in self.videos(batch, lock)]
            else:
                info["note"] = f"no lock yet: make.py lock {target}"
                for a in batch["areas"]:
                    info["areas"][a["id"]] = {"area_box": a["area_box"], "divisions_bbox": self.divisions_bbox(a)}
            say(dump_json(info).rstrip())
            return info
        recipe = self.recipe(target)
        batch = self.load_batch(recipe["batch"])
        lock = self.load_lock(batch)
        area = self.area_of(batch, recipe)
        feat = self.boundary_feature(target)
        bbox_km = self.boundary_bbox_km(area, feat) if feat else None
        info = {"id": target, "batch": batch["batch"], "area": area["id"],
                "boundary": lock["boundaries"].get(target),
                "trim_box_km": [round(v, 3) for v in trim_box(self.city_lock(lock, target)["frame"],
                                                              self.defaults()["trim_scale"], bbox_km)],
                "city.day.json": self.city_config(batch, recipe, "day", lock),
                "basemap.config.json": self.basemap_config(batch, recipe, lock),
                "queries": {v: dict(zip(("render", "brandhex"), self.render_query(recipe, v)))
                            for v in recipe_variants(recipe, batch)},
                "keys": self.keys_of(batch, target, lock),
                "area_keys": {tl: self.area_key(batch, area, tl, lock) for tl in ("day", "week")}}
        if "week" in recipe_variants(recipe, batch):
            info["city.week.json"] = self.city_config(batch, recipe, "week", lock)
        say(dump_json(info).rstrip())
        return info

    # ---------------------------------------------------------- Actions: plan, release, review (D6)

    def licence_failures(self, batch, allow_nc=False):
        import shorts_meta
        out = []
        for a in batch["areas"]:
            for f in a["feeds"]:
                _entry, err = shorts_meta.licence_entry(f, self.licences(), allow_nc=allow_nc)
                if err:
                    out.append(err)
        return out

    def plan(self, branch, gh=None):
        m = re.match(r"^batch/([a-z0-9][a-z0-9-]{1,39})$", branch or "")
        if not m:
            raise MakeError(f"--from-branch must be batch/<batch>, got {branch!r}")
        name = m.group(1)
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        bad = self.licence_failures(batch)
        if bad:
            raise MakeError("licence check failed:\n  " + "\n  ".join(bad))
        labels = {}
        if gh is not None:
            for a in batch["areas"]:
                for fid, fe in sorted(lock["areas"][a["id"]]["feeds"].items()):
                    if fe["source"] == "tag" and not gh.remote_tag_exists(fe["tag"]):
                        say(f"creating tag {fe['tag']} at {fe['commit'][:12]}")
                        gh.create_tag(fe["tag"], fe["commit"])
            rel = gh.find_or_create_release(batch["release"]["tag"], batch["release"]["name"],
                                            os.environ.get("GITHUB_SHA", ""))
            for asset in gh.assets(rel["id"]):
                if asset["name"].endswith(".part"):
                    say(f"deleting leftover {asset['name']}")
                    gh.delete_asset(asset["id"])
                else:
                    labels[asset["name"]] = asset.get("label") or ""
        else:
            warn("no GH_REPO and GH_TOKEN: planning as if the release were empty")
        renders, meta_keys, publish = [], [], False
        for rid, v in self.videos(batch, lock):
            recipe = self.recipe(rid, batch)
            rk = self.render_key(batch, recipe, v, lock)
            mk = self.meta_key(batch, recipe, v, lock, rk)
            meta_keys.append(mk)
            stem = stem_of(rid, v)
            # A job stopped between swaps can leave a new file next to old ones, so a
            # video is done only when the MP4 and both sidecars carry its key.
            if any(label_key(labels.get(stem + sfx)) != rk[:16] for sfx in (".mp4", ".json", ".netmeta.json")):
                renders.append({"city": rid, "variant": v, "key": rk})
            if label_key(labels.get(f"{stem}.meta.json")) != mk[:16]:
                publish = True
        if label_key(labels.get(f"{name}-youtube.csv")) != self.csv_key(meta_keys)[:16]:
            publish = True
        pending = {}
        for r in renders:
            pending.setdefault(r["city"], set()).add(r["variant"])
        areas, overture, builds, tags = {}, {}, [], set()
        for rid in batch["cities"]:
            if rid not in pending:
                continue
            recipe = self.recipe(rid, batch)
            a = self.area_of(batch, recipe)
            day = self.area_key(batch, a, "day", lock)
            areas[(a["id"], "day")] = day
            week = ""
            if "week" in pending[rid]:
                week = self.area_key(batch, a, "week", lock)
                areas[(a["id"], "week")] = week
            ok = self.overture_key(a, lock)
            overture[a["id"]] = ok
            builds.append({"city": rid, "area": a["id"], "day_key": day, "week_key": week, "overture_key": ok})
        for (aid, _tl) in areas:
            for fe in lock["areas"][aid]["feeds"].values():
                if fe["source"] == "tag":
                    if not TAG_RE.match(fe["tag"]):
                        raise MakeError(f"tag name {fe['tag']!r} does not match {TAG_RE.pattern}")
                    tags.add(fe["tag"])
        out = {
            "batch": name,
            "areas": {"include": [{"area": a, "timeline": tl, "key": k} for (a, tl), k in sorted(areas.items())]},
            "overture": {"include": [{"area": a, "key": k} for a, k in sorted(overture.items())]},
            "builds": {"include": builds},
            "renders": {"include": renders},
            "feed_refspecs": " ".join(f"refs/tags/{t}:refs/tags/{t}" for t in sorted(tags)),
            "publish": "true" if (renders or publish) else "false",
        }
        return out

    def cmd_plan(self, branch, github_output=None):
        import release
        out = self.plan(branch, gh=release.GitHub.from_env(cwd=self.root))
        lines = []
        for k, v in out.items():
            lines.append(f"{k}={v if isinstance(v, str) else json.dumps(v, separators=(',', ':'))}")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        for ln in lines:
            say(ln)
        say(f"plan: {len(out['renders']['include'])} render(s), publish {out['publish']}")
        return out

    def release_client(self):
        import release
        gh = release.GitHub.from_env(cwd=self.root)
        if gh is None:
            raise MakeError("release needs GH_REPO and GH_TOKEN (it runs on Actions)")
        return gh

    def cmd_release_upload(self, name, stem, gh=None):
        import release
        gh = gh or self.release_client()
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        rid, v = split_stem(stem, batch["cities"])
        out = self.out_dir(batch)
        mp4 = os.path.join(out, f"{stem}.mp4")
        if not os.path.exists(mp4):
            say(f"{stem}: no MP4 (nothing rendered), nothing to upload")
            return []
        side = read_json(os.path.join(out, f"{stem}.json"))
        meta = read_json(os.path.join(out, f"{stem}.meta.json"))
        rk = side.get("key") or self.render_key(batch, self.recipe(rid, batch), v, lock)
        rel = gh.find_release(batch["release"]["tag"])
        if not rel:
            raise MakeError(f"no draft release {batch['release']['tag']}; plan creates it")
        assets = {a["name"]: a for a in gh.assets(rel["id"])}
        done = []
        # The MP4 goes last: its label is what marks the video done, so a stop
        # before it leaves the video pending, never a new MP4 with old sidecars.
        for fname, key, mime in ((f"{stem}.json", rk, "application/json"),
                                 (f"{stem}.netmeta.json", rk, "application/json"),
                                 (f"{stem}.meta.json", meta["meta_key"], "application/json"),
                                 (f"{stem}.mp4", rk, "video/mp4")):
            label = asset_label(fname, key)
            release.swap_upload(gh, rel["id"], assets, os.path.join(out, fname), fname, label, mime)
            done.append(fname)
            say(f"uploaded {fname} ({label})")
        return done

    def release_notes(self, batch, rows, flags):
        lines = [f"# {batch['release']['name']}", "",
                 f"{len(rows)} videos. Download the MP4s and `{batch['batch']}-youtube.csv`; upload in publish_order.", "",
                 "| # | video | render key | kbps | crf | bitrate floor | licence flags |",
                 "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['publish_order']} | {r['video']} | `{r['key']}` | {r.get('kbps', '')} | {r.get('crf', '')} "
                         f"| {'met' if r.get('bitrate_floor_met') else 'NOT met' if r.get('bitrate_floor_met') is False else ''} "
                         f"| {'; '.join(r.get('flags', []))} |")
        if flags:
            lines += ["", "## Licence flags", ""] + [f"* {f}" for f in sorted(flags)]
        return "\n".join(lines) + "\n"

    def cmd_release_publish(self, name, gh=None):
        import release
        gh = gh or self.release_client()
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        rel = gh.find_release(batch["release"]["tag"])
        if not rel:
            raise MakeError(f"no draft release {batch['release']['tag']}")
        assets = {a["name"]: a for a in gh.assets(rel["id"])}
        work = self.path("build", "release", name)
        if os.path.isdir(work):
            shutil.rmtree(work)
        os.makedirs(work)
        stems = [stem_of(c, v) for c, v in self.videos(batch, lock)]
        for stem in stems:
            if f"{stem}.mp4" not in assets:
                continue
            for suffix in (".netmeta.json", ".json"):
                a = assets.get(stem + suffix)
                if a:
                    gh.download_asset(a["id"], os.path.join(work, stem + suffix))
        out = self.out_dir(batch)
        metas = self.cmd_meta(name, from_netmeta=work, out_dir=out)
        meta_keys = [m["meta_key"] for m in metas]
        # The meta key hashes every input of a .meta.json, so an asset that already carries it holds these
        # bytes; skipping it saves three API calls per video against the 1,000 an hour GITHUB_TOKEN gets.
        csv_name = f"{name}-youtube.csv"
        for fname, key, mime in [(os.path.basename(m["file"]).replace(".mp4", ".meta.json"), m["meta_key"],
                                  "application/json") for m in metas] + [(csv_name, self.csv_key(meta_keys), "text/csv")]:
            if fname in assets and label_key(assets[fname].get("label")) == key[:16]:
                continue
            release.swap_upload(gh, rel["id"], assets, os.path.join(out, fname), fname, asset_label(fname, key), mime)
        rows, flags = [], set()
        for m in metas:
            stem = os.path.basename(m["file"])[:-4]
            side_p = os.path.join(work, f"{stem}.json")
            side = read_json(side_p) if os.path.exists(side_p) else {}
            row = {"video": stem, "publish_order": m["publish_order"], "key": (side.get("key") or "")[:16],
                   "mp4": f"{stem}.mp4" in assets, "kbps": side.get("kbps"), "crf": side.get("crf"),
                   "bitrate_floor_met": side.get("bitrate_floor_met"), "ms_per_frame": side.get("ms_per_frame"),
                   "network_sha256": side.get("network_sha256"), "basemap_sha256": side.get("basemap_sha256"),
                   "flags": m.get("flags", [])}
            flags |= set(m.get("flags", []))
            rows.append(row)
            say("RUNSUMMARY " + json.dumps(row, separators=(",", ":"), ensure_ascii=False))
        gh.set_notes(rel["id"], self.release_notes(batch, rows, flags))
        say(f"published metadata for {len(metas)} videos to the draft release {batch['release']['tag']}")
        return rows

    def cmd_review_push(self, name, src, push=True):
        """One commit on review/<batch>: this run's review files, plus the last ones of videos not rendered now."""
        batch = self.load_batch(name)
        lock = self.load_lock(batch)
        src = os.path.abspath(src)
        cap = 40 * 1000 * 1000
        videos = [stem_of(c, v) for c, v in self.videos(batch, lock)]
        review = batch.get("review_videos") or [stem_of(batch["cities"][0], v)
                                                for v in recipe_variants(self.recipe(batch["cities"][0], batch), batch)]
        out = self.out_dir(batch)
        files, big, fresh = {}, [], set()
        for stem in videos:
            d = os.path.join(src, stem)
            if not os.path.isdir(d):
                continue
            fresh.add(stem)
            for fname in ("thumb-f0300.jpg", f"{stem}.json"):
                p = os.path.join(d, fname)
                if os.path.exists(p):
                    files[f"{stem}/{fname}"] = read_bytes(p)
            meta = os.path.join(out, f"{stem}.meta.json")
            if not os.path.exists(meta):
                meta = os.path.join(d, f"{stem}.meta.json")
            if os.path.exists(meta):
                files[f"{stem}/{stem}.meta.json"] = read_bytes(meta)
            if stem in review:
                for p in sorted(glob.glob(os.path.join(d, "f*.png"))):
                    big.append((f"{stem}/{os.path.basename(p)}", read_bytes(p)))
        # Videos this run did not render keep what the branch already holds for them.
        kept = {}
        ref = f"refs/heads/review/{name}"
        if self.git("fetch", "--no-tags", "--depth=1", "origin", f"+{ref}:refs/remotes/origin/review/{name}",
                    check=False).returncode == 0:
            for ln in self.git("ls-tree", "-r", "-l", f"refs/remotes/origin/review/{name}").stdout.splitlines():
                meta_part, arc = ln.split("\t", 1)
                _mode, _typ, sha, size = meta_part.split()
                stem = arc.split("/", 1)[0]
                if stem in videos and stem not in fresh and "/" in arc:
                    kept[arc] = (("blob", sha), int(size))
        total = sum(len(b) for b in files.values())
        dropped = []
        for arc, (blob, size) in sorted(kept.items(), key=lambda kv: kv[0].endswith(".png")):
            if total + size > cap:
                dropped.append(arc)
                continue
            files[arc] = blob
            total += size
        for arc, data in big:
            if total + len(data) > cap:
                dropped.append(arc)
                continue
            files[arc] = data
            total += len(data)
        if dropped:
            warn(f"review branch cap of 40 MB: left out {', '.join(dropped)}")
        if not fresh and not kept:
            say("no review files from this run and none on the branch; review branch unchanged")
            return None, 0, []
        index = {"batch": name, "videos": videos, "review_videos": review, "rendered_now": sorted(fresh),
                 "dropped": dropped, "commit": os.environ.get("GITHUB_SHA", ""), "files": sorted(files)}
        files["index.json"] = dump_json(index).encode()
        commit = self.commit_files(files, f"chore: review files for {name}")
        say(f"review commit {commit[:12]}: {len(files)} files, {total / 1e6:.1f} MB ({len(fresh)} videos from this run)")
        if push:
            self.git("push", "--force", "origin", f"{commit}:{ref}")
            say(f"pushed review/{name}")
        return commit, total, dropped

    # ---------------------------------------------------------- legacy identity (D9)

    def cmd_check_legacy(self, record=False):
        script = self.path("tests", "legacy", "record.py" if record else "check.py")
        cmd = [sys.executable, "-I", script] + ([] if record else ["--part", "D"])
        self.show_cmd(cmd)
        return subprocess.run(cmd, cwd=self.root, env=self.env()).returncode


# ------------------------------------------------------------------ command line (2.14)

def parser():
    ap = argparse.ArgumentParser(prog="make.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cities", default="cities", help="where batches/, recipes/, locks/ and brands.json live")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lock", help="pin feeds, divisions, boundaries, frames and clips")
    p.add_argument("batch")
    p.add_argument("--refresh", action="store_true", help="re-fetch divisions and URL feeds; keep nothing old")

    p = sub.add_parser("fetch", help="make cache/ match the lock, checking every sha256")
    p.add_argument("batch")
    p.add_argument("--area")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--feeds-only", action="store_true")
    g.add_argument("--overture-only", action="store_true")
    p.add_argument("--push-data-tag", action="store_true")
    p.add_argument("--frozen", action="store_true")

    p = sub.add_parser("build", help="area stores, boundaries, trims and basemaps")
    p.add_argument("batch")
    p.add_argument("--area")
    p.add_argument("--city")
    p.add_argument("--area-only", action="store_true")
    p.add_argument("--timeline", choices=("day", "week"))
    p.add_argument("--frozen", action="store_true")
    p.add_argument("--no-upstream", action="store_true")

    p = sub.add_parser("stills", help="still sheets per variant (E7)")
    p.add_argument("id")
    p.add_argument("--variant", choices=VARIANTS)
    p.add_argument("--extra-query")

    p = sub.add_parser("preview", help="540 x 960 preview MP4")
    p.add_argument("id")
    p.add_argument("--variant", choices=VARIANTS, required=True)

    p = sub.add_parser("render", help="the final MP4 and its sidecar")
    p.add_argument("id")
    p.add_argument("--variant", choices=VARIANTS, required=True)
    p.add_argument("--tier", choices=("final",), default="final")
    p.add_argument("--no-upstream", action="store_true")
    p.add_argument("--review-dir")

    p = sub.add_parser("tune", help="coordinate descent over the page knobs (G)")
    p.add_argument("id")
    p.add_argument("--variant", choices=VARIANTS, default="day")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--knob")
    g.add_argument("--next", action="store_true")
    p.add_argument("--pick", help="KNOB=a|b|c")
    p.add_argument("--why")
    p.add_argument("--auto", action="store_true")
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("meta", help="titles, descriptions, tags and the CSV")
    p.add_argument("batch")
    p.add_argument("--only", metavar="ID-VARIANT")
    p.add_argument("--from-netmeta", metavar="DIR")
    p.add_argument("--no-upstream", action="store_true")
    p.add_argument("--allow-nc", action="store_true", help="local experiments only")
    p.add_argument("--review-dir", help="also copy each .meta.json to DIR/<id>-<variant>/")

    p = sub.add_parser("plan", help="Actions: validate and print the job matrices")
    p.add_argument("--from-branch", required=True)
    p.add_argument("--github-output")

    p = sub.add_parser("release", help="Actions: draft release assets")
    p.add_argument("batch")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--upload", metavar="ID-VARIANT")
    g.add_argument("--publish", action="store_true")

    p = sub.add_parser("review-push", help="Actions: force-push review files to review/<batch>")
    p.add_argument("batch")
    p.add_argument("--from", dest="src", required=True)

    p = sub.add_parser("check-legacy", help="tests/legacy check (or --record)")
    p.add_argument("--record", action="store_true")

    p = sub.add_parser("show", help="derived configs, keys and the area_box suggestion")
    p.add_argument("target", metavar="batch|id")
    return ap


def main(argv=None, root=ROOT):
    args = parser().parse_args(argv)
    pl = Pipeline(root, args.cities)
    frozen = getattr(args, "frozen", False) or os.environ.get("GITHUB_ACTIONS") == "true" and args.cmd in ("fetch", "build")
    try:
        if args.cmd == "lock":
            pl.cmd_lock(args.batch, refresh=args.refresh)
        elif args.cmd == "fetch":
            pl.cmd_fetch(args.batch, area=args.area, feeds_only=args.feeds_only, overture_only=args.overture_only,
                         push_data_tag=args.push_data_tag, frozen=frozen)
        elif args.cmd == "build":
            pl.cmd_build(args.batch, area=args.area, city=args.city, area_only=args.area_only, timeline=args.timeline,
                         frozen=frozen, no_upstream=args.no_upstream)
        elif args.cmd == "stills":
            pl.cmd_stills(args.id, variant=args.variant, extra_query=args.extra_query)
        elif args.cmd == "preview":
            pl.cmd_preview(args.id, args.variant)
        elif args.cmd == "render":
            pl.cmd_render(args.id, args.variant, tier=args.tier, no_upstream=args.no_upstream, review_dir=args.review_dir)
        elif args.cmd == "tune":
            import tune
            tune.run(pl, args)
        elif args.cmd == "meta":
            pl.cmd_meta(args.batch, only=args.only, from_netmeta=args.from_netmeta, no_upstream=args.no_upstream,
                        allow_nc=args.allow_nc, review_dir=args.review_dir)
        elif args.cmd == "plan":
            pl.cmd_plan(args.from_branch, args.github_output)
        elif args.cmd == "release":
            if args.upload:
                pl.cmd_release_upload(args.batch, args.upload)
            else:
                pl.cmd_release_publish(args.batch)
        elif args.cmd == "review-push":
            pl.cmd_review_push(args.batch, args.src)
        elif args.cmd == "check-legacy":
            return pl.cmd_check_legacy(record=args.record)
        elif args.cmd == "show":
            pl.cmd_show(args.target)
    except MakeError as e:
        print(f"make.py: error: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        pl.memo.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())

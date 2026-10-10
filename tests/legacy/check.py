#!/usr/bin/env python3
"""Legacy identity check (spec F2).

Compares this working tree with the references that record.py wrote to
build/legacy_ref/ (in a worktree, a symlink to the recording checkout's).
Today's eight configs must build and render exactly as at the legacy commit.

  1 source    git diff --exit-code <base> on the files nobody edits (F1, F2), and
              bodies.py on web/app.js
  2 builds    rebuild every legacy network and basemap in this tree; each md5 equals
              legacy_md5.json and `git diff -- data/` stays clean (restored on failure)
  3 renders   the record's slices with this tree's scripts/render_video.mjs (no --tier),
              both capture modes; framemd5 and stream fields equal the reference, and
              Tsukuba frame 200 equals the earlier full render when the record kept it
  4 requests  a legacy page load requests nothing new except web/color.js and
              web/fonts/fontsX.css

Parts (F3): A = 1, 2; B = 1, 3, 4; C = 1, 3; D = 1 to 4 (make.py check-legacy).
With no --part or --steps, every step runs. Outputs go to build/legacy_check/.

Usage: python3 tests/legacy/check.py [--part A|B|C|D] [--steps 1,2,3,4] [--only ID,ID] [--ref DIR] [--keep]
"""

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# This file runs as `python3 -I`, which leaves its own directory off sys.path.
sys.path.insert(0, HERE)

import bodies  # noqa: E402
import record  # noqa: E402

ROOT = record.ROOT
OUT = os.path.join(ROOT, "build", "legacy_check")
PARTS = {"A": [1, 2], "B": [1, 3, 4], "C": [1, 3], "D": [1, 2, 3, 4]}

# Files nobody edits: the F2 list plus the rest of F1's "untouched by everyone",
# matched against the legacy commit's tree so new files beside them are fine.
UNTOUCHED = [
    "scripts/build_network.py", "scripts/build_basemap.py", "scripts/fetch_overture.py", "scripts/model_gtfs.py",
    "scripts/gtfs_summary.py", "scripts/osm_routes_to_geojson.py",
    "cities/*.json", "data/*", "web/fonts/Montserrat*", "web/fonts/Inter*", ".github/workflows/*", "out/*.mp4",
]

JAPAN = ["tokyo-trains", "tokyo-buses", "kyoto-trains", "kyoto-buses", "osaka-trains", "osaka-buses"]
BUILDS = (
    [["scripts/build_network.py", "--city", "tsukuba"], ["scripts/build_network.py", "--city", "gta"],
     ["scripts/model_gtfs.py", "--area", "all"]]
    + [["scripts/build_network.py", "--city", c] for c in JAPAN]
    + [["scripts/build_basemap.py", "--city", c] for c in ("tsukuba", "gta", "tokyo-trains", "kyoto-trains", "osaka-trains")]
)
# Gitignored inputs of the builds. A worktree links or copies them from the
# main checkout.
# model_gtfs.py --area all also reads ten community-bus zips, so the whole
# folder is listed rather than the two Toei zips.
BUILD_INPUTS = [
    "data/gta/gtfs/ttc.zip", "data/gta/gtfs/go.zip", "data/gta/gtfs/upx.zip", "data/gta/gtfs/yrt.zip",
    "data/gta/gtfs/miway.zip", "data/gta/gtfs/brampton.zip", "data/gta/gtfs/drt.zip", "data/gta/gtfs/oakville.zip",
    "data/gta/gtfs/burlington.zip", "data/gta/gtfs/milton.zip",
    "data/japan-src/gtfs", "data/japan-src/honsu", "data/japan-src/n07",
] + [f"data/{d}/{f}.geojson" for d in ("basemap", "gta/basemap", "tokyo/basemap", "kyoto/basemap", "osaka/basemap")
     for f in ("segments", "water")] + ["data/basemap/divisions.geojson"]
ALLOWED_NEW_REQUESTS = {"/web/color.js", "/web/fonts/fontsX.css"}
FORBIDDEN_REQUESTS = ("themes.json", "/presets/", "X.woff2")


def git(*args, check=False):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def say(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------------------ step 1

def step_source(base):
    tree = git("ls-tree", "-r", "--name-only", base, check=True).stdout.split()
    paths = sorted({p for p in tree for pat in UNTOUCHED if fnmatch.fnmatchcase(p, pat)})
    # data/ as a directory too, so a file added under it also counts.
    res = git("diff", "--exit-code", "--stat", base, "--", *paths, "data/")
    ok = True
    if res.returncode != 0:
        ok = False
        say(f"  FAIL untouched files differ from {base[:7]}:\n{res.stdout}{res.stderr}")
    else:
        say(f"  ok   {len(paths)} untouched files equal {base[:7]}")
    with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
        new_text = fh.read()
    try:
        problems = bodies.check(bodies.legacy_text(base, "web/app.js"), new_text)
    except ValueError as e:
        problems = [str(e)]
    if problems:
        ok = False
        say(f"  FAIL bodies.py: {len(problems)} problem(s)")
        for p in problems:
            say("    " + p.replace("\n", "\n    "))
    else:
        say("  ok   bodies.py: legacy function bodies, busmap object and page glue within the B1 whitelist")
    return ok


# ------------------------------------------------------------------ step 2

def step_builds(table):
    missing = [p for p in BUILD_INPUTS if not os.path.exists(os.path.join(ROOT, p))]
    if missing:
        say("  FAIL missing build inputs (gitignored; link them from the main checkout):\n    " + "\n    ".join(missing))
        return False
    if git("diff", "--quiet", "--", "data/").returncode != 0:
        say("  FAIL data/ already has changes in this tree; not building over them")
        return False
    os.makedirs(OUT, exist_ok=True)
    log = os.path.join(OUT, "builds.log")
    env = dict(os.environ, PYTHONHASHSEED="0")
    ok = True
    with open(log, "w", encoding="utf-8") as fh:
        for cmd in BUILDS:
            t0 = time.time()
            fh.write(f"\n$ python3 -I {' '.join(cmd)}\n")
            fh.flush()
            res = subprocess.run([sys.executable, "-I", *cmd], cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
            say(f"  {'ran ' if res.returncode == 0 else 'FAIL'} {' '.join(cmd):45s} {time.time() - t0:6.1f} s")
            if res.returncode != 0:
                ok = False
                break
    if not ok:
        say(f"  see {log}")
    else:
        for path, want in table["md5"].items():
            got = record.md5_file(os.path.join(ROOT, path))
            if got != want:
                ok = False
                say(f"  FAIL {path}: md5 {got}, legacy {want}")
        if ok:
            say(f"  ok   {len(table['md5'])} built files match legacy_md5.json")
    res = git("diff", "--exit-code", "--stat", "--", "data/")
    if res.returncode != 0:
        ok = False
        say(f"  FAIL the builds changed data/:\n{res.stdout}  restoring with git checkout -- data/")
        git("checkout", "--", "data/", check=True)
    elif ok:
        say("  ok   git diff -- data/ is clean")
    return ok


# ------------------------------------------------------------------ step 3

def framemd5_key(text):
    """The framemd5 without its #software line, which names the checksum tool."""
    return [l for l in text.splitlines() if not l.startswith("#software")]


def explain_mismatch(ref_dir, new_dir, start):
    lines = []
    same = 0
    for i in range(start, start + record.SLICE):
        f = f"frame_{i:05d}.png"
        a, b = os.path.join(ref_dir, f), os.path.join(new_dir, f)
        if not os.path.exists(b):
            lines.append(f"      frame {i}: no PNG in {new_dir}")
            continue
        d = record.png_diff(a, b)
        if d is None:
            same += 1
        elif len(lines) < 6:
            lines.append(f"      frame {i}: {d}")
    if same == record.SLICE:
        lines.append("      every PNG has the reference pixels, so the encode differs (ffmpeg arguments or encoder)")
    else:
        lines.append(f"      {same} of {record.SLICE} PNGs have the reference pixels")
    return "\n".join(lines)


def render2_check(ref, manifest, mode):
    """Tsukuba frame 200 against the earlier full render, for the capture modes
    whose frame matched it when the references were recorded. None = not checked."""
    render2 = os.path.join(ref, "render2_frame_00200.png")
    frame = os.path.join(OUT, record.name("tsukuba", mode), "frame_00200.png")
    if (manifest.get("render2") or {}).get(mode, "missing") is not None or not os.path.exists(render2) \
            or not os.path.exists(frame):
        return None
    return record.png_diff(render2, frame) or ""


def step_renders(ref, manifest, renders, keep):
    script = os.path.join(ROOT, "scripts", "render_video.mjs")
    os.makedirs(OUT, exist_ok=True)
    ok = True
    for mode in record.MODES:
        for rid, city, start in renders:
            n = record.name(rid, mode)
            try:
                secs, _ = record.render(script, ROOT, OUT, rid, city, start, mode)
            except RuntimeError as e:
                ok = False
                say(f"  FAIL {n}: {e}")
                continue
            with open(os.path.join(ref, n + ".framemd5"), encoding="utf-8") as fh:
                want = framemd5_key(fh.read())
            with open(os.path.join(OUT, n + ".framemd5"), encoding="utf-8") as fh:
                got = framemd5_key(fh.read())
            with open(os.path.join(ref, n + ".probe.json"), encoding="utf-8") as fh:
                want_probe = json.load(fh)
            got_probe = record.probe(os.path.join(OUT, n + ".mp4"))
            problems = []
            if got != want:
                bad = sum(1 for a, b in zip(want, got) if a != b) + abs(len(want) - len(got))
                problems.append(f"framemd5 differs on {bad} line(s)\n"
                                + explain_mismatch(os.path.join(ref, n), os.path.join(OUT, n), start))
            if got_probe != want_probe:
                diff = {k: (want_probe.get(k), got_probe.get(k)) for k in record.PROBE_FIELDS
                        if want_probe.get(k) != got_probe.get(k)}
                problems.append(f"stream fields differ (reference, new): {diff}")
            if rid == "tsukuba":
                d = render2_check(ref, manifest, mode)
                if d:
                    problems.append(f"frame 200 differs from the earlier full render: {d}")
                elif d == "":
                    say(f"  ok   {n} frame 200 equals the earlier full render")
            if problems:
                ok = False
                say(f"  FAIL {n}: " + "\n    ".join(problems))
                say(f"       PNGs kept in {os.path.join(OUT, n)}")
                continue
            say(f"  ok   {n:24s} {record.SLICE} frames from {start}, framemd5 equal ({secs:.0f} s)")
            # A passing slice's frames are the reference's; about 60 MB each is not worth keeping.
            if not keep:
                shutil.rmtree(os.path.join(OUT, n))
                os.remove(os.path.join(OUT, n + ".mp4"))
    return ok


# ------------------------------------------------------------------ step 4

def failed(r):
    """requests.mjs's rule: a non-200 response, or an error other than the
    net::ERR_ABORTED Chromium reports for some fully read .gz fetches."""
    return (r["status"] is not None and r["status"] != 200) \
        or (r["failure"] is not None and r["failure"] != "net::ERR_ABORTED") \
        or (r["status"] is None and r["failure"] is None)


def step_requests(ref, renders):
    with open(os.path.join(ref, "requests.json"), encoding="utf-8") as fh:
        want = json.load(fh)
    out = os.path.join(OUT, "requests.json")
    try:
        got = record.request_log(ROOT, out, renders)
    except RuntimeError as e:
        say(f"  FAIL {e}")
        return False
    ok = True
    for rid, _, _ in renders:
        old = {r["path"] for r in want[rid]["requests"]}
        new = {r["path"] for r in got[rid]["requests"]}
        added, gone = new - old, old - new
        forbidden = sorted(p for p in new if any(f in p for f in FORBIDDEN_REQUESTS))
        problems = []
        if added - ALLOWED_NEW_REQUESTS:
            problems.append(f"new requests {sorted(added - ALLOWED_NEW_REQUESTS)}")
        if forbidden:
            problems.append(f"forbidden requests {forbidden}")
        if gone:
            problems.append(f"requests no longer made {sorted(gone)}")
        bad = [f"{r['path']} {r['status']} {r['failure'] or ''}".rstrip() for r in got[rid]["requests"] if failed(r)]
        if bad:
            problems.append(f"failed requests {bad}")
        if got[rid]["errors"]:
            problems.append(f"page errors {got[rid]['errors']}")
        if problems:
            ok = False
            say(f"  FAIL {rid}: " + "; ".join(problems))
        else:
            extra = f" (plus {', '.join(sorted(added))})" if added else ""
            say(f"  ok   {rid:14s} {len(old)} legacy requests{extra}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", choices=sorted(PARTS), help="the steps one implementer's acceptance test needs (F3)")
    ap.add_argument("--steps", default=None, help="comma list of steps, e.g. 1,3")
    ap.add_argument("--only", default=None, help="comma list of reference ids for steps 3 and 4")
    ap.add_argument("--ref", default=record.REF, help="reference directory (default build/legacy_ref)")
    ap.add_argument("--keep", action="store_true", help="keep the PNGs and MP4s of passing slices in build/legacy_check/")
    args = ap.parse_args()
    if args.part and args.steps:
        ap.error("--part and --steps are exclusive")
    steps = PARTS[args.part] if args.part else [int(s) for s in args.steps.split(",")] if args.steps else [1, 2, 3, 4]
    renders = record.RENDERS
    if args.only:
        ids = args.only.split(",")
        unknown = set(ids) - {r[0] for r in record.RENDERS}
        if unknown:
            ap.error(f"unknown ids {sorted(unknown)}")
        renders = [r for r in record.RENDERS if r[0] in ids]
    # Builds and renders share the machine with other jobs.
    os.nice(10)

    table = record.load_md5()
    manifest = {}
    if any(s in steps for s in (3, 4)):
        mpath = os.path.join(args.ref, "manifest.json")
        if not os.path.exists(mpath):
            say(f"no references at {args.ref}: run tests/legacy/record.py in the main checkout, "
                f"or link build/legacy_ref from it")
            return 2
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("base") != table["base"]:
            say(f"references were recorded at {manifest.get('base')}, legacy_md5.json names {table['base']}")
            return 2

    names = {1: "source", 2: "builds", 3: "renders", 4: "requests"}
    results = {}
    for s in steps:
        say(f"step {s}: {names[s]}")
        t0 = time.time()
        if s == 1:
            results[s] = step_source(table["base"])
        elif s == 2:
            results[s] = step_builds(table)
        elif s == 3:
            results[s] = step_renders(args.ref, manifest, renders, args.keep)
        elif s == 4:
            results[s] = step_requests(args.ref, renders)
        say(f"step {s}: {'PASS' if results[s] else 'FAIL'} ({time.time() - t0:.0f} s)")
    failed = [names[s] for s, r in results.items() if not r]
    say(f"legacy check: {'FAIL (' + ', '.join(failed) + ')' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Record the legacy identity references (spec F2).

Runs once, on this machine, before any code change. It checks the legacy
commit out into build/legacy_base, renders a 30-frame slice of each legacy
config with that commit's scripts/render_video.mjs in both capture modes, and
writes to build/legacy_ref/ (gitignored):

  <id>-<mode>.mp4, <id>-<mode>/frame_NNNNN.png   the encoded slice, and every frame as PNG
  <id>-<mode>.framemd5                           ffmpeg -f framemd5 of the MP4
  <id>-<mode>.probe.json                         stream fields of the MP4 (codec, profile, colour tags)
  requests.json                                  every request a legacy page load makes
  render2_frame_00200.png                        the --render2 PNG, kept when it equals Tsukuba frame 200
  manifest.json                                  base commit, tools, and the md5 of every file above

Before rendering it checks tests/legacy/legacy_md5.json against
`git show <base>:<path> | md5sum`. check.py compares a working tree with all
of this. The references hold only on this machine: Skia and x264 may differ in
low bits elsewhere.

Usage: python3 tests/legacy/record.py [--render2 PNG] [--only ID,ID] [--force]
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
MD5_FILE = os.path.join(HERE, "legacy_md5.json")
REF = os.path.join(ROOT, "build", "legacy_ref")
LEGACY_BASE = os.path.join(ROOT, "build", "legacy_base")

# (reference id, --city value, first frame). Tsukuba runs without --city
# because that is how its video has always been made.
RENDERS = [
    ("tsukuba", None, 200),
    ("gta", "gta", 0),
    ("tokyo-trains", "tokyo-trains", 400),
    ("tokyo-buses", "tokyo-buses", 400),
    ("kyoto-trains", "kyoto-trains", 400),
    ("kyoto-buses", "kyoto-buses", 400),
    ("osaka-trains", "osaka-trains", 400),
    ("osaka-buses", "osaka-buses", 400),
]
MODES = ("screenshot", "canvas")
SLICE = 30
PROBE_FIELDS = ("codec_name", "profile", "level", "pix_fmt", "width", "height", "r_frame_rate", "nb_frames",
                "color_range", "color_space", "color_transfer", "color_primaries", "field_order")


def load_md5():
    with open(MD5_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def tool(name):
    """ffmpeg and ffprobe as scripts/render_video.mjs finds them on this machine."""
    env = os.environ.get(name.upper())
    for cand in (env, f"/usr/bin/{name}", shutil.which(name)):
        if cand and os.path.exists(cand):
            return cand
    raise SystemExit(f"{name} not found (tried ${name.upper()}, /usr/bin/{name}, PATH)")


def node():
    path = shutil.which("node")
    if not path:
        raise SystemExit("node not found on PATH")
    return path


def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git(*args, cwd=ROOT, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True)


def name(rid, mode):
    return f"{rid}-{mode}"


def render(script, root, out_dir, rid, city, start, mode):
    """One reference slice: the F2 command line, then framemd5 and probe.
    Returns (seconds, log path)."""
    stem = os.path.join(out_dir, name(rid, mode))
    png_dir = stem
    if os.path.isdir(png_dir):
        shutil.rmtree(png_dir)
    for ext in (".mp4", ".framemd5", ".probe.json"):
        if os.path.exists(stem + ext):
            os.remove(stem + ext)
    cmd = [node(), os.path.relpath(script, ROOT), "--root", os.path.relpath(root, ROOT)]
    if city:
        cmd += ["--city", city]
    cmd += ["--capture", mode, "--start", str(start), "--end", str(start + SLICE),
            "--png-dir", os.path.relpath(png_dir, ROOT), "--png-every", "1",
            "--out", os.path.relpath(stem + ".mp4", ROOT)]
    log = stem + ".log"
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(" ".join(cmd) + "\n")
        fh.flush()
        res = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    if res.returncode != 0:
        with open(log, encoding="utf-8") as fh:
            tail = fh.read()[-3000:]
        raise RuntimeError(f"{name(rid, mode)}: render failed (exit {res.returncode}), log {log}\n{tail}")
    fm = subprocess.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", stem + ".mp4",
                         "-f", "framemd5", "-"], check=True, capture_output=True).stdout
    with open(stem + ".framemd5", "wb") as fh:
        fh.write(fm)
    with open(stem + ".probe.json", "w", encoding="utf-8") as fh:
        json.dump(probe(stem + ".mp4"), fh, indent=1, sort_keys=True)
        fh.write("\n")
    pngs = sorted(f for f in os.listdir(png_dir) if f.endswith(".png"))
    frames = framemd5_frames(fm.decode())
    want = [f"frame_{i:05d}.png" for i in range(start, start + SLICE)]
    if pngs != want or len(frames) != SLICE:
        raise RuntimeError(f"{name(rid, mode)}: expected {SLICE} frames from {start}, "
                           f"got {len(pngs)} PNGs and {len(frames)} framemd5 lines")
    return time.time() - t0, log


def framemd5_frames(text):
    return [l for l in text.splitlines() if l and not l.startswith("#")]


def probe(mp4):
    out = subprocess.run([tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=" + ",".join(PROBE_FIELDS), "-of", "json", mp4],
                         check=True, capture_output=True, text=True).stdout
    stream = json.loads(out)["streams"][0]
    return {k: stream.get(k) for k in PROBE_FIELDS}


def request_log(root, out_file, renders):
    """Runs requests.mjs on `root`; returns its parsed JSON."""
    configs = [{"id": rid, "city": city, "frame": start} for rid, city, start in renders]
    cmd = [node(), os.path.join(HERE, "requests.mjs"), "--root", root, "--out", out_file,
           "--configs", json.dumps(configs)]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(res.stdout)
    if res.returncode != 0:
        raise RuntimeError(f"request log failed (exit {res.returncode})\n{res.stderr[-3000:]}")
    with open(out_file, encoding="utf-8") as fh:
        return json.load(fh)


def pixels(path):
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def png_diff(a, b):
    """None when the two PNGs hold the same pixels, else a short description."""
    import numpy as np
    pa, pb = pixels(a), pixels(b)
    if pa.shape != pb.shape:
        return f"size {pa.shape[1]}x{pa.shape[0]} against {pb.shape[1]}x{pb.shape[0]}"
    d = np.abs(pa.astype(np.int16) - pb.astype(np.int16)).max(axis=2)
    ys, xs = np.nonzero(d)
    if not len(ys):
        return None
    return (f"{len(ys)} pixels differ (max {int(d.max())} per channel), "
            f"box x {xs.min()}..{xs.max()}, y {ys.min()}..{ys.max()}")


def ensure_legacy_base(base):
    """build/legacy_base: a detached worktree at the legacy commit, with a
    node_modules link so the legacy script finds Playwright."""
    if os.path.isdir(LEGACY_BASE):
        head = git("rev-parse", "HEAD", cwd=LEGACY_BASE).stdout.decode().strip()
        dirty = git("status", "--porcelain", "--untracked-files=no", cwd=LEGACY_BASE).stdout.decode().strip()
        if head != base or dirty:
            raise SystemExit(f"{LEGACY_BASE} is at {head} with changes {dirty!r}; remove it with "
                             f"`git worktree remove --force build/legacy_base` and run again")
    else:
        git("worktree", "add", "--detach", LEGACY_BASE, base)
    nm = os.path.join(ROOT, "node_modules")
    link = os.path.join(LEGACY_BASE, "node_modules")
    if os.path.exists(nm) and not os.path.lexists(link):
        os.symlink(os.path.realpath(nm), link)


def tool_versions():
    def first_line(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines()[0].strip()
        except (OSError, subprocess.CalledProcessError, IndexError):
            return None
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium"
    return {"node": first_line([node(), "--version"]), "ffmpeg": first_line([tool("ffmpeg"), "-version"]),
            "chromium": first_line([chromium, "--version"]), "chromium_path": os.path.realpath(chromium),
            "nproc": os.cpu_count()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render2", default=None,
                    help="the canvas frame_00200.png of an earlier full Tsukuba render; kept for check step 3 when it matches")
    ap.add_argument("--only", default=None, help="comma list of reference ids to (re)record into an existing build/legacy_ref")
    ap.add_argument("--force", action="store_true", help="overwrite a complete build/legacy_ref")
    args = ap.parse_args()
    # Renders share the machine with other jobs; their pixels do not depend on speed.
    os.nice(10)

    table = load_md5()
    base = table["base"]
    bad = []
    for path, want in table["md5"].items():
        got = md5_bytes(git("show", f"{base}:{path}").stdout)
        if got != want:
            bad.append(f"  {path}: table {want}, git show {got}")
    if bad:
        raise SystemExit("legacy_md5.json does not match git show:\n" + "\n".join(bad))
    print(f"legacy_md5.json: {len(table['md5'])} files match git show {base[:7]}")

    renders = RENDERS
    manifest_path = os.path.join(REF, "manifest.json")
    if args.only:
        ids = args.only.split(",")
        unknown = set(ids) - {r[0] for r in RENDERS}
        if unknown:
            raise SystemExit(f"unknown ids {sorted(unknown)}")
        renders = [r for r in RENDERS if r[0] in ids]
    elif os.path.exists(manifest_path) and not args.force:
        raise SystemExit(f"{REF} is already recorded; pass --force to record it again")
    os.makedirs(REF, exist_ok=True)
    ensure_legacy_base(base)
    script = os.path.join(LEGACY_BASE, "scripts", "render_video.mjs")

    for mode in MODES:
        for rid, city, start in renders:
            secs, _ = render(script, LEGACY_BASE, REF, rid, city, start, mode)
            print(f"  {name(rid, mode):24s} frames {start}..{start + SLICE - 1}  {secs:6.1f} s")

    print("request log of the legacy pages")
    req_path = os.path.join(REF, "requests.json")
    fresh = request_log(LEGACY_BASE, req_path + ".part", renders)
    os.remove(req_path + ".part")
    merged = {}
    if args.only and os.path.exists(req_path):
        with open(req_path, encoding="utf-8") as fh:
            merged = json.load(fh)
    merged.update(fresh)
    with open(req_path, "w", encoding="utf-8") as fh:
        json.dump({rid: merged[rid] for rid, _, _ in RENDERS if rid in merged}, fh, indent=1)
        fh.write("\n")

    render2 = None
    if args.render2:
        src = os.path.abspath(args.render2)
        render2 = {"source": src}
        for mode in MODES:
            frame = os.path.join(REF, name("tsukuba", mode), "frame_00200.png")
            if os.path.exists(frame):
                render2[mode] = png_diff(src, frame)
        if any(render2.get(m, "missing") is None for m in MODES):
            shutil.copyfile(src, os.path.join(REF, "render2_frame_00200.png"))
        for mode in MODES:
            print(f"  render2 frame 200 against {mode}: {render2.get(mode) or 'same pixels'}")

    old = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            old = json.load(fh)
    files = {}
    for dirpath, _, names in os.walk(REF):
        for f in names:
            if f == "manifest.json" or f.endswith(".log"):
                continue
            p = os.path.join(dirpath, f)
            files[os.path.relpath(p, REF)] = md5_file(p)
    manifest = {
        "base": base,
        "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "renders": [{"id": rid, "city": city, "start": start, "frames": SLICE} for rid, city, start in RENDERS],
        "modes": list(MODES),
        "tools": tool_versions(),
        "render2": render2 if render2 is not None else old.get("render2"),
        "files": dict(sorted(files.items())),
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    n_png = sum(1 for f in files if f.endswith(".png"))
    print(f"wrote {manifest_path}: {len(files)} files, {n_png} PNGs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

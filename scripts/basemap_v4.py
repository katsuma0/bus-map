"""The v4 base map of one city: the unmodified build_basemap.build() on a derived config.

    python3 -I scripts/basemap_v4.py --config build/gta-markham/basemap.config.json [--key HEX]

The config (spec 2.7, written by make.py) is exactly the dict build() reads:
id, origin, clip, basemap_dir, basemap, gzip, boundary, built_dir, plus
"schema": 4, which build() ignores. The output is <built_dir>/basemap.json.gz
with today's gzip and float rules. With --key the step key is written to
<output>.key, and the output's sha256 goes into build/<id>/manifest.json.
"""
import argparse
import contextlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
# -I leaves the script directory off sys.path; the sibling modules are ours.
sys.path.insert(0, HERE)
import area_store  # noqa: E402
import build_basemap  # noqa: E402

REQUIRED = ("id", "origin", "clip", "basemap_dir", "basemap", "built_dir")


def output_path(cfg):
    name = "basemap.json.gz" if cfg.get("gzip") else "basemap.json"
    return os.path.join(ROOT, cfg["built_dir"], name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="basemap config (spec 2.7), schema 4")
    ap.add_argument("--key", default=None, help="step key from make.py, written next to the output")
    ap.add_argument("--manifest", default=None, help="default: <built_dir>/manifest.json")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if cfg.get("schema") != 4:
        sys.exit(f"{args.config}: not a schema 4 basemap config")
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        sys.exit(f"{args.config}: missing {missing}")
    if cfg.get("basemap_city"):
        sys.exit(f"{args.config}: basemap_city is a legacy key; a v4 city has its own base map")
    if cfg.get("boundary"):
        sys.exit(f"{args.config}: boundary must be null; the v4 outline travels in the network")
    # build() prints its progress on stdout; keep stdout for callers that parse it.
    with contextlib.redirect_stdout(sys.stderr):
        build_basemap.build(cfg)
    out = output_path(cfg)
    if args.key:
        with open(out + ".key", "w", encoding="utf-8") as fh:
            fh.write(args.key + "\n")
    manifest = args.manifest or os.path.join(ROOT, cfg["built_dir"], "manifest.json")
    area_store.update_manifest(manifest, {area_store.rel(out): area_store.sha256_file(out)})


if __name__ == "__main__":
    main()

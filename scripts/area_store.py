"""The columnar area store of the v4 build (spec 2.8): write it once, read it with mmap.

An area build (build_area.py) writes every stored trip class of one area and
timeline as flat .npy arrays, so a city trim (trim_network.py) touches only
the rows of the trips near its frame and peak memory stays small even for the
largest metros. Coordinates are integers: metres for x, y and distance along
a shape, 0.1 m for the cumulative shape length. Dividing by 1000 (or 10000)
gives exactly the floats np.round(km, 3) (or 4) gives, so a network built from
the store is byte-identical to one built from floats.

Besides the arrays of 2.8 the store keeps two internal ones:
`trip_copies.npy` (int16, N x K: copies drawn per day class, which the
median-date rule makes different from the multiplicity) and `shape_bbox.npy`
(int32, S x 4: x0, y0, x1, y1 in metres).

The module also holds the small helpers every A step shares for its outputs:
sha256 of a file and the merge into build/<id>/manifest.json.
"""
import fcntl
import hashlib
import json
import os
import shutil

import numpy as np

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ARRAYS = {
    "shape_off": np.int64, "shape_xy": np.int32, "shape_cum": np.int32, "shape_bbox": np.int32,
    "trip_off": np.int64, "trip_t": np.int32, "trip_d": np.int32,
    "trip_r": np.int32, "trip_s": np.int32, "trip_feed": np.int32,
    "trip_sha": np.uint8, "trip_mult": np.int16, "trip_dates": np.int16, "trip_runs": np.int32,
    "trip_draw": np.uint8, "trip_copies": np.int16,
}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rel(p):
    """Repo-relative path when under the repo, else absolute."""
    a = os.path.abspath(p)
    return os.path.relpath(a, ROOT) if a.startswith(ROOT + os.sep) else a


def update_manifest(path, files, build_key=None, drop=()):
    """Merge `files` (path -> sha256) into build/<id>/manifest.json (A12).

    Each A step that writes into build/<id>/ records its output here, so the
    manifest always matches the files on disk; paths are repo-relative.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # The day trim, the week trim and the base map of one city may run at once.
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        man = {"build_key": None, "files": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                man = json.load(fh)
        man.setdefault("files", {})
        for p in drop:
            man["files"].pop(p, None)
        man["files"].update(files)
        man["files"] = dict(sorted(man["files"].items()))
        if build_key is not None:
            man["build_key"] = build_key
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"build_key": man.get("build_key"), "files": man["files"]}, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)


def write(out_dir, meta, arrays, key=None):
    """Write meta.json, every array of ARRAYS and stamp.json into `out_dir`.

    The store is written next to its final place and renamed over it, so a
    reader never sees half a store and an interrupted build leaves the old one.
    """
    missing = sorted(set(ARRAYS) - set(arrays))
    if missing:
        raise ValueError(f"area store arrays missing: {missing}")
    out_dir = os.path.abspath(out_dir)
    tmp = out_dir + ".tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    for name in sorted(ARRAYS):
        arr = np.ascontiguousarray(arrays[name], dtype=np.dtype(ARRAYS[name]).newbyteorder("<"))
        with open(os.path.join(tmp, name + ".npy"), "wb") as fh:
            np.save(fh, arr, allow_pickle=False)
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
    outputs = {name: sha256_file(os.path.join(tmp, name)) for name in sorted(os.listdir(tmp))}
    with open(os.path.join(tmp, "stamp.json"), "w", encoding="utf-8") as fh:
        json.dump({"key": key, "outputs": outputs}, fh, indent=1, sort_keys=True)
        fh.write("\n")
    if os.path.exists(out_dir):
        old = out_dir + ".old"
        if os.path.exists(old):
            shutil.rmtree(old)
        os.rename(out_dir, old)
        os.rename(tmp, out_dir)
        shutil.rmtree(old)
    else:
        os.rename(tmp, out_dir)
    return outputs


class Store:
    """A written store, arrays memory-mapped read-only."""

    def __init__(self, path):
        self.path = path
        with open(os.path.join(path, "meta.json"), encoding="utf-8") as fh:
            self.meta = json.load(fh)
        stamp = os.path.join(path, "stamp.json")
        self.stamp = None
        if os.path.exists(stamp):
            with open(stamp, encoding="utf-8") as fh:
                self.stamp = json.load(fh)
        for name in ARRAYS:
            setattr(self, name, np.load(os.path.join(path, name + ".npy"), mmap_mode="r"))
        self.n_trips = len(self.trip_off) - 1
        self.n_shapes = len(self.shape_off) - 1
        self.day_classes = list(self.meta["day_classes"])

    @property
    def key(self):
        return (self.stamp or {}).get("key")

    def trip_t0_t1(self):
        """First and last stop time of every trip (int64 arrays)."""
        off = np.asarray(self.trip_off)
        t = self.trip_t
        return t[off[:-1]].astype(np.int64), t[off[1:] - 1].astype(np.int64)

    def trip_bbox(self):
        """Shape bbox in metres per trip, (N, 4)."""
        return np.asarray(self.shape_bbox)[np.asarray(self.trip_s)]

    def touching(self, box_km):
        """Indices (store order) of trips whose shape bbox touches `box_km` [x0, y0, x1, y1]."""
        b = self.trip_bbox().astype(np.float64) / 1000.0
        x0, y0, x1, y1 = box_km
        ok = (b[:, 0] <= x1) & (b[:, 2] >= x0) & (b[:, 1] <= y1) & (b[:, 3] >= y0)
        return np.flatnonzero(ok)

    def trip_arrays(self, i):
        """(t int64 seconds, d float64 km) of trip i."""
        a, b = int(self.trip_off[i]), int(self.trip_off[i + 1])
        return np.asarray(self.trip_t[a:b], dtype=np.int64), np.asarray(self.trip_d[a:b], dtype=np.int64) / 1000.0

    def shape_arrays(self, s):
        """(xy float64 (n, 2) km, cum float64 (n,) km) of shape s."""
        a, b = int(self.shape_off[s]), int(self.shape_off[s + 1])
        xy = np.asarray(self.shape_xy[2 * a:2 * b], dtype=np.int64).reshape(-1, 2) / 1000.0
        cum = np.asarray(self.shape_cum[a:b], dtype=np.int64) / 10000.0
        return xy, cum

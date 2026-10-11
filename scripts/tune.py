"""Tuning loop for one city and pass (spec G1 to G6): one knob at a time, three arms, keep the best.

    python3 scripts/make.py tune <id> [--variant day|rush|week] --next
    python3 scripts/make.py tune <id> --pick trailmin=c --why "12 min keeps Highway 7 continuous"
    python3 scripts/make.py tune <id> --auto            # auto picks for every remaining knob of the pass
    python3 scripts/make.py tune <id> --apply           # picks into the recipe's override and variety.card_line

Every arm is the city's normal render query plus one knob in `render=`, rendered
by `render_video.mjs --tier tune --roundtrip` on the network and basemap already
built: tuning never runs a data step. Arm b is the current value; it reuses the
previous knob's chosen arm when that arm rendered the same things.

Outputs per knob in build/<id>/tune/<variant>/<nn>-<knob>/: a/, b/, c/ (the C5
files), sheet.jpg, crops.jpg, motion.jpg, clock.jpg, camera.jpg or card.jpg (each at most
1,568 px on its long edge, the judge's image limit), and scores.json; the pass
keeps its picks in state.json.

The card knobs (18 to 21) are in a pass only when the video shows the card
(CARD, off in the shorts preset); asked for with --knob, their arms turn the
card on so the judge sees what the pick would open on.
"""

import json
import math
import os
import re
import shutil

# (number, query name, CONFIG key, kind, renders a clip, passes)
KNOBS = [
    (1, "zoom", "FRAME_ZOOM", "frame", False, ("day", "rush")),
    (2, "cx", "FRAME_DX_KM", "frame", False, ("day", "rush")),
    (3, "cy", "FRAME_DY_KM", "frame", False, ("day", "rush")),
    # The camera move (B18) right after the frame it moves over. Numbered after
    # the spec's knobs, like 23, so earlier tune directories keep their names.
    (24, "camamp", "CAMERA_AMP", "camera", True, ("day", "rush", "week")),
    (4, "trailmin", "TRAIL_MINUTES", "look", True, ("day", "rush", "week")),
    (5, "corew", "TRAIL_CORE_W", "look", True, ("day",)),
    (6, "shoulderw", "TRAIL_SHOULDER_W", "look", True, ("day",)),
    (7, "shoulder", "TRAIL_SHOULDER_ALPHA", "look", True, ("day",)),
    (8, "trailalpha", "TRAIL_ALPHA", "look", True, ("day",)),
    (9, "layeralpha", "TRAIL_LAYER_ALPHA", "look", True, ("day",)),
    (10, "dotcore", "BUS_CORE_R", "look", True, ("day", "week")),
    (11, "halor", "BUS_HALO_R", "look", True, ("day",)),
    (12, "haloalpha", "BUS_HALO_ALPHA", "look", True, ("day", "week")),
    (13, "routealpha", "ROUTE_ALPHA", "look", False, ("day",)),
    (14, "roads", "BASE_ROADS_GAIN", "look", False, ("day",)),
    (15, "water", "BASE_WATER_GAIN", "look", False, ("day",)),
    (16, "warpgamma", "TIME_WARP_GAMMA", "warp", True, ("day",)),
    (17, "warpfloor", "TIME_WARP_FLOOR", "warp", True, ("day", "week")),
    (18, "cardsize", "CARD_TITLE_MAX", "card", False, ("day",)),
    (19, "cardline", "CARD_LINES", "card", False, ("day", "rush", "week")),
    (20, "cardscrim", "CARD_SCRIM", "card", False, ("day",)),
    (21, "cardy", "CARD_CENTER_Y", "card", False, ("day",)),
    # The panel backdrop behind the HUD: scored by text contrast over the stills, the rush's
    # close-up too, where the most trails run behind the panel.
    (22, "panelalpha", "PANEL_ALPHA", "panel", False, ("day", "rush")),
    # Numbered after the spec's 22 so earlier tune directories keep their names.
    (23, "outside", "OUTSIDE_DIM", "look", False, ("day",)),
]
BY_NAME = {k[1]: k for k in KNOBS}
FRAME_KEYS = ("FRAME_ZOOM", "FRAME_DX_KM", "FRAME_DY_KM")
CROP_KNOBS = range(4, 13)
MAX_EDGE = 1568
TILE_W, TILE_H, HEADER, GAP = 270, 480, 36, 8
# The page's text safe zone (web/app.js SAFE, B9) and its panel width; the map
# metrics start under the title block, 140 px below the zone's top.
SAFE = (120, 290, 800, 1440)
PANEL_W = 560
MAP_TOP = SAFE[1] + 140
CLIP_FRAMES = 90
HUD_TEXT = ("title", "subtitle", "weekday", "clock", "count", "count2", "chips", "axis", "credit", "credit2")
CARD_TEXT = ("card_title", "card_title2", "card_line0", "card_line0b", "card_line1", "card_line1b")
PANEL_TEXT = ("weekday", "clock", "count", "count2", "chips", "axis", "credit", "credit2", "peak")
# B9 and B10 minimum sizes in px (web/app.js MIN_SIZE).
MIN_SIZES = {"title": 44, "subtitle": 26, "weekday": 48, "clock": 30, "count": 28, "count2": 28, "chips": 20,
             "peak": 20, "axis": 20, "credit": 18, "credit2": 18, "card_title": 72, "card_title2": 72,
             "card_line0": 40, "card_line0b": 40, "card_line1": 30, "card_line1b": 30}
LIMITS = {"whiteout": 0.03, "contrast": 4.5, "safe_share": 0.85, "strobe": 0.004, "card_cover": 0.4}
# The card band reaches 60 px past the card text (B10), feathers included.
CARD_BAND_PAD = 60
WHY_MAX = 120


class TuneError(Exception):
    pass


def r2(x):
    return round(float(x), 2)


# ------------------------------------------------------------------ arms (G1)

def arm_values(name, v, k_km=None, other=None):
    """(a, c) for knob `name` at current value v. cardline returns the other two of 0, 1, 2."""
    if name == "zoom":
        return r2(v * 0.9), r2(v * 1.1)
    if name in ("cx", "cy"):
        return r2(v - 0.04 * k_km), r2(v + 0.04 * k_km)
    if name == "trailmin":
        return round(v * 0.67), round(v * 1.5)
    if name == "corew":
        return max(1.5, r2(v - 1)), r2(v + 1)
    if name == "shoulderw":
        return round(v * 0.7), round(v * 1.3)
    if name == "shoulder":
        return r2(v * 0.6), min(1, r2(v * 1.4))
    if name == "trailalpha":
        return r2(v * 0.75), min(1, r2(v * 1.25))
    if name == "layeralpha":
        return max(0.3, r2(v - 0.15)), min(1, r2(v + 0.15))
    if name == "dotcore":
        return (1.0, 1.8) if v == 0 else (max(1.2, r2(v - 0.6)), r2(v + 0.6))
    if name == "halor":
        return round(v * 0.75), round(v * 1.3)
    if name in ("haloalpha", "routealpha"):
        return r2(v * 0.6), min(1, r2(v * 1.5))
    if name in ("roads", "water"):
        return r2(v * 0.75), r2(v * 1.35)
    if name == "warpgamma":
        return r2(v * 0.7), r2(v * 1.5)
    if name == "warpfloor":
        return r2(v * 0.55), r2(v * 1.65)
    if name == "cardsize":
        return v - 24, v + 24
    if name == "cardline":
        a, c = [x for x in (0, 1, 2) if x != int(v)]
        return a, c
    if name == "cardscrim":
        return r2(v * 0.6), r2(v * 1.6)
    if name == "cardy":
        return v - 80, v + 80
    if name == "panelalpha":
        return r2(v * 0.85), r2(v * 1.15)
    if name == "outside":
        # At 1 the neighbours vanish and the outline loses its context.
        return max(0, r2(v - 0.2)), min(0.95, r2(v + 0.2))
    if name == "camamp":
        # The arms scale the move B18's caps leave, which differs by path: on the day 0.75
        # pushes in 4.9 to 5.7% on a pull-out (3% on a drift) and 1.25 8.2 to 9.5% (5%), on
        # the rush 2.4 to 2.8% and 4.0 to 4.7%. Where the city line's cap holds the move
        # under 0.75 of it (a city that fills the fit box width) all three arms render the
        # same move. 1.5 holds the fastest point under 1% of the frame width a second.
        return (0.5, 1.0) if v == 0 else (r2(v * 0.75), min(1.5, r2(v * 1.25)))
    raise TuneError(f"unknown knob {name}")


def fit_frame_arm(name, value, frame, trim_km, zoom, dx, dy):
    """Clamp a frame arm so the visible frame stays inside the trim box: the nearest value that fits."""
    kv = frame["km_vertical"]
    cx, cy = frame["center_km"]
    t0, t1, t2, t3 = trim_km
    if name == "zoom":
        need = []
        for half, room in ((kv * 9 / 32, cx + dx - t0), (kv * 9 / 32, t2 - cx - dx), (kv / 2, cy + dy - t1),
                           (kv / 2, t3 - cy - dy)):
            if room <= 0:
                return value
            need.append(half / room)
        zmin = max(need)
        return value if value >= zmin else math.ceil(zmin * 100 - 1e-9) / 100
    hw, hh = kv * 9 / 32 / zoom, kv / 2 / zoom
    if name == "cx":
        lo, hi = t0 + hw - cx, t2 - hw - cx
    else:
        lo, hi = t1 + hh - cy, t3 - hh - cy
    if lo > hi:
        return value
    if value < lo:
        return math.ceil(lo * 100 - 1e-9) / 100
    if value > hi:
        return math.floor(hi * 100 + 1e-9) / 100
    return value


# ------------------------------------------------------------------ images and scores (G4)

def lazy_numpy():
    """numpy, imported on first use: make.py imports tune for its helpers, and most commands need no numpy."""
    import numpy
    return numpy


def load_rgb(path):
    from PIL import Image
    with Image.open(path) as im:
        return lazy_numpy().asarray(im.convert("RGB"))


def luminance(rgb):
    """WCAG relative luminance per pixel."""
    np = lazy_numpy()
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722


def parse_color(s):
    """'#rrggbb', '#rgb', 'rgb(r,g,b)' or 'rgba(r,g,b,a)' to ((r, g, b), alpha)."""
    s = (s or "").strip()
    m = re.match(r"^#([0-9a-fA-F]{6})$", s)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)), 1.0
    m = re.match(r"^#([0-9a-fA-F]{3})$", s)
    if m:
        return tuple(int(ch * 2, 16) for ch in m.group(1)), 1.0
    m = re.match(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$", s)
    if m:
        return tuple(int(float(m.group(i))) for i in (1, 2, 3)), float(m.group(4)) if m.group(4) else 1.0
    return (255, 255, 255), 1.0


def panel_rect(boxes):
    """The panel backdrop: its text sits 28 px inside the left and top edges (B9), PANEL_W wide, down to SAFE's bottom."""
    pb = [b for b in boxes if b.get("name") in PANEL_TEXT]
    if not pb:
        return None
    x0 = min(b["x0"] for b in pb) - 28
    y0 = min(b["y0"] for b in pb) - 28
    return [x0, y0, x0 + PANEL_W, SAFE[3]]


def map_mask(h, w, boxes, scale=1.0):
    """The frame minus the HUD boxes, the panel rectangle and y < 380."""
    np = lazy_numpy()
    m = np.ones((h, w), dtype=bool)
    m[: int(round(MAP_TOP * scale))] = False
    rects = [[b["x0"], b["y0"], b["x1"], b["y1"]] for b in boxes]
    pr = panel_rect(boxes)
    if pr:
        rects.append(pr)
    for x0, y0, x1, y1 in rects:
        x0, y0 = max(0, int(math.floor(x0 * scale))), max(0, int(math.floor(y0 * scale)))
        x1, y1 = min(w, int(math.ceil(x1 * scale))), min(h, int(math.ceil(y1 * scale)))
        if x1 > x0 and y1 > y0:
            m[y0:y1, x0:x1] = False
    return m


def whiteout(rgb, mask):
    y = luminance(rgb)
    sel = y[mask] if mask is not None else y.ravel()
    return float((sel >= 0.85).mean()) if sel.size else 0.0


def text_contrast(boxes, bg_rgb, names, pct=90):
    """min over boxes of (Y_text + 0.05) / (P<pct>(Y_bg under the box) + 0.05)."""
    np = lazy_numpy()
    y_bg = luminance(bg_rgb)
    h, w = y_bg.shape
    worst = None
    for b in boxes:
        if not any(b.get("name") == n for n in names):
            continue
        x0, y0 = max(0, int(b["x0"])), max(0, int(b["y0"]))
        x1, y1 = min(w, int(math.ceil(b["x1"]))), min(h, int(math.ceil(b["y1"])))
        if x1 <= x0 or y1 <= y0:
            continue
        region = y_bg[y0:y1, x0:x1]
        bright = float(np.percentile(region, pct))
        rgb, alpha = parse_color(b.get("color"))
        if alpha < 1:
            # Translucent text (card line 1 at 0.85) sits over that background.
            under = bg_rgb[y0:y1, x0:x1].reshape(-1, 3)[region.ravel() >= bright].mean(axis=0)
            rgb = tuple(alpha * c + (1 - alpha) * u for c, u in zip(rgb, under))
        y_text = float(luminance(np.array([[rgb]], dtype=np.float64).clip(0, 255).astype(np.uint8))[0, 0])
        ratio = (max(y_text, bright) + 0.05) / (min(y_text, bright) + 0.05)
        worst = ratio if worst is None else min(worst, ratio)
    return worst


def safe_share(vehicles, boxes):
    """Inside vehicles whose dot lies in the safe zone and outside the panel and title boxes, over all inside."""
    flat = list(vehicles.values()) if isinstance(vehicles, dict) else list(vehicles)
    pr = panel_rect(boxes)
    title = [b for b in boxes if b.get("name") in ("title", "title2")]
    inside = ok = 0
    for i in range(0, len(flat) - 2, 3):
        x, y, flag = flat[i], flat[i + 1], flat[i + 2]
        if not flag:
            continue
        inside += 1
        if not (SAFE[0] <= x <= SAFE[2] and SAFE[1] <= y <= SAFE[3]):
            continue
        if pr and pr[0] <= x <= pr[2] and pr[1] <= y <= pr[3]:
            continue
        if any(b["x0"] <= x <= b["x1"] and b["y0"] <= y <= b["y1"] for b in title):
            continue
        ok += 1
    return ok / inside if inside else 1.0


def card_cover(vehicles, boxes):
    """Share of the on-screen inside vehicles whose dot lies in the rows of the card band.

    safe_share reads stills without the card, so nothing else sees a card that
    sits over the city's busiest half on frame 0, where the map is the hook.
    """
    card = [b for b in boxes if str(b.get("name", "")).startswith("card_")]
    if not card:
        return None
    y0 = min(b["y0"] for b in card) - CARD_BAND_PAD
    y1 = max(b["y1"] for b in card) + CARD_BAND_PAD
    flat = list(vehicles.values()) if isinstance(vehicles, dict) else list(vehicles)
    inside = under = 0
    for i in range(0, len(flat) - 2, 3):
        x, y, flag = flat[i], flat[i + 1], flat[i + 2]
        if not flag or not (0 <= x <= 1080 and 0 <= y <= 1920):
            continue
        inside += 1
        under += y0 <= y <= y1
    return under / inside if inside else 0.0


def motion_strobe(frames, mask):
    """motion: mean abs dY x 100 in the map mask; strobe: share of pixels whose largest channel jumps by > 64."""
    np = lazy_numpy()
    if len(frames) < 2:
        return None, None
    mot, stro = [], []
    prev, prev_y = None, None
    for f in frames:
        rgb = load_rgb(f).astype(np.int16)
        y = luminance(rgb.astype(np.uint8))
        if prev is not None:
            mot.append(float(np.abs(y - prev_y)[mask].mean() * 100))
            stro.append(float((np.abs(rgb - prev).max(axis=2) > 64)[mask].mean()))
        prev, prev_y = rgb, y
    return float(np.mean(mot)), float(np.mean(stro))


def size_breaks(box_lists):
    out = []
    for boxes in box_lists:
        for b in boxes:
            lim = MIN_SIZES.get(b.get("name"))
            if lim is not None and b.get("size") is not None and b["size"] < lim:
                out.append(f"{b['name']} {b['size']} < {lim}")
    return sorted(set(out))


def breaks_of(scores):
    out = []
    if scores.get("whiteout") is not None and scores["whiteout"] > LIMITS["whiteout"]:
        out.append("whiteout")
    if scores.get("contrast") is not None and scores["contrast"] < LIMITS["contrast"]:
        out.append("contrast")
    if scores.get("safe_share") is not None and scores["safe_share"] < LIMITS["safe_share"]:
        out.append("safe_share")
    if scores.get("strobe") is not None and scores["strobe"] > LIMITS["strobe"]:
        out.append("strobe")
    if scores.get("card_cover") is not None and scores["card_cover"] > LIMITS["card_cover"]:
        out.append("card_cover")
    if scores.get("sizes"):
        out.append("sizes")
    return out


def auto_pick(kind, arms, v):
    """G4: drop arms that break a hard limit, then pick by the knob kind's score. Returns a label."""
    fewest = min(len(a["breaks"]) for a in arms)
    pool = [a for a in arms if len(a["breaks"]) == fewest]
    by = {a["label"]: a for a in pool}
    if kind == "warp":
        return "b" if "b" in by else pool[0]["label"]

    def tie_pick(key, best_is_max, within):
        vals = [(a["scores"].get(key), a) for a in pool]
        known = [(s, a) for s, a in vals if s is not None]
        if not known:
            return "b" if "b" in by else pool[0]["label"]
        best = max(s for s, _ in known) if best_is_max else min(s for s, _ in known)
        tied = [a for s, a in known if abs(s - best) <= within + 1e-12]
        return tied

    if kind == "camera":
        # At these speeds the move adds no strobe worth the name: b, unless an arm is
        # calmer by more than the noise between two renders.
        tied = tie_pick("strobe", False, 0.0005)
        if isinstance(tied, str):
            return tied
        if "b" in [a["label"] for a in tied]:
            return "b"
        return min(tied, key=lambda a: a["scores"]["strobe"])["label"]
    if kind == "frame":
        tied = tie_pick("safe_share", True, 0.01)
        if isinstance(tied, str):
            return tied
        tied.sort(key=lambda a: (abs(a["value"] - v), a["label"] != "b"))
        return tied[0]["label"]
    if kind == "look":
        calm = [a for a in pool if a["scores"].get("strobe") is None or a["scores"]["strobe"] <= 0.002]
        if calm:
            pool, by = calm, {a["label"]: a for a in calm}
        tied = tie_pick("whiteout", False, 0.002)
    else:
        tied = tie_pick("contrast", True, 0.5)
    if isinstance(tied, str):
        return tied
    labels = [a["label"] for a in tied]
    if "b" in labels:
        return "b"
    key = "whiteout" if kind == "look" else "contrast"
    best = (min if kind == "look" else max)(tied, key=lambda a: a["scores"][key])
    return best["label"]


# ------------------------------------------------------------------ sheets (G3)

def font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/opentype/inter/Inter-Regular.otf", "/usr/share/fonts/opentype/inter/Inter-Medium.otf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def clock_label(t, week=False):
    s = int(t)
    day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][(s // 86400) % 7] + " " if week else ""
    s %= 86400
    return f"{day}{s // 3600:02d}:{s % 3600 // 60:02d}"


def cap_edge(im):
    from PIL import Image
    w, h = im.size
    if max(w, h) <= MAX_EDGE:
        return im
    k = MAX_EDGE / max(w, h)
    return im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)


def tile_sheet(groups, out, cols=5, tile=(TILE_W, TILE_H), crop=None):
    """groups: [(header, [(path, label)])]; each group starts a line under a 36 px header strip and wraps at cols."""
    from PIL import Image, ImageDraw
    tw, th = tile
    lines = []
    for header, tiles in groups:
        n = max(1, math.ceil(len(tiles) / cols))
        lines.append((header, tiles, n))
    width = GAP + cols * (tw + GAP)
    height = sum(HEADER + n * th + (n - 1) * GAP for _h, _t, n in lines) + GAP
    sheet = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    f_head, f_lab = font(20), font(15)
    y = 0
    for header, tiles, n in lines:
        draw.text((GAP, y + 8), header, fill=(235, 235, 240), font=f_head)
        for i, (path, label) in enumerate(tiles):
            x0 = GAP + (i % cols) * (tw + GAP)
            y0 = y + HEADER + (i // cols) * (th + GAP)
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    if crop:
                        im = im.crop(crop(im))
                    im = im.resize((tw, th), Image.LANCZOS)
                sheet.paste(im, (x0, y0))
            except (OSError, ValueError):
                draw.rectangle([x0, y0, x0 + tw - 1, y0 + th - 1], outline=(200, 60, 60))
            if label:
                tb = draw.textbbox((0, 0), label, font=f_lab)
                draw.rectangle([x0, y0 + th - (tb[3] - tb[1]) - 10, x0 + tb[2] - tb[0] + 10, y0 + th], fill=(0, 0, 0))
                draw.text((x0 + 5, y0 + th - (tb[3] - tb[1]) - 7), label, fill=(240, 240, 240), font=f_lab)
        y += HEADER + n * th + (n - 1) * GAP
    sheet = cap_edge(sheet)
    sheet.save(out, quality=90)
    return out


def trail_square(rgb, mask, size=360, step=20):
    """Top-left of the size x size square in the map mask with the most trail pixels."""
    np = lazy_numpy()
    sat = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
    trail = ((sat >= 40) | (luminance(rgb) >= 0.25)) & mask
    ii = np.pad(trail.astype(np.int64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    h, w = trail.shape
    best, at = -1, (0, MAP_TOP)
    for y in range(MAP_TOP, max(MAP_TOP, h - size) + 1, step):
        for x in range(0, max(0, w - size) + 1, step):
            s = ii[y + size, x + size] - ii[y, x + size] - ii[y + size, x] + ii[y, x]
            if s > best:
                best, at = s, (x, y)
    return at


def crops_sheet(arms, out, still_name, rt_name, square):
    from PIL import Image, ImageDraw
    x, y = square
    width = GAP + 2 * (360 + GAP)
    sheet = Image.new("RGB", (width, 3 * (HEADER + 360)), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    f = font(20)
    for i, arm in enumerate(arms):
        y0 = i * (HEADER + 360)
        draw.text((GAP, y0 + 8), arm.get("short", arm["header"]), fill=(235, 235, 240), font=f)
        for j, (name, box) in enumerate(((still_name, (x, y, x + 360, y + 360)),
                                         (rt_name, (x * 2 / 3, y * 2 / 3, x * 2 / 3 + 240, y * 2 / 3 + 240)))):
            p = os.path.join(arm["dir"], name) if name else None
            if not p or not os.path.exists(p):
                continue
            with Image.open(p) as im:
                im = im.convert("RGB").crop(tuple(int(round(v)) for v in box))
                if im.size != (360, 360):
                    im = im.resize((360, 360), Image.LANCZOS)
                sheet.paste(im, (GAP + j * (360 + GAP), y0 + HEADER))
    sheet = cap_edge(sheet)
    sheet.save(out, quality=90)
    return out


# ------------------------------------------------------------------ the harness

class Tuner:
    def __init__(self, pl, rid, variant):
        self.pl = pl
        self.rid = rid
        self.variant = variant
        self.recipe = pl.recipe(rid)
        self.batch = pl.load_batch(self.recipe["batch"])
        self.lock = pl.load_lock(self.batch)
        if variant not in self.batch_variants():
            raise TuneError(f"{rid} has no {variant} variant")
        self.net = pl.network_path(rid, variant)
        self.basemap = pl.path("build", rid, "basemap.json.gz")
        for p in (self.net, self.basemap):
            if not os.path.exists(p):
                raise TuneError(f"{pl.rel(p)} is missing; run make.py build {self.batch['batch']} --city {rid} first "
                                f"(tuning never runs a data step)")
        self.meta = pl.network_meta(self.net)
        self.dir = pl.path("build", rid, "tune", variant)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                self.state = json.load(fh)
        except (FileNotFoundError, ValueError):
            self.state = {"id": rid, "variant": variant, "values": {}, "history": [], "pending": None}

    def batch_variants(self):
        import make
        return make.recipe_variants(self.recipe, self.batch)

    def save(self):
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.state, indent=1, ensure_ascii=False) + "\n")

    def knobs(self):
        card = self.card_on()
        return [k for k in KNOBS if self.variant in k[5] and (card or k[3] != "card")]

    def card_on(self):
        """Whether this video shows the card (B10), the way the page decides it."""
        return self.pl.card_on(self.recipe, self.variant, self.meta)

    def knob_dir(self, knob):
        return os.path.join(self.dir, f"{knob[0]:02d}-{knob[1]}")

    def frame(self):
        V = self.meta["variants"][self.variant]
        return V.get("frame") or self.meta["frame"]

    def trim_km(self):
        import make
        tb = (self.meta.get("trim") or {}).get("box_km")
        return tb or make.trim_box(self.meta["frame"], self.pl.defaults()["trim_scale"])

    def base_query(self):
        rj, bh, _r, n = self.pl.effective_render(self.recipe, self.variant, self.meta)
        rj = dict(rj)
        rj.update(self.state["values"])
        return rj, bh, n

    def plan_render(self, knob, n):
        """(times, frames, clip) one arm of this knob renders (G2)."""
        times = self.pl.still_times(self.meta, self.variant)
        kind, clip = knob[3], knob[4]
        frames = []
        if kind == "warp":
            frames = sorted({min(n - 1, round(k * n / 8)) for k in range(9)})
        if kind == "camera":
            # The push-in, the way out, the fitted frame and the way back.
            frames = sorted({min(n - 1, round(k * n / 4)) for k in range(4)})
        if kind == "card":
            return [], [0, 15, 45], False
        return times, frames, clip

    def render_arm(self, out, query, bh, times, frames, clip):
        os.makedirs(out, exist_ok=True)
        args = ["--data", self.pl.rel(self.net), "--basemap", self.pl.rel(self.basemap), "--variant", self.variant,
                "--render-json", json.dumps(query, sort_keys=True, separators=(",", ":"))]
        if bh:
            args += ["--brandhex", bh]
        args += ["--tier", "tune", "--roundtrip", "--out-dir", self.pl.rel(out),
                 "--times", ",".join(str(t) for t in times) if times else "none"]
        if frames:
            args += ["--frames", ",".join(str(f) for f in frames)]
        if clip:
            args += ["--clip-at", str(self.pl.still_times(self.meta, self.variant)[0]), "--clip-frames", str(CLIP_FRAMES)]
        else:
            args += ["--clip-frames", "0"]
        self.pl.run_node(args)
        with open(os.path.join(out, "arm.json"), "w", encoding="utf-8") as fh:
            json.dump({"query": query, "brandhex": bh, "times": times, "frames": frames, "clip": clip}, fh, sort_keys=True)

    def reuse_previous(self, out, query, bh, times, frames, clip):
        """Arm b is the previous knob's chosen arm when that arm rendered the same query and files."""
        for h in reversed(self.state["history"]):
            if h.get("picked") is None:
                continue
            src = os.path.join(h["dir"], h["picked"])
            try:
                with open(os.path.join(src, "arm.json"), encoding="utf-8") as fh:
                    sig = json.load(fh)
            except (FileNotFoundError, ValueError):
                return False
            want = {"query": query, "brandhex": bh, "times": times, "frames": frames, "clip": clip}
            if json.loads(json.dumps(sig, sort_keys=True)) != json.loads(json.dumps(want, sort_keys=True)):
                return False
            if os.path.isdir(out):
                shutil.rmtree(out)
            os.makedirs(out)
            for name in os.listdir(src):
                try:
                    os.link(os.path.join(src, name), os.path.join(out, name))
                except OSError:
                    shutil.copyfile(os.path.join(src, name), os.path.join(out, name))
            return True
        return False

    def find(self, d, prefix, t):
        """C5 names a still by its clock time; the week adds the day, and 26:30 style hours are allowed."""
        s = int(t)
        names = []
        if self.variant == "week":
            ddd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][(s // 86400) % 7]
            names.append(f"{prefix}-{ddd}-{s % 86400 // 3600:02d}{s % 3600 // 60:02d}")
        names += [f"{prefix}-{s // 3600:02d}{s % 3600 // 60:02d}", f"{prefix}-{s // 3600 % 24:02d}{s % 3600 // 60:02d}"]
        ext = ".json" if prefix in ("vehicles", "boxes") else ".png"
        for n in names:
            p = os.path.join(d, n + ext)
            if os.path.exists(p):
                return p
        return None

    def score_arm(self, d, times, frames, clip, pct=90):
        """The arm's scores; pct is the background percentile text contrast is taken against."""
        np = lazy_numpy()
        sc = {"whiteout": None, "contrast": None, "safe_share": None, "motion": None, "strobe": None,
              "card_cover": None, "sizes": [], "ms_per_frame": None}
        box_lists, contrasts = [], []

        def boxes_at(path):
            if not path or not os.path.exists(path):
                return []
            with open(path, encoding="utf-8") as fh:
                b = json.load(fh)
            box_lists.append(b)
            return b

        if times:
            am_boxes = boxes_at(self.find(d, "boxes", times[0]))
            still = self.find(d, "still", times[0])
            if still:
                rgb = load_rgb(still)
                w1 = whiteout(rgb, map_mask(rgb.shape[0], rgb.shape[1], am_boxes))
                rt = self.find(d, "rt-still", times[0])
                if rt:
                    rrgb = load_rgb(rt)
                    w1 = max(w1, whiteout(rrgb, map_mask(rrgb.shape[0], rrgb.shape[1], am_boxes,
                                                         scale=rrgb.shape[1] / 1080)))
                sc["whiteout"] = w1
            shares = []
            for i, t in enumerate(times):
                boxes = am_boxes if i == 0 else boxes_at(self.find(d, "boxes", t))
                bg = self.find(d, "bg", t)
                if bg and boxes:
                    c = text_contrast(boxes, load_rgb(bg), HUD_TEXT, pct)
                    if c is not None:
                        contrasts.append(c)
                veh = self.find(d, "vehicles", t)
                if veh and i < 3:
                    with open(veh, encoding="utf-8") as fh:
                        shares.append(safe_share(json.load(fh), boxes))
            if shares:
                sc["safe_share"] = float(np.mean(shares))
        for f in frames:
            boxes = boxes_at(os.path.join(d, f"boxes-f{f:04d}.json"))
            bg = os.path.join(d, f"bgframe-{f:04d}.png")
            if boxes and os.path.exists(bg):
                card = any(str(b.get("name", "")).startswith("card_") for b in boxes)
                names = CARD_TEXT if card else HUD_TEXT
                c = text_contrast(boxes, load_rgb(bg), names, pct)
                if c is not None:
                    contrasts.append(c)
            veh = os.path.join(d, f"vehicles-f{f:04d}.json")
            if f == 0 and boxes and os.path.exists(veh):
                with open(veh, encoding="utf-8") as fh:
                    sc["card_cover"] = card_cover(json.load(fh), boxes)
        if contrasts:
            sc["contrast"] = float(min(contrasts))
        if clip:
            rts = sorted(p for p in os.listdir(d) if re.match(r"^rt-clip-\d+\.png$", p))
            if len(rts) >= 2:
                first = load_rgb(os.path.join(d, rts[0]))
                am_boxes = boxes_at(self.find(d, "boxes", times[0])) if times else []
                mask = map_mask(first.shape[0], first.shape[1], am_boxes, scale=first.shape[1] / 1080)
                sc["motion"], sc["strobe"] = motion_strobe([os.path.join(d, p) for p in rts], mask)
        sc["sizes"] = size_breaks(box_lists)
        tp = os.path.join(d, "timing.json")
        if os.path.exists(tp):
            with open(tp, encoding="utf-8") as fh:
                t = json.load(fh)
            sc["ms_per_frame"] = t.get("ms_per_frame", t.get("msPerFrame"))
        return sc

    def sheets(self, knob, arms, times, frames, n):
        d = self.knob_dir(knob)
        out = {}
        kind = knob[3]
        groups = []
        for arm in arms:
            tiles = []
            if kind == "card":
                tiles = [(os.path.join(arm["dir"], f"frame-{f:04d}.png"), f"frame {f}") for f in frames]
            else:
                for i, t in enumerate(times):
                    p = self.find(arm["dir"], "rt-still" if i == 0 else "still", t) or self.find(arm["dir"], "still", t)
                    tiles.append((p, clock_label(t, self.variant == "week") + (" RT" if i == 0 else "")))
            groups.append((arm["header"], tiles))
        cols = max(len(g[1]) for g in groups) if groups else 5
        out["sheet"] = tile_sheet(groups, os.path.join(d, "sheet.jpg"), cols=max(1, cols))
        if kind == "card":
            out["card"] = tile_sheet(groups, os.path.join(d, "card.jpg"), cols=3)
        if knob[0] in CROP_KNOBS and times:
            b = next(a for a in arms if a["label"] == "b")
            still = self.find(b["dir"], "still", times[0])
            if still:
                rgb = load_rgb(still)
                boxes = []
                bp = self.find(b["dir"], "boxes", times[0])
                if bp:
                    with open(bp, encoding="utf-8") as fh:
                        boxes = json.load(fh)
                sq = trail_square(rgb, map_mask(rgb.shape[0], rgb.shape[1], boxes))
                still_name = os.path.basename(still)
                rt = self.find(b["dir"], "rt-still", times[0])
                out["crops"] = crops_sheet(arms, os.path.join(d, "crops.jpg"), still_name,
                                           os.path.basename(rt) if rt else None, sq)
        if knob[4]:
            groups = []
            for arm in arms:
                rts = sorted(p for p in os.listdir(arm["dir"]) if re.match(r"^rt-clip-\d+\.png$", p))
                pick = [rts[i] for i in (0, 15, 30, 45, 60, 75, 89) if i < len(rts)]
                groups.append((arm["header"], [(os.path.join(arm["dir"], p), p[8:-4]) for p in pick]))
            out["motion"] = tile_sheet(groups, os.path.join(d, "motion.jpg"), cols=7, tile=(180, 320))
        if kind == "camera" and frames:
            groups = [(arm["header"], [(os.path.join(arm["dir"], f"frame-{f:04d}.png"), f"frame {f}") for f in frames])
                      for arm in arms]
            out["camera"] = tile_sheet(groups, os.path.join(d, "camera.jpg"), cols=4)
        if kind == "warp" and frames:
            groups = [(arm["header"], [(os.path.join(arm["dir"], f"frame-{f:04d}.png"), f"frame {f}") for f in frames])
                      for arm in arms]
            out["clock"] = tile_sheet(groups, os.path.join(d, "clock.jpg"), cols=5, tile=(270, 132),
                                      crop=lambda im: (int(SAFE[0] * im.width / 1080), int((SAFE[3] - 400) * im.height / 1920),
                                                       int(SAFE[2] * im.width / 1080), int(SAFE[3] * im.height / 1920)))
        return out

    def run_knob(self, knob):
        n_, name, key, kind, clip, _passes = knob
        d = self.knob_dir(knob)
        base, bh, n = self.base_query()
        if kind == "card" and not self.card_on():
            # Only an explicit --knob gets here with the card off: its arms show the card they tune.
            base = dict(base, CARD=True)
        times, frames, clip = self.plan_render(knob, n)
        b_dir = os.path.join(d, "b")
        if not self.reuse_previous(b_dir, base, bh, times, frames, clip):
            self.render_arm(b_dir, base, bh, times, frames, clip)
        cfg_path = os.path.join(b_dir, "config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            config = json.load(fh)
        if key not in config:
            raise TuneError(f"{self.pl.rel(cfg_path)} has no {key}; the page must report every knob in busmap.config")
        v = config[key]
        k_km = config.get("KM_VERTICAL") or self.frame()["km_vertical"] / (config.get("FRAME_ZOOM") or 1)
        a, c = arm_values(name, v, k_km)
        if kind == "frame":
            z, dx, dy = config.get("FRAME_ZOOM", 1), config.get("FRAME_DX_KM", 0), config.get("FRAME_DY_KM", 0)
            a = fit_frame_arm(name, a, self.frame(), self.trim_km(), z, dx, dy)
            c = fit_frame_arm(name, c, self.frame(), self.trim_km(), z, dx, dy)
        arms = []
        for label, val in (("a", a), ("b", v), ("c", c)):
            q = dict(base)
            q[key] = val
            arm_dir = os.path.join(d, label)
            if label != "b":
                self.render_arm(arm_dir, q, bh, times, frames, clip)
            arms.append({"label": label, "value": val, "query": q, "dir": arm_dir})
        # The panel's backdrop is there for the few bright dots and lines that cross the text, which
        # P90 of the background under a box never sees: its arms are scored against P99.
        pct = 99 if kind == "panel" else 90
        for arm in arms:
            arm["scores"] = self.score_arm(arm["dir"], times, frames, clip, pct)
            arm["breaks"] = breaks_of(arm["scores"])
            s = arm["scores"]
            bits = [f"{k} {s[k]:.3f}" for k in ("whiteout", "contrast", "safe_share", "motion", "strobe", "card_cover")
                    if s.get(k) is not None]
            arm["header"] = f"{arm['label']}: {key}={arm['value']}  " + "  ".join(bits) + \
                            (f"  BREAKS {','.join(arm['breaks'])}" if arm["breaks"] else "")
            # crops.jpg is 744 px wide: the arm, its value and the score the crops are about.
            arm["short"] = f"{arm['label']}: {key}={arm['value']}" + "".join(
                f"  {k} {s[k]:.3f}" for k in ("whiteout", "strobe") if s.get(k) is not None)
        pick = auto_pick(kind, arms, v)
        sheets = self.sheets(knob, arms, times, frames, n)
        scores = {"knob": name, "key": key,
                  "arms": [{"label": a_["label"], "value": a_["value"], "query": a_["query"], "scores": a_["scores"],
                            "breaks": a_["breaks"]} for a_ in arms],
                  "auto_pick": pick, "picked": None, "why": None}
        with open(os.path.join(d, "scores.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(scores, indent=1) + "\n")
        self.state["history"] = [h for h in self.state["history"] if h["knob"] != name]
        self.state["history"].append({"n": n_, "knob": name, "key": key, "dir": d,
                                      "arms": {a_["label"]: a_["value"] for a_ in arms},
                                      "auto_pick": pick, "picked": None, "why": None})
        self.state["pending"] = name
        self.save()
        self.report(knob, scores, sheets)
        return scores

    def report(self, knob, scores, sheets):
        print(f"knob {knob[0]} {knob[1]} ({knob[2]}), pass {self.variant}")
        for name, p in sheets.items():
            print(f"  {name}: {self.pl.rel(p)}")
        for a in scores["arms"]:
            s = a["scores"]
            bits = ", ".join(f"{k} {s[k]:.4g}" for k in ("whiteout", "contrast", "safe_share", "motion", "strobe",
                                                          "card_cover", "ms_per_frame") if s.get(k) is not None)
            print(f"  {a['label']}: {knob[2]}={a['value']}  {bits}" + (f"  breaks {a['breaks']}" if a["breaks"] else ""))
        print(f"  auto pick: {scores['auto_pick']}")
        print(f"  next: make.py tune {self.rid} --variant {self.variant} --pick {knob[1]}=<a|b|c> --why \"...\"")

    def next_knob(self):
        done = {h["knob"] for h in self.state["history"] if h.get("picked")}
        for k in self.knobs():
            if k[1] not in done:
                return k
        return None

    def pick(self, name, label, why):
        if label not in ("a", "b", "c"):
            raise TuneError("--pick wants KNOB=a, b or c")
        check_why(why)
        h = next((h for h in self.state["history"] if h["knob"] == name), None)
        if not h:
            raise TuneError(f"{name} has not been rendered in the {self.variant} pass; run --next or --knob {name}")
        h["picked"], h["why"] = label, why
        self.state["values"][h["key"]] = h["arms"][label]
        if self.state.get("pending") == name:
            self.state["pending"] = None
        self.save()
        sp = os.path.join(h["dir"], "scores.json")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as fh:
                s = json.load(fh)
            s["picked"], s["why"] = label, why
            with open(sp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(s, indent=1) + "\n")
        print(f"{name}: picked {label} ({h['key']}={h['arms'][label]})")

    def apply(self):
        """G5: write each pick that differs from the value it was tuned from into the recipe."""
        import make
        recipe = make.read_json(self.pl.recipe_path(self.rid))
        blocked = make.variant_block_keys(self.pl.defaults())
        variants = make.recipe_variants(recipe, self.batch)
        zoom = (recipe.get("variety") or {}).get("zoom", 1.0)
        ov = recipe.setdefault("override", {})
        for k in ("render", "variant_render", "brand_colors", "_why"):
            ov.setdefault(k, {})
        wrote = []
        for h in self.state["history"]:
            if not h.get("picked") or h["arms"][h["picked"]] == h["arms"]["b"]:
                continue
            key, val, why = h["key"], h["arms"][h["picked"]], h.get("why") or ""
            if key in FRAME_KEYS:
                if self.variant == "day":
                    stored = make.r6(val / zoom) if key == "FRAME_ZOOM" else val
                    for t in ["day"] + (["week"] if "week" in variants else []):
                        ov["variant_render"].setdefault(t, {})[key] = stored
                        ov["_why"][f"{t}.{key}"] = why
                        wrote.append(f"variant_render.{t}.{key}={stored}")
                else:
                    ov["variant_render"].setdefault(self.variant, {})[key] = val
                    ov["_why"][f"{self.variant}.{key}"] = why
                    wrote.append(f"variant_render.{self.variant}.{key}={val}")
            elif key == "CARD_LINES":
                recipe.setdefault("variety", {})["card_line"] = int(val)
                ov["_why"]["CARD_LINES"] = why
                wrote.append(f"variety.card_line={int(val)}")
            elif key in blocked or self.variant != "day":
                ov["variant_render"].setdefault(self.variant, {})[key] = val
                ov["_why"][f"{self.variant}.{key}"] = why
                wrote.append(f"variant_render.{self.variant}.{key}={val}")
            else:
                ov["render"][key] = val
                ov["_why"][key] = why
                wrote.append(f"render.{key}={val}")
        errors = []
        make.validate_recipe(recipe, self.batch, self.pl.defaults(), self.pl.root, errors, file_stem=self.rid)
        if errors:
            raise TuneError("the applied recipe does not validate:\n  " + "\n  ".join(errors))
        make.write_json(self.pl.recipe_path(self.rid), recipe)
        print(f"{self.pl.rel(self.pl.recipe_path(self.rid))}: " + (", ".join(wrote) if wrote else "nothing differs"))
        return wrote


def check_why(why):
    if not why or not why.strip():
        raise TuneError("--why is required with --pick: one plain line from the judge")
    if "\n" in why or len(why) > WHY_MAX:
        raise TuneError(f"--why must be one line of at most {WHY_MAX} characters")
    if "\u2014" in why:
        raise TuneError("--why must not contain an em dash")


def run(pl, args):
    import make
    try:
        t = Tuner(pl, args.id, args.variant)
        if args.apply:
            if args.pick or args.next or args.knob:
                raise TuneError("--apply runs on its own")
            t.apply()
            return
        if args.pick:
            if "=" not in args.pick:
                raise TuneError("--pick wants KNOB=a|b|c")
            name, label = args.pick.split("=", 1)
            if name not in BY_NAME:
                raise TuneError(f"unknown knob {name}")
            t.pick(name, label, args.why)
            if not (args.next or args.knob or args.auto):
                return
        if args.knob:
            if args.knob not in BY_NAME or args.variant not in BY_NAME[args.knob][5]:
                raise TuneError(f"{args.knob} is not a knob of the {args.variant} pass")
            s = t.run_knob(BY_NAME[args.knob])
            if args.auto:
                t.pick(args.knob, s["auto_pick"], f"auto pick by score ({s['auto_pick']})")
            return
        if args.next or args.auto:
            while True:
                pending = t.state.get("pending")
                if pending:
                    h = next(h for h in t.state["history"] if h["knob"] == pending)
                    if not args.auto:
                        print(f"{pending} is waiting for --pick (auto pick {h['auto_pick']}); sheets in {pl.rel(h['dir'])}")
                        return
                    t.pick(pending, h["auto_pick"], f"auto pick by score ({h['auto_pick']})")
                k = t.next_knob()
                if not k:
                    print(f"every knob of the {args.variant} pass has a pick; make.py tune {args.id} --apply writes them")
                    return
                t.run_knob(k)
                if not args.auto:
                    return
        raise TuneError("tune wants --next, --knob K, --pick K=x --why TEXT, --auto or --apply")
    except TuneError as e:
        raise make.MakeError(str(e))

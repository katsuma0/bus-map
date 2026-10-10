#!/usr/bin/env python3
"""B-9: every HUD and card string of the GTA batch is covered by the X fonts (fontTools cmap).

The strings: every place and title of the recipes, brand, rule and group labels of brands.json,
the batch's mode words, the variant labels, the labels and card lines of
cities/templates/shorts_en.json, the credit line and its fallback, the weekday
names, the axis words and the digits. Both MontserratX and InterX must map every character
(the panel uses one, the credit and card lines the other).

  python3 -I tests/web/test_fonts.py
"""

import glob
import json
import os
import sys

from fontTools.ttLib import TTFont

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
FIX = os.path.join(ROOT, "tests", "fixtures", "gta")


def strings():
    out = []
    with open(os.path.join(FIX, "batches", "gta.json"), encoding="utf-8") as fh:
        batch = json.load(fh)
    for m in batch["modes"]:
        out += [m["label"], m["singular"], m["id"]]
    places = []
    for p in sorted(glob.glob(os.path.join(FIX, "recipes", "*.json"))):
        with open(p, encoding="utf-8") as fh:
            r = json.load(fh)
        places.append(r["place"])
        out += [r["place"], r["place"].upper(), f"12,345 vehicles in {r['place']}", f"in {r['place']}"]
    with open(os.path.join(FIX, "brands.json"), encoding="utf-8") as fh:
        brands = json.load(fh)
    labels = []
    for e in brands["agencies"]:
        labels.append(e["label"])
        labels += [rule["label"] for rule in e.get("rules", [])]
    out += labels + ["other", f"Data: {', '.join(labels)} · Map: Overture, OSM",
                     "Data: 10 transit agencies · Map: Overture, OSM"]
    out += [f"1,234 {label}" for label in labels]
    month_words = ["October", "November"]
    for month in month_words:
        out += [f"An average {month} weekday", f"Morning rush, an average {month} weekday", f"An average {month} week"]
    out += ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY", "M T W T F S S",
            "midnight", "12:00 am", "8:04 pm", "8 am", "peak 3,951", "0123456789", "vehicle", "vehicles"]
    with open(os.path.join(ROOT, "cities", "templates", "shorts_en.json"), encoding="utf-8") as fh:
        tpl = json.load(fh)
    # Only labels and card lines reach the screen; titles and descriptions are metadata.
    out += [s for s in walk([tpl["labels"], tpl["card"]]) if isinstance(s, str)]
    src = "cities/templates/shorts_en.json"
    # Placeholders are filled with the strings above; their braces never reach the screen.
    return [s.replace("{", "").replace("}", "") for s in out], src


def walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    else:
        yield obj


def main():
    texts, src = strings()
    chars = sorted({c for t in texts for c in t})
    failures = []
    for name in ("MontserratX", "InterX"):
        cmap = TTFont(os.path.join(ROOT, "web", "fonts", f"{name}.woff2")).getBestCmap()
        missing = [c for c in chars if ord(c) not in cmap]
        if missing:
            failures.append(f"{name} lacks {' '.join(f'U+{ord(c):04X} {c!r}' for c in missing)}")
    for f in failures:
        print(f"FAIL {f}")
    print(f"fonts: {len(texts)} strings, {len(chars)} characters (templates from {src}), {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

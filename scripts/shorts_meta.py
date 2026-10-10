"""Titles, descriptions, tags, licence flags and the YouTube CSV for a Shorts batch (spec D5, 2.13).

Everything here reads the batch, the recipe, cities/templates/shorts_en.json,
cities/licences.json and one video's <stem>.netmeta.json, so make.py can rebuild
the metadata of a whole release from the small netmeta files alone. Every
number comes from the netmeta's `peak` and feeds, the same values the card and
the peak label on screen show.

Library only; make.py meta and make.py release --publish call it.
"""

import csv
import datetime
import re

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CLASS_DAYS = {"wd": ("weekday", "weekdays"), "mon": ("Monday", "Mondays"), "tue": ("Tuesday", "Tuesdays"),
              "wed": ("Wednesday", "Wednesdays"), "thu": ("Thursday", "Thursdays"), "fri": ("Friday", "Fridays"),
              "sat": ("Saturday", "Saturdays"), "sun": ("Sunday", "Sundays")}
CLASS_ORDER = ["wd", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TITLE_MAX = 99
DESCRIPTION_MAX = 4900
TAGS_MAX = 500
HASHTAGS_MAX = 5
CSV_COLUMNS = ["publish_order", "file", "title", "description", "tags", "category", "made_for_kids", "visibility",
               "licence_flags"]
EM_DASH = "\u2014"
EN_DASH = "\u2013"


class MetaError(Exception):
    pass


# ------------------------------------------------------------------ formatting

def fmt(template, values, where):
    """str.format with a strict check, so an unknown placeholder fails instead of leaking into a title."""
    def sub(m):
        k = m.group(1)
        if k not in values:
            raise MetaError(f"{where}: unknown placeholder {{{k}}}")
        return str(values[k])
    return re.sub(r"\{(\w+)\}", sub, template)


def with_commas(n):
    return f"{int(n):,}"


def clock_text(t):
    """app.js clockText: 8:01 am."""
    s = int(t) % 86400
    h, m = s // 3600, s % 3600 // 60
    ap = "am" if h < 12 else "pm"
    h %= 12
    return f"{h or 12}:{m:02d} {ap}"


def join_words(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def clean(s):
    """Names come from feeds and configs; an en dash becomes a hyphen (D5)."""
    return (s or "").replace(EN_DASH, "-")


def iso_date(s):
    return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def date_label(s):
    d = iso_date(s)
    return f"{MONTHS[d.month - 1]} {d.day}"


def range_label(dates):
    lo, hi = iso_date(min(dates)), iso_date(max(dates))
    if lo == hi:
        return f"{MONTHS[lo.month - 1]} {lo.day}"
    if (lo.year, lo.month) == (hi.year, hi.month):
        return f"{MONTHS[lo.month - 1]} {lo.day} to {hi.day}"
    return f"{MONTHS[lo.month - 1]} {lo.day} to {MONTHS[hi.month - 1]} {hi.day}"


def month_name(ym):
    return MONTHS[int(ym[5:7]) - 1]


def all_dates(dates):
    if isinstance(dates, dict):
        return sorted({d for v in dates.values() for d in v})
    return sorted(dates or [])


# ------------------------------------------------------------------ pieces

def inside_feeds(netmeta):
    """Feeds with vehicles inside the boundary, busiest first."""
    def ivm(f):
        v = f.get("inside_vehicle_minutes")
        return v if v is not None else f.get("inside_share") or 0
    feeds = [f for f in netmeta.get("feeds", []) if ivm(f) > 0]
    return sorted(feeds, key=lambda f: (-(f.get("inside_share") or 0), -ivm(f), f["id"]))


def modes_words(netmeta, modes):
    present = set(netmeta.get("modes_present") or [])
    ms = [m for m in modes if m["id"] in present] or list(modes)
    return join_words(m["singular"] for m in ms), join_words(m["label"] for m in ms)


def dates_sentence(netmeta, templates, week):
    s = templates["sentences"]
    month = netmeta.get("month") or ""
    mname, year = month_name(month), month[:4]
    feeds = inside_feeds(netmeta)
    holidays, dst = {}, set()
    for f in feeds:
        for x in f.get("excluded", []) or []:
            if x.get("why") == "holiday":
                holidays[x["date"]] = clean(x.get("name") or "a holiday")
            elif x.get("why") == "dst":
                dst.add(x["date"])
    hol = join_words(f"{name} ({date_label(d)})" for d, name in sorted(holidays.items()))
    kind = "week" if week else "day"
    leaves = fmt(s[f"leaves_out_{kind}"], {"holidays": hol}, "sentences") if hol else ""
    out = [fmt(s[f"dates_{kind}"], {"month": mname, "year": year, "leaves_out": leaves}, "sentences")]
    for f in feeds:
        used = f.get("month_used")
        if used and used != month and all_dates(f.get("dates")):
            out.append(fmt(s["fallback"], {"feed": clean(f["name"]), "month": mname,
                                           "range": range_label(all_dates(f["dates"]))}, "sentences"))
    if dst:
        labels = join_words(date_label(d) for d in sorted(dst))
        out.append(fmt(s["dst_one" if len(dst) == 1 else "dst_many"], {"dates": labels}, "sentences"))
    for f in feeds:
        for x in sorted((x for x in f.get("excluded", []) or [] if x.get("why") == "low"), key=lambda x: x["date"]):
            out.append(fmt(s["low"], {"feed": clean(f["name"]), "date": date_label(x["date"]),
                                      "weekday": DAYS[iso_date(x["date"]).weekday()]}, "sentences"))
    for f in feeds:
        rule, dates = f.get("rule"), f.get("dates")
        if isinstance(rule, dict):
            classes = [k for k in CLASS_ORDER if rule.get(k) == "median-date"]
        else:
            classes = ["wd"] if rule == "median-date" else []
        few = [k for k in classes if len((dates.get(k, []) if isinstance(dates, dict) else dates) or []) < 3]
        changed = [k for k in classes if k not in few]
        for group, key in ((changed, "median_changed"), (few, "median_few")):
            if not group:
                continue
            out.append(fmt(s[key], {"feed": clean(f["name"]), "month": mname,
                                    "days": join_words(CLASS_DAYS[k][0] for k in group),
                                    "days_plural": join_words(CLASS_DAYS[k][1] for k in group),
                                    "verb": s["verb_one" if len(group) == 1 else "verb_many"]}, "sentences"))
    return " ".join(out)


def licence_entry(feed, licences, allow_nc=False):
    """(entry, error) for one batch feed; matched on licence_id only, never on free text."""
    lic = licences.get("licences", {}).get(feed.get("licence_id"))
    if lic is None:
        return None, f"{feed.get('id')}: unknown licence id {feed.get('licence_id')!r}"
    entry = {"feed": feed["id"], "licence_id": feed["licence_id"], "commercial": lic.get("commercial", "check"),
             "note": lic.get("note"), "allow_nc": feed.get("allow_nc")}
    if entry["commercial"] == "no" and not feed.get("allow_nc") and not allow_nc:
        return entry, (f"{feed['id']}: licence {feed['licence_id']} is non-commercial; give the feed an allow_nc "
                       f"reason in the batch file or drop it")
    return entry, None


def licence_flags(entries, names):
    out = []
    for e in entries:
        name = names.get(e["feed"], e["feed"])
        if e["commercial"] == "check":
            out.append(f"{name}: check the licence ({e['licence_id']}): {e.get('note') or 'terms not confirmed'}")
        elif e["commercial"] == "no":
            out.append(f"{name}: non-commercial licence ({e['licence_id']}), allowed: {e.get('allow_nc') or '--allow-nc'}")
    return out


def credit_lines(netmeta, licences, templates):
    lines = []
    for f in inside_feeds(netmeta):
        lic = licences.get("licences", {}).get(f.get("licence_id"), {})
        line = fmt(templates["sentences"]["credit_line"],
                   {"name": clean(f.get("name")), "publisher": clean(f.get("publisher")),
                    "licence": clean(lic.get("name") or f.get("licence_text") or f.get("licence_id"))}, "credit_line")
        if lic.get("statement"):
            line += " " + clean(lic["statement"])
        lines.append(line)
    return lines


def hashtag_list(batch, place):
    tags = []
    seen = set()
    own = "#" + re.sub(r"[\W_]+", "", place.lower())
    for t in ["#shorts"] + list(batch.get("hashtags", [])) + [own]:
        if len(t) > 1 and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    return tags[:HASHTAGS_MAX]


def tag_list(netmeta, batch, recipe, templates):
    area_id = recipe.get("area") or batch["areas"][0]["id"]
    area = next(a for a in batch["areas"] if a["id"] == area_id)
    region = templates.get("regions", {}).get(area.get("region"))
    labels = [clean(g.get("label")) for g in netmeta.get("groups", []) if g.get("id") != "other" and g.get("label")]
    out, seen, total = [], set(), 0
    for t in [clean(recipe["place"])] + labels + list(templates.get("tags", [])) + ([region] if region else []):
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (1 if out else 0)
        if total + cost > TAGS_MAX:
            break
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def check_text(field, text, banned):
    if EM_DASH in text:
        raise MetaError(f"{field} contains an em dash (U+2014)")
    for word in banned:
        m = re.search(rf"\b({re.escape(word)})\w*\b", text, re.IGNORECASE)
        if m:
            raise MetaError(f"{field} contains the banned word {m.group(0)!r}")


# ------------------------------------------------------------------ one video

def build_meta(*, batch, recipe, netmeta, templates, licences, defaults, publish_order, meta_key, sidecar=None,
               allow_nc=False):
    variant = netmeta["variant"]
    stem = f"{recipe['id']}-{variant}"
    place = clean(recipe["place"])
    singular, plural = modes_words(netmeta, netmeta.get("modes") or batch["modes"])
    peak = netmeta["peak"]
    month = netmeta.get("month") or batch["month"]
    netmeta = dict(netmeta, month=month)
    mlabel = netmeta.get("month_label") or month_name(month)
    year = month[:4]
    if mlabel != month_name(month):
        # The label follows a major fallback feed (Burlington: November); so does the year.
        for f in netmeta.get("feeds", []):
            if f.get("month_used") and month_name(f["month_used"]) == mlabel:
                year = f["month_used"][:4]
    credits = credit_lines(netmeta, licences, templates)
    values = {
        "place": place, "modes_singular": singular, "modes_plural": plural,
        "peak_time": clock_text(peak["time"]), "peak_count": with_commas(round(peak["count"])),
        "peak_day": DAYS[int(peak["time"] // 86400) % 7],
        "month": mlabel, "year": year, "seconds": netmeta.get("seconds"),
        "trips": with_commas(netmeta.get("trips_total") or 0),
        "dates_sentence": dates_sentence(netmeta, templates, variant == "week"),
        "credits": "\n".join(credits), "author": defaults.get("author", ""),
        "hashtags": " ".join(hashtag_list(batch, place)),
    }
    title = fmt(templates["title"][variant], values, f"{stem} title")
    description = fmt(templates["description"][variant], values, f"{stem} description")
    tags = tag_list(netmeta, batch, recipe, templates)
    banned = templates.get("banned", [])
    check_text(f"{stem} title", title, banned)
    check_text(f"{stem} description", description, banned)
    check_text(f"{stem} tags", ", ".join(tags), banned)
    if len(title) > TITLE_MAX:
        raise MetaError(f"{stem} title is {len(title)} characters, the limit is {TITLE_MAX}: {title}")
    if len(description) > DESCRIPTION_MAX:
        raise MetaError(f"{stem} description is {len(description)} characters, the limit is {DESCRIPTION_MAX}")
    batch_feeds = {f["id"]: f for a in batch["areas"] for f in a["feeds"]}
    entries, errors, names = [], [], {}
    for f in netmeta.get("feeds", []):
        bf = dict(batch_feeds.get(f["id"], {}), **{k: f[k] for k in ("id", "licence_id") if k in f})
        bf.setdefault("allow_nc", batch_feeds.get(f["id"], {}).get("allow_nc"))
        names[f["id"]] = clean(f.get("name") or bf.get("name") or f["id"])
        entry, err = licence_entry(bf, licences, allow_nc=allow_nc)
        if err:
            errors.append(err)
        if entry:
            entries.append(entry)
    if errors:
        raise MetaError("; ".join(errors))
    flags = licence_flags(entries, names)
    if sidecar and sidecar.get("bitrate_floor_met") is False:
        flags.append(f"bitrate floor not met: {sidecar.get('kbps')} kbps at crf {sidecar.get('crf')}")
    return {
        "file": f"{stem}.mp4", "title": title, "description": description, "tags": tags,
        "hashtags": hashtag_list(batch, place), "category": batch.get("category"),
        "credits": credits, "licences": entries, "flags": flags, "licence_flags": licence_flags(entries, names),
        "publish_order": publish_order, "meta_key": meta_key,
    }


# ------------------------------------------------------------------ the batch CSV

def write_csv(path, metas):
    """UTF-8 with BOM, RFC 4180 quoting, one row per video in publish order."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        for m in sorted(metas, key=lambda m: m["publish_order"]):
            w.writerow([m["publish_order"], m["file"], m["title"], m["description"], ",".join(m["tags"]),
                        m.get("category") or "", "no", "private", "; ".join(m.get("licence_flags", []))])

"""Titles, descriptions, tags, licence flags and the YouTube CSV for a Shorts batch (spec D5, 2.13).

Everything here reads the batch, the recipe, cities/templates/shorts_en.json,
cities/licences.json and one video's <stem>.netmeta.json, so make.py can rebuild
the metadata of a whole release from the small netmeta files alone. Every
number comes from the netmeta's `peak` and feeds, the same values the count
line on screen reaches at the peak (and the card and the peak label, when a
render turns them on).

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


def dates_label(dates):
    """October 10 and 11; October 31 and November 1."""
    ds = [iso_date(d) for d in sorted(dates)]
    if len({(d.year, d.month) for d in ds}) == 1:
        return f"{MONTHS[ds[0].month - 1]} {join_words(str(d.day) for d in ds)}"
    return join_words(date_label(d) for d in sorted(dates))


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


def drawn_feeds(netmeta):
    """Feeds whose trips the video draws on screen in its window, for the credits and the licence flags:
    a neighbour's buses on screen, faded outside the boundary, are its data too. Those with vehicles
    inside come first, busiest first, then the rest by trips drawn. A netmeta without window_trips
    (written before make.py recorded them) has only the inside counts to go on."""
    feeds = netmeta.get("feeds", [])
    if not any("window_trips" in f for f in feeds):
        return inside_feeds(netmeta)
    drawn = [f for f in feeds if (f.get("window_trips") or 0) > 0]
    return sorted(drawn, key=lambda f: (-(f.get("inside_share") or 0), -(f.get("inside_vehicle_minutes") or 0),
                                        -f["window_trips"], f["id"]))


def present_modes(netmeta, modes):
    """B9's modes of the window, else every mode (as the page's modesInWindow)."""
    present = set(netmeta.get("modes_present") or [])
    return [m for m in modes if m["id"] in present] or list(modes)


def modes_words(netmeta, modes):
    ms = present_modes(netmeta, modes)
    return join_words(m["singular"] for m in ms), join_words(m["label"] for m in ms)


def count_noun(netmeta, modes, n):
    """The count line's noun (B9): the mode's own word when only one mode is in the window, else vehicles."""
    ms = present_modes(netmeta, modes)
    if len(ms) == 1:
        return ms[0]["singular"] if n == 1 else ms[0]["label"]
    return "vehicle" if n == 1 else "vehicles"


def limits_words(recipe, place, templates):
    """Where the counted vehicles are: one place's limits, or the divisions a union boundary joins."""
    s = templates["sentences"]
    b = recipe.get("boundary")
    if isinstance(b, list) and len(b) > 1:
        parts = join_words(clean(p["name"]) for p in b)
        return (fmt(s["limits_many"], {"place": place, "parts": parts}, "limits_many"),
                fmt(s["the_city_many"], {"place": place}, "the_city_many"))
    # The recipe's limits says what the place is: Oakville and Whitby are towns. A place that already
    # says it (New York City, Mexico City) would read "the New York City city limits".
    kind = recipe.get("limits") or "city"
    key = "limits_one_city" if re.search(rf"\b{re.escape(kind)}\b", place, re.IGNORECASE) else "limits_one"
    return (fmt(s[key], {"place": place, "kind": kind}, key),
            fmt(s["the_city_one"], {"place": place, "kind": kind}, "the_city_one"))


def colors_sentence(netmeta, templates):
    """{colors}: one plain sentence naming a colour-group video's colours (2.16), with its leading space,
    or empty for a brand network, so that video's description is what it was. A shared legend word
    stands once for its groups; other reads "everything else" unless the recipe named it."""
    if netmeta.get("color_by") != "group":
        return ""
    s = templates["sentences"]
    words, other = [], None
    for g in netmeta.get("groups") or []:
        if g["id"] == "other":
            # The trim lists other when any route it keeps falls in it, and its share is the inside
            # share at the morning peak minute alone, so neither says whether the video draws anything
            # grey: Philadelphia's M line has share 0 and runs faded west of the city line all day.
            if g.get("window_trips", 1) > 0:
                other = s["colors_everything"] if g.get("label", "Other") == "Other" else clean(g["label"])
            continue
        lg = g.get("legend")
        w = clean(lg if isinstance(lg, str) else g.get("label") or g["id"])
        if w not in words:
            words.append(w)
    if not words:
        return ""
    if other:
        return " " + fmt(s["colors_other"], {"groups": join_words(words), "other": other}, "sentences")
    return " " + fmt(s["colors"], {"groups": join_words(words)}, "sentences")


def label_feeds(netmeta, month, mlabel):
    """(lead, others): the fallback feeds whose month the label follows, and whether the video draws any
    other feed. Burlington Transit has no October timetable and runs most of Burlington's buses, so that
    video is an average November weekday while GO and Oakville Transit keep October's timetables."""
    if mlabel == month_name(month):
        return [], True
    lead = [f for f in inside_feeds(netmeta) if f.get("month_used") not in (None, month)
            and month_name(f["month_used"]) == mlabel and all_dates(f.get("dates"))]
    ids = {f["id"] for f in lead}
    return lead, any(f["id"] not in ids for f in drawn_feeds(netmeta))


def timetables_sentence(variant, month, lead, others, templates):
    """{timetables}: which timetables the video uses. When the label follows a fallback feed, the reason
    comes first and the batch month is named for the other feeds only, so the description never says
    "an average November weekday" and then "I took the October 2026 timetables"."""
    s = templates["sentences"]
    mname, year = month_name(month), month[:4]
    if not lead:
        return fmt(s[f"timetables_{variant}"], {"month": mname, "year": year}, "sentences")
    n = "many" if len(lead) > 1 else "one"
    rest = fmt(s["fallback_others"], {"month": mname, "year": year}, "sentences") if others else ""
    why = fmt(s["fallback_lead"], {"feed": join_words(clean(f["name"]) for f in lead), "month": mname,
                                   "has": s[f"has_{n}"], "it": s[f"it_{n}"], "others": rest,
                                   "range": range_label([d for f in lead for d in all_dates(f["dates"])])},
              "sentences")
    return fmt(s[f"timetables_{variant}_fallback"], {"fallback": why}, "sentences")


def dates_sentence(netmeta, templates, week, lead=(), others=True):
    """The dates each timetable averages and what they leave out. `lead` are the fallback feeds the
    timetables sentence already explained (label_feeds), so their fallback is not said twice."""
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
    lead_ids = {f["id"] for f in lead}
    if lead and not others:
        out = [fmt(s[f"dates_{kind}_lead"], {"leaves_out": leaves}, "sentences")]
    else:
        out = [fmt(s[f"dates_{kind}"], {"month": mname, "year": year, "leaves_out": leaves,
                                        "which": s["which_others" if lead else "which_each"]}, "sentences")]
    for f in feeds:
        if f["id"] in lead_ids:
            continue
        used = f.get("month_used")
        if used and used != month and all_dates(f.get("dates")):
            out.append(fmt(s["fallback"], {"feed": clean(f["name"]), "month": mname,
                                           "range": range_label(all_dates(f["dates"]))}, "sentences"))
    if dst:
        labels = join_words(date_label(d) for d in sorted(dst))
        out.append(fmt(s["dst_one" if len(dst) == 1 else "dst_many"], {"dates": labels}, "sentences"))
    # Each caveat once: a feed's reduced days in one sentence, and feeds with the same caveat in one
    # sentence. A GTA week had four sentences on GO alone (October 10, October 11, a timetable change,
    # too few weekend dates), and New York's trains said the same two things of LIRR and Metro-North.
    low = []
    for f in feeds:
        days = tuple(sorted({x["date"] for x in f.get("excluded", []) or [] if x.get("why") == "low"}))
        if days:
            low.append((days, clean(f["name"])))
    for days, names in grouped(low):
        if len(days) == 1:
            out.append(fmt(s["low"], {"feed": join_words(names), "date": date_label(days[0]),
                                      "weekday": DAYS[iso_date(days[0]).weekday()]}, "sentences"))
        else:
            out.append(fmt(s["low_many"], {"feed": join_words(names), "dates": dates_label(days)}, "sentences"))
    median = []
    for f in feeds:
        rule, dates = f.get("rule"), f.get("dates")
        if isinstance(rule, dict):
            classes = [k for k in CLASS_ORDER if rule.get(k) == "median-date"]
        else:
            classes = ["wd"] if rule == "median-date" else []
        few = tuple(k for k in classes if len((dates.get(k, []) if isinstance(dates, dict) else dates) or []) < 3)
        changed = tuple(k for k in classes if k not in few)
        if classes:
            median.append(((changed, few), clean(f["name"])))
    for (changed, few), names in grouped(median):
        n = "many" if len(names) > 1 else "one"
        days = [k for k in CLASS_ORDER if k in changed or k in few]
        key = "median_both" if changed and few else "median_changed" if changed else "median_few"
        out.append(fmt(s[key], {"feed": join_words(names), "month": mname, "its": s[f"its_{n}"], "has": s[f"has_{n}"],
                                "days": join_words(CLASS_DAYS[k][0] for k in days),
                                "days_plural": join_words(CLASS_DAYS[k][1] for k in few),
                                "verb": s["verb_one" if len(days) == 1 else "verb_many"]}, "sentences"))
    return " ".join(out)


def grouped(items):
    """[(key, name)] as [(key, [names])], in the order each key first comes."""
    out = {}
    for key, name in items:
        out.setdefault(key, []).append(name)
    return list(out.items())


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


def data_date_line(feed, data_dates, templates):
    """{feed} timetables as of 4 October 2026: the date a licence asks the credit to give (Metra's)."""
    iso = (data_dates or {}).get(feed["id"])
    if not iso:
        raise MetaError(f"{feed['id']}: licence {feed.get('licence_id')} asks for the date the data was last "
                        f"updated, and the lock gives none for it; lock the feed from an MDB dataset URL "
                        f"(.../mdb-<id>-<YYYYMMDDHHMM>.zip), whose stamp is that date")
    d = iso_date(iso)
    return fmt(templates["sentences"]["data_date"], {"feed": clean(feed.get("name") or feed["id"]), "day": d.day,
                                                     "month": MONTHS[d.month - 1], "year": d.year}, "data_date")


def credit_lines(netmeta, licences, templates, data_dates=None):
    """One line per feed the video draws, and after it the data date when its licence asks for one.
    `data_dates` maps feed ids to the ISO date make.py reads from the lock."""
    lines = []
    s = templates["sentences"]
    for f in drawn_feeds(netmeta):
        lic = licences.get("licences", {}).get(f.get("licence_id"), {})
        name, publisher = clean(f.get("name")), clean(f.get("publisher"))
        # An agency that publishes its own feed under its own name would read
        # "Chicago Transit Authority (Chicago Transit Authority)".
        key = "credit_line_same" if publisher.strip().lower() in ("", name.strip().lower()) and "credit_line_same" in s \
            else "credit_line"
        line = fmt(s[key], {"name": name, "publisher": publisher,
                            "licence": clean(lic.get("name") or f.get("licence_text") or f.get("licence_id"))}, key)
        if lic.get("statement"):
            line += " " + clean(lic["statement"])
        lines.append(line)
        if lic.get("data_date"):
            lines.append(data_date_line(f, data_dates, templates))
    return lines


def legend_mode_faults(netmeta, modes):
    """A colour group whose legend word names a mode ("Bus") while the video draws another mode's trips
    in it: San Diego's Coronado ferry took the MTS navy of the Bus group, so the title named a ferry the
    legend had no word for and the map drew it as a bus. Needs window_modes, so an older netmeta passes."""
    if netmeta.get("color_by") != "group":
        return []
    word_mode, label, singular = {}, {}, {}
    for m in modes:
        label[m["id"]] = m.get("label") or m["id"]
        singular[m["id"]] = m.get("singular") or m["id"]
        for w in (m["id"], m.get("label"), m.get("singular")):
            if w:
                word_mode[w.strip().lower()] = m["id"]
    out = []
    for g in netmeta.get("groups") or []:
        lg = g.get("legend")
        word = clean(lg if isinstance(lg, str) else g.get("label") or g["id"])
        mode = word_mode.get(word.strip().lower())
        drawn = g.get("window_modes") or {}
        stray = sorted(k for k, n in drawn.items() if k != mode and n > 0)
        if mode and stray:
            trips = ", ".join(f"{drawn[k]} {singular.get(k, k)} trips" for k in stray)
            out.append(f"group {g['id']} reads {word!r} in the legend, but the video draws "
                       f"{join_words(label.get(k, k) for k in stray)} in its colour ({trips}); give them a group of "
                       f"their own in the recipe's colors, or leave their mode out of the recipe's modes")
    return out


def tag_place(place, templates):
    """The place as a tag and a hashtag name it. "the GTA" is written as a sentence reads it, but nobody
    searches for "the GTA" or #thegta, so a lowercase leading article goes (a capital one is part of the
    name: The Hague). A short name that means something else on YouTube is spelled out from the
    templates' place_tags: #gta files a video with Grand Theft Auto."""
    bare = re.sub(r"^the\s+", "", place)
    return (templates or {}).get("place_tags", {}).get(bare, bare)


def hashtag_list(batch, place, templates=None):
    tags = []
    seen = set()
    own = "#" + re.sub(r"[\W_]+", "", tag_place(place, templates).lower())
    for t in ["#shorts"] + list(batch.get("hashtags", [])) + [own]:
        if len(t) > 1 and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    return tags[:HASHTAGS_MAX]


def tag_list(netmeta, batch, recipe, templates, modes=None):
    area_id = recipe.get("area") or batch["areas"][0]["id"]
    area = next(a for a in batch["areas"] if a["id"] == area_id)
    region = templates.get("regions", {}).get(area.get("region"))
    # Feed names rather than the chip labels: a bare "GO" or "UP" matches nothing a viewer searches for.
    names = [clean(f.get("name")) for f in inside_feeds(netmeta) if f.get("name")]
    # "{mode_tags}" stands for one tag per mode the title names, so a video of trains is not tagged "bus map".
    mode_tags = templates.get("mode_tags", {})
    fixed = []
    for t in templates.get("tags", []):
        if t == "{mode_tags}":
            fixed += [mode_tags[m["id"]] for m in present_modes(netmeta, modes or batch["modes"]) if m["id"] in mode_tags]
        else:
            fixed.append(t)
    out, seen, total = [], set(), 0
    for t in [tag_place(clean(recipe["place"]), templates)] + names + fixed + ([region] if region else []):
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
               allow_nc=False, data_dates=None):
    """One video's metadata. `data_dates` maps feed ids to the ISO date of their locked data, for a licence
    that asks the description to give it (make.py lock_data_dates)."""
    variant = netmeta["variant"]
    stem = f"{recipe['id']}-{variant}"
    place = clean(recipe["place"])
    modes = netmeta.get("modes") or batch["modes"]
    singular, plural = modes_words(netmeta, modes)
    limits, the_city = limits_words(recipe, place, templates)
    peak = netmeta["peak"]
    month = netmeta.get("month") or batch["month"]
    netmeta = dict(netmeta, month=month)
    mlabel = netmeta.get("month_label") or month_name(month)
    lead, others = label_feeds(netmeta, month, mlabel)
    # The label follows a major fallback feed (Burlington: November); so does the year.
    year = lead[0]["month_used"][:4] if lead else month[:4]
    faults = legend_mode_faults(netmeta, batch["modes"])
    if faults:
        raise MetaError("; ".join(faults))
    credits = credit_lines(netmeta, licences, templates, data_dates)
    hashtags = hashtag_list(batch, place, templates)
    values = {
        "place": place, "modes_singular": singular, "modes_plural": plural,
        "peak_time": clock_text(peak["time"]), "peak_count": with_commas(round(peak["count"])),
        "vehicles": count_noun(netmeta, modes, round(peak["count"])), "limits": limits, "the_city": the_city,
        "peak_day": DAYS[int(peak["time"] // 86400) % 7],
        "month": mlabel, "year": year, "seconds": netmeta.get("seconds"),
        # Most feeds still use the batch month when the label follows a fallback,
        # so a sentence about the timetables names that month, as the dates
        # sentence does.
        "timetable_month": month_name(month), "timetable_year": month[:4],
        "timetables": timetables_sentence(variant, month, lead, others, templates),
        "trips": with_commas(netmeta.get("trips_total") or 0),
        "dates_sentence": dates_sentence(netmeta, templates, variant == "week", lead, others),
        "colors": colors_sentence(netmeta, templates),
        "credits": "\n".join(credits), "author": defaults.get("author", ""),
        "hashtags": " ".join(hashtags),
    }
    title = fmt(templates["title"][variant], values, f"{stem} title")
    description = fmt(templates["description"][variant], values, f"{stem} description")
    tags = tag_list(netmeta, batch, recipe, templates, modes)
    banned = templates.get("banned", [])
    check_text(f"{stem} title", title, banned)
    check_text(f"{stem} description", description, banned)
    check_text(f"{stem} tags", ", ".join(tags), banned)
    for h in hashtags:
        why = templates.get("hashtags_banned", {}).get(h.lower())
        if why:
            raise MetaError(f"{stem} hashtag {h}: {why}; take it out of the batch's hashtags, or spell the place "
                            f"out in the templates' place_tags")
    if len(title) > TITLE_MAX:
        raise MetaError(f"{stem} title is {len(title)} characters, the limit is {TITLE_MAX}: {title}")
    if len(description) > DESCRIPTION_MAX:
        raise MetaError(f"{stem} description is {len(description)} characters, the limit is {DESCRIPTION_MAX}")
    batch_feeds = {f["id"]: f for a in batch["areas"] for f in a["feeds"]}
    entries, errors, names = [], [], {}
    # The feeds this video draws, as the credits: a split video's flags leave out the other modes' feeds.
    for f in drawn_feeds(netmeta):
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
        "hashtags": hashtags, "category": batch.get("category"),
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

#!/usr/bin/env python3
"""Legacy function bodies of web/app.js (spec B1, test B-1).

Every top-level function of web/app.js at the legacy commit (a line starting
with "function name(" or "async function name(" at column 0, up to the next
line starting with "}") must appear unchanged in the new file, except the B1
whitelist:

  1. init(): one inserted line right after the Promise.all fetch,
       if (network.meta && network.meta.schema === 4) return initV4(basemap, network);
  2. the busmap object: renderFrame and renderAt may become the isV4 dispatchers,
     and new members may be appended after the last legacy member;
  3. the page glue: its calls to renderAt / renderFrame may go through
     busmap.renderAt / busmap.renderFrame;
  4. new top-level declarations anywhere outside legacy bodies, and new
     `KEY: value,` lines (with their comments) inside the CONFIG literal; every
     legacy top-level line before the page glue (CONFIG and its defaults,
     COLORS, params, HUD_OVERRIDE, ...) stays as it is.

An unchanged file passes. Usage:
  python3 tests/legacy/bodies.py [--base 302d811] [--file web/app.js]
"""

import argparse
import difflib
import os
import re
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
BASE = "302d811"

FUNC_RE = re.compile(r"^(?:async )?function ([A-Za-z_$][\w$]*)\(")
INIT_LINE = "if (network.meta && network.meta.schema === 4) return initV4(basemap, network);"
DISPATCH = {
    "renderFrame,": "renderFrame: (i) => (isV4 ? renderFrameV4(i) : renderFrame(i)),",
    "renderAt,": "renderAt: (T) => (isV4 ? renderAtV4(T) : renderAt(T)),",
}
GLUE_MARK = re.compile(r"^// -+ page glue\s*$")
CONFIG_OPEN = "const CONFIG = {"
# One new top-level CONFIG key on its own line; nested objects stay as they were.
CONFIG_KEY_LINE = re.compile(r"^  [A-Z][A-Z0-9_]*: [^{}\[\]]*,$")
BUSMAP_OPEN = "const busmap = {"
MEMBER_RE = re.compile(r"^  (?:get )?([A-Za-z_$][\w$]*)\s*(?:[,:(]|$)")
# Rerouting a glue call is the only edit allowed inside a glue statement.
REROUTE_RE = re.compile(r"\bbusmap\.(renderAt|renderFrame)\(")


def functions(lines):
    """[(name, start, end)] of top-level functions; end is the closing line."""
    out = []
    k = 0
    while k < len(lines):
        m = FUNC_RE.match(lines[k])
        if not m:
            k += 1
            continue
        end = k + 1
        while end < len(lines) and not lines[end].startswith("}"):
            end += 1
        if end == len(lines):
            raise ValueError(f"function {m.group(1)} at line {k + 1} has no closing brace at column 0")
        out.append((m.group(1), k, end))
        k = end + 1
    return out


def busmap_block(lines):
    starts = [k for k, l in enumerate(lines) if l.rstrip() == BUSMAP_OPEN]
    if len(starts) != 1:
        raise ValueError(f"expected one '{BUSMAP_OPEN}' line, found {len(starts)}")
    k = starts[0]
    end = k + 1
    while end < len(lines) and not lines[end].startswith("};"):
        end += 1
    if end == len(lines):
        raise ValueError("busmap object has no closing '};' at column 0")
    return k, end


def check_init(old, new):
    if new == old:
        return []
    at = None
    for k, line in enumerate(old):
        if "Promise.all(" in line:
            for j in range(k, len(old)):
                if old[j].strip() == "]);":
                    at = j + 1
                    break
            break
    if at is None:
        return ["init: legacy body has no Promise.all fetch to anchor the inserted line"]
    if len(new) == len(old) + 1 and new[:at] == old[:at] and new[at + 1:] == old[at:] \
            and new[at].strip() == INIT_LINE:
        return []
    return ["init: body differs from the legacy one by more than the inserted line\n"
            + diff(old, new, "init (302d811)", "init (new)")]


def check_busmap(old, new):
    problems = []
    if new[0] != old[0]:
        problems.append(f"busmap: first line changed to {new[0]!r}")
    old_members = old[1:-1]
    if len(new) - 2 < len(old_members):
        return problems + ["busmap: legacy members were removed\n" + diff(old, new, "busmap (302d811)", "busmap (new)")]
    for k, line in enumerate(old_members):
        got = new[1 + k]
        if got == line:
            continue
        allowed = DISPATCH.get(line.strip())
        if allowed is not None and got.strip() == allowed:
            continue
        problems.append(f"busmap: legacy member line {k + 1} {line.strip()!r} became {got.strip()!r}")
    names = set()
    for line in old_members:
        m = MEMBER_RE.match(line)
        if m:
            names.add(m.group(1))
    for line in new[1 + len(old_members):-1]:
        m = MEMBER_RE.match(line)
        if m and m.group(1) in names:
            problems.append(f"busmap: appended member {m.group(1)!r} shadows a legacy member")
    return problems


def glue_lines(lines, funcs, bm):
    """The page glue with the busmap object and top-level functions folded to one
    placeholder line each (they are checked on their own)."""
    marks = [k for k, l in enumerate(lines) if GLUE_MARK.match(l)]
    if len(marks) != 1:
        raise ValueError(f"expected one '// --- page glue' marker, found {len(marks)}")
    start = marks[0]
    fold = {s: (e, f"@@function {n}@@") for n, s, e in funcs if s > start}
    fold[bm[0]] = (bm[1], "@@busmap@@")
    out = []
    k = start
    while k < len(lines):
        if k in fold:
            end, label = fold[k]
            out.append(label)
            k = end + 1
        else:
            out.append(lines[k])
            k += 1
    return out


def prelude_lines(lines, funcs, bm):
    """Everything before the page glue, with the top-level functions and the
    busmap object folded to one placeholder line each."""
    marks = [k for k, l in enumerate(lines) if GLUE_MARK.match(l)]
    if len(marks) != 1:
        raise ValueError(f"expected one '// --- page glue' marker, found {len(marks)}")
    end = marks[0]
    fold = {s: (e, f"@@function {n}@@") for n, s, e in funcs if s < end}
    if bm[0] < end:
        fold[bm[0]] = (bm[1], "@@busmap@@")
    out = []
    k = 0
    while k < end:
        if k in fold:
            e, label = fold[k]
            out.append(label)
            k = e + 1
        else:
            out.append(lines[k])
            k += 1
    return out


def top_level_gap(old, i):
    """True when an insertion before old[i] falls between two top-level statements."""
    before = old[i - 1] if i > 0 else ""
    after = old[i] if i < len(old) else ""
    ends = before.strip() == "" or before.startswith("@@") or before.startswith("//") \
        or (not before[:1].isspace() and before.rstrip().endswith((";", "}")))
    begins = after.strip() == "" or not after[:1].isspace() and not after.startswith("}")
    return ends and begins


def check_glue(old, new):
    norm = [REROUTE_RE.sub(r"\1(", l) for l in new]
    problems = []
    sm = difflib.SequenceMatcher(a=old, b=norm, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        added = new[j1:j2]
        if op == "insert" and top_level_gap(old, i1):
            first = next((l for l in added if l.strip()), "")
            if not first or (not first[:1].isspace() and not first.startswith("}")):
                continue
        problems.append(f"page glue: {op} at legacy glue line {i1 + 1}\n"
                        + "".join(f"  - {l}\n" for l in old[i1:i2])
                        + "".join(f"  + {l}\n" for l in added))
    return problems


def check_prelude(old, new):
    """Legacy top-level lines before the glue are unchanged: a new default for a
    legacy knob would otherwise pass, and F2 renders too few frames to catch it."""
    starts = [k for k, l in enumerate(old) if l.rstrip() == CONFIG_OPEN]
    if len(starts) != 1:
        return [f"expected one '{CONFIG_OPEN}' line in the legacy file, found {len(starts)}"]
    c0 = starts[0]
    c1 = next((k for k in range(c0 + 1, len(old)) if old[k].startswith("};")), None)
    if c1 is None:
        return ["the legacy CONFIG literal has no closing '};' at column 0"]
    problems = []
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        added = new[j1:j2]
        if op == "insert" and top_level_gap(old, i1):
            first = next((l for l in added if l.strip()), "")
            if not first or (not first[:1].isspace() and not first.startswith("}")):
                continue
        if op == "insert" and c0 < i1 <= c1 and old[i1 - 1].startswith("  ") and not old[i1 - 1].startswith("   ") \
                and all(l.strip() == "" or l.strip().startswith("//") or CONFIG_KEY_LINE.match(l) for l in added):
            continue
        problems.append(f"top level: {op} at legacy line {i1 + 1} (before the page glue, functions folded)\n"
                        + "".join(f"  - {l}\n" for l in old[i1:i2])
                        + "".join(f"  + {l}\n" for l in added))
    return problems


def diff(a, b, name_a, name_b):
    return "".join(difflib.unified_diff([l + "\n" for l in a], [l + "\n" for l in b], name_a, name_b, n=2))


def check(old_text, new_text):
    """Problems found, as strings; an empty list means the whitelist holds."""
    old = old_text.split("\n")
    new = new_text.split("\n")
    problems = []
    old_funcs = functions(old)
    new_funcs = functions(new)
    by_name = {}
    for name, s, e in new_funcs:
        by_name.setdefault(name, []).append((s, e))
    for name, s, e in old_funcs:
        found = by_name.get(name, [])
        if len(found) != 1:
            problems.append(f"function {name}: defined {len(found)} times at column 0 in the new file")
            continue
        ns, ne = found[0]
        a, b = old[s:e + 1], new[ns:ne + 1]
        if name == "init":
            problems += check_init(a, b)
        elif a != b:
            problems.append(f"function {name}: body changed\n" + diff(a, b, f"{name} (302d811)", f"{name} (new)"))
    for name in {n for n, _, _ in old_funcs}:
        shadow = re.compile(rf"^(?:let|const|var|class)\s+{re.escape(name)}\b")
        hits = [k + 1 for k, l in enumerate(new) if shadow.match(l)]
        if hits:
            problems.append(f"function {name}: shadowed by a top-level declaration at line(s) {hits}")
    obm, nbm = busmap_block(old), busmap_block(new)
    problems += check_busmap(old[obm[0]:obm[1] + 1], new[nbm[0]:nbm[1] + 1])
    problems += check_glue(glue_lines(old, old_funcs, obm), glue_lines(new, new_funcs, nbm))
    problems += check_prelude(prelude_lines(old, old_funcs, obm), prelude_lines(new, new_funcs, nbm))
    return problems


def legacy_text(base, path):
    return subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE, help="legacy commit (default %(default)s)")
    ap.add_argument("--file", default="web/app.js", help="repo-relative file to check (default %(default)s)")
    args = ap.parse_args()
    with open(os.path.join(ROOT, args.file), encoding="utf-8") as fh:
        new_text = fh.read()
    try:
        problems = check(legacy_text(args.base, args.file), new_text)
    except ValueError as e:
        problems = [str(e)]
    for p in problems:
        print(p)
    n = len(functions(legacy_text(args.base, args.file).split("\n")))
    print(f"bodies: {n} legacy functions, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

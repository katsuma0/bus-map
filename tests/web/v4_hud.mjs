#!/usr/bin/env node
// B-5, B-6 and B-10 on the v4 fixture and, when they exist, the GTA networks:
// build/<id>/{day,week}/network.json.gz from make.py build, else the stubs of
// tests/web/stub_gta_v4.py in build/stub_v4/ (legacy GTA trips, so the
// numbers are one date's, not the composite's; the layout, the names, the
// brands and the sizes are the real ones).
//
//  B-5  every hudBoxes() box inside the tall-phone safe zone x 120..800,
//       y 290..1440 (busmap.safe) and at or above its minimum size, on every
//       stillTimes() still and on frames 0 and 15 (the card with ?card=1), no
//       two rows' boxes overlapping, and no console.error from the page's own
//       layout asserts
//  B-6  at V.peak.time the count line equals V.peak.count (and B9: its noun
//       names a mode only when it is the one mode inside over the window); over every 5th
//       frame it never exceeds it; the chips add up to it; lastVehicles flags
//       equal point-in-polygon on meta.boundary except within 25 m of its edge
//  B-10 every pair of placed brands at least BRAND_MIN_DE (0.08) apart, and
//       no card line pair splits the place name
//  B-9  a group the chips line folds into "other" was tried at 24 px with
//       the 8 px gap first,
//       and (B6) its brands are drawn foreign, not placed
//
// The shorts preset shows none of the card, the chips, the peak marker and
// the sparkline's labels, so each network also runs with all four on (FULL):
// the card and the chips checks run there, and every box of it must still
// fit the safe zone at TEXT_SCALE 0.85.
//
// The fixture, a two-line credit and the long names also run at the low end
// of TEXT_SCALE (0.6), where most sizes sit at their floors: the gaps between
// rows must stop shrinking with them.
//
// Credit copies of the fixture carry the credits of the GTA batch with the
// author (B9): each still shows the credit on one line when it fits the
// panel's column, else the data line and then "Map: Overture, OSM · Made by
// SOtownships", on either panel side.
//
// Stress copies scale every count of a GTA network (Richmond Hill x200 for a
// five-digit count that has to split, Mississauga x12 and Toronto x8 for the
// widest chips lines) and run the same checks. Renamed copies of the fixture
// carry the longest place names (Mississauga, Richmond Hill, Philadelphia,
// San Antonio, San Francisco) through the title, the card and the count line.
//
// Usage: node tests/web/v4_hud.mjs [--only gta-toronto,...] [--no-gta]

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { openBrowser, ROOT, TINY, TINY_WEEK, check } from './browser.mjs';

const MIN = {
  title: 44, subtitle: 26, weekday: 48, clock: 30, count: 28, count2: 28, chips: 20, peak: 20, axis: 20, credit: 18, credit2: 18,
  card_title: 72, card_title2: 72, card_line0: 40, card_line0b: 40, card_line1: 30, card_line1b: 30,
};
const FULL = 'card=1&chips=1&peak=1&sparklabels=1';
const GTA = ['gta-toronto', 'gta-mississauga', 'gta-brampton', 'gta-markham', 'gta-vaughan', 'gta-oakville',
  'gta-richmond-hill', 'gta-burlington', 'gta-oshawa', 'gta-whitby'];
const failures = [];
const ok = (cond, what) => check(failures, cond, what);
const args = process.argv.slice(2);
const only = args.includes('--only') ? new Set(args[args.indexOf('--only') + 1].split(',')) : null;

function gtaNetworks() {
  const out = [];
  for (const id of GTA) {
    if (only && !only.has(id)) continue;
    for (const tl of ['day', 'week']) {
      const real = `build/${id}/${tl}/network.json.gz`;
      const stub = `build/stub_v4/${id}/${tl}/network.json.gz`;
      const file = fs.existsSync(path.join(ROOT, real)) ? real : fs.existsSync(path.join(ROOT, stub)) ? stub : null;
      if (!file) continue;
      const bm = fs.existsSync(path.join(ROOT, `build/${id}/basemap.json.gz`)) ? `build/${id}/basemap.json.gz` : 'data/gta/built/basemap.json.gz';
      const variants = tl === 'day' ? ['day', 'rush'] : ['week'];
      out.push({ id, tl, query: `data=../${file}&basemap=../${bm}`, variants, stub: file === stub });
    }
  }
  return out;
}

// Everything is measured in the page: the boxes, the counts over the frames,
// the vehicle flags against the polygon and the brand distances.
function inPage() {
  const bm = window.busmap;
  const C = window.BusmapColor;
  const meta = bm.meta;
  const V = meta.variants[bm.variant];
  const P = meta.timeline.period;
  const out = { stills: {}, frames: {}, errors: [], safe: bm.safe };
  for (const [k, T] of Object.entries(bm.stillTimes())) {
    bm.renderAt(T);
    out.stills[k] = bm.hudBoxes();
  }
  for (const i of [0, 15]) {
    bm.renderFrame(i);
    out.frames[i] = bm.hudBoxes();
  }
  let pt = V.peak.time;
  while (pt < bm.window.start) pt += P;
  bm.renderAt(pt);
  out.peakT = pt;
  out.peakCount = V.peak.count;
  out.atPeak = bm.countAt(pt);
  out.countText = (bm.hudBoxes().find((b) => b.name === 'count') || {}).text;
  out.count2 = bm.hudBoxes().some((b) => b.name === 'count2');
  out.place = meta.place || '';
  // B9: one mode's own noun only when no other mode has half a vehicle
  // inside in any minute of [start, end), not just at the peak.
  const present = (meta.modes || []).filter((md) => {
    const a = (meta.hist_by_mode || {})[md.id] || [];
    for (let m = Math.ceil(V.start / 60); m < Math.ceil(V.end / 60) && a.length; m++) {
      if ((a[((m % a.length) + a.length) % a.length] || 0) >= 0.5) return true;
    }
    return false;
  });
  const one = V.peak.count === 1;
  out.wantNoun = present.length === 1 ? (one ? present[0].singular : present[0].label) : (one ? 'vehicle' : 'vehicles');
  out.maxCount = 0;
  out.badSum = [];
  for (let i = 0; i < bm.totalFrames; i += 5) {
    const T = bm.timeAtProgress(i / bm.totalFrames);
    const c = bm.countAt(T);
    out.maxCount = Math.max(out.maxCount, c.total);
    const s = Object.values(c.byGroup).reduce((a, b) => a + b, 0);
    if (Object.keys(c.byGroup).length && s !== c.total) out.badSum.push([i, c.total, s]);
  }
  // Vehicle flags against the polygon, at the three stills safe_share uses.
  const cfg = bm.config;
  const S = 1920 / cfg.KM_VERTICAL;
  const OX = 540 - cfg.CENTER_KM[0] * S, OY = 960 + cfg.CENTER_KM[1] * S;
  const rings = meta.boundary ? meta.boundary.rings.concat(meta.boundary.holes || []) : [];
  const inside = (x, y) => {
    let c = false;
    for (const r of rings) {
      for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
        const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
      }
    }
    return c;
  };
  const edge = (x, y) => {
    let d = Infinity;
    for (const r of rings) {
      for (let i = 0; i + 3 < r.length; i += 2) {
        const ax = r[i], ay = r[i + 1], bx = r[i + 2], by = r[i + 3];
        const dx = bx - ax, dy = by - ay;
        const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy || 1)));
        d = Math.min(d, Math.hypot(x - ax - t * dx, y - ay - t * dy));
      }
    }
    return d;
  };
  out.vehicles = 0;
  out.flagMismatch = [];
  out.nearEdge = 0;
  const st = bm.stillTimes();
  for (const k of Object.keys(st).slice(0, 3)) {
    bm.renderAt(st[k]);
    const v = bm.lastVehicles;
    // lastVehicles are where the dots are on screen; a still draws the camera
    // (B18) at the phase of its time, so undo it before the projection.
    const cm = bm.cameraAt(bm.progressAt(st[k]));
    for (let i = 0; i < v.length; i += 3) {
      const bx = (v[i] - cm.e) / cm.zoom, by = (v[i + 1] - cm.f) / cm.zoom;
      const x = (bx - OX) / S, y = (OY - by) / S;
      out.vehicles++;
      const want = rings.length ? (inside(x, y) ? 1 : 0) : 1;
      if (want !== v[i + 2]) {
        if (edge(x, y) <= 0.025) out.nearEdge++;
        else if (out.flagMismatch.length < 5) out.flagMismatch.push([k, x.toFixed(3), y.toFixed(3), v[i + 2], want]);
      }
    }
  }
  // B9 and B6: a group the chips line folds into "other" is fitted only
  // after 24 px fails, and none of its brands keeps a colour of its own.
  out.chips = bm.chips;
  out.foldedPlaced = [];
  if (out.chips && out.chips.merged.length) {
    const folded = new Set(out.chips.merged);
    (meta.brands || []).forEach((b, i) => {
      const g = b.kind === 'gtfs' || b.kind === 'mode' ? b.id : b.entry || b.id;
      const e = bm.brandMap[i];
      if (folded.has(g) && (e.placed || e.how !== 'foreign')) out.foldedPlaced.push(`${e.id}=${e.trail}(${e.how})`);
    });
  }
  const placed = (bm.brandMap || []).filter((b) => b.placed);
  out.placed = placed.map((b) => `${b.id}=${b.trail}(${b.how})`);
  out.closest = Infinity;
  out.closestPair = '';
  for (let i = 0; i < placed.length; i++) {
    for (let j = i + 1; j < placed.length; j++) {
      const d = C.deltaE(placed[i].trail, placed[j].trail);
      if (d < out.closest) { out.closest = d; out.closestPair = `${placed[i].id}/${placed[j].id}`; }
    }
  }
  out.minDE = cfg.BRAND_MIN_DE;
  return out;
}

const SAFE = { x0: 120, y0: 290, x1: 800, y1: 1440 };
function checkBoxes(tag, where, boxes) {
  for (const b of boxes) {
    ok(b.x0 >= SAFE.x0 - 0.5 && b.x1 <= SAFE.x1 + 0.5 && b.y0 >= SAFE.y0 - 0.5 && b.y1 <= SAFE.y1 + 0.5,
      `${tag} ${where}: ${b.name} "${b.text}" at x ${b.x0.toFixed(0)}..${b.x1.toFixed(0)}, y ${b.y0.toFixed(0)}..${b.y1.toFixed(0)}`);
    if (MIN[b.name]) ok(b.size >= MIN[b.name], `${tag} ${where}: ${b.name} at ${b.size} px < ${MIN[b.name]}`);
  }
}

// Two rows never share pixels: every box drawn at all against every box of
// another row (the axis labels and a chips line's parts are one row each).
function checkOverlap(tag, where, boxes) {
  const shown = boxes.filter((b) => b.alpha > 0);
  for (let i = 0; i < shown.length; i++) {
    for (let j = i + 1; j < shown.length; j++) {
      const a = shown[i], b = shown[j];
      if (a.name === b.name) continue;
      ok(!(a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1),
        `${tag} ${where}: ${a.name} "${a.text}" y ${a.y0.toFixed(1)}..${a.y1.toFixed(1)} overlaps ${b.name} "${b.text}" y ${b.y0.toFixed(1)}..${b.y1.toFixed(1)}`);
    }
  }
}

async function run(h, name, query, variant) {
  const tag = `${name} ${variant}`;
  const page = await h.open(`${query}&variant=${variant}`, { allowErrors: true });
  if (page.failed) {
    failures.push(`${tag}: ${page.failed}`);
    await page.context().close();
    return null;
  }
  const r = await page.evaluate(inPage);
  ok(JSON.stringify(r.safe) === JSON.stringify(SAFE), `${tag}: busmap.safe ${JSON.stringify(r.safe)}`);
  for (const [k, boxes] of Object.entries(r.stills)) {
    checkBoxes(tag, `still ${k}`, boxes);
    checkOverlap(tag, `still ${k}`, boxes);
  }
  for (const [i, boxes] of Object.entries(r.frames)) {
    checkBoxes(tag, `frame ${i}`, boxes);
    checkOverlap(tag, `frame ${i}`, boxes);
  }
  for (const pair of [['card_line0', 'card_line0b'], ['card_line1', 'card_line1b']]) {
    const lines = r.frames[0].filter((b) => pair.includes(b.name)).map((b) => b.text);
    if (query.includes('card=1')) ok(r.frames[0].some((b) => b.name === 'card_title'), `${tag}: no card on frame 0 with card=1`);
    ok(!lines.join(' ').includes(r.place) || lines.some((l) => l.includes(r.place)),
      `${tag} B-10: the card splits "${r.place}": ${JSON.stringify(lines)}`);
  }
  if (r.chips) {
    // Every width tried but the last is too wide, the last fits, and each
    // fold comes right after a 24 px try.
    const t = r.chips.tried, W = r.chips.width;
    const order = t.every((x, k) => (k === t.length - 1 ? x.w <= W + 1e-6 || x.n <= 2 : x.w > W)
      && (k + 1 >= t.length || t[k + 1].n === x.n || (x.size === 24 && x.gap === 8)));
    ok(order && t[t.length - 1].size === r.chips.size && t[t.length - 1].gap === r.chips.gap, `${tag} B-9: chip fit order ${JSON.stringify(t)} for ${W} px`);
    ok(!r.foldedPlaced.length, `${tag} B-6: brands of folded groups keep their colour: ${r.foldedPlaced.join(', ')}`);
  }
  ok(r.atPeak.total === r.peakCount, `${tag} B-6: count ${r.atPeak.total} at the peak ${r.peakT}, V.peak.count ${r.peakCount}`);
  ok(r.countText && r.countText.replace(/,/g, '').startsWith(String(r.peakCount)), `${tag} B-6: count line "${r.countText}" at the peak`);
  ok(r.countText && r.countText.split(' ')[1] === r.wantNoun, `${tag} B-9: count line "${r.countText}", want the noun "${r.wantNoun}"`);
  ok(r.maxCount <= r.peakCount, `${tag} B-6: count reaches ${r.maxCount} > peak ${r.peakCount}`);
  ok(!r.badSum.length, `${tag} B-6: chips do not add up at frames ${JSON.stringify(r.badSum.slice(0, 3))}`);
  ok(!r.flagMismatch.length, `${tag} B-6: lastVehicles flags differ from the polygon away from the edge: ${JSON.stringify(r.flagMismatch)}`);
  ok(r.closest >= r.minDE - 1e-9 || r.placed.length < 2, `${tag} B-10: ${r.closestPair} only ${r.closest.toFixed(3)} apart`);
  if (page.errors.length) failures.push(`${tag}: page errors ${page.errors.join(' | ')}`);
  const sizes = Object.fromEntries(r.stills.am.filter((b) => ['count', 'chips', 'title', 'weekday'].includes(b.name)).map((b) => [b.name, b.size]));
  const card = r.frames[0].filter((b) => b.name.startsWith('card_title')).map((b) => `${b.text}@${b.size}`).join(' / ');
  const folded = r.chips && r.chips.merged.length ? `  folded ${r.chips.merged.join(',')}` : '';
  console.log(`  ${tag.padEnd(24)} peak ${String(r.peakCount).padStart(5)}  sizes ${JSON.stringify(sizes)}${r.count2 ? ' split' : ''}  `
    + `card ${card}  vehicles ${r.vehicles} (${r.nearEdge} edge)  closest ${r.placed.length > 1 ? `${r.closestPair} ${r.closest.toFixed(3)}` : '-'}${folded}`);
  await page.context().close();
  return r;
}

function roundHalfEven(x) {
  const r = Math.round(x);
  return Math.abs(x - Math.trunc(x)) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

// A copy of a network with every count multiplied, peaks recomputed from
// the scaled 2-decimal hist as the builder would write them.
function stress(net, factor, name) {
  const file = path.join(ROOT, net.query.match(/data=\.\.\/([^&]+)/)[1]);
  const obj = JSON.parse(zlib.gunzipSync(fs.readFileSync(file)).toString('utf8'));
  const m = obj.meta;
  const sc = (arr) => arr.map((v) => Math.round(v * factor * 100) / 100);
  obj.hist = sc(obj.hist);
  for (const k of Object.keys(m.hist_by_mode)) m.hist_by_mode[k] = sc(m.hist_by_mode[k]);
  for (const k of Object.keys(m.hist_by_group)) m.hist_by_group[k] = sc(m.hist_by_group[k]);
  const at = (t) => roundHalfEven(obj.hist[(t / 60) % obj.hist.length]);
  m.am_peak.count = at(m.am_peak.time);
  m.pm_peak.count = at(m.pm_peak.time);
  for (const v of Object.values(m.variants)) v.peak.count = at(v.peak.time);
  const out = path.join(ROOT, 'build', 'test_web', `${name}.json`);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(obj));
  return net.query.replace(/data=[^&]+/, `data=../build/test_web/${name}.json`);
}

// A copy of a fixture network under another place name, for the long-name checks.
function renamed(query, place) {
  const file = path.join(ROOT, query.match(/data=\.\.\/([^&]+)/)[1]);
  const obj = JSON.parse(fs.readFileSync(file, 'utf8'));
  obj.meta.place = place;
  obj.meta.title = place.toUpperCase();
  if (obj.meta.card) obj.meta.card.title = place.toUpperCase();
  const name = `name-${place.toLowerCase().replace(/ /g, '-')}-${path.basename(file, '.json')}`;
  const out = path.join(ROOT, 'build', 'test_web', `${name}.json`);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(obj));
  return query.replace(/data=[^&]+/, `data=../build/test_web/${name}.json`);
}

// A copy of a fixture network with another credit line.
function credited(query, credit, name) {
  const file = path.join(ROOT, query.match(/data=\.\.\/([^&]+)/)[1]);
  const obj = JSON.parse(fs.readFileSync(file, 'utf8'));
  obj.meta.credit = credit;
  obj.meta.attribution = [credit];
  const out = path.join(ROOT, 'build', 'test_web', `credit-${name}.json`);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(obj));
  return query.replace(/data=[^&]+/, `data=../build/test_web/credit-${name}.json`);
}

// B9: the credits the trim writes with cities/defaults.json's template and
// author, on both panel sides, in the panel's column at the credit's own size:
// on one line when it fits there, else the data on the first line and the map
// and the author on the second. Markham and Brampton keep their agencies;
// Toronto and Mississauga (seven) and a two-digit count take the fallback;
// Chicago's buses (one short agency) is the one-line case.
const MADE_BY = 'Map: Overture, OSM · Made by SOtownships';
// [name, data line, on one line]: at 18.7 px "Data: CTA · " and the rest is
// 494 px of the 504 px column, "Data: YRT, TTC, GO · " and the rest 578.
const CREDITS = [['markham', 'Data: YRT, TTC, GO', false], ['brampton', 'Data: Brampton, GO, MiWay, YRT, Milton', false],
  ['seven', 'Data: 7 transit agencies', false], ['twelve', 'Data: 12 transit agencies', false], ['cta', 'Data: CTA', true]];
const LONG_NAMES = ['Mississauga', 'Richmond Hill', 'Philadelphia', 'San Antonio', 'San Francisco'];
const h = await openBrowser();
try {
  for (const extra of ['', `&${FULL}`, '&textscale=0.6', `&textscale=0.6&${FULL}`]) {
    const tag = `v4_tiny${extra.includes('card') ? ' full' : ''}${extra.includes('textscale') ? ' 0.6' : ''}`;
    await run(h, tag, `${TINY}${extra}`, 'day');
    await run(h, tag, `${TINY}${extra}`, 'rush');
    await run(h, tag, `${TINY_WEEK}${extra}`, 'week');
  }
  for (const side of ['left', 'right']) {
    const q = `${credited(TINY, `Data: YRT, TTC, GO · ${MADE_BY}`, 'markham')}&panelside=${side}&textscale=0.6`;
    await run(h, `credit markham ${side} 0.6`, q, 'day');
    await run(h, `credit markham ${side} 0.6 full`, `${q}&${FULL}`, 'rush');
  }
  for (const [name, data, oneLine] of CREDITS) {
    for (const side of ['left', 'right']) {
      const q = `${credited(TINY, `${data} · ${MADE_BY}`, name)}&panelside=${side}`;
      const r = await run(h, `credit ${name} ${side}`, q, 'day');
      if (!r) continue;
      const want = oneLine ? [`${data} · ${MADE_BY}`] : [data, MADE_BY];
      for (const [k, boxes] of Object.entries(r.stills)) {
        const credit = boxes.filter((b) => b.name === 'credit' || b.name === 'credit2');
        const lines = credit.map((b) => b.text);
        ok(JSON.stringify(lines) === JSON.stringify(want), `credit ${name} ${side} still ${k} B-9: lines ${JSON.stringify(lines)}`);
        // The panel's text column: 28 px into the panel on SAFE's left or right side, 504 px wide.
        const x = side === 'right' ? SAFE.x1 - 560 + 28 : SAFE.x0 + 28;
        ok(credit.every((b) => b.size === credit[0].size && b.size >= MIN.credit && b.x1 <= x + 504 + 0.5),
          `credit ${name} ${side} still ${k}: ${credit.map((b) => `${b.size} px to x ${b.x1.toFixed(0)}`).join(', ')}, column ends at ${x + 504}`);
        ok(credit.every((b) => Math.abs(b.x0 - x) < 1), `credit ${name} ${side} still ${k}: x0 ${credit.map((b) => b.x0.toFixed(1))}, want ${x}`);
      }
    }
  }
  for (const place of LONG_NAMES) {
    for (const extra of ['', `&${FULL}`, `&textscale=0.6&${FULL}`]) {
      const tag = `${place}${extra.includes('card') ? ' full' : ''}${extra.includes('textscale') ? ' 0.6' : ''}`;
      await run(h, tag, `${renamed(TINY, place)}${extra}`, 'day');
      await run(h, tag, `${renamed(TINY, place)}${extra}`, 'rush');
      await run(h, tag, `${renamed(TINY_WEEK, place)}${extra}`, 'week');
    }
  }
  if (!args.includes('--no-gta')) {
    const nets = gtaNetworks();
    if (!nets.length) console.log('SKIP GTA: no build/<id>/ networks and no stubs (python3 -I tests/web/stub_gta_v4.py)');
    if (nets.some((n) => n.stub)) console.log('  (GTA networks from the stubs in build/stub_v4/)');
    for (const n of nets) {
      for (const v of n.variants) {
        await run(h, n.id, n.query, v);
        await run(h, `${n.id} full`, `${n.query}&${FULL}`, v);
      }
    }
    for (const [id, factor] of [['gta-richmond-hill', 200], ['gta-mississauga', 12], ['gta-toronto', 8]]) {
      const n = nets.find((x) => x.id === id && x.tl === 'day');
      if (!n) continue;
      const q = stress(n, factor, `stress-${id}`);
      for (const v of n.variants) {
        await run(h, `${id} x${factor}`, q, v);
        await run(h, `${id} x${factor} full`, `${q}&${FULL}`, v);
      }
    }
  }
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 hud: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

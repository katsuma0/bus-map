#!/usr/bin/env node
// B-5, B-6 and B-10 on the v4 fixture and, when they exist, the GTA networks:
// build/<id>/{day,week}/network.json.gz from make.py build, else the stubs of
// tests/web/stub_gta_v4.py in build/stub_v4/ (legacy GTA trips, so the
// numbers are one date's, not the composite's; the layout, the names, the
// brands and the sizes are the real ones).
//
//  B-5  every hudBoxes() box inside x 60..880, y 240..1500 and at or above its
//       minimum size, on every stillTimes() still and on frames 0 and 15
//       (card), and no console.error from the page's own layout asserts
//  B-6  at V.peak.time the count line equals V.peak.count (and B9: its noun
//       names a mode only when it is the one mode inside over the window); over every 5th
//       frame it never exceeds it; the chips add up to it; lastVehicles flags
//       equal point-in-polygon on meta.boundary except within 25 m of its edge
//  B-10 every pair of placed brands at least BRAND_MIN_DE (0.08) apart, and
//       no card line pair splits the place name
//
// Stress copies scale every count of a GTA network (Richmond Hill x200 for a
// five-digit count that has to split, Mississauga x12 and Toronto x8 for the
// widest chips lines) and run the same checks.
//
// Usage: node tests/web/v4_hud.mjs [--only gta-toronto,...] [--no-gta]

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { openBrowser, ROOT, TINY, TINY_WEEK, check } from './browser.mjs';

const MIN = {
  title: 48, subtitle: 32, weekday: 64, clock: 40, count: 36, count2: 36, chips: 24, peak: 26, axis: 26, credit: 22, credit2: 22,
  card_title: 72, card_title2: 72, card_line0: 40, card_line0b: 40, card_line1: 30, card_line1b: 30,
};
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
  const out = { stills: {}, frames: {}, errors: [] };
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
    for (let i = 0; i < v.length; i += 3) {
      const x = (v[i] - OX) / S, y = (OY - v[i + 1]) / S;
      out.vehicles++;
      const want = rings.length ? (inside(x, y) ? 1 : 0) : 1;
      if (want !== v[i + 2]) {
        if (edge(x, y) <= 0.025) out.nearEdge++;
        else if (out.flagMismatch.length < 5) out.flagMismatch.push([k, x.toFixed(3), y.toFixed(3), v[i + 2], want]);
      }
    }
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

function checkBoxes(tag, where, boxes) {
  for (const b of boxes) {
    ok(b.x0 >= 59.5 && b.x1 <= 880.5 && b.y0 >= 239.5 && b.y1 <= 1500.5,
      `${tag} ${where}: ${b.name} "${b.text}" at x ${b.x0.toFixed(0)}..${b.x1.toFixed(0)}, y ${b.y0.toFixed(0)}..${b.y1.toFixed(0)}`);
    if (MIN[b.name]) ok(b.size >= MIN[b.name], `${tag} ${where}: ${b.name} at ${b.size} px < ${MIN[b.name]}`);
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
  for (const [k, boxes] of Object.entries(r.stills)) checkBoxes(tag, `still ${k}`, boxes);
  for (const [i, boxes] of Object.entries(r.frames)) checkBoxes(tag, `frame ${i}`, boxes);
  for (const pair of [['card_line0', 'card_line0b'], ['card_line1', 'card_line1b']]) {
    const lines = r.frames[0].filter((b) => pair.includes(b.name)).map((b) => b.text);
    ok(!lines.join(' ').includes(r.place) || lines.some((l) => l.includes(r.place)),
      `${tag} B-10: the card splits "${r.place}": ${JSON.stringify(lines)}`);
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
  console.log(`  ${tag.padEnd(24)} peak ${String(r.peakCount).padStart(5)}  sizes ${JSON.stringify(sizes)}${r.count2 ? ' split' : ''}  `
    + `card ${card}  vehicles ${r.vehicles} (${r.nearEdge} edge)  closest ${r.placed.length > 1 ? `${r.closestPair} ${r.closest.toFixed(3)}` : '-'}`);
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

const h = await openBrowser();
try {
  await run(h, 'v4_tiny', TINY, 'day');
  await run(h, 'v4_tiny', TINY, 'rush');
  await run(h, 'v4_tiny', TINY_WEEK, 'week');
  if (!args.includes('--no-gta')) {
    const nets = gtaNetworks();
    if (!nets.length) console.log('SKIP GTA: no build/<id>/ networks and no stubs (python3 -I tests/web/stub_gta_v4.py)');
    if (nets.some((n) => n.stub)) console.log('  (GTA networks from the stubs in build/stub_v4/)');
    for (const n of nets) for (const v of n.variants) await run(h, n.id, n.query, v);
    for (const [id, factor] of [['gta-richmond-hill', 200], ['gta-mississauga', 12], ['gta-toronto', 8]]) {
      const n = nets.find((x) => x.id === id && x.tl === 'day');
      if (!n) continue;
      const q = stress(n, factor, `stress-${id}`);
      for (const v of n.variants) await run(h, `${id} x${factor}`, q, v);
    }
  }
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 hud: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

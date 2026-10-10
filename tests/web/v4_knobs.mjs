#!/usr/bin/env node
// B-7: every knob of spec 2.10 and G1 changes the frame when moved and leaves
// it unchanged when given its effective value (the "v" of G1: the default
// after the profile, the preset and the variant block), the camera knobs of
// B18 included.
//
// Each knob is read on the frame where it shows: map and HUD knobs on the
// am-peak still, the sparkline knobs after the peak, the warp knobs at a
// frame in the middle of the video, the card knobs on frame 0 and the loop
// on frame N - 10. colorby runs on a copy of the fixture without
// meta.color_by, the only file where moving it can change anything.
// trailstep, trailmode and osm are not listed: brand colouring always draws
// ribbon trails, and the fixture basemap has no OSM routes.
//
// Usage: node tests/web/v4_knobs.mjs [--only q1,q2]

import fs from 'node:fs';
import path from 'node:path';
import { openBrowser, ROOT, TINY, check } from './browser.mjs';

const failures = [];
const ok = (cond, what) => check(failures, cond, what);
const STILL = { kind: 'still', at: 'am' };
const LATE = { kind: 'still', at: 'pm' };
const MID = { kind: 'frame', at: 700 };
const F0 = { kind: 'frame', at: 0 };
const END = { kind: 'frame', at: -10 };
// [query, CONFIG key, moved value, where to look]
const KNOBS = [
  ['layout', 'HUD_LAYOUT', 'panel', STILL], ['theme', 'THEME', 'ink', STILL], ['variant', 'VARIANT', 'rush', STILL],
  ['colorby', 'COLOR_BY', 'brand', STILL, 'nobrand'],
  ['warp', 'TIME_WARP_MODE', 'linear', MID], ['warpgamma', 'TIME_WARP_GAMMA', 2, MID], ['warpfloor', 'TIME_WARP_FLOOR', 0.4, MID],
  ['loop', 'LOOP', 'none', END], ['clockround', 'CLOCK_ROUND', 60, STILL],
  ['card', 'CARD', 0, F0], ['cardscrim', 'CARD_SCRIM', 0.6, F0], ['cardband', 'CARD_BAND', 0.4, F0], ['cardy', 'CARD_CENTER_Y', 760, F0],
  ['cardsize', 'CARD_TITLE_MAX', 96, F0], ['cardline', 'CARD_LINES', 1, F0],
  ['peak', 'PEAK_MARKER', 0, LATE], ['chips', 'MODE_CHIPS', 0, STILL], ['outside', 'OUTSIDE_DIM', 0, STILL],
  ['panelalpha', 'PANEL_ALPHA', 0.5, STILL], ['panelside', 'PANEL_SIDE', 'right', STILL], ['fonts', 'FONT_SET', 'classic', STILL],
  ['zoom', 'FRAME_ZOOM', 1.1, STILL], ['cx', 'FRAME_DX_KM', 0.4, STILL], ['cy', 'FRAME_DY_KM', 0.4, STILL],
  ['roads', 'BASE_ROADS_GAIN', 1.35, STILL], ['water', 'BASE_WATER_GAIN', 1.35, STILL],
  ['dotcore', 'BUS_CORE_R', 1.8, STILL], ['halor', 'BUS_HALO_R', 8, STILL], ['haloalpha', 'BUS_HALO_ALPHA', 0.6, STILL],
  ['layeralpha', 'TRAIL_LAYER_ALPHA', 0.6, STILL], ['smooth', 'SPARK_SMOOTH_MIN', 5, LATE],
  ['trailmin', 'TRAIL_MINUTES', 12, STILL], ['corew', 'TRAIL_CORE_W', 9, STILL], ['shoulderw', 'TRAIL_SHOULDER_W', 15, STILL],
  ['shoulder', 'TRAIL_SHOULDER_ALPHA', 0.3, STILL], ['trailalpha', 'TRAIL_ALPHA', 0.4, STILL], ['routealpha', 'ROUTE_ALPHA', 0.3, STILL],
  ['trailscale', 'TRAIL_SCALE', 0.5, STILL], ['trailbands', 'TRAIL_BANDS', 6, STILL], ['shoulderbands', 'TRAIL_SHOULDER_BANDS', 2, STILL],
  ['simplify', 'TRAIL_SIMPLIFY_PX', 12, STILL],
  // The camera (B18) mid loop, where the zoom and the drift both show, on a
  // frame that leaves the city line room for the whole move: on the fixture's
  // own frame the line's cap binds, and a smaller amplitude or speed below it
  // changes nothing.
  ['camera', 'CAMERA', false, MID, 'framed'], ['campath', 'CAMERA_PATH', 'drift-orbit', MID, 'framed'],
  ['camzoom', 'CAMERA_ZOOM', 0.05, MID, 'framed'], ['camdrift', 'CAMERA_DRIFT', 0.015, MID, 'framed'],
  ['camamp', 'CAMERA_AMP', 0.5, MID, 'framed'], ['camspeed', 'CAMERA_MAX_SPEED', 0.003, MID, 'framed'],
  ['cambase', 'CAMERA_BASE', 'vector', MID, 'framed'],
];

async function hashOf(h, query, where) {
  const page = await h.open(query);
  const r = await page.evaluate(([w]) => {
    const bm = window.busmap;
    if (w.kind === 'still') bm.renderAt(bm.stillTimes()[w.at]);
    else bm.renderFrame(w.at < 0 ? bm.totalFrames + w.at : w.at);
    const d = bm.canvas.getContext('2d').getImageData(0, 0, bm.canvas.width, bm.canvas.height).data;
    let x = 2166136261;
    for (let k = 0; k < d.length; k++) x = Math.imul(x ^ d[k], 16777619);
    return { hash: x >>> 0, config: bm.config };
  }, [where]);
  const errors = page.errors.slice();
  await page.context().close();
  return { ...r, errors };
}

function queryValue(v) {
  if (typeof v === 'boolean') return v ? '1' : '0';
  return encodeURIComponent(String(v));
}

const only = (() => {
  const k = process.argv.indexOf('--only');
  return k >= 0 ? new Set(process.argv[k + 1].split(',')) : null;
})();

// The fixture without meta.color_by, so that colorby has something to switch.
const nobrandPath = path.join(ROOT, 'build', 'test_web', 'tiny_nobrand.json');
fs.mkdirSync(path.dirname(nobrandPath), { recursive: true });
const fixture = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'v4_tiny', 'network.json'), 'utf8'));
delete fixture.meta.color_by;
fs.writeFileSync(nobrandPath, JSON.stringify(fixture));
const BASES = {
  tiny: TINY,
  framed: `${TINY}&zoom=0.8`,
  nobrand: 'data=../build/test_web/tiny_nobrand.json&basemap=../tests/fixtures/v4_tiny/basemap.json&colorby=',
};

const h = await openBrowser();
let n = 0;
try {
  const base = {};
  for (const [q, key, moved, where, fixtureName = 'tiny'] of KNOBS) {
    if (only && !only.has(q)) continue;
    n++;
    const bq = BASES[fixtureName];
    const bkey = `${fixtureName}/${where.kind}/${where.at}`;
    if (!base[bkey]) base[bkey] = await hashOf(h, bq, where);
    const b = base[bkey];
    // A knob that the base query already pins is replaced, not repeated.
    const strip = (s) => s.split('&').filter((p) => !p.startsWith(`${q}=`)).join('&');
    const v = b.config[key];
    const same = await hashOf(h, `${strip(bq)}&${q}=${queryValue(v)}`, where);
    const diff = await hashOf(h, `${strip(bq)}&${q}=${queryValue(moved)}`, where);
    ok(same.hash === b.hash, `${q} (${key}) at its value ${JSON.stringify(v)} changes the frame`);
    ok(diff.hash !== b.hash, `${q} (${key}) moved from ${JSON.stringify(v)} to ${JSON.stringify(moved)} leaves the frame unchanged`);
    for (const r of [b, same, diff]) if (r.errors.length) failures.push(`${q}: page errors ${r.errors.join('; ')}`);
    console.log(`  ${q.padEnd(13)} ${key.padEnd(22)} v ${JSON.stringify(v).padEnd(10)} moved ${JSON.stringify(moved).padEnd(8)} `
      + `${same.hash === b.hash && diff.hash !== b.hash ? 'ok' : 'FAIL'}`);
  }
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 knobs: ${n} knobs, ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

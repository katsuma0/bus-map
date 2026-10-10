#!/usr/bin/env node
// The busmap API of spec 2.11 on the v4 fixture (tests/fixtures/v4_tiny/), for
// the day, rush and week variants, plus the startup errors of B14 and the
// inert members on a legacy file.
//
// With --via-render it also renders the fixture through
// scripts/render_video.mjs --tier stills (part C) and checks the PNGs equal
// the canvas pixels of renderAt and renderFrame here; the step is skipped
// while that script has no --tier.
//
// Usage: node tests/web/v4_smoke.mjs [--via-render]

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { openBrowser, ROOT, TINY, TINY_WEEK, check } from './browser.mjs';

const NAMES = new Set(['title', 'subtitle', 'weekday', 'clock', 'count', 'count2', 'chips', 'axis', 'credit', 'credit2',
  'peak', 'card_title', 'card_title2', 'card_line0', 'card_line0b', 'card_line1', 'card_line1b']);
const failures = [];
const ok = (cond, what) => check(failures, cond, what);

async function api(h, query, variant, keys) {
  const page = await h.open(query);
  const r = await page.evaluate(() => {
    const bm = window.busmap;
    const st = bm.stillTimes();
    const am = st.am;
    const T = bm.renderAt(am);
    const boxes = bm.hudBoxes();
    const veh = bm.lastVehicles;
    const count = bm.countAt(am);
    const g = bm.canvas.getContext('2d');
    const sum = (x0, y0, x1, y1) => {
      const d = g.getImageData(x0, y0, x1 - x0, y1 - y0).data;
      let s = 0;
      for (let k = 0; k < d.length; k += 4) s += d[k] + d[k + 1] + d[k + 2];
      return s;
    };
    const clock = boxes.find((b) => b.name === 'clock');
    const full = sum(Math.floor(clock.x0), Math.floor(clock.y0), Math.ceil(clock.x1), Math.ceil(clock.y1));
    bm.setHud('notext');
    bm.renderAt(am);
    const notext = sum(Math.floor(clock.x0), Math.floor(clock.y0), Math.ceil(clock.x1), Math.ceil(clock.y1));
    bm.setHud('none');
    bm.renderAt(am);
    const none = sum(Math.floor(clock.x0), Math.floor(clock.y0), Math.ceil(clock.x1), Math.ceil(clock.y1));
    bm.setHud('full');
    const a0 = bm.cardAlpha(0);
    bm.setCard(false);
    const a0off = bm.cardAlpha(0);
    bm.setCard(true);
    const T5 = bm.renderFrame(5);
    return {
      variant: bm.variant, window: bm.window, safe: bm.safe, totalFrames: bm.totalFrames, st, T, T5,
      frameTime5: bm.frameTime(5), boxes, veh: Array.from(veh), isF32: veh instanceof Float32Array, count, full, notext, none,
      a0, a0off, brandMap: bm.brandMap, peak: bm.meta.variants[bm.variant].peak,
      countText: (boxes.find((b) => b.name === 'count') || {}).text,
    };
  });
  const tag = variant;
  ok(r.variant === variant, `${tag}: busmap.variant is ${r.variant}`);
  ok(r.window && r.window.start < r.window.end, `${tag}: busmap.window ${JSON.stringify(r.window)}`);
  ok(JSON.stringify(r.safe) === JSON.stringify({ x0: 60, y0: 240, x1: 880, y1: 1500 }), `${tag}: busmap.safe ${JSON.stringify(r.safe)}`);
  ok(JSON.stringify(Object.keys(r.st)) === JSON.stringify(keys), `${tag}: stillTimes keys ${Object.keys(r.st)}`);
  for (const [k, t] of Object.entries(r.st)) ok(t >= r.window.start && t < r.window.end, `${tag}: stillTimes.${k} ${t} outside the window`);
  ok(typeof r.T === 'number' && r.T === r.count.total, `${tag}: renderAt returns the count line's number (${r.T}, ${r.count.total})`);
  ok(r.T5 === r.frameTime5, `${tag}: renderFrame(5) returns its time (${r.T5} vs frameTime ${r.frameTime5})`);
  ok(r.boxes.length > 5, `${tag}: hudBoxes has ${r.boxes.length} boxes`);
  for (const b of r.boxes) {
    ok(NAMES.has(b.name), `${tag}: unexpected box name ${b.name}`);
    ok(['x0', 'y0', 'x1', 'y1', 'size'].every((k) => Number.isFinite(b[k])) && b.x1 > b.x0 && b.y1 > b.y0
      && typeof b.color === 'string' && typeof b.font === 'string', `${tag}: box ${b.name} malformed ${JSON.stringify(b)}`);
  }
  ok(r.isF32 && r.veh.length % 3 === 0 && r.veh.length > 0, `${tag}: lastVehicles is a Float32Array of triples (${r.veh.length})`);
  ok(r.veh.every((v, k) => k % 3 !== 2 || v === 0 || v === 1), `${tag}: lastVehicles flags are 0 or 1`);
  const chipSum = Object.values(r.count.byGroup).reduce((a, b) => a + b, 0);
  ok(chipSum === r.count.total, `${tag}: countAt byGroup adds up to ${chipSum}, total ${r.count.total}`);
  ok(r.countText && r.countText.startsWith(String(r.count.total)), `${tag}: count line "${r.countText}" vs countAt ${r.count.total}`);
  ok(r.full > r.notext, `${tag}: notext leaves the clock box darker (${r.full} vs ${r.notext})`);
  ok(r.notext !== r.none, `${tag}: notext draws the panel backdrop, none does not (${r.notext} vs ${r.none})`);
  ok(r.a0 === 1 && r.a0off === 0, `${tag}: cardAlpha(0) ${r.a0}, after setCard(false) ${r.a0off}`);
  ok(Array.isArray(r.brandMap) && r.brandMap.length === 2 && r.brandMap.every((e) => e.placed
    && /^[0-9a-f]{6}$/.test(e.trail) && /^[0-9a-f]{6}$/.test(e.line) && typeof e.how === 'string'), `${tag}: brandMap ${JSON.stringify(r.brandMap)}`);
  if (page.errors.length) failures.push(`${tag}: page errors ${page.errors.join('; ')}`);
  await page.context().close();
  return r;
}

async function viaRender(h) {
  const help = execFileSync('node', [path.join(ROOT, 'scripts', 'render_video.mjs'), '--help'], { encoding: 'utf8' });
  if (!help.includes('--tier')) {
    console.log('SKIP --via-render: scripts/render_video.mjs has no --tier yet (part C)');
    return;
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'v4smoke-'));
  execFileSync('node', [path.join(ROOT, 'scripts', 'render_video.mjs'), '--tier', 'stills',
    '--data', 'tests/fixtures/v4_tiny/network.json', '--basemap', 'tests/fixtures/v4_tiny/basemap.json',
    '--times', '7:56', '--frames', '0,last', '--out-dir', dir, '--name', 'tiny'], { stdio: 'inherit' });
  const pngs = fs.readdirSync(dir).filter((f) => f.endsWith('.png'));
  ok(pngs.length === 3, `--tier stills wrote ${pngs.join(', ')}`);
  const page = await h.open(TINY);
  const want = {
    still: await h.still(page, 7 * 3600 + 56 * 60),
    f0: await h.frame(page, 0),
    flast: await h.frame(page, 1499),
  };
  // Decode both sides in the page and compare pixels, not PNG bytes.
  for (const f of pngs) {
    const key = /-f0+\.png$/.test(f) ? 'f0' : /-f\d+\.png$/.test(f) ? 'flast' : 'still';
    const same = await page.evaluate(async ([a, b]) => {
      const load = async (b64) => {
        const img = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
        const c = new OffscreenCanvas(img.width, img.height);
        const g = c.getContext('2d');
        g.drawImage(img, 0, 0);
        return g.getImageData(0, 0, img.width, img.height).data;
      };
      const x = await load(a), y = await load(b);
      return x.length === y.length && x.every((v, k) => v === y[k]);
    }, [fs.readFileSync(path.join(dir, f)).toString('base64'), want[key].toString('base64')]);
    ok(same, `--tier stills ${f} differs from the page's own ${key} pixels`);
  }
}

const h = await openBrowser();
try {
  const day = await api(h, TINY, 'day', ['am', 'noon', 'pm', 'late', 'night']);
  ok(day.totalFrames === 1500, `day totalFrames ${day.totalFrames}`);
  const rush = await api(h, `${TINY}&variant=rush`, 'rush', ['am', 'early', 'mid', 'late']);
  ok(rush.window.start === 23400 && rush.window.end === 34200 && rush.totalFrames === 750, `rush window ${JSON.stringify(rush.window)}, ${rush.totalFrames} frames`);
  const week = await api(h, TINY_WEEK, 'week', ['am', 'noon', 'pm', 'sat', 'sun']);
  ok(week.boxes.some((b) => b.name === 'weekday' && b.text === 'MONDAY'), 'week: weekday line MONDAY at the am peak');
  ok(week.window.end - week.window.start === 604800 && week.totalFrames === 1800, `week window ${JSON.stringify(week.window)}`);

  // B14: an unknown variant (and theme) is a startup error, reported like an
  // unknown ?city: busmap.ready rejects and the page logs a console.error.
  for (const [q, want] of [[`${TINY}&variant=week`, 'unknown variant'], [`${TINY}&theme=neon`, 'unknown theme']]) {
    const page = await h.open(q, { allowErrors: true });
    ok(page.failed && page.failed.includes(want) && page.errors.some((e) => e.includes(want)), `${q}: ${page.failed || 'loaded'}; errors ${page.errors}`);
    await page.context().close();
  }
  // The additions are inert on a legacy file: Tsukuba's own path, nothing v4.
  const legacy = await h.open('');
  const lg = await legacy.evaluate(() => ({ v: window.busmap.variant, st: window.busmap.stillTimes(), c: window.busmap.countAt(0),
    a: window.busmap.cardAlpha(0), layout: window.busmap.config.HUD_LAYOUT }));
  ok(lg.v === '' && lg.st === null && lg.c === null && lg.a === 0 && lg.layout === 'panel', `legacy: ${JSON.stringify(lg)}`);
  if (legacy.errors.length) failures.push(`legacy: page errors ${legacy.errors.join('; ')}`);
  if (process.argv.includes('--via-render')) await viaRender(h);
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 smoke: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

#!/usr/bin/env node
// B-3, B-4 and B-12 on the v4 fixture:
//
//  B-3  renderAtV4(W0) equals renderAtV4(W0 + P) on the map (the HUD's
//       sparkline shows where in the window T is, so it is off for this);
//       wrap: frame N - 1 is one ordinary step before frame 0 and both carry
//       the full card, and the virtual frame N equals frame 0 exactly; rush
//       (xfade): the virtual frame N equals frame 0 exactly and frame N - 1
//       differs from it by one cross-fade step
//  B-4  card alpha 1 for frames 0 to 26, 0 from 45 to N - 31, 1 at N - 1
//       (wrap); xfade: 0 from 45 to N - 60, then 1 from N - 30, where the
//       cross-fade starts, to the end; the card and the HUD never both show
//  B-12 activity-daily (week): minutes per frame at frame N - 1 and at frame 0
//       within 5%; the same for the day's activity warp
//
// The wrap checks also run on the Markham and Toronto day and week networks
// (build/<id>/, else the stubs in build/stub_v4/) when they exist.
//
// Usage: node tests/web/v4_loop.mjs

import fs from 'node:fs';
import path from 'node:path';
import { openBrowser, ROOT, TINY, TINY_WEEK, check } from './browser.mjs';

const failures = [];
const ok = (cond, what) => check(failures, cond, what);

// Pixel statistics computed in the page: renders with fn(busmap, i) and
// keeps the last image for the next comparison.
const PAGE_HELPERS = `
  window.__img = (draw) => { draw(); const c = busmap.canvas; return c.getContext('2d').getImageData(0, 0, c.width, c.height).data; };
  window.__diff = (a, b) => { let n = 0, max = 0; for (let k = 0; k < a.length; k++) { const d = Math.abs(a[k] - b[k]); if (d) { n++; if (d > max) max = d; } } return { n, max }; };
`;

async function wrapChecks(h, query, tag) {
  const page = await h.open(query);
  await page.evaluate(PAGE_HELPERS);
  const r = await page.evaluate(() => {
    const bm = window.busmap;
    const N = bm.totalFrames;
    const P = bm.meta.timeline.period;
    const W0 = bm.window.start;
    const out = { N, period: [], card: [] };
    // The occurrences cover the window [W0, W0 + P], so W0 and its period
    // shift are the pair that must agree (and are the loop seam).
    bm.setHud('none');
    const a = __img(() => bm.renderAt(W0));
    const b = __img(() => bm.renderAt(W0 + P));
    out.period.push({ T: W0, ...__diff(a, b) });
    bm.setHud('full');
    const T = (i) => bm.timeAtProgress(i / N);
    out.dt0 = T(1) - T(0);
    out.dtLast = T(N) - T(N - 1);
    out.TN = T(N);
    out.end = bm.window.end;
    const f0 = __img(() => bm.renderFrame(0));
    const f1 = __img(() => bm.renderFrame(1));
    const fl = __img(() => bm.renderFrame(N - 1));
    const fN = __img(() => bm.renderFrame(N));
    out.d01 = __diff(f0, f1);
    out.dl0 = __diff(fl, f0);
    out.dN0 = __diff(fN, f0);
    for (let i = 0; i < N; i++) out.card.push(bm.cardAlpha(i));
    out.loop = bm.config.LOOP;
    out.overlap = [];
    for (let i = 26; i < 46; i++) out.overlap.push(i);
    for (let i = N - 32; i < N; i++) out.overlap.push(i);
    out.overlap = out.overlap.filter((i) => {
      bm.renderFrame(i);
      const b = bm.hudBoxes();
      return b.some((x) => x.name === 'title' && x.alpha > 0) && b.some((x) => x.name === 'card_title' && x.alpha > 0);
    });
    return out;
  });
  for (const p of r.period) ok(p.n === 0, `${tag} B-3: renderAt(${p.T}) and renderAt(T + P) differ in ${p.n} channels (max ${p.max})`);
  ok(r.TN === r.end, `${tag}: the virtual frame N is at the window end (${r.TN} vs ${r.end})`);
  ok(Math.abs(r.dtLast - r.dt0) <= 0.05 * r.dt0, `${tag} B-12: minutes per frame at N - 1 ${(r.dtLast / 60).toFixed(3)} vs frame 0 ${(r.dt0 / 60).toFixed(3)}`);
  console.log(`  ${tag.padEnd(18)} minutes per frame: frame 0 ${(r.dt0 / 60).toFixed(3)}, frame N - 1 ${(r.dtLast / 60).toFixed(3)}; `
    + `frame N - 1 to 0 changes ${r.dl0.n} channels, 0 to 1 ${r.d01.n}`);
  ok(r.dN0.n === 0, `${tag} B-3: the virtual frame N differs from frame 0 in ${r.dN0.n} channels`);
  ok(r.dl0.n > 0 && r.dl0.n <= 3 * r.d01.n + 1000, `${tag} B-3: frame N - 1 to 0 changes ${r.dl0.n} channels, frame 0 to 1 ${r.d01.n}`);
  const N = r.N;
  ok(r.card.slice(0, 27).every((a) => a === 1), `${tag} B-4: card alpha below 1 in frames 0 to 26`);
  ok(r.card.slice(45, N - 30).every((a) => a === 0), `${tag} B-4: card alpha above 0 in frames 45 to N - 31`);
  ok(r.card[N - 1] === 1, `${tag} B-4: card alpha ${r.card[N - 1]} at N - 1`);
  ok(r.card[26] === 1 && r.card[27] === 1 && r.card[28] < 1 && r.card[44] > 0, `${tag}: fade-out frames 26..28, 44: ${[26, 27, 28, 44].map((i) => r.card[i].toFixed(3))}`);
  ok(r.overlap.length === 0, `${tag} B-10: card and HUD both drawn on frames ${r.overlap.join(', ')}`);
  if (page.errors.length) failures.push(`${tag}: page errors ${page.errors.join('; ')}`);
  await page.context().close();
}

async function xfadeChecks(h) {
  const page = await h.open(`${TINY}&variant=rush`);
  await page.evaluate(PAGE_HELPERS);
  const r = await page.evaluate(() => {
    const bm = window.busmap;
    const N = bm.totalFrames;
    const f0 = __img(() => bm.renderFrame(0));
    const fN = __img(() => bm.renderFrame(N));
    const fl = __img(() => bm.renderFrame(N - 1));
    const fl2 = __img(() => bm.renderFrame(N - 2));
    const card = [];
    for (let i = 0; i < N; i++) card.push(bm.cardAlpha(i));
    // Text drawn per frame over both fades and the cross-fade: the HUD's
    // title and the card's title never show at once.
    const overlap = [];
    for (const i of [...Array(30).keys()].map((k) => 26 + k).concat([...Array(70).keys()].map((k) => N - 70 + k))) {
      bm.renderFrame(i);
      const b = bm.hudBoxes();
      const hud = b.some((x) => x.name === 'title' && x.alpha > 0);
      const crd = b.some((x) => x.name === 'card_title' && x.alpha > 0);
      if (hud && crd) overlap.push(i);
    }
    return { N, loop: bm.config.LOOP, dN0: __diff(fN, f0), dl0: __diff(fl, f0), dl2: __diff(fl2, f0), card, overlap };
  });
  ok(r.loop === 'xfade', `rush LOOP is ${r.loop}`);
  ok(r.dN0.n === 0, `rush B-3: the virtual frame N differs from frame 0 in ${r.dN0.n} channels`);
  // One cross-fade step: the snapshot is at smooth(29/30) = 0.9963, so no
  // channel can be more than about 1 code value off frame 0. Both carry the
  // same card, so only the map differs, and the fixture's map at 9:30 is its
  // map at 6:30: there the last second of the loop equals frame 0.
  ok(r.dl0.max <= 2, `rush B-3: frame N - 1 is off frame 0 in ${r.dl0.n} channels, by at most ${r.dl0.max}`);
  ok(r.dl2.max >= r.dl0.max, `rush: frame N - 2 is at least as far from frame 0 as N - 1 (${r.dl2.max} vs ${r.dl0.max})`);
  const N = r.N;
  ok(r.card.slice(0, 27).every((a) => a === 1) && r.card.slice(45, N - 59).every((a) => a === 0),
    'rush B-4: card alpha 1 to 26, then 0 from 45 to N - 60');
  ok(r.card.slice(N - 30).every((a) => a === 1) && r.card[N - 45] > 0 && r.card[N - 45] < 1,
    `rush B-4: card alpha back to 1 by N - 30, where the cross-fade starts (N - 45: ${r.card[N - 45]})`);
  ok(r.overlap.length === 0, `rush B-10: card and HUD both drawn on frames ${r.overlap.join(', ')}`);
  if (page.errors.length) failures.push(`rush: page errors ${page.errors.join('; ')}`);
  await page.context().close();
}

const h = await openBrowser();
try {
  await wrapChecks(h, TINY, 'day');
  await wrapChecks(h, TINY_WEEK, 'week');
  await xfadeChecks(h);
  for (const id of ['gta-markham', 'gta-toronto']) {
    for (const tl of ['day', 'week']) {
      const real = `build/${id}/${tl}/network.json.gz`;
      const file = fs.existsSync(path.join(ROOT, real)) ? real : `build/stub_v4/${id}/${tl}/network.json.gz`;
      if (!fs.existsSync(path.join(ROOT, file))) continue;
      const bm = fs.existsSync(path.join(ROOT, `build/${id}/basemap.json.gz`)) ? `build/${id}/basemap.json.gz` : 'data/gta/built/basemap.json.gz';
      await wrapChecks(h, `data=../${file}&basemap=../${bm}`, `${id} ${tl}`);
    }
  }
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 loop: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

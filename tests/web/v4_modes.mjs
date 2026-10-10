#!/usr/bin/env node
// Card text of videos split by mode (B10): {vehicles} is the count line's
// noun, so a video that keeps one mode says "3 buses" where a mixed one says
// "3 vehicles", and a card line that opens with a place such as "the GTA"
// still starts with a capital. The networks are the v4 fixture with its
// rail trips taken out, written to build/test_web/.
//
// Usage: node tests/web/v4_modes.mjs

import fs from 'node:fs';
import path from 'node:path';
import { openBrowser, ROOT, check } from './browser.mjs';

const failures = [];
const ok = (cond, what) => check(failures, cond, what);
const DIR = path.join(ROOT, 'build', 'test_web');
const BASEMAP = 'basemap=../tests/fixtures/v4_tiny/basemap.json';
const TEMPLATES = [['{place} wakes up', 'Busiest at {peak_time} with {peak_count} {vehicles}']];

// First argmax of the rounded hist over the window, as the trim takes V.peak.
function peakOf(hist, V) {
  let best = -1, at = 0;
  for (let m = Math.ceil(V.start / 60); m < Math.ceil(V.end / 60); m++) {
    const v = hist[((m % hist.length) + hist.length) % hist.length];
    if (v > best) { best = v; at = m; }
  }
  return { count: Math.round(best), time: at * 60 };
}

function derived(keepRail) {
  const net = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/v4_tiny/network.json'), 'utf8'));
  const m = net.meta;
  m.place = 'the Tinytown';
  m.card.templates = Object.fromEntries(Object.keys(m.variants).map((v) => [v, TEMPLATES]));
  if (!keepRail) {
    // What the trim writes for modes ["bus"]: the bus trips, their counts and their modes only.
    const rail = new Set(net.routes.map((r, i) => (r.mode === 'rail' ? i : -1)).filter((i) => i >= 0));
    net.trips = net.trips.filter((t) => !rail.has(t.r));
    m.trips_total = net.trips.length;
    net.hist = m.hist_by_mode.bus;
    m.hist_by_mode = { bus: m.hist_by_mode.bus };
    m.modes = m.modes.filter((md) => md.id === 'bus');
    m.groups = m.groups.filter((g) => g.id === 'alpha');
    m.hist_by_group = { alpha: m.hist_by_group.alpha };
    for (const V of Object.values(m.variants)) V.peak = peakOf(net.hist, V);
  }
  const name = keepRail ? 'two-mode.json' : 'bus-only.json';
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(path.join(DIR, name), JSON.stringify(net));
  return { query: `data=../build/test_web/${name}&${BASEMAP}`, peaks: Object.fromEntries(Object.entries(m.variants).map(([k, V]) => [k, V.peak])) };
}

const h = await openBrowser();
try {
  for (const [keepRail, noun] of [[false, 'buses'], [true, 'vehicles']]) {
    const { query, peaks } = derived(keepRail);
    for (const variant of ['day', 'rush']) {
      const page = await h.open(`${query}&variant=${variant}`);
      const r = await page.evaluate(() => {
        window.busmap.renderFrame(0);
        const boxes = window.busmap.hudBoxes();
        const text = (n) => (boxes.find((b) => b.name === n) || {}).text || '';
        return { line0: text('card_line0'), line1: [text('card_line1'), text('card_line1b')].filter(Boolean).join(' '),
          modes: window.busmap.meta.modes.map((m) => m.id) };
      });
      const tag = `${keepRail ? 'two modes' : 'bus only'} ${variant}`;
      const n = peaks[variant].count;
      ok(r.line0 === 'The Tinytown wakes up', `${tag}: card line 0 "${r.line0}"`);
      ok(r.line1.endsWith(` with ${n.toLocaleString('en-US')} ${n === 1 ? noun.replace(/es$|s$/, '') : noun}`),
        `${tag}: card line 1 "${r.line1}" (peak ${n}, want ${noun})`);
      ok(JSON.stringify(r.modes) === JSON.stringify(keepRail ? ['bus', 'rail'] : ['bus']), `${tag}: modes ${r.modes}`);
      if (page.errors.length) failures.push(`${tag}: page errors ${page.errors.join('; ')}`);
      await page.context().close();
    }
  }
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 modes: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

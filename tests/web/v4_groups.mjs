#!/usr/bin/env node
// B19: colour groups and the quiet legend on group networks made from the v4 fixture.
//
//  plans    every checked plan (tests/fixtures/colours/plans.json, the 22 recipes) on a group
//           network with one clone of route alpha:1 and its trips per group (plus one in other
//           when the plan has other), drawn in its batch theme with its paint: every trail and
//           dormant line equals the plan's exactly; placement runs other, neutrals, pins, then
//           the free groups; every free group is at least BRAND_MIN_DE from everything placed
//           before it; the foreign grey is placed exactly when a route is in other; every
//           group's trips draw in its own colour class; no page error
//  line 6   Toronto trains' Line 6 draws its neutral silver b4bac4 (dormant line over the
//           background at 0.5), never the TTC red; "neutral": true draws the foreign grey; with
//           no paint at all the page warns and draws every group from its fallback, none neutral
//  legend   legend=1 draws each plan's legend_items on one row at 20.4 px in the preset (San
//           Diego's Trolley one entry with its three line dots), inside the safe zone and at
//           18 px or more; Houston fits at step 1 or 2 at TEXT_SCALE 1 and weight 500; long
//           labels walk the fit steps in order to two rows; seven 16-character labels drop
//           entries with one console.error each and still end inside the 504 px column;
//           legend=0 draws no legend and the HUD of a brand network
//  chips    nothing folds for colour on a group network, with or without chips=1 at TEXT_SCALE
//           0.6 and 1; chips=1 with legend=1 draws the legend and not the chips, with a warning
//  paint    two paint= parameters merge: the second changes only the key it names
//  order    other draws first however busy, then the bus groups with the fewest trips on
//           top, then rail over every bus class
//
// Usage: node tests/web/v4_groups.mjs [--only gta-toronto-trains,...]

import fs from 'node:fs';
import path from 'node:path';
import { openBrowser, ROOT, TINY, check } from './browser.mjs';
import { loadColor } from './color_vectors.mjs';

const failures = [];
const ok = (cond, what) => check(failures, cond, what);
const args = process.argv.slice(2);
const only = args.includes('--only') ? new Set(args[args.indexOf('--only') + 1].split(',')) : null;
const BC = loadColor();
const THEMES = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'themes.json'), 'utf8'));
const PLANS = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'colours', 'plans.json'), 'utf8')).plans;
const BASEMAP = 'basemap=../tests/fixtures/v4_tiny/basemap.json';
const SAFE = { x0: 120, y0: 290, x1: 800, y1: 1440 };
const MIN_DE = 0.08;

// A group network from the fixture: route alpha:1 and its trips once per group (g.copies
// times when given, in g.mode when given), the groups in the order given (other last when
// listed), meta.color_by "group".
function groupNet(name, groups) {
  const net = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'v4_tiny', 'network.json'), 'utf8'));
  const base = net.routes[0];
  const baseTrips = net.trips.filter((t) => t.r === 0);
  net.routes = groups.map((g, i) => ({ ...base, id: `alpha:g${i}`, short: `g${i}`, group: g.id, mode: g.mode || base.mode }));
  net.trips = [];
  groups.forEach((g, i) => {
    for (let k = 0; k < (g.copies || 1); k++) for (const t of baseTrips) net.trips.push({ ...t, r: i });
  });
  const m = net.meta;
  m.color_by = 'group';
  m.groups = groups.map((g, i) => ({ id: g.id, label: g.label, brand: null, share: i ? 0 : 1, legend: g.legend }));
  m.hist_by_group = Object.fromEntries(groups.map((g, i) => [g.id, i ? net.hist.map(() => 0) : net.hist.slice()]));
  m.trips_total = net.trips.length;
  const rel = path.join('build', 'test_web', `groups-${name}.json`);
  fs.mkdirSync(path.join(ROOT, 'build', 'test_web'), { recursive: true });
  fs.writeFileSync(path.join(ROOT, rel), JSON.stringify(net));
  return `data=../${rel}&${BASEMAP}`;
}

const paintOf = (plan) => Object.fromEntries(plan.groups.filter((g) => g.paint).map((g) => [g.id, g.paint]));
const paintQ = (paint) => `paint=${encodeURIComponent(JSON.stringify(paint))}`;

function inPage() {
  const bm = window.busmap;
  bm.renderAt(bm.stillTimes().am);
  return { groupMap: bm.groupMap, brandMap: bm.brandMap, colors: bm.colors.map((c) => ({ hex: c.hex, trips: c.trips })),
    legend: bm.legend, chips: bm.chips, boxes: bm.hudBoxes(), minDE: bm.config.BRAND_MIN_DE, routes: bm.meta.groups.length };
}

async function open(h, query, opts = {}) {
  const page = await h.open(query, { allowErrors: true, ...opts });
  if (page.failed) {
    failures.push(`${query.slice(0, 80)}: ${page.failed}`);
    await page.context().close();
    return null;
  }
  const r = await page.evaluate(inPage);
  r.errors = page.errors.slice();
  r.warnings = page.warnings.slice();
  await page.context().close();
  return r;
}

function legendBoxes(tag, r) {
  const boxes = r.boxes.filter((b) => b.name === 'legend' || b.name === 'legend2');
  for (const b of boxes) {
    ok(b.x0 >= SAFE.x0 - 0.5 && b.x1 <= SAFE.x1 + 0.5 && b.y0 >= SAFE.y0 - 0.5 && b.y1 <= SAFE.y1 + 0.5,
      `${tag}: ${b.name} "${b.text}" at x ${b.x0.toFixed(0)}..${b.x1.toFixed(0)}, y ${b.y0.toFixed(0)}..${b.y1.toFixed(0)} leaves the safe zone`);
    ok(b.size >= 18, `${tag}: ${b.name} at ${b.size} px, under 18`);
  }
  return boxes;
}

const h = await openBrowser();
try {
  for (const plan of PLANS) {
    if (only && !only.has(plan.id)) continue;
    const tag = plan.id;
    const bg = THEMES[plan.theme].colors.bg.replace('#', '');
    const foreign = THEMES[plan.theme].colors.foreign.replace('#', '');
    const q = groupNet(plan.id, plan.groups);
    const r = await open(h, `${q}&theme=${plan.theme}&${paintQ(paintOf(plan))}${plan.legend ? '&legend=1' : ''}`);
    if (!r) continue;
    ok(!r.errors.length, `${tag}: page errors ${r.errors.join(' | ')}`);
    ok(r.brandMap === null, `${tag}: brandMap is not null on a group network`);
    const gm = new Map(r.groupMap.map((e) => [e.id, e]));
    for (const g of plan.groups) {
      const e = gm.get(g.id);
      if (!ok(e, `${tag}: no groupMap entry for ${g.id}`)) continue;
      const line = g.line || BC.over(g.trail, 0.5, bg);
      ok(e.trail === g.trail && e.line === line, `${tag} ${g.id}: trail ${e.trail} line ${e.line} (${e.how}), plan ${g.trail} ${line}`);
    }
    const placed = r.groupMap.filter((e) => e.order >= 0).sort((a, b) => a.order - b.order);
    const rank = (e) => (e.id === 'other' ? 0 : e.how === 'neutral' ? 1 : e.how.startsWith('pin') ? 2 : 3);
    ok(placed.every((e, i) => i === 0 || rank(placed[i - 1]) <= rank(e)), `${tag}: placement order ${placed.map((e) => `${e.id}(${e.how})`).join(', ')}`);
    for (const e of placed) {
      if (rank(e) === 3) ok(e.dmin === null || e.dmin >= r.minDE - 1e-9, `${tag} ${e.id}: free group only ${e.dmin} from ${e.near}`);
    }
    const otherPlaced = placed.some((e) => e.id === 'other');
    ok(otherPlaced === plan.other, `${tag}: the foreign grey placed ${otherPlaced}, a route in other ${plan.other}`);
    // Each group's two trips draw in its own class: per trail, the trips of every group with it.
    const want = new Map();
    for (const g of plan.groups) want.set(g.trail, (want.get(g.trail) || 0) + 2);
    for (const [hex, n] of want) {
      const got = r.colors.filter((c) => c.hex === hex).reduce((s, c) => s + c.trips, 0);
      ok(got === n, `${tag}: ${got} trips drawn in #${hex}, want ${n}`);
    }
    if (plan.legend) {
      const lg = r.legend;
      if (ok(lg, `${tag}: no legend with legend=1`)) {
        ok(lg.rows === 1 && lg.size === 20.4 && lg.base === 24, `${tag}: legend ${lg.rows} rows at ${lg.size} px (base ${lg.base}), want 1 at 20.4`);
        ok(JSON.stringify(lg.entries.map((e) => e.label)) === JSON.stringify(plan.legend_items),
          `${tag}: legend ${lg.entries.map((e) => e.label).join(' · ')}, plan ${plan.legend_items.join(' · ')}`);
        ok(!lg.dropped.length, `${tag}: legend dropped ${lg.dropped.join(', ')}`);
      }
      ok(legendBoxes(tag, r).length === 1, `${tag}: want one legend box`);
      if (plan.id === 'usa-san-diego' && lg) {
        const t = lg.entries.find((e) => e.label === 'Trolley');
        ok(t && JSON.stringify(t.ids) === JSON.stringify(['blue_line', 'green_line', 'orange_line']), `${tag}: Trolley entry ${JSON.stringify(t)}`);
      }
    } else {
      ok(r.legend === null && !legendBoxes(tag, r).length, `${tag}: a legend drawn with the legend off`);
    }
    const pairs = placed.slice(1).map((e) => `${e.id} ${e.dmin.toFixed(3)}`).join(', ');
    console.log(`  ${tag.padEnd(24)} ${placed.map((e) => `${e.id}=${e.trail}`).join(' ')}  ${plan.legend ? `legend ${r.legend && r.legend.width} px` : ''}`);
    if (only) console.log(`    dmin ${pairs}`);
    if (foreign && plan.other) ok(gm.get('other').trail === foreign, `${tag}: other drawn ${gm.get('other').trail}, the theme's foreign is ${foreign}`);
  }

  // Line 6: neutral silver, the foreign grey with neutral true, the fallback without paint.
  if (!only || only.has('gta-toronto-trains')) {
    const plan = PLANS.find((p) => p.id === 'gta-toronto-trains');
    const q = `${groupNet('line6', plan.groups)}&theme=lake`;
    const bg = THEMES.lake.colors.bg.replace('#', '');
    const ttcRed = BC.normalise('ed1c24', THEMES.lake.env, bg).trail;
    const a = await open(h, `${q}&${paintQ(paintOf(plan))}`);
    const l6 = a && a.groupMap.find((e) => e.id === 'line6');
    ok(l6 && l6.trail === 'b4bac4' && l6.line === BC.over('b4bac4', 0.5, bg) && l6.how === 'neutral' && l6.trail !== ttcRed,
      `line 6: ${JSON.stringify(l6)}`);
    const b = await open(h, `${q}&${paintQ(paintOf(plan))}&${paintQ({ line6: { neutral: true } })}`);
    const l6b = b && b.groupMap.find((e) => e.id === 'line6');
    ok(l6b && l6b.trail === '8a93a0' && l6b.how === 'neutral', `line 6 neutral true: ${JSON.stringify(l6b)}`);
    const c = await open(h, q);
    ok(c && c.warnings.some((w) => w.includes('no ?paint')), `no paint: no warning (${c && c.warnings.join(' | ')})`);
    ok(c && c.groupMap.every((e) => e.how !== 'neutral' && !e.how.startsWith('pin')), `no paint: ${c && c.groupMap.map((e) => `${e.id} ${e.how}`).join(', ')}`);
    console.log(`  line 6                   ${l6 && l6.trail}, neutral true ${l6b && l6b.trail}, no paint ${c && c.groupMap.find((e) => e.id === 'line6').trail}`);
  }

  if (!only) {
    // Houston at TEXT_SCALE 1 and weight 500: one row at step 1 or 2.
    const hou = PLANS.find((p) => p.id === 'usa-houston');
    const hq = `${groupNet('houston', hou.groups)}&theme=sodium&${paintQ(paintOf(hou))}&legend=1`;
    const r1 = await open(h, `${hq}&textscale=1&bodyweight=500`);
    ok(r1 && r1.legend && r1.legend.rows === 1 && [24, 22].includes(r1.legend.base) && r1.legend.gap === 20,
      `houston at TEXT_SCALE 1: ${JSON.stringify(r1 && r1.legend)}`);
    if (r1) legendBoxes('houston 1', r1);
    console.log(`  houston textscale 1      base ${r1 && r1.legend.base}, ${r1 && r1.legend.width} px, tried ${JSON.stringify(r1 && r1.legend.tried)}`);

    // Long labels: steps 1 to 4 in order, two rows at 24.
    const longs = ['Airport Express', 'Lakeshore Local', 'Downtown Shuttle', 'Night Network'].map((label, i) => ({ id: `g${i}`, label, legend: true }));
    const lp = Object.fromEntries(longs.map((g, i) => [g.id, { hex: ['d33517', '026bcd', '00853e', 'b300b3'][i] }]));
    const r2 = await open(h, `${groupNet('long', longs)}&${paintQ(lp)}&legend=1`);
    const steps = r2 && r2.legend && r2.legend.tried.map((t) => `${t.rows}/${t.base}/${t.gap}`).join(' ');
    ok(steps === '1/24/20 1/22/20 1/22/12 2/24/20' && r2.legend.rows === 2, `long labels: tried ${steps}, rows ${r2 && r2.legend && r2.legend.rows}`);
    if (r2) {
      const rows = legendBoxes('long labels', r2);
      ok(rows.length === 2 && rows[0].y1 < rows[1].y0, `long labels: rows ${rows.map((b) => `${b.name} y ${b.y0.toFixed(0)}..${b.y1.toFixed(0)}`).join(', ')}`);
      const count = r2.boxes.find((b) => b.name === 'count');
      ok(count && count.y1 < rows[0].y0, 'long labels: the count line does not stand over the top legend row');
    }
    console.log(`  long labels              ${steps}`);

    // Seven 16-character labels at TEXT_SCALE 1: entries dropped, each with a console.error.
    const seven = ['Wellington North', 'Kingston Express', 'Riverside Limits', 'Harbourfront Way', 'Mountain Shuttle',
      'University Ridge', 'Industrial Parks'].map((label, i) => ({ id: `g${i}`, label, legend: true }));
    const sp = Object.fromEntries(seven.map((g, i) => [g.id, { hex: ['d33517', '026bcd', '00853e', 'b300b3', 'f2b705', '009cdb', 'cc6600'][i] }]));
    const r3 = await open(h, `${groupNet('seven', seven)}&${paintQ(sp)}&legend=1&textscale=1`);
    if (r3) {
      const drops = r3.errors.filter((e) => /^legend: ".*" dropped/.test(e));
      ok(r3.legend && r3.legend.dropped.length > 0 && drops.length === r3.legend.dropped.length && drops.length === r3.errors.length,
        `seven labels: dropped ${r3.legend && r3.legend.dropped}, errors ${r3.errors.join(' | ')}`);
      const rows = legendBoxes('seven labels', r3);
      ok(rows.length && rows.every((b) => b.x1 <= 148 + 504 + 0.5), `seven labels: rows end at ${rows.map((b) => b.x1.toFixed(0))}`);
      console.log(`  seven labels             ${r3.legend.entries.length} drawn, dropped ${r3.legend.dropped.join(', ')}`);
    }

    // legend=0: no legend, and the HUD of the brand network the fixture is.
    const tb = PLANS.find((p) => p.id === 'gta-toronto-buses');
    const tq = `${groupNet('toronto-buses', tb.groups)}&${paintQ(paintOf(tb))}`;
    const off = await open(h, `${tq}&legend=0`);
    const brand = await open(h, TINY);
    if (off && brand) {
      const key = (bs) => JSON.stringify(bs.map((b) => [b.name, b.text, Math.round(b.x0), Math.round(b.y0), Math.round(b.x1), Math.round(b.y1)]));
      ok(off.legend === null && key(off.boxes) === key(brand.boxes), 'legend=0: the HUD differs from the brand network\'s');
    }

    // Chips on a group network: no colour folds; with the legend on they are not drawn.
    for (const ts of [0.6, 1]) {
      const a = await open(h, `${tq}&textscale=${ts}`);
      const b = await open(h, `${tq}&textscale=${ts}&chips=1`);
      ok(a && b && JSON.stringify(a.groupMap) === JSON.stringify(b.groupMap), `chips=1 at TEXT_SCALE ${ts} repaints a group`);
    }
    const both = await open(h, `${tq}&chips=1&legend=1`);
    ok(both && both.legend && both.chips === null && !both.boxes.some((b) => b.name === 'chips')
      && both.warnings.some((w) => w.includes('legend takes the row')), `chips=1&legend=1: ${JSON.stringify(both && both.chips)}`);

    // Two paint= parameters: the second changes only the key it names.
    const m = await open(h, `${tq}&${paintQ({ bus: { dL: -0.2 } })}`);
    if (m) {
      const bus = m.groupMap.find((e) => e.id === 'bus');
      ok(bus.source === 'ed1c24' && bus.dL === -0.2 && bus.how === 'pin dL-0.20', `paint merge: bus ${JSON.stringify(bus)}`);
      const ex = m.groupMap.find((e) => e.id === 'express');
      ok(ex.source === '00a651' && ex.dL === null, `paint merge: express ${JSON.stringify(ex)}`);
    }
    // A paint for a group the network does not list is a warning, not an error.
    const stray = await open(h, `${tq}&${paintQ({ nosuch: { hex: '123456' } })}`);
    ok(stray && !stray.errors.length && stray.warnings.some((w) => w.includes('no group "nosuch"')), 'paint for an unknown group: no warning');

    // Draw order: other first however busy, then the bus groups with the fewest trips on top,
    // then rail (the busiest group here) over every bus class.
    const order = [{ id: 'grid', label: 'Grid', legend: true, copies: 3 }, { id: 'rapid', label: 'Rapid', legend: true, copies: 1 },
      { id: 'express', label: 'Express', legend: true, copies: 2 }, { id: 'rail', label: 'Rail', legend: true, copies: 5, mode: 'rail' },
      { id: 'other', label: 'Other', legend: false, copies: 4 }];
    const op = { grid: { hex: '0067b1' }, rapid: { hex: 'e31837' }, express: { hex: '00a651' }, rail: { hex: 'b300b3' } };
    const d = await open(h, `${groupNet('order', order)}&${paintQ(op)}`);
    if (d) {
      const trailOf = new Map(d.groupMap.map((e) => [e.trail, e.id]));
      const got = d.colors.map((c) => trailOf.get(c.hex)).join(' ');
      ok(got === 'other grid express rapid rail', `draw order bottom to top: ${got}, want other grid express rapid rail`);
      console.log(`  draw order               ${got}`);
    }
  }
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 groups: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;

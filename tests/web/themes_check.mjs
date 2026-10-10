#!/usr/bin/env node
// B-11: the batch themes of web/themes.json are told apart and keep their text legible (B4).
//
//  * every pair of themes: mean OKLab distance over water, major, cityLine, accent >= 0.06
//  * every text token on the panel (panel colour at its alpha over bg): contrast >= 4.5
//  * cityLine and foreign on bg: contrast >= 3.0
//  * accent chroma <= 0.12 (no neon)
//  * every theme names the same tokens, and an envelope with all six numbers
//
// Usage: node tests/web/themes_check.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadColor } from './color_vectors.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const TEXT = ['title', 'subtitle', 'clock', 'accent', 'breakdown', 'axis', 'credit'];
const KEYS = ['water', 'major', 'cityLine', 'accent'];
const ENV = ['trailL', 'trailCmin', 'trailCmax', 'lineL', 'lineCmax', 'minContrast'];

function rgba(css) {
  const m = /^rgba\((\d+),(\d+),(\d+),([\d.]+)\)$/.exec(css);
  if (!m) throw new Error(`panel token ${css} is not rgba(r,g,b,a)`);
  return { rgb: [Number(m[1]), Number(m[2]), Number(m[3])], a: Number(m[4]) };
}

function main() {
  const C = loadColor();
  const themes = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'themes.json'), 'utf8'));
  const names = Object.keys(themes);
  const failures = [];
  const lines = [];
  const tokens = Object.keys(themes[names[0]].colors).sort().join(',');
  let worst = Infinity, worstPair = '';
  for (const n of names) {
    const t = themes[n];
    if (Object.keys(t.colors).sort().join(',') !== tokens) failures.push(`${n}: tokens differ from ${names[0]}`);
    for (const k of ENV) if (!Number.isFinite(t.env[k])) failures.push(`${n}: env.${k} missing`);
    const p = rgba(t.colors.panel);
    const panel = C.over(p.rgb, p.a, t.colors.bg);
    const row = [];
    for (const k of TEXT) {
      const c = C.contrast(t.colors[k], panel);
      row.push(`${k} ${c.toFixed(2)}`);
      if (c < 4.5) failures.push(`${n}: ${k} on the panel has contrast ${c.toFixed(2)} < 4.5`);
    }
    for (const k of ['cityLine', 'foreign']) {
      const c = C.contrast(t.colors[k], t.colors.bg);
      row.push(`${k}/bg ${c.toFixed(2)}`);
      if (c < 3) failures.push(`${n}: ${k} on bg has contrast ${c.toFixed(2)} < 3`);
    }
    const chroma = C.oklch(t.colors.accent)[1];
    row.push(`accent C ${chroma.toFixed(3)}`);
    if (chroma > 0.12 + 1e-9) failures.push(`${n}: accent chroma ${chroma.toFixed(3)} > 0.12`);
    lines.push(`  ${n.padEnd(6)} ${row.join(', ')}`);
  }
  for (let i = 0; i < names.length; i++) {
    for (let j = i + 1; j < names.length; j++) {
      const a = themes[names[i]].colors, b = themes[names[j]].colors;
      const mean = KEYS.reduce((s, k) => s + C.deltaE(a[k], b[k]), 0) / KEYS.length;
      if (mean < worst) { worst = mean; worstPair = `${names[i]}/${names[j]}`; }
      if (mean < 0.06) failures.push(`${names[i]}/${names[j]}: mean distance ${mean.toFixed(3)} < 0.06`);
    }
  }
  console.log(lines.join('\n'));
  console.log(`  closest theme pair ${worstPair}: ${worst.toFixed(3)}`);
  for (const f of failures) console.log(`FAIL ${f}`);
  console.log(`themes: ${names.length} themes, ${failures.length} failure(s)`);
  process.exitCode = failures.length ? 1 : 0;
}

main();

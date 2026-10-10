#!/usr/bin/env node
// B-2: web/color.js against the Python reference vectors (spec B5).
//
// vectors_rev.json is scratchpad/spec/rev/vectors_rev.json: for every theme, 21 source colours
// normalised to [trail, line] by the Python port of legible.py, plus three ladder adjustments.
// The envelopes and backgrounds come from web/themes.json, so this also checks that the
// committed themes are the ones the vectors were made with. Every channel must be within 1.
//
// Usage: node tests/web/color_vectors.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

export function loadColor() {
  const ctx = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'color.js'), 'utf8'), ctx, { filename: 'color.js' });
  return ctx.BusmapColor;
}

function main() {
  const C = loadColor();
  const themes = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'themes.json'), 'utf8'));
  const vectors = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'web', 'vectors_rev.json'), 'utf8'));
  let checked = 0;
  const failures = [];
  const close = (got, want) => {
    if (got === null || want === null) return got === want;
    const a = C.parseHex(got), b = C.parseHex(want);
    return a.every((v, i) => Math.abs(v - b[i]) <= 1);
  };
  const expect = (what, got, want) => {
    checked++;
    if (!close(got, want)) failures.push(`${what}: got ${got}, want ${want}`);
  };
  for (const [name, vec] of Object.entries(vectors)) {
    const t = themes[name];
    if (!t) { failures.push(`theme ${name} missing from web/themes.json`); continue; }
    const bg = t.colors.bg;
    for (const [src, want] of Object.entries(vec)) {
      if (src === '_adjust') continue;
      const got = C.normalise(src, t.env, bg);
      expect(`${name} ${src} trail`, got && got.trail, want[0]);
      expect(`${name} ${src} line`, got && got.line, want[1]);
    }
    const adj = vec._adjust;
    expect(`${name} 0058a9 dL -0.12`, C.normalise('0058a9', t.env, bg, -0.12).trail, adj['0058a9 L-0.12']);
    expect(`${name} 0058a9 rot +30`, C.normalise('0058a9', t.env, bg, 0, 30).trail, adj['0058a9 h+30']);
    expect(`${name} 00853e dL -0.12 rot -15`, C.normalise('00853e', t.env, bg, -0.12, -15).trail, adj['00853e L-0.12 h-15']);
  }
  // The B5 table as printed in the spec, so a regenerated vector file cannot drift from it.
  const TABLE = {
    lake: { ed1c24: ['ff968a', 'bb584f'], '00853e': ['69d186', '328f50'], '0058a9': ['83bbff', '3e7cc5'],
      d33517: ['ff9781', 'ba5946'], d5c82b: ['c7bb32', '867d00'], b300b3: ['eb93e7', 'a35ca0'], '0a295d': ['92b8f8', '577ab5'],
      _dl: '4b94ea', _rot: 'aeadff' },
    ink: { ed1c24: ['e88f85', 'a45951'], '00853e': ['75bf87', '418252'], '0058a9': ['79aeef', '4674ab'],
      d33517: ['e8907e', 'a45a4b'], d5c82b: ['b6ae57', '7b7424'], b300b3: ['d191ce', '915b8f'], '0a295d': ['86acea', '4f71ac'],
      _dl: '5589c7', _rot: 'a2a1ee' },
    teal: { ed1c24: ['ffa096', 'c7534a'], '00853e': ['6fd88c', '23954d'], '0058a9': ['8fc1ff', '357ed3'],
      d33517: ['ffa18e', 'c6553f'], d5c82b: ['cfc220', '8a8000'], b300b3: ['fb90f7', 'ac58a9'], '0a295d': ['98bffe', '5a7db9'],
      _dl: '519af1', _rot: 'b5b5ff' },
  };
  for (const [name, rows] of Object.entries(TABLE)) {
    const t = themes[name];
    for (const [src, want] of Object.entries(rows)) {
      if (src.startsWith('_')) continue;
      const got = C.normalise(src, t.env, t.colors.bg);
      expect(`table ${name} ${src} trail`, got.trail, want[0]);
      expect(`table ${name} ${src} line`, got.line, want[1]);
    }
    expect(`table ${name} 808080`, C.normalise('808080', t.env, t.colors.bg), null);
    expect(`table ${name} 0058a9 dL -0.12`, C.normalise('0058a9', t.env, t.colors.bg, -0.12).trail, rows._dl);
    expect(`table ${name} 0058a9 rot +30`, C.normalise('0058a9', t.env, t.colors.bg, 0, 30).trail, rows._rot);
  }
  // informative(): the A10 and B6 rule.
  const info = { ed1c24: true, '#ED1C24': true, ffffff: false, '000000': false, '808080': false, '': false, '2f6bf': false, '0058a9': true };
  for (const [hex, want] of Object.entries(info)) {
    checked++;
    if (C.informative(hex) !== want) failures.push(`informative(${JSON.stringify(hex)}) is ${!want}`);
  }
  checked++;
  if (Math.abs(C.contrast('ffffff', '000000') - 21) > 1e-9) failures.push('contrast(white, black) is not 21');
  checked++;
  if (C.deltaE('0058a9', '0058a9') !== 0) failures.push('deltaE of a colour with itself is not 0');
  for (const f of failures) console.log(`FAIL ${f}`);
  console.log(`color vectors: ${checked} checks, ${failures.length} failure(s)`);
  process.exitCode = failures.length ? 1 : 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();

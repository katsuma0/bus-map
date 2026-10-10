/* Colour maths for the Shorts renderer (spec B5): sRGB and linear light, OKLab and OKLCH, a
 * gamut clamp by bisection on chroma, WCAG luminance and contrast, and the theme envelope that
 * moves a brand colour to the batch's trail and dormant-line lightness.
 *
 * A port of scratchpad/spec/legible.py, so the page and the Python checks agree to within one
 * code value. It draws nothing and touches no page state: a legacy page loads it and never calls
 * it. In Node, run it in a vm context and read BusmapColor from that context.
 */
(function (root) {
'use strict';

function s2l(c) {
  c /= 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function l2s(c) {
  c = c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055;
  return c * 255;
}

// '#rrggbb' or 'rrggbb' (any case) to [r, g, b]; anything else is null.
function parseHex(str) {
  if (typeof str !== 'string') return null;
  const h = str.trim().replace(/^#/, '').toLowerCase();
  if (!/^[0-9a-f]{6}$/.test(h)) return null;
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

function toHex(rgb) {
  return rgb.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
}

// Every function below takes a colour as a hex string or an [r, g, b] array.
function rgbOf(c) {
  if (Array.isArray(c)) return c;
  const rgb = parseHex(c);
  if (!rgb) throw new Error(`not a colour: ${JSON.stringify(c)}`);
  return rgb;
}

function oklab(c) {
  const [r, g, b] = rgbOf(c).map(s2l);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
  ];
}

// Linear sRGB of an OKLab colour, unclamped, so the caller can test the gamut.
function fromOklab(L, a, b) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ];
}

function inGamut(lin) {
  return lin.every((v) => v >= -1e-4 && v <= 1 + 1e-4);
}

// [L, C, h] with h in radians, as atan2 gives it.
function oklch(c) {
  const [L, a, b] = oklab(c);
  return [L, Math.hypot(a, b), Math.atan2(b, a)];
}

// The largest chroma <= C that fits sRGB at this lightness and hue, found in 30 bisection
// steps, so a saturated source keeps its hue instead of clipping per channel. Returns
// [r, g, b] floats in 0..255.
function oklchToRgb(L, C, h) {
  let lo = 0, hi = C;
  for (let k = 0; k < 30; k++) {
    const mid = (lo + hi) / 2;
    if (inGamut(fromOklab(L, mid * Math.cos(h), mid * Math.sin(h)))) lo = mid; else hi = mid;
  }
  return fromOklab(L, lo * Math.cos(h), lo * Math.sin(h)).map((v) => l2s(Math.min(1, Math.max(0, v))));
}

function luminance(c) {
  const [r, g, b] = rgbOf(c).map(s2l);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

function deltaE(a, b) {
  const p = oklab(a), q = oklab(b);
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

// A route or brand colour carries information when it is six hex digits, not pure white or
// black, and has some chroma: greys (TTC Line 6, 808080) say nothing about the line.
function informative(hex) {
  const rgb = parseHex(hex);
  if (!rgb) return false;
  const h = toHex(rgb);
  if (h === 'ffffff' || h === '000000') return false;
  return oklch(rgb)[1] >= 0.03;
}

// Moves a source colour into a theme envelope: the hue stays (plus rotDeg), the trail gets the
// envelope's lightness and a chroma clamped into [trailCmin, trailCmax], the dormant line a
// darker lightness and at most lineCmax. Either is then lightened in 0.01 steps until it keeps
// env.minContrast against the background. dL shifts both lightnesses (the B6 ladder).
function normalise(hex, env, bgRGB, dL = 0, rotDeg = 0) {
  if (!informative(hex)) return null;
  const bg = rgbOf(bgRGB);
  const min = Number.isFinite(env.minContrast) ? env.minContrast : 3;
  const [, C, h0] = oklch(parseHex(hex));
  const h = h0 + (rotDeg * Math.PI) / 180;
  const fit = (L, Cx) => {
    let out = oklchToRgb(L, Cx, h);
    while (contrast(out, bg) < min && L < 0.98) {
      // Rounded like the Python reference so both walk the same lightness steps.
      L = Math.round((L + 0.01) * 10000) / 10000;
      out = oklchToRgb(L, Cx, h);
    }
    return toHex(out);
  };
  return {
    trail: fit(env.trailL + dL, Math.min(Math.max(C, env.trailCmin), env.trailCmax)),
    line: fit(env.lineL + dL, Math.min(C, env.lineCmax)),
  };
}

// fg at alpha a over bg, per sRGB channel (what canvas source-over does).
function over(fg, a, bg) {
  const f = rgbOf(fg), b = rgbOf(bg);
  return toHex([0, 1, 2].map((i) => f[i] * a + b[i] * (1 - a)));
}

root.BusmapColor = {
  parseHex, toHex, informative, normalise, deltaE, contrast, luminance, oklab, oklch, oklchToRgb, over,
};
})(typeof window !== 'undefined' ? window : globalThis);

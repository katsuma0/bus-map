// --capture canvas (spec C3): canvas.toDataURL('image/png') in the page,
// base64-decoded here and piped into the lossless RGB intermediate. The same
// per-frame capture is today's --capture canvas, which the legacy render
// loop in render_video.mjs still uses unchanged.

import { pngSegment } from './common.mjs';

// One frame: { T, drawMs, png }. drawMs is the time spent issuing canvas
// commands in the page; Chromium rasterises them lazily, so most of the real
// drawing cost shows up under capture instead.
export async function captureFrame(page, i) {
  const { T, drawMs, data } = await page.evaluate((n) => {
    const t0 = performance.now();
    const T = window.busmap.renderFrame(n);
    const drawMs = performance.now() - t0;
    const c = window.busmap.canvas || document.querySelector('canvas');
    return { T, drawMs, data: c.toDataURL('image/png') };
  }, i);
  return { T, drawMs, png: Buffer.from(data.slice(data.indexOf(',') + 1), 'base64') };
}

export default {
  name: 'canvas',
  maxJobs: 1,
  async segment(ctx) {
    return pngSegment(ctx, captureFrame, async (page, i, png) => png);
  },
};

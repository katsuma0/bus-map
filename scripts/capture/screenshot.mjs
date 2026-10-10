// --capture screenshot (spec C3): page.screenshot() of the 1080x1920 clip,
// today's default capture. The PNGs go into the lossless RGB intermediate;
// the legacy render loop in render_video.mjs uses captureFrame unchanged.

import { HEIGHT, WIDTH, pngFromDataUrl, pngSegment } from './common.mjs';

// One frame: { T, drawMs, png }.
export async function captureFrame(page, i) {
  const { T, drawMs } = await page.evaluate((n) => {
    const t0 = performance.now();
    const T = window.busmap.renderFrame(n);
    return { T, drawMs: performance.now() - t0 };
  }, i);
  const png = await page.screenshot({
    type: 'png',
    clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT },
    animations: 'disabled',
    caret: 'hide',
    timeout: 30000,
  });
  return { T, drawMs, png };
}

export default {
  name: 'screenshot',
  maxJobs: 1,
  async segment(ctx) {
    // Review frames are the canvas's own PNG, which the screenshot matches
    // pixel for pixel on this renderer but is not guaranteed to everywhere.
    return pngSegment(ctx, captureFrame, async (page) => pngFromDataUrl(await page.evaluate(() => {
      const c = window.busmap.canvas || document.querySelector('canvas');
      return c.toDataURL('image/png');
    })));
  },
};

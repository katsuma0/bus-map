#!/bin/sh
# Every renderer test of part B (spec F3 B-2 to B-12) in one go. The GTA checks
# use build/<id>/ when make.py has built it, else the stubs; build those first
# with: python3 -I tests/web/stub_gta_v4.py
# B-1 is tests/legacy/check.py --part B.
set -u
cd "$(dirname "$0")/../.."
status=0
run() {
  echo "== $*"
  "$@" || status=1
}
run node tests/web/color_vectors.mjs
run node tests/web/themes_check.mjs
run python3 -I tests/web/test_fonts.py
run node tests/web/v4_smoke.mjs --via-render
run node tests/web/v4_loop.mjs
run node tests/web/v4_knobs.mjs
run node tests/web/v4_hud.mjs
run node tests/web/v4_perf.mjs
echo "== renderer tests: $([ $status -eq 0 ] && echo PASS || echo FAIL)"
exit $status

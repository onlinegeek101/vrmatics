# Issue-fixer loop: authority + closing rules

## What the fixer may change
Fixes land at whichever layer is actually wrong:

1. **The source DXF** (`plan-to-vr/plans-src/home-l*.dxf`) — the in-repo,
   PII-scrubbed source of truth. Drafting errors (mis-layered geometry,
   erroneous symbols, stray linework) are fixed by **editing the DXF
   directly with ezdxf and committing the change** — the git diff IS the
   record. No massage scripts. Keep edits minimal and note in the commit
   what moved/why. Never re-introduce title-block/owner text.
2. **The deterministic pipeline** (`parser/*.py`) — when the drawing is
   right but extraction is wrong, fix the rule, never per-coordinate.
3. **Semantic ground truth** (`parser/corrections/dxf-l*.json`) — facts
   geometry can't carry (stair down/direction, forced flights, camera).
4. **The viewer** (`viewer/index.html`) — rendering/interaction bugs.

After any of 1-3: regenerate BOTH floors with parser/dxf2plan.py, run the
PII grep (pavlu|2611|shelburne|wyman|design.planning) on the JSONs, and
check opening invariants before pushing.

## Closing validated fixes
Close an issue (state_reason `completed`) ONLY after validating the
defect is gone in regenerated output/build, with a proof comment:

- one-line root cause -> what changed (commit sha on main) -> proof
  image(s) -> "reopen if it persists after hard refresh (build tag >= sha)".
- **VR render (EVERY close)** -> a headless-Playwright screenshot of the
  actual 3D viewer positioned at the note's RECORDED VANTAGE: its (x, y)
  plan-inches and yaw (world x=(px-cx)*0.0254, z=-(py-cy)*0.0254 via
  `PLAN2VR_DRAW.planOffset`, `dolly.rotation.y = yaw`). For a fix with a
  visible before/after, include BOTH a before render (viewer loaded with
  the plan state just before the fix commit) and an after render (current
  main).
- **DXF / parser / plan-data fixes** -> ALSO attach a FULL-FLOOR
  color-coded map: the ENTIRE floor plan rendered over its underlay PNG
  (grey walls, green doors, blue windows, red cased openings, purple
  stairs) as a **before/after pair** so the change is unmistakable — full
  extent, not a partial zoom/crop; mark or arrow the changed feature.
- **Viewer-only fixes** -> the VR render before/after is sufficient (no
  color map needed).
- Commit proof images under `vr-notes/proof/<issue>/` and embed via
  raw.githubusercontent URLs.
- **Deploy gate**: do not close until a successful Pages run includes
  your sha as ancestor (note bursts cancel deploys; check the newest
  successful run, not just yours).

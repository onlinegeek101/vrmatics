# plan-to-vr

Convert 2D architectural DXF floor plans into a browser-based VR walkthrough
you can step into on a Meta Quest — or explore with WASD + mouse on a desktop.

```
DXF floor plan ──▶ parser/extract.py ──▶ plan.json ──▶ viewer/index.html (WebXR)
```

```
plan-to-vr/
  sample/floorplan.dxf     # real-world sample plan (see provenance below)
  sample/fetch_sample.py   # re-download the sample if you don't want it in git
  parser/extract.py        # DXF -> plan.json (pure geometry, no AI)
  viewer/index.html        # single-file Three.js/WebXR walkthrough
```

## Requirements

- Python 3.10+ and `ezdxf`:

  ```bash
  pip install ezdxf
  ```

- Any modern browser for the desktop walkthrough; a Meta Quest (or other
  WebXR headset) for VR. No build step, no npm — the viewer is one HTML file
  that pulls Three.js from a CDN.

## Zero-setup: just open it

The parsed result for the sample is checked in (`viewer/plan.json` plus an
embedded copy in `viewer/plan.js`), so you can simply **double-click
`viewer/index.html`** — no server, no Python. The desktop walkthrough works
directly from disk (internet access is needed once for the Three.js CDN).
VR requires serving over HTTP — browsers only expose WebXR to pages with an
origin — so for the Quest follow the steps below.

## Quick start (full flow)

```bash
cd plan-to-vr

# 1. Parse the sample DXF into plan.json, written next to the viewer
python parser/extract.py sample/floorplan.dxf -o viewer/plan.json \
    --wall-layers A-WALL --door-layers A-OPENING --window-layers A-OPENING \
    --fixture-layers A-CASE-1,A-FIXTURE

# 2. Serve the viewer
cd viewer
python -m http.server 8000
```

Then open <http://localhost:8000> — click the page to grab the mouse, walk
with **WASD**, look with the mouse, press **C** to toggle the ceiling and
**F** to toggle furniture. You can also drag-and-drop any other `plan.json`
onto the page.

Expected parser summary for the sample:

```
Parsed sample/floorplan.dxf -> viewer/plan.json
  drawing units       : inches (auto-detected)
  input wall segments : 195
  walls found         : 25
  openings matched    : 36 (11 door, 8 opening, 17 window) from 97 hints
  fixtures found      : 6
  gaps snapped        : 0
  gaps filled (breaks): 10
  orphan lines skipped: 37
  short pieces dropped: 4
  warnings            : 40 (see 'warnings' in viewer/plan.json)
```

What you should see in the browser: a single-story ranch house at 1:1 scale —
white walls with door openings cut to 6'8", windows floating between a 30"
sill and the header, wide cased openings between living spaces, the 16'
garage-door opening, a wood-toned floor slab, and grey-blue furniture
stand-ins (fridge, washer/dryer, kitchen counters, tubs, toilets) built from
the drawing's own fixture blocks. A HUD in the top-left shows wall / opening
/ fixture counts and any parser warnings.

A second test file with completely different conventions is included —
metric (meters), walls on `wall high` / `wall low`, openings all on one
`doorswindows` layer:

```bash
python parser/extract.py sample/ceco-metric.dxf -o viewer/plan.json \
    --wall-layers "wall high,wall low" --door-layers doorswindows \
    --window-layers doorswindows --fixture-layers equipment
```

## Viewing on a Meta Quest

1. Make sure the PC running `python -m http.server 8000` and the Quest are
   on the **same wifi network**.
2. Find your PC's LAN IP (`ipconfig` on Windows, `ip addr` / `ifconfig` on
   Linux/macOS) — say it's `192.168.1.42`.
3. In the Quest's **Browser**, visit `http://192.168.1.42:8000`.
4. Tap **Enter VR** at the bottom of the page.
5. Locomotion: push the **left thumbstick forward** to aim the teleport arc,
   release to jump there; flick the **right thumbstick** left/right for 45°
   snap turns.

> WebXR normally requires HTTPS, but plain-HTTP works for LAN addresses in
> the Quest Browser. If Enter VR is greyed out, tunnel through
> `adb reverse tcp:8000 tcp:8000` and use `http://localhost:8000`, or serve
> over HTTPS.

## The sample plan

`sample/floorplan.dxf` is a **real residential floor plan** (a single-story
house with garage, saved from AutoCAD 2004), taken from the MIT-licensed
[jscad/sample-files](https://github.com/jscad/sample-files) repository
(`dxf/dxf-parser/floorplan.dxf`). It is realistically messy:

- walls drawn as **parallel line pairs** (6" exterior / 4" interior) on an
  xref-bound layer `xref-Bishop-Overland-08$0$A-WALL`
- door swings drawn as **arcs**, window glazing as **lines**, both on
  `...$A-OPENING` — no tidy door/window blocks
- wall lines broken wherever another wall meets them
- dimensions, notes, plumbing fixtures, roof/structural layers as noise
- a units header that **lies** ($INSUNITS says millimeters; the geometry is
  inches) — which is why the parser measures instead of trusting it
- a bathroom detail vignette drawn beside the plan (correctly skipped by
  the fixture bounds filter)

`python sample/fetch_sample.py` re-downloads it from the source.

`sample/ceco-metric.dxf` is a second real-world plan from the MIT-licensed
[bjnortier/dxf](https://github.com/bjnortier/dxf) test suite
(`Ceco.NET-Architecture-Tm-53.dxf`): metric (meters), different layer
conventions, site/topography noise. Useful as a units + layer-scheme
counter-test.

Looking for more test plans? Freely downloadable, genuinely realistic DXFs
are rare — most CAD sites serve DWG behind logins. Reasonable sources:
[dwgvieweronline samples](https://dwgvieweronline.com/samples),
[freecadfloorplans.com](https://freecadfloorplans.com/),
[cadbull](https://cadbull.com/Architecture-House-Plan-CAD-Drawings),
[concepthome sample files](https://www.concepthome.com/how-it-works/sample-files/).
DWG files can be converted to DXF with the free ODA File Converter, then fed
straight to the parser.

## Parser

```
python parser/extract.py INPUT.dxf -o plan.json
    [--wall-layers A-WALL]      comma-separated wall layer names
    [--door-layers A-DOOR]      layers holding door evidence (arcs/blocks)
    [--window-layers A-GLAZ]    layers holding window evidence (lines/blocks)
    [--fixture-layers ...]      layers whose block INSERTs become furniture
    [--units auto]              auto | inches | feet | mm | cm | m
    [--tolerance 2.0]           endpoint snap tolerance, inches
    [--max-wall 12.0]           max wall thickness when pairing lines, inches
```

**Units are measured, not trusted.** With `--units auto` (the default) the
parser pairs wall lines under each unit hypothesis and keeps the one where
walls come out long-and-thin with a plausible thickness (3"–14"); the DXF
header is only a tie-breaker, because real exports routinely lie about it.
Output is always inches (`drawing_units` in the JSON records what was
detected); the viewer converts inches → meters.

**Furnishing.** Blocks inserted on the `--fixture-layers` (casework,
appliances, plumbing, furniture) become `fixtures` entries: footprint from
the block's bounding box, rotation from the insert, and a stand-in height
picked from the block name (REF → 66", WASHER/DRYER → 38", counters → 36",
TUB → 22", TOILET → 15", BED → 24", SOFA → 30", ...). Fixtures landing
outside the walls' bounding box (legend symbols, detail vignettes) are
skipped. The viewer renders them as simple grey-blue volumes — enough to
read the rooms in VR without any asset library.

Layer matching is **case-insensitive and xref-aware**: `A-WALL` also matches
`xref-house$0$A-WALL`, so bound-xref exports (and DataCAD layer schemes)
work without spelling out the full prefix. Door and window layers may be the
same layer — geometry disambiguates.

How it works (pure geometry, deterministic):

1. Collect LINE/LWPOLYLINE segments on the wall layers.
2. Snap endpoints within `--tolerance` to close small drafting gaps.
3. Pair facing parallel segments (≤ `--max-wall` apart) into wall pieces
   with a centerline + thickness; leftover segments are reported as orphans.
4. Merge collinear pieces into walls; the spaces between pieces become
   candidate openings.
5. Classify each candidate from nearby geometry on the opening layers:
   - a door **swing arc** whose radius matches the gap width → `door`
     (cut floor-to-6'8")
   - **glazing lines** running parallel to the wall inside the gap, or a
     window block → `window` (30" sill to 6'8" head)
   - wide gaps with no evidence → `opening` (full-height cased opening —
     archways, pass-throughs, the garage opening)
   - narrow gaps with no evidence → filled back in (they're the breaks
     drafters leave where a crossing wall meets)
6. Write `plan.json`; anything unresolvable lands in its `warnings` array —
   the parser never crashes on messy input.

Output format:

```json
{
  "units": "inches",
  "drawing_units": "m",
  "walls":    [{"start": [x,y], "end": [x,y], "thickness": 6.0, "height": 96.0}],
  "openings": [{"wall_index": 0, "position": 0.45, "width": 36.0,
                "type": "door", "sill": 0, "head": 80.0}],
  "fixtures": [{"name": "FIXT-SNGREF30", "center": [x,y], "rotation": 270.0,
                "size": [30.0, 30.0], "height": 66.0}],
  "warnings": ["orphan line not paired into a wall: ..."]
}
```

`position` is the opening's center as a 0–1 fraction along the wall
centerline. `type` is `door`, `window`, or `opening`.

## Viewer

`viewer/index.html` is self-contained: Three.js r160 via es-module-shims +
importmap from a CDN, no build step. It:

- fetches `plan.json` from its own directory; falls back to the embedded
  `plan.js` copy when opened via `file://`, and accepts drag-and-drop
- converts inches → meters (1 scene unit = 1 m) and recenters the plan
- builds each wall as segmented boxes split at openings (solid pieces,
  lintels above doors, sills below windows — no CSG)
- adds a wood-toned floor slab, a ceiling (toggle **C**), and furniture
  stand-ins (toggle **F**)
- hemisphere + directional light with soft shadows
- desktop: PointerLockControls, WASD + mouse look
- mobile/touch: 1-finger look, **2-finger pan** to move, **pinch** to walk
  forward/back, **long-press** to return to the start position
- a plan picker in the HUD switches between the bundled sample plans
- VR: standard `VRButton`; left-stick arc teleport, right-stick 45° snap turn
- desktop HUD with wall/opening counts and parser warnings

## Swapping in your own drawings (e.g. DataCAD exports)

Export to DXF (R2010 or later works well), then point the flags at your
office's layer names:

```bash
python parser/extract.py myhouse.dxf -o viewer/plan.json \
    --wall-layers A-WALL,A-WALL-EXT --door-layers A-DOOR --window-layers A-GLAZ \
    --fixture-layers A-FURN,A-FIXTURE,A-EQPM --tolerance 2.0
```

For a renovation set (existing / demo / new wall layers), pass only the
layers for the state you want to walk through — e.g. existing + new but not
demo — and run the parser once per floor for multi-story projects (one DXF
per floor is the cleanest DataCAD export).

Check the summary: a high orphan count usually means the wall layers are
wrong or walls are drawn as single lines (not pairs); `gaps snapped` > 0
means sloppy corners were auto-closed; unexplained openings land in
`warnings` rather than silently disappearing.

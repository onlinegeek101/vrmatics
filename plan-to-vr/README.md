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

## Quick start (full flow)

```bash
cd plan-to-vr

# 1. Parse the sample DXF into plan.json, written next to the viewer
python parser/extract.py sample/floorplan.dxf -o viewer/plan.json \
    --wall-layers A-WALL --door-layers A-OPENING --window-layers A-OPENING

# 2. Serve the viewer
cd viewer
python -m http.server 8000
```

Then open <http://localhost:8000> — click the page to grab the mouse, walk
with **WASD**, look with the mouse, press **C** to toggle the ceiling.
You can also drag-and-drop any other `plan.json` onto the page.

Expected parser summary for the sample:

```
Parsed sample/floorplan.dxf -> viewer/plan.json
  input wall segments : 195
  walls found         : 25
  openings matched    : 36 (11 door, 8 opening, 17 window) from 97 hints
  gaps snapped        : 0
  gaps filled (breaks): 10
  orphan lines skipped: 37
  short pieces dropped: 4
  warnings            : 39 (see 'warnings' in viewer/plan.json)
```

What you should see in the browser: a single-story ranch house at 1:1 scale —
white walls with door openings cut to 6'8", windows floating between a 30"
sill and the header, wide cased openings between living spaces, the 16'
garage-door opening, and a wood-toned floor slab. A HUD in the top-left
shows wall/opening counts and any parser warnings.

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

`python sample/fetch_sample.py` re-downloads it from the source.

## Parser

```
python parser/extract.py INPUT.dxf -o plan.json
    [--wall-layers A-WALL]      comma-separated wall layer names
    [--door-layers A-DOOR]      layers holding door evidence (arcs/blocks)
    [--window-layers A-GLAZ]    layers holding window evidence (lines/blocks)
    [--tolerance 2.0]           endpoint snap tolerance, drawing units
    [--max-wall 12.0]           max wall thickness when pairing lines
```

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
  "walls":    [{"start": [x,y], "end": [x,y], "thickness": 6.0, "height": 96.0}],
  "openings": [{"wall_index": 0, "position": 0.45, "width": 36.0,
                "type": "door", "sill": 0, "head": 80.0}],
  "warnings": ["orphan line not paired into a wall: ..."]
}
```

`position` is the opening's center as a 0–1 fraction along the wall
centerline. `type` is `door`, `window`, or `opening`.

## Viewer

`viewer/index.html` is self-contained: Three.js r160 via es-module-shims +
importmap from a CDN, no build step. It:

- fetches `plan.json` from its own directory (or accepts drag-and-drop)
- converts inches → meters (1 scene unit = 1 m) and recenters the plan
- builds each wall as segmented boxes split at openings (solid pieces,
  lintels above doors, sills below windows — no CSG)
- adds a wood-toned floor slab and a ceiling (toggle **C**, desktop only)
- hemisphere + directional light with soft shadows
- desktop: PointerLockControls, WASD + mouse look
- VR: standard `VRButton`; left-stick arc teleport, right-stick 45° snap turn
- desktop HUD with wall/opening counts and parser warnings

## Swapping in your own drawings (e.g. DataCAD exports)

Export to DXF (R2010 or later works well), then point the flags at your
office's layer names:

```bash
python parser/extract.py myhouse.dxf -o viewer/plan.json \
    --wall-layers A-WALL,A-WALL-EXT --door-layers A-DOOR --window-layers A-GLAZ \
    --tolerance 2.0
```

Check the summary: a high orphan count usually means the wall layers are
wrong or walls are drawn as single lines (not pairs); `gaps snapped` > 0
means sloppy corners were auto-closed; unexplained openings land in
`warnings` rather than silently disappearing.

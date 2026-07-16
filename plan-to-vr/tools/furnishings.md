# Furnishings layer: assets + stubs

Featured furnishings (fireplaces, cubbies, sofas...) are GT-sidecar
entries (`furnishings` in corrections/dxf-l*.json) that ride the
fixtures pipeline - placement, rendering, collision. Two tiers:

1. **Real asset**: a `.glb` in `viewer/assets/` is loaded at RUNTIME by
   the viewer (`GLTFLoader`, vendored `three` under `viewer/vendor/` -
   same version, NOT a CDN), **sized to the schematic it replaces** and
   seated on the floor. The plan footprint matches exactly - model +x maps
   to the fixture width, +z to its depth (author models length-along-x,
   depth-along-z) - so the asset reads at the same size as the DXF outline
   from above and shares its collision capsule; height scales with the
   footprint (geometric mean, capped to the schematic height). Anything
   that fails to load (missing file, blocked host) falls back to the
   schematic DXF-outline render, so the model is at worst the linework it
   replaces. Assets are committed to the
   repo, so they MUST be redistributable:
   - Poly Haven (CC0, PBR furniture/props, quality first choice)
   - Kenney.nl furniture kit + KayKit / Quaternius (CC0 low-poly)
   - Sketchfab CC0-filtered as fallback (verify license per item)
   Keep each GLB Quest-friendly: < ~2 MB, < ~50k tris; strip textures
   to 1k; record source+license in `viewer/assets/CREDITS.md`.

   **Attaching an asset (survives regeneration).** Placement stays
   DXF-authoritative - a model only changes how a mined fixture *looks*,
   never where it sits. Add a location entry to the `assets` list in
   `corrections/dxf-l*.json`; `dxf2plan.py` tags the nearest surviving
   fixture (after dedup) with the `asset` path on every rebuild:

   ```json
   "assets": [
     {"near": [142, -256], "asset": "assets/sofa.glb", "rotation": 0}
   ]
   ```

   `near` = plan-inch fixture center (read it off the 2D furnishings map),
   `rotation` (optional, deg) reorients the model to face the room, `tol`
   (optional, default 60in) bounds the match. Drop the `.glb` in, add the
   entry, rerun `dxf2plan.py` - no code change, no loader rebuild. The
   sandbox proxy blocks asset CDNs (Poly Haven/Kenney/GitHub raw), so
   download real models OUTSIDE the sandbox; the seed `sofa.glb` (leather
   sectional, living room) is authored in-repo as the pipeline demo.

   **Swapping a model at a fixture (per-fixture, no rebuild).** Point at a
   furniture fixture and open its picker - hold the right trigger on it in
   VR (wheel), crosshair-click it on desktop, or long-press it on touch
   (2D menu). The picker offers the catalog models **relevant to that
   fixture** (see the tag catalog below) plus *schematic* (force the
   DXF-outline render), *use default* (drop the override), and *cancel*.
   Picking one re-renders just that fixture and persists per plan in
   `localStorage` (`plan2vr-fixture-assets:<plan>`) - the GT `assets` list
   stays the authoritative baseline; the picker is a walkthrough-time
   override on top of it. Exposed on `window.PLAN2VR_FIXT`
   (`commitFixtureAsset`, `openFixtureMenu`, `fixtures()`,
   `assetsForFixture`...) so the swap is scriptable / headless-testable
   (WebXR can't run headless).

   **The tag catalog (`viewer/assets/assets.json`).** A checked-in list of
   candidate models; each fixture surfaces only the entries whose `tags`
   overlap its kind (fridge->fridges, sofa->sofas; an untagged entry is
   generic and always offered). `id` is a repo path OR any URL - a URL is
   fetched **on demand** the first time that model is picked.

   ```json
   [
    {"id": "assets/sofa.glb", "label": "leather sofa", "icon": "sofa",
     "tags": ["sofa","seating","drawn"], "license": "CC0-1.0"}
   ]
   ```

   Fixture->tag mapping lives in `fixtureTags()` in the viewer (derived
   from the fixture name: `FURN-FRIDGE`->fridge, `FURN-TOILET`->toilet,
   generic `FURN-DRAWN`->seating/table...). Add tags there when you add a
   new fixture kind.

   **Curating models (e.g. Meshy free / Poly Haven / Kenney).** For each
   model: (1) confirm the license permits **redistribution** (repo is
   public) and record it in `CREDITS.md`; (2) add a catalog entry with the
   right `tags`. On the `id`:
   - **Local file (recommended, most robust):** commit the `.glb` under
     `viewer/assets/` and point `id` at it. No CORS/expiry/auth risk.
   - **Remote URL:** only if the host serves the raw `.glb` **publicly with
     CORS** and a stable link. Note: Meshy's *free/community library*
     downloads are **account-gated and quota-limited** - those URLs are
     behind a login (signed/expiring), so they will 403 when hotlinked from
     the page; download the GLB (signed in) and commit it as a local file
     instead. Meshy's *generation API* is a different thing (bearer key,
     paid Pro tier, async) and can't be called from this static public page
     without a key-holding proxy - do NOT put an API key in the repo.
   Keep each GLB Quest-friendly: < ~2 MB, < ~50k tris.
2. **Stub block**: no good open-source match yet -> `"stub": "<label>"`
   renders a massing box with a floating NEED-ASSET label naming what to
   find (and which inspiration photo it matches). The living-room leather
   sectional now loads `assets/sofa.glb` (pipeline demo); the two-sided
   fireplace and other unlabeled drawn furniture stay stubs until a CC0
   model is vendored for each.

Owner inspiration photos map (confirmed room by room in chat, 2026-07-14):
kitchen granite/maple 3606/3607/3614 - playroom chartreuse+mural
3603/3604/3605 - reading room (sheet SUNROOM) teal + fieldstone 3608-3610 -
living room cherry fireplace 3611 - front entry leaded-oval oak door 3612 -
dining arched lattice window 3613 - mudroom cubbies/tile 3601/3602 -
sunroom door 3599.

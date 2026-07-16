# Furnishings layer: assets + stubs

Featured furnishings (fireplaces, cubbies, sofas...) are GT-sidecar
entries (`furnishings` in corrections/dxf-l*.json) that ride the
fixtures pipeline - placement, rendering, collision. Two tiers:

1. **Real asset**: a `.glb` in `viewer/assets/` is loaded at RUNTIME by
   the viewer (`GLTFLoader`, vendored `three` under `viewer/vendor/` -
   same version, NOT a CDN), uniformly scaled to the fixture's footprint
   and seated on the floor. Anything that fails to load (missing file,
   blocked host) falls back to the schematic DXF-outline render, so the
   model is at worst the linework it replaces. Assets are committed to the
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

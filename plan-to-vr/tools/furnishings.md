# Furnishings layer: assets + stubs

Featured furnishings (fireplaces, cubbies, sofas...) are GT-sidecar
entries (`furnishings` in corrections/dxf-l*.json) that ride the
fixtures pipeline - placement, rendering, collision. Two tiers:

1. **Real asset**: entry gets `"asset": "assets/<file>.glb"` and the
   viewer loads it (GLTFLoader vendored, same three version). Assets are
   committed to the repo, so they MUST be redistributable:
   - Poly Haven (CC0, PBR furniture/props, quality first choice)
   - Kenney.nl furniture kit + KayKit / Quaternius (CC0 low-poly)
   - Sketchfab CC0-filtered as fallback (verify license per item)
   Keep each GLB Quest-friendly: < ~2 MB, < ~50k tris; strip textures
   to 1k; note source+license in assets/CREDITS.md.
2. **Stub block**: no good open-source match yet -> `"stub": "<label>"`
   renders a massing box with a floating NEED-ASSET label naming what to
   find (and which inspiration photo it matches). Current stubs: the
   living/sunroom two-sided fireplace (photos 3610/3611), mudroom
   cubbies (3602), leather sectional (3611).

Owner inspiration photos map (confirmed room by room in chat, 2026-07-14):
kitchen granite/maple 3606/3607/3614 - playroom chartreuse+mural
3603/3604/3605 - reading room (sheet SUNROOM) teal + fieldstone 3608-3610 -
living room cherry fireplace 3611 - front entry leaded-oval oak door 3612 -
dining arched lattice window 3613 - mudroom cubbies/tile 3601/3602 -
sunroom door 3599.

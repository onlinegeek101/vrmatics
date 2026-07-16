# Furnishing assets — sources & licenses

Every `.glb` in this folder is loaded at runtime by the viewer (`GLTFLoader`,
vendored `three`) and swapped onto a DXF-placed fixture via the `asset` field
(see `../../tools/furnishings.md`). List every file here with its source and
license so the set stays auditable — this repo is public.

| File | What | Source | License | Tris | Size |
|------|------|--------|---------|------|------|
| `sofa.glb` | Leather sectional (living room) | Authored in-repo (`trimesh` primitives, `parser/`-adjacent one-off) | CC0-1.0 (public domain) | 192 | 5.3 KB |

## Adding a vendored asset

Keep each file **< 2 MB / < 50k tris**, `.glb` (self-contained — geometry +
materials + textures in one binary), and record it in the table above with a
real source + license. Preferred license order: **CC0** (Poly Haven, Kenney,
Quaternius, KayKit) → CC-BY (with attribution kept here) → self-authored CC0.
No CC-BY-NC / no unlicensed Sketchfab rips.

## Sandbox note

The build sandbox's egress proxy blocks the asset CDNs (Poly Haven `000`,
Kenney / GitHub raw `403`, unpkg `000`); only npm and pypi are allowlisted.
So `three` + `GLTFLoader` are vendored from npm under `../vendor/`, and the
seed `sofa.glb` is authored in-repo rather than downloaded. To vendor a real
CC0 model, download it **outside** the sandbox, drop the `.glb` here, add the
row above, and point an `asset` entry at it — no code change, no rebuild of
the loader.

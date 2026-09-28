# Furnishing assets — sources & licenses

The viewer loads GLB models at runtime (`GLTFLoader`, vendored `three`) and
swaps them onto DXF-placed fixtures. Models come from the **tag catalog**
`assets.json` (each fixture surfaces the entries whose `tags` match it) and/or
a GT `assets` entry (see `../../tools/furnishings.md`). A catalog `id` may be
a **local file in this folder** or a **remote URL** fetched on demand.

List **every model that ships in the repo or is referenced by `assets.json`**
here with its source + license so the set stays auditable — this repo is
public, so everything must be redistributable.

All current models are **procedural CC0 placeholders** authored in-repo with
`trimesh` (the sandbox can't reach model CDNs). They're recognizable low-poly
stand-ins; swap any for a real CC0/CC-BY model by replacing the file and
updating its row — the catalog entry (`assets.json`) doesn't change.

| File / URL | What | Source | License | Tris | Size |
|------------|------|--------|---------|------|------|
| `stool.glb`      | Bar stool (kitchen island stools) | [Poly Haven — bar_chair_round_01](https://polyhaven.com/a/bar_chair_round_01), decimated 14.4k→2.6k tris, 512px WebP textures (`gltf-transform`) | CC0-1.0 | 2,586 | 164 KB |
| `nightstand.glb` | Bedside table | [Poly Haven — side_table_01](https://polyhaven.com/a/side_table_01), 512px WebP textures | CC0-1.0 | 2,756 | 136 KB |
| `table-wood.glb` | Wooden table (coffee bar) | [Poly Haven — painted_wooden_table](https://polyhaven.com/a/painted_wooden_table), 512px WebP textures | CC0-1.0 | 600 | 61 KB |
| `sofa.glb`       | Leather sectional (seating) | Authored in-repo (`trimesh`) | CC0-1.0 | 192 | 5.3 KB |
| `bed.glb`        | Bed (frame + headboard)     | Authored in-repo (`trimesh`) | CC0-1.0 | 108 | 3.4 KB |
| `table.glb`      | Dining table                | Authored in-repo (`trimesh`) | CC0-1.0 |  72 | 2.6 KB |
| `chair.glb`      | Chair                       | Authored in-repo (`trimesh`) | CC0-1.0 |  72 | 2.6 KB |
| `fridge.glb`     | Refrigerator                | Authored in-repo (`trimesh`) | CC0-1.0 |  60 | 2.3 KB |
| `range.glb`      | Range / stove               | Authored in-repo (`trimesh`) | CC0-1.0 | 316 | 7.6 KB |
| `dishwasher.glb` | Dishwasher                  | Authored in-repo (`trimesh`) | CC0-1.0 |  36 | 1.8 KB |
| `washer.glb`     | Washer                      | Authored in-repo (`trimesh`) | CC0-1.0 |  76 | 2.6 KB |
| `dryer.glb`      | Dryer                       | Authored in-repo (`trimesh`) | CC0-1.0 |  76 | 2.6 KB |
| `toilet.glb`     | Toilet                      | Authored in-repo (`trimesh`) | CC0-1.0 | 100 | 3.1 KB |
| `vanity.glb`     | Vanity / sink               | Authored in-repo (`trimesh`) | CC0-1.0 |  84 | 2.9 KB |
| `shower.glb`     | Shower stall                | Authored in-repo (`trimesh`) | CC0-1.0 |  60 | 2.3 KB |
| `tub.glb`        | Bathtub                     | Authored in-repo (`trimesh`) | CC0-1.0 |  36 | 1.8 KB |

The v2 viewer builds most fixtures procedurally at their drawn size (code in
`v2.html`, no asset): stainless French-door fridge, 36in gas range, dishwasher,
ice machine, front-load washer/dryer, vanities with undermount basins,
kitchen/laundry sinks cut into the counters, tubs, made beds, dressers,
closet shelving, TV, bench — and uses the Poly Haven scans above for the bar
stools, bedside tables and the coffee-bar table.

## Surface textures (`tex/`) — v2 viewer

PBR surface maps for the v2 viewer's per-room materials (the owner's palette:
oak plank floors, tile in the wet rooms, concrete garage, clapboard siding,
green-black granite counters). All from **ambientCG** (CC0-1.0), downsized to
≤ 1K and re-tinted in-repo (`numpy`/`PIL`) to the owner's colours. The copies
were taken from public mirrors of the unmodified 1K-JPG sets.

| File | Made from | License | Size |
|------|-----------|---------|------|
| `tex/oak_albedo.jpg`, `oak_normal.jpg`, `oak_rough.jpg` | ambientCG **WoodFloor040** (Color, NormalGL, Roughness), warmed toward v1's honey oak | CC0-1.0 | 1K / 512 / 512 |
| `tex/tile_albedo.jpg`, `tile_normal.jpg` | ambientCG **Tiles002** (Color), recoloured to v1's warm off-white 12in tile + grey grout; normal derived from the grout lines | CC0-1.0 | 512 |
| `tex/concrete_albedo.jpg` | ambientCG **Concrete031** (Color), lightened | CC0-1.0 | 512 |
| `tex/grass_albedo.jpg` | ambientCG **Grass004** (Color) | CC0-1.0 | 512 |
| `tex/siding_albedo.jpg` | ambientCG **WoodSiding009** (Color), greyscale (tinted to v1's siding colour in the shader) | CC0-1.0 | 512x256 |
| `tex/granite_albedo.jpg` | ambientCG **Granite002A** (Color), darkened + tinted green-black | CC0-1.0 | 512 |

## Adding a model

Keep each GLB **< 2 MB / < 50k tris**, self-contained (geometry + materials +
textures in one binary), and record it in the table above with a real source
+ license. Preferred license order: **CC0** (Poly Haven, Kenney, Quaternius,
KayKit) → CC-BY (attribution kept here) → self-authored CC0. No CC-BY-NC / no
unlicensed rips.

- **Local (recommended):** commit the `.glb` here, point the catalog `id` at
  `assets/<file>.glb`. No CORS/expiry/auth risk.
- **Remote URL:** only if the host serves the raw `.glb` publicly with CORS
  and a stable link. **Meshy's free/community library** downloads are
  account-gated / quota-limited — those links sit behind a login and 403 when
  hotlinked, so download the GLB (signed in) and commit it locally instead.
  Meshy's *generation* API needs a paid key + a proxy and must never have its
  key checked into this public repo.

## Sandbox note

The build sandbox's egress proxy blocks the asset CDNs (Poly Haven `000`,
Kenney / GitHub raw `403`, unpkg `000`); only npm and pypi are allowlisted.
So `three` + `GLTFLoader` are vendored from npm under `../vendor/`, and the
seed `sofa.glb` is authored in-repo rather than downloaded. To vendor a real
CC0 model, download it **outside** the sandbox, drop the `.glb` here, add the
row above, and point an `asset` entry at it — no code change, no rebuild of
the loader.

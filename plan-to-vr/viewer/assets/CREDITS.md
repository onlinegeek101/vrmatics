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

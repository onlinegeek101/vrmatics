# Furnishing assets — sources & licenses

The viewer loads GLB models at runtime (`GLTFLoader`, vendored `three`) and
swaps them onto DXF-placed fixtures. Models come from the **tag catalog**
`assets.json` (each fixture surfaces the entries whose `tags` match it) and/or
a GT `assets` entry (see `../../tools/furnishings.md`). A catalog `id` may be
a **local file in this folder** or a **remote URL** fetched on demand.

List **every model that ships in the repo or is referenced by `assets.json`**
here with its source + license so the set stays auditable — this repo is
public, so everything must be redistributable.

| File / URL | What | Source | License | Tris | Size |
|------------|------|--------|---------|------|------|
| `sofa.glb` | Leather sectional (seating) | Authored in-repo (`trimesh` primitives, one-off) | CC0-1.0 (public domain) | 192 | 5.3 KB |

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

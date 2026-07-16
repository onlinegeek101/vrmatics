# Vendored runtime libraries

Pinned copies so the viewer is self-contained — no CDN at load time (the
build sandbox's egress proxy blocks unpkg/jsDelivr), and the `asset` GLB
pipeline uses the same `three` build as the rest of the scene.

| Path | Package | Version | License |
|------|---------|---------|---------|
| `three.module.js`, `addons/**` | [three](https://www.npmjs.com/package/three) | 0.160.0 | MIT |
| `es-module-shims.js` | [es-module-shims](https://www.npmjs.com/package/es-module-shims) | 1.10.0 | MIT |

Sourced via `npm pack` (npm is allowlisted in the sandbox). `addons/` holds
only the modules the viewer imports (controls, environments, geometries,
objects, loaders/GLTFLoader, utils). To bump: `npm pack three@<ver>`, extract,
and copy `build/three.module.js` + the needed `examples/jsm/**` files here,
keeping the importmap in `../index.html` in sync.

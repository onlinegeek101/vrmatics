#!/usr/bin/env python3
"""Convert a sanitized plan JSON (viewer/plans/*.json) into a DXF the v2 viewer
parses natively — so every layout renders through the same DXF pipeline.

The DXF carries exactly the layers v2's parseDXF()/build() consume:
  TOPO    LWPOLYLINE  footprint slab (code 38 = elevation, 0)
  WALL    LINE        wall centrelines
  OPENING LINE        opening span, colour 1=door 5=window 2=cased opening
  STAIR   LWPOLYLINE  stair footprint polygon(s)
  FURN    LWPOLYLINE  fixture footprint + TEXT label (mapped GLB name)

Input is already PII-free (the committed JSON); this is a pure geometry copy,
so the output DXF is equally safe for the public repo.

    python3 tools/json2dxf.py viewer/plans/home-l1.json viewer/plans/home-l1.dxf
"""
import json
import sys


def fixture_asset_name(name):
    """Mirror v2.html fixtureAssetName(): fixture label -> GLB basename ('' = box)."""
    n = (name or '').upper()
    def has(*ss): return any(s in n for s in ss)
    if has('SOFA', 'SECT', 'COUCH', 'LOVE'): return 'sofa'
    if has('BED'): return 'bed'
    if has('COFFEE', 'TABLE', 'DESK', 'DINING'): return 'table'
    if has('CHAIR', 'RECLIN'): return 'chair'
    if has('FRIDGE', 'REFRIG'): return 'fridge'
    if has('RANGE', 'STOVE', 'OVEN', 'COOK'): return 'range'
    if has('DISHWASH'): return 'dishwasher'
    if has('WASHER'): return 'washer'
    if has('DRYER'): return 'dryer'
    if has('TOILET', 'WC'): return 'toilet'
    if has('SHOWER'): return 'shower'
    if has('TUB', 'BATHTUB'): return 'tub'
    if has('VANITY', 'SINK', 'LAV'): return 'vanity'
    return ''


def fmt(v):
    return f'{v:.4f}'


def json2dxf(plan):
    out = ['0', 'SECTION', '2', 'ENTITIES']

    def line(layer, a, b, color=None):
        out.extend(['0', 'LINE', '8', layer])
        if color is not None:
            out.extend(['62', str(color)])
        out.extend(['10', fmt(a[0]), '20', fmt(a[1]), '11', fmt(b[0]), '21', fmt(b[1])])

    def lwpoly(layer, pts, elev=None):
        out.extend(['0', 'LWPOLYLINE', '8', layer])
        if elev is not None:
            out.extend(['38', fmt(elev)])
        for p in pts:
            out.extend(['10', fmt(p[0]), '20', fmt(p[1])])

    def text(layer, pos, s):
        out.extend(['0', 'TEXT', '8', layer, '1', s, '10', fmt(pos[0]), '20', fmt(pos[1])])

    # TOPO — footprint slab at elevation 0
    fp = plan.get('footprint') or []
    if len(fp) >= 3:
        lwpoly('TOPO', fp, elev=0.0)

    # WALL — centrelines
    walls = plan.get('walls') or []
    for w in walls:
        line('WALL', w['start'], w['end'])

    # OPENING — span centred on the wall at 'position', coloured by type
    for o in plan.get('openings') or []:
        w = walls[o['wall_index']] if 0 <= o['wall_index'] < len(walls) else None
        if not w:
            continue
        ax, ay = w['start']
        bx, by = w['end']
        t = o['position']
        cx, cy = ax + (bx - ax) * t, ay + (by - ay) * t
        L = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 or 1.0
        ux, uy = (bx - ax) / L, (by - ay) / L
        half = o['width'] / 2
        color = 1 if o['type'] == 'door' else 5 if o['type'] == 'window' else 2
        line('OPENING', (cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half), color)

    # STAIR — footprint polygons
    for s in plan.get('stairs') or []:
        if s.get('polygon'):
            lwpoly('STAIR', s['polygon'])

    # FURN — footprint rect + label
    for f in plan.get('fixtures') or []:
        cx, cy = f['center']
        w, d = f['size']
        lwpoly('FURN', [(cx - w / 2, cy - d / 2), (cx + w / 2, cy - d / 2),
                        (cx + w / 2, cy + d / 2), (cx - w / 2, cy + d / 2)])
        text('FURN', (cx, cy), fixture_asset_name(f.get('name')))

    out.extend(['0', 'ENDSEC', '0', 'EOF'])
    return '\n'.join(out) + '\n'


if __name__ == '__main__':
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as fh:
        plan = json.load(fh)
    with open(dst, 'w') as fh:
        fh.write(json2dxf(plan))
    print(f'wrote {dst}')

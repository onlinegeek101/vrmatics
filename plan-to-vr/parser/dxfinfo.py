#!/usr/bin/env python3
"""Inspect a DXF's layers so real CAD exports can be mapped to the parser.

DataCAD / AutoCAD files rarely use the AIA `A-WALL`/`A-DOOR`/`A-GLAZ`
layer names extract.py defaults to. Run this first on an unfamiliar
export to see every layer, what entity types live on it, its extent,
and a heuristic guess at its role, then feed the right names into
extract.py's --wall-layers / --door-layers / --window-layers /
--fixture-layers.

Usage:
    python dxfinfo.py plan.dxf
"""
import sys
import math
from collections import Counter, defaultdict

import ezdxf


def seg_len(e):
    try:
        if e.dxftype() == "LINE":
            a, b = e.dxf.start, e.dxf.end
            return math.dist((a[0], a[1]), (b[0], b[1]))
    except Exception:
        pass
    return 0.0


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python dxfinfo.py plan.dxf")
    doc = ezdxf.readfile(sys.argv[1])
    msp = doc.modelspace()

    insunits = doc.header.get("$INSUNITS", 0)
    unit_name = {0: "unitless", 1: "inches", 2: "feet", 4: "mm",
                 5: "cm", 6: "m"}.get(insunits, f"code {insunits}")
    print(f"file        : {sys.argv[1]}")
    print(f"dxf version : {doc.dxfversion} ({doc.acad_release})")
    print(f"$INSUNITS   : {insunits} ({unit_name})")

    per_layer = defaultdict(Counter)        # layer -> {LINE: n, ARC: n, ...}
    line_len = defaultdict(list)            # layer -> [line lengths]
    bbox = defaultdict(lambda: [1e18, 1e18, -1e18, -1e18])
    blocks = Counter()

    for e in msp:
        lyr = e.dxf.layer
        t = e.dxftype()
        per_layer[lyr][t] += 1
        if t == "LINE":
            line_len[lyr].append(seg_len(e))
        if t == "INSERT":
            blocks[e.dxf.name] += 1
        try:
            pts = []
            if t == "LINE":
                pts = [e.dxf.start, e.dxf.end]
            elif t in ("LWPOLYLINE", "POLYLINE"):
                pts = [(p[0], p[1]) for p in e.get_points()] \
                    if t == "LWPOLYLINE" else [v.dxf.location for v in e.vertices]
            elif t in ("CIRCLE", "ARC"):
                c, r = e.dxf.center, e.dxf.radius
                pts = [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
            for p in pts:
                b = bbox[lyr]
                b[0] = min(b[0], p[0]); b[1] = min(b[1], p[1])
                b[2] = max(b[2], p[0]); b[3] = max(b[3], p[1])
        except Exception:
            pass

    def guess(lyr):
        c = per_layer[lyr]
        lines = c.get("LINE", 0) + c.get("LWPOLYLINE", 0) + c.get("POLYLINE", 0)
        arcs = c.get("ARC", 0)
        text = c.get("TEXT", 0) + c.get("MTEXT", 0)
        ins = c.get("INSERT", 0)
        name = lyr.upper()
        if any(k in name for k in ("WALL", "PARTITION", "STRUCT")):
            return "WALLS (by name)"
        if any(k in name for k in ("DOOR",)):
            return "DOORS (by name)"
        if any(k in name for k in ("WIN", "GLAZ", "GLASS")):
            return "WINDOWS (by name)"
        if any(k in name for k in ("DIM", "TEXT", "NOTE", "ANNO")):
            return "annotation/dims (skip)"
        if any(k in name for k in ("FURN", "FIXT", "EQUIP", "APPL", "PLUMB", "CASE")):
            return "fixtures (by name)"
        # geometry heuristics when names are opaque
        lens = sorted(line_len[lyr])
        med = lens[len(lens) // 2] if lens else 0
        if arcs > 3 and arcs > lines * 0.15:
            return f"maybe DOORS (arcs={arcs})"
        if lines > 20 and med > 12:
            return f"maybe WALLS (lines={lines}, median len={med:.0f})"
        if ins > 0:
            return f"blocks/fixtures (inserts={ins})"
        if text > 0 and lines < 5:
            return "annotation/dims (skip)"
        return "?"

    print(f"\nlayers ({len(per_layer)}):")
    print(f"{'layer':28s} {'entities':22s} {'extent (w x h)':20s} guess")
    print("-" * 92)
    for lyr in sorted(per_layer, key=lambda k: -sum(per_layer[k].values())):
        c = per_layer[lyr]
        ent = ", ".join(f"{t}:{n}" for t, n in c.most_common(3))
        b = bbox[lyr]
        ext = f"{b[2]-b[0]:.0f} x {b[3]-b[1]:.0f}" if b[2] > b[0] else "-"
        print(f"{lyr:28s} {ent:22s} {ext:20s} {guess(lyr)}")

    if blocks:
        print(f"\nblock inserts ({sum(blocks.values())} total):")
        for name, n in blocks.most_common(20):
            print(f"  {name:34s} x{n}")

    walls = [l for l in per_layer if "WALL" in l.upper()
             or guess(l).startswith("maybe WALLS")]
    doors = [l for l in per_layer if "DOOR" in l.upper()
             or guess(l).startswith("maybe DOORS")]
    wins = [l for l in per_layer if any(k in l.upper() for k in ("WIN", "GLAZ"))]
    print("\nsuggested extract.py invocation:")
    print(f"  python extract.py {sys.argv[1]} -o plan.json \\")
    if walls:
        print(f"    --wall-layers {','.join(walls)} \\")
    if doors:
        print(f"    --door-layers {','.join(doors)} \\")
    if wins:
        print(f"    --window-layers {','.join(wins)} \\")
    print(f"    --units {unit_name if insunits else 'auto'}")


if __name__ == "__main__":
    main()

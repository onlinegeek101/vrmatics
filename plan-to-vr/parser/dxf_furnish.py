#!/usr/bin/env python3
"""Mine the architect DXF for drawn furnishings -> stub entries.

Two authoritative sources, no photo guessing:

 1. NOTES text labels sit ON the items they describe ("3'6 x 8' island",
    "36\" gas range", "Sink", "DW", "Washer"...). Keyword catalog maps
    each to a footprint (the island even carries its dimensions - parse
    them). Label insert point = item location.
 2. Freestanding linework clusters on the doors/windows layer: furniture
    is drawn as little islands of lines away from any wall (dining set,
    sofa). Union-find on endpoint proximity; keep components that are
    furniture-sized and not already explained (stairs, fireplace,
    fixtures, labeled items).

Writes the proposals into the GT sidecar's "furnishings" with
"auto": true (re-runs replace only auto entries; hand-placed ones and
homeowner labels are never touched).

Usage: dxf_furnish.py plan.dxf corrections.json [--wall-layers ...]
"""
import json
import math
import re
import sys

import ezdxf

# keyword -> (name, w, d, h) footprint inches; None = parse from label
CATALOG = [
    ("ISLAND", "FURN-ISLAND", None, 36.0),
    ("GAS RANGE", "FURN-RANGE", (30, 26), 36.0),
    ("RANGE", "FURN-RANGE", (30, 26), 36.0),
    ("SINK", "FURN-SINK", (30, 22), 36.0),
    ("DW", "FURN-DISHWASHER", (24, 24), 34.0),
    ("REF", "FURN-FRIDGE", (36, 30), 70.0),
    ("WASHER", "FURN-WASHER", (27, 27), 38.0),
    ("DRYER", "FURN-DRYER", (27, 27), 38.0),
    ("COFFEE", "FURN-COFFEE-BAR", (24, 16), 36.0),
    ("BROOM CAB", "FURN-BROOM-CAB", (16, 24), 84.0),
    ("BENCH", "FURN-BENCH", (48, 18), 18.0),
]

DIM = re.compile(r"(\d+)'\s*(\d+)?\"?\s*x\s*(\d+)'\s*(\d+)?", re.I)


def label_items(msp, notes_layer):
    out = []
    for e in msp:
        if e.dxftype() != "TEXT" or e.dxf.layer != notes_layer:
            continue
        t = (e.dxf.text or "").strip()
        up = t.upper()
        for kw, name, size, h in CATALOG:
            if kw in up:
                if size is None:
                    m = DIM.search(t)
                    if m:
                        w = int(m.group(1)) * 12 + int(m.group(2) or 0)
                        d = int(m.group(3)) * 12 + int(m.group(4) or 0)
                        size = (max(w, d), min(w, d))
                    else:
                        size = (96, 42)
                p = e.dxf.insert
                out.append({"name": name, "center": [round(p[0]), round(p[1])],
                            "size": list(size), "height": h,
                            "rotation": 0, "auto": True,
                            "stub": f"{t.strip()} (DXF note label)"})
                break
    return out


def _clusters(msp, dw_layer, wall_layer):
    """Furniture-sized islands of linework away from any wall."""
    walls = [(e.dxf.start[0], e.dxf.start[1], e.dxf.end[0], e.dxf.end[1])
             for e in msp.query("LINE") if e.dxf.layer == wall_layer]

    def near_wall(x, y, thr=7.0):
        for (x0, y0, x1, y1) in walls:
            dx, dy = x1 - x0, y1 - y0
            L2 = dx * dx + dy * dy or 1.0
            t = max(0, min(1, ((x - x0) * dx + (y - y0) * dy) / L2))
            if math.hypot(x - x0 - t * dx, y - y0 - t * dy) < thr:
                return True
        return False

    segs = []
    for e in msp.query("LINE"):
        if e.dxf.layer != dw_layer:
            continue
        (x0, y0, _), (x1, y1, _) = e.dxf.start, e.dxf.end
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if not near_wall(mx, my):
            segs.append((x0, y0, x1, y1))
    n = len(segs)
    par = list(range(n))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            a, b = segs[i], segs[j]
            if min(math.hypot(a[k] - b[m], a[k + 1] - b[m + 1])
                   for k in (0, 2) for m in (0, 2)) < 5.0:
                par[find(i)] = find(j)
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(segs[i])
    return comps.values()


def cluster_items(msp, dw_layer, wall_layer, taken, min_lines=8):
    out = []
    for comp in _clusters(msp, dw_layer, wall_layer):
        if len(comp) < min_lines:
            continue
        xs = [c for s in comp for c in (s[0], s[2])]
        ys = [c for s in comp for c in (s[1], s[3])]
        w, d = max(xs) - min(xs), max(ys) - min(ys)
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        if not (18 <= w <= 150 and 18 <= d <= 150):
            continue
        if any(abs(cx - tx) < (tw + w) / 2 and abs(cy - ty) < (td + d) / 2
               for (tx, ty, tw, td) in taken):
            continue
        out.append({"name": "FURN-DRAWN", "center": [round(cx), round(cy)],
                    "size": [round(w), round(d)], "height": 30.0,
                    "rotation": 0, "auto": True,
                    "stub": "furniture drawn on the sheet (unlabeled linework)"})
    return out


def main():
    dxf, gtf = sys.argv[1], sys.argv[2]
    floor = "1.1"
    doc = ezdxf.readfile(dxf)
    msp = doc.modelspace()
    gt = json.load(open(gtf))
    manual = [f for f in gt.get("furnishings", []) if not f.get("auto")]
    items = label_items(msp, floor + "NOTES")
    taken = [(f["center"][0], f["center"][1], f["size"][0] + 12,
              f["size"][1] + 12) for f in manual + items]
    # stairs + big known masses also count as taken
    for s in gt.get("stairs", []):
        if s.get("bbox"):
            x0, x1, y0, y1 = s["bbox"]
            taken.append(((x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0), abs(y1 - y0)))
    items += cluster_items(msp, floor + "DRWDWS", floor + "WALL", taken)
    taken += [(f["center"][0], f["center"][1], f["size"][0] + 12,
               f["size"][1] + 12) for f in items if f["name"] == "FURN-DRAWN"]
    items += cluster_items(msp, floor + "NOTES", floor + "WALL", taken)
    items += cluster_items(msp, floor + "FURN", floor + "WALL", taken,
                           min_lines=2)
    # keep only items inside the wall bbox (detail vignettes sit outside)
    wx = [c for e in msp.query("LINE") if e.dxf.layer == floor + "WALL"
          for c in (e.dxf.start[0], e.dxf.end[0])]
    wy = [c for e in msp.query("LINE") if e.dxf.layer == floor + "WALL"
          for c in (e.dxf.start[1], e.dxf.end[1])]
    items = [f for f in items
             if min(wx) - 10 < f["center"][0] < max(wx) + 10
             and min(wy) - 10 < f["center"][1] < max(wy) + 10]
    gt["furnishings"] = manual + items
    json.dump(gt, open(gtf, "w"), indent=1)
    print(f"kept {len(manual)} manual, mined {len(items)} auto:")
    for f in items:
        print(f"  {f['name']:18s} at ({f['center'][0]:5d},{f['center'][1]:5d}) "
              f"{f['size'][0]}x{f['size'][1]}  {f['stub'][:48]}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Audit pdf2plan output against the dimensions printed on the sheets.

The binder's title text and room labels carry ground truth the vector
extraction never sees (text plots as stroke fonts): labeled room sizes
and the area schedule. Comparing the reconstructed rooms against those
numbers is the same blind-audit discipline used for ZInD and IFC - the
GT below was transcribed by eye from the rendered sheets, the pipeline
never reads it.

Usage:
    python audit_pavlu.py l1.json l2.json
"""
import json
import math
import sys

# room labels: (floor, name, width_in, depth_in) - order-free dims
GT_ROOMS = [
    (0, "MUDROOM", 7 * 12 + 0, 8 * 12 + 0),
    (0, "PANTRY", 6 * 12 + 9, 8 * 12 + 3),
    (0, "BATHROOM", 8 * 12 + 3, 8 * 12 + 4),
    (0, "LAUNDRY", 6 * 12 + 7, 10 * 12 + 10),
    (1, "BEDROOM NW", 11 * 12 + 8, 15 * 12 + 9),
    (1, "BEDROOM NE", 14 * 12 + 8, 12 * 12 + 6),
    (1, "BATHROOM", 11 * 12 + 8, 8 * 12 + 6),
    (1, "PRIMARY BATHROOM", 13 * 12 + 0, 12 * 12 + 3),
]

# area schedule (sq ft): garage on page 1's schedule, gross total
GT_GARAGE_SQFT = 1248.0
GT_TOTAL_SQFT = 3034.0      # first floor 1786 + garage 1248


def poly_area(pts):
    s = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def room_dims(room):
    xs = [p[0] for p in room["polygon"]]
    ys = [p[1] for p in room["polygon"]]
    return max(xs) - min(xs), max(ys) - min(ys)


def main():
    plans = [json.load(open(p)) for p in sys.argv[1:3]]
    print("=== sheet-label audit (PDF pipeline) ===")

    # labels state clear interior dims; detected polygons follow wall
    # centerlines, one half-thickness out on each side
    T = 5.0
    cands = []
    for gi, (floor, name, gw, gd) in enumerate(GT_ROOMS):
        if floor >= len(plans):
            continue
        want = sorted((gw + T, gd + T))
        for ri, r in enumerate(plans[floor]["rooms"]):
            have = sorted(room_dims(r))
            d = math.hypot(have[0] - want[0], have[1] - want[1])
            cands.append((d, gi, (floor, ri), want, have))
    cands.sort()
    used_g, used_r, picked = set(), set(), {}
    for d, gi, rk, want, have in cands:
        if gi in used_g or rk in used_r:
            continue
        used_g.add(gi)
        used_r.add(rk)
        picked[gi] = (want, have)

    errs = []
    for gi, (floor, name, gw, gd) in enumerate(GT_ROOMS):
        if gi not in picked:
            print(f"  {name:<18} label {gw:.0f}x{gd:.0f}\"  NO MATCH")
            continue
        want, have = picked[gi]
        e = max(abs(have[0] - want[0]), abs(have[1] - want[1]))
        errs.append(e)
        print(f"  {name:<18} label+t {want[0]:.0f}x{want[1]:.0f}\"  "
              f"room {have[0]:.0f}x{have[1]:.0f}\"  err {e:.1f}\"")
    if errs:
        print(f"  room-dimension MAE {sum(errs)/len(errs):.1f}\"  "
              f"max {max(errs):.1f}\"  (n={len(errs)})")

    # garage area
    garages = [r for r in plans[0]["rooms"] if r["kind"] == "garage"]
    if garages:
        a = poly_area(garages[0]["polygon"]) / 144.0
        print(f"  garage area {a:.0f} sqft vs schedule {GT_GARAGE_SQFT:.0f} "
              f"({100 * a / GT_GARAGE_SQFT - 100:+.1f}%)")
    else:
        print("  garage room: NOT DETECTED")

    fp = plans[0].get("footprint") or []
    if fp:
        a = poly_area(fp) / 144.0
        print(f"  L1 footprint {a:.0f} sqft vs gross schedule "
              f"{GT_TOTAL_SQFT:.0f} ({100 * a / GT_TOTAL_SQFT - 100:+.1f}%)")


if __name__ == "__main__":
    main()

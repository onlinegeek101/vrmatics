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


def opening_center(plan, o):
    w = plan["walls"][o["wall_index"]]
    return (w["start"][0] + (w["end"][0] - w["start"][0]) * o["position"],
            w["start"][1] + (w["end"][1] - w["start"][1]) * o["position"])


def audit_openings(plan, dxf_path, tag):
    """Reconcile doors against the sheet's swing arcs and windows against
    the facade: every drawn swing should own a door (position + width +
    orientation), every facade gap should carry glass."""
    import ezdxf
    doors = [o for o in plan["openings"] if o["type"] == "door"]
    wins = [o for o in plan["openings"] if o["type"] == "window"]
    print(f"  [{tag}] doors {len(doors)} ({sum(1 for o in doors if o.get('swing'))} "
          f"with hinge+swing), windows {len(wins)}")

    arcs = []
    for e in ezdxf.readfile(dxf_path).modelspace():
        if e.dxftype() == "ARC":
            arcs.append(((e.dxf.center.x, e.dxf.center.y), e.dxf.radius))
    unmatched = []
    dists = []
    for (cx, cy), r in arcs:
        best = None
        for o in doors:
            gx, gy = opening_center(plan, o)
            d = math.hypot(cx - gx, cy - gy)
            if best is None or d < best[0]:
                best = (d, o)
        if best is None or best[0] > best[1]["width"] * 0.75 + 8:
            unmatched.append((round(cx), round(cy), round(r)))
        else:
            dists.append(best[0])
            # hinge must sit at a jamb: distance ~ half the width
            err = abs(best[0] - best[1]["width"] / 2)
            if err > 8:
                print(f"    hinge offset {err:.0f}\" off-jamb for door at "
                      f"({round(cx)},{round(cy)})")
    if dists:
        print(f"    swing arcs matched to doors: {len(dists)}/{len(arcs)}, "
              f"hinge-to-gap distance mean {sum(dists)/len(dists):.1f}\"")
    for u in unmatched:
        print(f"    arc without a door: center ({u[0]},{u[1]}) r={u[2]}\"")
    widths = sorted(round(o["width"]) for o in doors)
    print(f"    door widths: {widths}")

    fp = plan.get("footprint") or []
    if len(fp) >= 3:
        def inside(px, py):
            hit = False
            for i in range(len(fp)):
                ax, ay = fp[i]
                bx, by = fp[(i + 1) % len(fp)]
                if (ay > py) != (by > py):
                    if ax + (py - ay) * (bx - ax) / (by - ay) > px:
                        hit = not hit
            return hit
        holes = []
        for o in plan["openings"]:
            if o["type"] != "opening":
                continue
            gx, gy = opening_center(plan, o)
            w = plan["walls"][o["wall_index"]]
            dx = w["end"][0] - w["start"][0]
            dy = w["end"][1] - w["start"][1]
            L = math.hypot(dx, dy) or 1
            off = w["thickness"] / 2 + 4
            s = inside(gx - dy / L * off, gy + dx / L * off) + \
                inside(gx + dy / L * off, gy - dx / L * off)
            if s == 1 and o["width"] < 96:
                holes.append((round(gx), round(gy), round(o["width"])))
        if holes:
            print(f"    facade gaps left glass-less: {holes}")
        else:
            print("    facade check: every sub-garage-width exterior gap "
                  "carries glass or a panel")


def main():
    plans = [json.load(open(p)) for p in sys.argv[1:3]]
    print("=== sheet-label audit (PDF pipeline) ===")
    dxfs = [p.replace(".json", ".dxf") for p in sys.argv[1:3]]
    import os
    for plan, dxf, tag in zip(plans, dxfs, ("L1", "L2")):
        if os.path.exists(dxf):
            audit_openings(plan, dxf, tag)

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

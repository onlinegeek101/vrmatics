#!/usr/bin/env python3
"""Audit a converted plan.json against its ZInD tour's ground truth.

ZInD annotates what our pipeline otherwise assumes: true per-opening
vertical extents (sill/head), true per-room ceiling heights, and true
camera heights. This script measures the gap between the projection and
that ground truth, so improvements can be verified blind - rerun after
regenerating the plan and every metric must move toward zero.

Usage:
    python audit_zind.py zind_data.json plan.json [--floor floor_01]

Reports:
  - opening sill/head MAE vs ground truth (doors and windows separately)
  - wall height error vs per-room measured ceilings
  - ground-truth wall coverage (redraw edges represented by plan walls)
  - viewer eye height vs measured camera heights
"""
import argparse
import json
import math

M2IN = 1000.0 / 25.4


def rot(p, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def collect_ground_truth(tour, floor):
    """Return (openings, ceilings, cam_heights) in inches / plan-inch coords.

    openings: [{center:(x,y) inches, width, sill, head, type}]
    ceilings: [ceiling height inches per primary pano]
    """
    floor_scale = tour["scale_meters_per_coordinate"].get(floor)
    if floor_scale is None:
        raise SystemExit(f"floor {floor} has no scale; cannot audit")
    merger = tour["merger"][floor]

    openings, ceilings, cams = [], [], []
    for cr in merger.values():
        for pr in cr.values():
            for pano in pr.values():
                if not isinstance(pano, dict) or "layout_complete" not in pano:
                    continue
                tr = pano["floor_plan_transformation"]
                vscale = tr["scale"] * floor_scale          # metres per n.u.
                if pano.get("is_primary"):
                    ceilings.append(pano["ceiling_height"] * vscale * M2IN)
                    cams.append(pano["camera_height"] * vscale * M2IN)
                lay = pano["layout_complete"]
                for kind, key in (("door", "doors"), ("window", "windows"),
                                  ("opening", "openings")):
                    pts = lay.get(key) or []
                    for i in range(0, len(pts) - 2, 3):
                        left, right, vert = pts[i], pts[i + 1], pts[i + 2]
                        # to global floor coords, then inches
                        gl = rot((left[0] * tr["scale"], left[1] * tr["scale"]),
                                 tr["rotation"])
                        gr = rot((right[0] * tr["scale"], right[1] * tr["scale"]),
                                 tr["rotation"])
                        gl = ((gl[0] + tr["translation"][0]) * floor_scale * M2IN,
                              (gl[1] + tr["translation"][1]) * floor_scale * M2IN)
                        gr = ((gr[0] + tr["translation"][0]) * floor_scale * M2IN,
                              (gr[1] + tr["translation"][1]) * floor_scale * M2IN)
                        sill = max(0.0, (1.0 + vert[0]) * vscale * M2IN)
                        head = (1.0 + vert[1]) * vscale * M2IN
                        openings.append({
                            # negate y: ZInD global coords are y-down, the
                            # emitted plan is y-up (see zind2plan.flip_plan_y)
                            "center": ((gl[0] + gr[0]) / 2, -(gl[1] + gr[1]) / 2),
                            "width": math.dist(gl, gr),
                            "sill": sill, "head": head, "type": kind,
                        })
    return openings, ceilings, cams


def plan_opening_centers(plan):
    out = []
    for o in plan["openings"]:
        w = plan["walls"][o["wall_index"]]
        dx = w["end"][0] - w["start"][0]
        dy = w["end"][1] - w["start"][1]
        out.append({
            "center": (w["start"][0] + dx * o["position"],
                       w["start"][1] + dy * o["position"]),
            "sill": o["sill"], "head": o["head"], "type": o["type"],
            "width": o["width"],
        })
    return out


def seg_dist(p, a, b):
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    L2 = vx * vx + vy * vy
    if not L2:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / L2))
    return math.dist(p, (ax + vx * t, ay + vy * t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tour")
    ap.add_argument("plan")
    ap.add_argument("--floor", default="floor_01")
    args = ap.parse_args()

    tour = json.load(open(args.tour))
    plan = json.load(open(args.plan))
    gt_open, gt_ceil, gt_cam = collect_ground_truth(tour, args.floor)
    pl_open = plan_opening_centers(plan)

    print("=== ZInD ground-truth audit ===")

    # 1. opening verticals: match each GT door/window to nearest plan opening
    errs = {"door": {"sill": [], "head": []}, "window": {"sill": [], "head": []}}
    matched = 0
    for g in gt_open:
        if g["type"] not in errs:
            continue
        best, bd = None, 24.0   # within 24" counts as the same opening
        for p in pl_open:
            d = math.dist(g["center"], p["center"])
            if d < bd:
                best, bd = p, d
        if best is None:
            continue
        matched += 1
        errs[g["type"]]["sill"].append(abs(g["sill"] - best["sill"]))
        errs[g["type"]]["head"].append(abs(g["head"] - best["head"]))
    for kind, e in errs.items():
        for field, vals in e.items():
            if vals:
                mae = sum(vals) / len(vals)
                mx = max(vals)
                print(f"  {kind:<7}{field:<5} MAE {mae:5.1f}\"  max {mx:5.1f}\"  (n={len(vals)})")
    print(f"  GT door/window annotations matched to plan openings: {matched}")

    # 2. wall heights vs measured ceilings
    heights = sorted({w["height"] for w in plan["walls"]})
    if gt_ceil:
        lo, hi = min(gt_ceil), max(gt_ceil)
        mean = sum(gt_ceil) / len(gt_ceil)
        h_errs = []
        for c in gt_ceil:
            h_errs.append(min(abs(c - h) for h in heights))
        print(f"  ceilings (GT): {lo:.1f}\"..{hi:.1f}\" mean {mean:.1f}\"; "
              f"plan wall heights: {[round(h,1) for h in heights]}")
        print(f"  ceiling-vs-wall-height MAE {sum(h_errs)/len(h_errs):.1f}\" "
              f" max {max(h_errs):.1f}\"")

    # 3. GT wall coverage: redraw polygon edges represented by a plan wall?
    redraw = tour.get("redraw", {}).get(args.floor, {})
    fs = tour["scale_meters_per_coordinate"][args.floor] * M2IN
    total = covered = 0.0
    for room in redraw.values():
        verts = [(v[0] * fs, -v[1] * fs) for v in room.get("vertices", [])]
        for i in range(len(verts)):
            a, b = verts[i], verts[(i + 1) % len(verts)]
            L = math.dist(a, b)
            if L < 2:
                continue
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            near = any(
                seg_dist(mid, tuple(w["start"]), tuple(w["end"])) <=
                w["thickness"] / 2 + 4.0
                for w in plan["walls"])
            total += L
            if near:
                covered += L
    if total:
        print(f"  GT wall-edge coverage: {100*covered/total:.1f}% "
              f"({covered/12:.0f} of {total/12:.0f} ft)")

    # 4. eye height
    if gt_cam:
        cam_mean = sum(gt_cam) / len(gt_cam)
        eye = plan.get("eye_height")
        eye_str = f"{eye:.1f} in" if eye else "MISSING (viewer defaults 63 in)"
        print(f"  camera height (GT): mean {cam_mean:.1f}\" "
              f"({min(gt_cam):.1f}..{max(gt_cam):.1f}); "
              f"plan eye_height: {eye_str}")


if __name__ == "__main__":
    main()

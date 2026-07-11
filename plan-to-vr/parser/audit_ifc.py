#!/usr/bin/env python3
"""Audit a converted plan.json against its own IFC model's schedule data.

IFC carries authoritative, non-geometric "schedule" attributes alongside
the meshes the converter works from: IfcDoor/IfcWindow OverallWidth and
OverallHeight, IfcMaterialLayerSet wall build-ups, and the IfcSpace
program (one space per room with a LongName). This script measures the
gap between the emitted plan and those attributes, so converter changes
can be verified blind - rerun after regenerating the plan and every
metric must move toward zero.

Usage:
    python audit_ifc.py model.ifc plan.json [--storey 0|"Level 1"]

Reports:
  - opening width MAE vs OverallWidth and height MAE vs OverallHeight
    (doors and windows separately), matched by world-space center
  - wall thickness MAE vs the total IfcMaterialLayerSet thickness,
    matched by centerline proximity
  - room count vs IfcSpace count; per-space centroid point-in-polygon
    hits and name/kind agreement
"""
import argparse
import json
import math
import sys

M2IN = 39.3701
MATCH_RADIUS = 24.0      # opening centers within this (in) are the same one
WALL_MATCH = 6.0         # wall centerline must pass this close to the center


def seg_dist(p, a, b):
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    L2 = vx * vx + vy * vy
    if not L2:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / L2))
    return math.dist(p, (ax + vx * t, ay + vy * t))


def point_in_polygon(p, poly):
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and \
                x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# --------------------------------------------------------------------------
# IFC side (mirrors ifc2plan's storey selection so both agree)
# --------------------------------------------------------------------------
def open_model(path):
    import ifcopenshell
    return ifcopenshell.open(path)


def model_scale_in(model):
    from ifcopenshell.util.unit import calculate_unit_scale
    return float(calculate_unit_scale(model)) * M2IN


def storey_map_and_list(model, scale_in):
    def elev(s):
        e = getattr(s, "Elevation", None)
        return float(e) * scale_in if isinstance(e, (int, float)) else 0.0

    storeys = sorted(model.by_type("IfcBuildingStorey"), key=elev)

    def space_storey(obj):
        hops = 0
        while obj is not None and hops < 8:
            hops += 1
            if obj.is_a("IfcBuildingStorey"):
                return obj
            parent = None
            for rel in getattr(obj, "Decomposes", None) or []:
                parent = rel.RelatingObject
                break
            if parent is None:
                for rel in getattr(obj, "ContainedInStructure", None) or []:
                    parent = rel.RelatingStructure
                    break
            obj = parent
        return None

    smap = {}
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        structure = rel.RelatingStructure
        storey = structure if structure.is_a("IfcBuildingStorey") \
            else space_storey(structure)
        if storey is None:
            continue
        for el in rel.RelatedElements:
            smap[el.id()] = storey
    for space in model.by_type("IfcSpace"):
        st = space_storey(space)
        if st is not None:
            smap[space.id()] = st
    return storeys, smap


def pick_storey(model, storeys, smap, storey_arg):
    if storey_arg is not None:
        for s in storeys:
            if (s.Name or "") == storey_arg:
                return s
        try:
            idx = int(storey_arg)
            if 0 <= idx < len(storeys):
                return storeys[idx]
        except ValueError:
            pass
        raise SystemExit(f"unknown storey '{storey_arg}'")
    counts = {}
    for el in model.by_type("IfcWall"):
        st = smap.get(el.id())
        if st is not None:
            counts[st.id()] = counts.get(st.id(), 0) + 1
    return max(storeys, key=lambda s: counts.get(s.id(), 0))


def world_verts(settings, el, scale_in):
    import ifcopenshell.geom
    import numpy as np
    try:
        shape = ifcopenshell.geom.create_shape(settings, el)
    except Exception:
        return None
    v = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3) * scale_in
    return v if v.size else None


def layer_set_thickness(el, scale_in):
    """Total IfcMaterialLayerSet thickness for a wall (inches), or None."""
    try:
        from ifcopenshell.util.element import get_material
        mat = get_material(el)
        if mat is None:
            return None
        if mat.is_a("IfcMaterialLayerSetUsage"):
            mat = mat.ForLayerSet
        if mat.is_a("IfcMaterialLayerSet"):
            total = sum(l.LayerThickness for l in mat.MaterialLayers)
            return total * scale_in if total > 0 else None
    except Exception:
        pass
    return None


def collect_ground_truth(model, storey, smap, scale_in):
    """(openings, walls, spaces) on the storey, all inches / world XY.

    openings: [{center, type, width, height}]  from OverallWidth/Height
    walls:    [{center, layers}]               layers = None if unresolved
    spaces:   [{centroid, name, long_name}]
    """
    import ifcopenshell.geom
    import numpy as np
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    openings = []
    for ifc_type, kind in (("IfcDoor", "door"), ("IfcWindow", "window")):
        for el in model.by_type(ifc_type):
            if smap.get(el.id()) != storey:
                continue
            v = world_verts(settings, el, scale_in)
            if v is None:
                continue
            ow = getattr(el, "OverallWidth", None)
            oh = getattr(el, "OverallHeight", None)
            openings.append({
                "center": (float((v[:, 0].min() + v[:, 0].max()) / 2),
                           float((v[:, 1].min() + v[:, 1].max()) / 2)),
                "type": kind,
                "width": ow * scale_in
                if isinstance(ow, (int, float)) and ow > 0 else None,
                "height": oh * scale_in
                if isinstance(oh, (int, float)) and oh > 0 else None,
                "name": el.Name or "",
            })

    walls = []
    for el in model.by_type("IfcWall"):
        if smap.get(el.id()) != storey:
            continue
        v = world_verts(settings, el, scale_in)
        if v is None:
            continue
        walls.append({
            "center": (float((v[:, 0].min() + v[:, 0].max()) / 2),
                       float((v[:, 1].min() + v[:, 1].max()) / 2)),
            "layers": layer_set_thickness(el, scale_in),
            "name": el.Name or "",
        })

    spaces = []
    for space in model.by_type("IfcSpace"):
        if smap.get(space.id()) != storey:
            continue
        v = world_verts(settings, space, scale_in)
        if v is None:
            continue
        z0 = v[:, 2].min()
        base = v[np.abs(v[:, 2] - z0) <= 2.0]
        spaces.append({
            "centroid": (float(base[:, 0].mean()), float(base[:, 1].mean())),
            "name": space.Name or "",
            "long_name": str(space.LongName or ""),
        })
    return openings, walls, spaces


# --------------------------------------------------------------------------
# plan side
# --------------------------------------------------------------------------
def plan_opening_centers(plan):
    out = []
    for o in plan["openings"]:
        w = plan["walls"][o["wall_index"]]
        dx = w["end"][0] - w["start"][0]
        dy = w["end"][1] - w["start"][1]
        out.append({
            "center": (w["start"][0] + dx * o["position"],
                       w["start"][1] + dy * o["position"]),
            "type": o["type"], "width": o["width"],
            "height": o["head"] - o["sill"],
        })
    return out


def polygon_area(poly):
    return abs(sum(poly[i][0] * poly[(i + 1) % len(poly)][1]
                   - poly[(i + 1) % len(poly)][0] * poly[i][1]
                   for i in range(len(poly)))) / 2.0


def expected_kind(long_name):
    name = long_name.lower()
    for key, kind in (("bath", "bath"), ("kitchen", "kitchen"),
                      ("laundry", "laundry"), ("utility", "laundry"),
                      ("garage", "garage")):
        if key in name:
            return kind
    return "room"


def stats(vals):
    return sum(vals) / len(vals), max(vals)


def main():
    ap = argparse.ArgumentParser(
        description="audit plan.json vs its IFC's schedule data")
    ap.add_argument("model", help="the IFC file the plan was converted from")
    ap.add_argument("plan", help="plan.json emitted by ifc2plan.py")
    ap.add_argument("--storey", default=None,
                    help="storey index or name (default = most walls; must "
                         "match the one the plan was converted with)")
    args = ap.parse_args()

    model = open_model(args.model)
    plan = json.load(open(args.plan))
    scale_in = model_scale_in(model)
    storeys, smap = storey_map_and_list(model, scale_in)
    if not storeys:
        raise SystemExit("no IfcBuildingStorey in model")
    storey = pick_storey(model, storeys, smap, args.storey)
    if plan.get("storey") and plan["storey"] != (storey.Name or ""):
        print(f"warning: plan was converted from storey "
              f"'{plan['storey']}' but auditing '{storey.Name}'",
              file=sys.stderr)

    gt_open, gt_walls, gt_spaces = collect_ground_truth(
        model, storey, smap, scale_in)
    pl_open = plan_opening_centers(plan)

    print(f"=== IFC schedule audit: storey '{storey.Name}' ===")

    # 1. opening width/height vs OverallWidth/OverallHeight
    errs = {"door": {"width": [], "height": []},
            "window": {"width": [], "height": []}}
    matched = 0
    unmatched = []
    for g in gt_open:
        best, bd = None, MATCH_RADIUS
        for p in pl_open:
            if p["type"] != g["type"]:
                continue
            d = math.dist(g["center"], p["center"])
            if d < bd:
                best, bd = p, d
        if best is None:
            unmatched.append(f"{g['type']} {g['name']}")
            continue
        matched += 1
        if g["width"] is not None:
            errs[g["type"]]["width"].append(abs(g["width"] - best["width"]))
        if g["height"] is not None:
            errs[g["type"]]["height"].append(abs(g["height"] - best["height"]))
    for kind, e in errs.items():
        for field, vals in e.items():
            if vals:
                mae, mx = stats(vals)
                print(f"  {kind:<7}{field:<7} MAE {mae:5.2f}\"  "
                      f"max {mx:5.2f}\"  (n={len(vals)})")
    print(f"  IFC doors+windows matched to plan openings: {matched} of "
          f"{len(gt_open)}")
    for u in unmatched:
        print(f"    unmatched: {u}")

    # 2. wall thickness vs material layer set totals
    t_errs, unresolved, unmatched_walls = [], 0, 0
    for g in gt_walls:
        if g["layers"] is None:
            unresolved += 1
            continue
        best, bd = None, WALL_MATCH + 1e9
        for w in plan["walls"]:
            d = seg_dist(g["center"], tuple(w["start"]), tuple(w["end"]))
            if d < bd:
                best, bd = w, d
        if best is None or bd > max(WALL_MATCH, best["thickness"]):
            unmatched_walls += 1
            continue
        t_errs.append(abs(best["thickness"] - g["layers"]))
    if t_errs:
        mae, mx = stats(t_errs)
        print(f"  wall thickness vs layer set: MAE {mae:5.2f}\"  "
              f"max {mx:5.2f}\"  (n={len(t_errs)}, "
              f"{unresolved} wall(s) without layer set"
              + (f", {unmatched_walls} unmatched" if unmatched_walls else "")
              + ")")
    else:
        print(f"  wall thickness: no walls with resolvable layer sets")

    # 3. rooms vs spaces
    rooms = plan.get("rooms", [])
    print(f"  rooms: plan {len(rooms)} vs IFC spaces {len(gt_spaces)}")
    hits = kind_ok = 0
    for g in gt_spaces:
        # smallest containing room wins: convex-hull approximations of
        # concave spaces can swallow their neighbors' centroids
        containing = [r for r in rooms
                      if point_in_polygon(g["centroid"], r["polygon"])]
        room = min(containing, key=lambda r: polygon_area(r["polygon"])) \
            if containing else None
        want = expected_kind(g["long_name"])
        if room is None:
            print(f"    MISS  {g['name']:<6} {g['long_name']:<12} "
                  f"centroid in no plan room")
            continue
        hits += 1
        got = room.get("kind", "room")
        label = room.get("label", room.get("name", ""))
        ok = got == want and (not room.get("label")
                              or room["label"] == g["long_name"])
        kind_ok += ok
        if not ok:
            print(f"    DIFF  {g['name']:<6} {g['long_name']:<12} -> "
                  f"plan room '{label}' kind '{got}' (expected '{want}')")
    print(f"  space centroid -> plan room hits: {hits}/{len(gt_spaces)}; "
          f"name+kind agreement: {kind_ok}/{hits}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Symbol-first opening detection for DataCAD DXF (doors / windows / openings).

The plotted-PDF pipeline had to *infer* openings by merging wall pieces and
guessing at gaps - fragile, and it produced two failure modes on the clean
architect DXF: it merged a whole wall run into one giant "door", and it
dropped doors that sit in the gap between two collinear wall segments.

This module throws that away and reads the architect's own drafting
convention directly. On the combined doors/windows layer every symbol is
colour-coded (DataCAD ByEntity colour, visible in any CAD viewer):

    * a DOOR is a RED swing ARC (ACI 1 or 11); centre = hinge, sweep = swing.
    * a WINDOW is drawn with GREEN frame/mullion lines (ACI 3) inside the gap.
    * a bare CASED OPENING / pass-through has neither - just the wall faces
      carried across.

So the passes are, in order, each authoritative from one source:

    1. walls        - taken as given (extract.py, from the WALL layer)
    2. gaps         - where BOTH wall faces are absent along a wall = an opening
    3. doors        - every red swing arc -> a door, mapped to its wall/gap
                      (this alone recovers arc-marked doors the merge missed)
    4. windows      - a remaining gap with green frame lines in it
    5. openings     - a remaining gap with neither -> pass-through

`detect()` returns a list shaped exactly like extract's plan["openings"].
"""
import math
from collections import defaultdict

import ezdxf

REDS = {1, 11}          # ACI red family - swing arcs / glazing centrelines
GREEN = {3}             # ACI green - window frames, mullions, door leaves
DOOR_HEAD = 80.0
WINDOW_SILL = 30.0
WINDOW_HEAD = 80.0
MIN_GAP = 16.0          # narrower breaks are drafting slop / jamb ticks
MAX_GAP = 540.0         # wider than a garage door -> not one opening
FACE_LAT = 3.0          # a raw face line counts toward a wall if within this
                        # of the wall centreline, laterally (+half thickness)


def _load(path, wall_layers, dw_layers):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    faces, green, arcs = [], [], []
    for e in msp.query("LINE"):
        (x0, y0, _), (x1, y1, _) = e.dxf.start, e.dxf.end
        if math.hypot(x1 - x0, y1 - y0) < 1.5:
            continue
        if e.dxf.layer in wall_layers:
            faces.append((x0, y0, x1, y1))
        elif e.dxf.layer in dw_layers and e.dxf.color in GREEN:
            green.append((x0, y0, x1, y1))
    for e in msp.query("ARC"):
        if e.dxf.layer in dw_layers and e.dxf.color in REDS:
            r = e.dxf.radius
            span = (e.dxf.end_angle - e.dxf.start_angle) % 360
            if 16 <= r <= 46 and 30 <= span <= 115:
                c = e.dxf.center
                arcs.append({"c": (c[0], c[1]), "r": r,
                             "a0": e.dxf.start_angle, "a1": e.dxf.end_angle})
    # de-duplicate double-drawn arcs (same hinge + radius)
    uniq = []
    for a in arcs:
        if not any(math.hypot(a["c"][0] - b["c"][0], a["c"][1] - b["c"][1]) < 3
                   and abs(a["r"] - b["r"]) < 2 for b in uniq):
            uniq.append(a)
    return faces, green, uniq


def _wall_frame(w):
    (ax, ay), (bx, by) = w["start"], w["end"]
    L = math.hypot(bx - ax, by - ay) or 1.0
    ux, uy = (bx - ax) / L, (by - ay) / L
    return (ax, ay), (ux, uy), (-uy, ux), L


def _covered(w, faces):
    """Along-wall intervals covered by a raw WALL face line (either face)."""
    (ax, ay), (ux, uy), (nx, ny), L = _wall_frame(w)
    lat = w["thickness"] / 2.0 + FACE_LAT
    ivs = []
    for (x0, y0, x1, y1) in faces:
        # segment must be parallel to the wall
        sdx, sdy = x1 - x0, y1 - y0
        sl = math.hypot(sdx, sdy) or 1.0
        if abs((sdx * ux + sdy * uy) / sl) < 0.985:
            continue
        a0 = (x0 - ax) * ux + (y0 - ay) * uy
        a1 = (x1 - ax) * ux + (y1 - ay) * uy
        p0 = (x0 - ax) * nx + (y0 - ay) * ny
        p1 = (x1 - ax) * nx + (y1 - ay) * ny
        if abs(p0) > lat or abs(p1) > lat:
            continue
        lo, hi = sorted((a0, a1))
        if hi > 0 and lo < L:
            ivs.append((max(0.0, lo), min(L, hi)))
    ivs.sort()
    merged = []
    for lo, hi in ivs:
        if merged and lo <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged, L


def _gaps(w, faces):
    """Internal openings = along-wall stretches with no face on either side."""
    merged, L = _covered(w, faces)
    if not merged:
        return []
    gaps = []
    for i in range(len(merged) - 1):
        g0, g1 = merged[i][1], merged[i + 1][0]
        if MIN_GAP <= g1 - g0 <= MAX_GAP:
            gaps.append([g0, g1])
    return gaps


def _green_in(w, g0, g1, green):
    (ax, ay), (ux, uy), (nx, ny), L = _wall_frame(w)
    lat = w["thickness"] / 2.0 + 1.5
    n = 0
    for (x0, y0, x1, y1) in green:
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        a = (mx - ax) * ux + (my - ay) * uy
        p = (mx - ax) * nx + (my - ay) * ny
        sdx, sdy = x1 - x0, y1 - y0
        sl = math.hypot(sdx, sdy) or 1.0
        par = abs((sdx * ux + sdy * uy) / sl) > 0.9
        if g0 <= a <= g1 and abs(p) <= lat and par:
            n += 1
    return n


def _door_from_arc(w, arc, g0, g1, L):
    """Hinge side + swing direction for a door arc on wall w."""
    (ax, ay), (ux, uy), (nx, ny), _ = _wall_frame(w)
    cx, cy = arc["c"]
    along = (cx - ax) * ux + (cy - ay) * uy
    center = (g0 + g1) / 2
    hinge = -1 if along < center else 1
    a0, a1 = math.radians(arc["a0"]), math.radians(arc["a1"])
    span = (a1 - a0) % (2 * math.pi)
    amid = a0 + span / 2
    swing = 1 if (math.cos(amid) * nx + math.sin(amid) * ny) >= 0 else -1
    return hinge, swing, round(along, 1)


def detect(path, wall_layers, dw_layers, walls):
    faces, green, arcs = _load(path, set(wall_layers), set(dw_layers))
    openings = []
    used_arc = [False] * len(arcs)

    # per-wall gaps, classified door / window / opening
    for wi, w in enumerate(walls):
        (ax, ay), (ux, uy), (nx, ny), L = _wall_frame(w)
        lat_arc = w["thickness"] / 2.0 + 12.0
        for g0, g1 in _gaps(w, faces):
            width = g1 - g0
            center = (g0 + g1) / 2
            # a red swing arc whose hinge sits in this gap -> door
            arc_k = None
            for k, a in enumerate(arcs):
                if used_arc[k]:
                    continue
                al = (a["c"][0] - ax) * ux + (a["c"][1] - ay) * uy
                pe = (a["c"][0] - ax) * nx + (a["c"][1] - ay) * ny
                if g0 - 6 <= al <= g1 + 6 and abs(pe) <= lat_arc:
                    arc_k = k
                    break
            if arc_k is not None:
                used_arc[arc_k] = True
                hinge, swing, hinge_at = _door_from_arc(
                    w, arcs[arc_k], g0, g1, L)
                openings.append({
                    "wall_index": wi, "position": round(center / L, 4),
                    "width": round(width, 2), "type": "door",
                    "sill": 0.0, "head": DOOR_HEAD, "hinge": hinge,
                    "swing": swing, "leaf": round(arcs[arc_k]["r"], 1),
                    "hinge_at": hinge_at})
            elif _green_in(w, g0, g1, green) >= 2:
                openings.append({
                    "wall_index": wi, "position": round(center / L, 4),
                    "width": round(width, 2), "type": "window",
                    "sill": WINDOW_SILL, "head": WINDOW_HEAD})
            else:
                openings.append({
                    "wall_index": wi, "position": round(center / L, 4),
                    "width": round(width, 2), "type": "opening",
                    "sill": 0.0, "head": DOOR_HEAD})

    # doors whose arc fell in an inter-segment gap (no per-wall gap hosts it):
    # attach to the nearest wall and place the door at the arc, so an
    # architect-drawn swing is never dropped.
    for k, a in enumerate(arcs):
        if used_arc[k]:
            continue
        best, bestd = None, 1e18
        for wi, w in enumerate(walls):
            (ax, ay), (ux, uy), (nx, ny), L = _wall_frame(w)
            al = (a["c"][0] - ax) * ux + (a["c"][1] - ay) * uy
            pe = (a["c"][0] - ax) * nx + (a["c"][1] - ay) * ny
            if -8 <= al <= L + 8:
                d = abs(pe)
                if d < bestd and d <= w["thickness"] / 2.0 + 14.0:
                    best, bestd = wi, d
        if best is None:
            continue
        w = walls[best]
        (ax, ay), (ux, uy), (nx, ny), L = _wall_frame(w)
        al = (a["c"][0] - ax) * ux + (a["c"][1] - ay) * uy
        al = max(0.0, min(L, al))
        width = min(2.0 * a["r"], 40.0)
        g0, g1 = al - width / 2, al + width / 2
        hinge, swing, hinge_at = _door_from_arc(w, a, g0, g1, L)
        used_arc[k] = True
        openings.append({
            "wall_index": best, "position": round(al / L, 4),
            "width": round(width, 2), "type": "door", "sill": 0.0,
            "head": DOOR_HEAD, "hinge": hinge, "swing": swing,
            "leaf": round(a["r"], 1), "hinge_at": round(hinge_at, 1)})

    # keep every opening inside its host wall: an arc-driven door mapped to
    # a short stub beside an inter-segment gap can otherwise be wider than
    # the wall (which would erase more wall than intended for collision).
    for o in openings:
        w = walls[o["wall_index"]]
        L = math.hypot(w["end"][0] - w["start"][0],
                       w["end"][1] - w["start"][1]) or 1.0
        width = min(o["width"], L * 0.98)
        hf = (width / 2.0) / L
        pos = min(max(o["position"], hf), 1.0 - hf)
        o["width"] = round(width, 2)
        o["position"] = round(pos, 4)

    n_drop = sum(1 for u in used_arc if not u)
    return openings, {"arcs": len(arcs), "arcs_unplaced": n_drop}


if __name__ == "__main__":
    import sys
    import json
    walls = json.load(open(sys.argv[4]))["walls"]
    ops, rep = detect(sys.argv[1], sys.argv[2].split(","),
                      sys.argv[3].split(","), walls)
    from collections import Counter
    print(rep, dict(Counter(o["type"] for o in ops)))

#!/usr/bin/env python3
"""Build a viewer plan.json from a native architect DXF (one floor).

Unlike pdf2plan (which reconstructs geometry from a plotted raster), the
architect's DXF is clean layered vector CAD, so this is thin: run the
shared extractor for walls/doors/windows/rooms, then augment with what
the DXF gives us for free that the PDF never did -

  * real room NAMES (the RMNAMES text layer) -> room labels + kinds,
    which also drives garage split-level and per-room floor materials;
  * stair runs detected straight from the tread ladders (dxf_stairs),
    with down / direction / level from a small ground-truth sidecar
    (the "Up/Down" text is unreadable geometry, same as the PDF);
  * the DEMO layer is simply excluded, so historical/demo linework never
    pollutes the model (no dash-filtering heuristics needed).

Usage:
    python dxf2plan.py plan.dxf -o home-l1.json --floor 1 \
        --gt corrections/dxf-l1.json
"""
import argparse
import json
import math
import os

import ezdxf

import extract as X
import dxf_stairs
import dxf_openings
from pdf2plan import default_camera, point_in_poly


def room_labels(path, layer):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace():
        if e.dxftype() in ("TEXT", "MTEXT") and e.dxf.layer == layer:
            t = (e.plain_text() if e.dxftype() == "MTEXT"
                 else e.dxf.text).strip()
            p = e.dxf.insert
            if t:
                out.append((t, (p[0], p[1])))
    return out


NAME_KIND = [
    ("GARAGE", "garage"), ("BATH", "bath"), ("LAUNDRY", "laundry"),
    ("KITCHEN", "kitchen"), ("MUDROOM", "mud"), ("CLOSET", "closet"),
    ("PANTRY", "pantry"), ("BEDROOM", "bedroom"), ("LIVING", "living"),
    ("DINING", "dining"), ("SUNROOM", "sun"), ("PORCH", "porch"),
    ("STAIR", "stair"), ("HALL", "hall"), ("PLAY", "play"),
    ("LINEN", "closet"), ("WALK-IN", "closet"), ("COAT", "closet"),
]


def kind_for(name):
    up = name.upper()
    for key, kind in NAME_KIND:
        if key in up:
            return kind
    return "room"


def assign_names(plan, labels):
    """Give each detected room the label whose point sits inside it (else
    the nearest label), and a kind derived from that name."""
    for r in plan.get("rooms", []):
        poly = r["polygon"]
        best, bestd = None, 1e18
        for name, (lx, ly) in labels:
            if point_in_poly(lx, ly, poly):
                best, bestd = name, -1
                break
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            d = math.hypot(cx - lx, cy - ly)
            if d < bestd:
                best, bestd = name, d
        if best is not None:
            r["name"] = best.replace("  ", " ")
            k = kind_for(best)
            # The architect's label is authoritative for the kind. This
            # must also OVERRIDE the geometric garage guess: wide interior
            # cased openings (the hall/kitchen pass-throughs) look like a
            # garage door to classify_garages, and a "garage" hall would
            # drop the main floor slab by 4 risers in the viewer.
            r["kind"] = k if k in ("garage", "bath", "laundry",
                                   "kitchen", "mud") else "room"


def filter_disconnected(plan, snap=6.0, min_component=3, min_keep_len=72.0):
    """Drop walls in tiny disconnected components (stray fragments a CAD
    file leaves floating - a lone bay-window wall west of the house).
    Keeps the main wall network and any substantial sub-structure;
    reindexes openings and recomputes the footprint so nothing dangles.

    A component is only droppable if ALL its walls are short: a stray
    fragment is a couple of feet of linework, while a long wall is real
    even when the graph says it is isolated (the L2 west perimeter wall
    merges with a corner gap just over snap and would otherwise vanish,
    taking the bedrooms' exterior wall with it)."""
    walls = plan["walls"]
    n = len(walls)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    def pt_seg(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0, min(1, ((p[0] - a[0]) * dx +
                                              (p[1] - a[1]) * dy) / L2))
        return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)

    def touch(u, v):
        # connected if any endpoint of one lands on the other's span
        # (endpoint-to-endpoint OR a T-junction into mid-span)
        for p in (u["start"], u["end"]):
            if pt_seg(p, v["start"], v["end"]) <= snap:
                return True
        for p in (v["start"], v["end"]):
            if pt_seg(p, u["start"], u["end"]) <= snap:
                return True
        return False

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) != find(j) and touch(walls[i], walls[j]):
                union(i, j)
    comp = {}
    for i in range(n):
        comp.setdefault(find(i), []).append(i)
    if not comp:
        return 0
    def wall_len(i):
        w = walls[i]
        return math.hypot(w["end"][0] - w["start"][0],
                          w["end"][1] - w["start"][1])

    keep = set()
    biggest = max(comp.values(), key=len)
    for members in comp.values():
        if (members is biggest or len(members) >= min_component
                or any(wall_len(i) >= min_keep_len for i in members)):
            keep.update(members)
    dropped = n - len(keep)
    if not dropped:
        return 0
    # remap surviving walls, drop openings on removed walls
    order = sorted(keep)
    remap = {old: new for new, old in enumerate(order)}
    plan["walls"] = [walls[i] for i in order]
    plan["openings"] = [dict(o, wall_index=remap[o["wall_index"]])
                        for o in plan["openings"] if o["wall_index"] in remap]

    class _W:  # adapter for compute_footprint
        pass
    ad = []
    for w in plan["walls"]:
        a = _W(); a.c0 = tuple(w["start"]); a.c1 = tuple(w["end"])
        a.thickness = w["thickness"]; ad.append(a)
    plan["footprint"] = X.compute_footprint(ad, plan.setdefault("warnings", []))
    plan["warnings"].append(
        f"{dropped} disconnected wall fragment(s) dropped")
    return dropped


def _switchback_bands(path, layers, g):
    """Split ONE stairwell's tread ladder into its two flights.

    A switchback main stair draws its two flights as two parallel tread
    ladders offset laterally (here: the up-flight to L2 in one band, the
    down-flight to the basement in the other). dxf_stairs' generic chaining
    folds them into one messy run, so the viewer renders a single flat run
    that dead-ends. When a GT entry marks the well `split`, re-read the raw
    treads inside its `bbox`, cluster them into the two lateral bands, and
    emit each band as its own stair - one carries `down` (the band nearest
    `down_near`), the other ascends. The viewer's existing switchback code
    then builds the landing + return flight from that up/down pair."""
    doc = ezdxf.readfile(path)
    x0, x1, y0, y1 = g["bbox"]
    segs = []
    for e in doc.modelspace().query("LINE"):
        if e.dxf.layer not in layers:
            continue
        a, b = e.dxf.start, e.dxf.end
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if 22 <= L <= 80 and x0 <= mx <= x1 and y0 <= my <= y1:
            segs.append((a[0], a[1], b[0], b[1], mx, my))
    if not segs:
        return []

    def bucket(s):
        return round(math.degrees(
            math.atan2(s[3] - s[1], s[2] - s[0]) % math.pi) / 5) * 5
    from collections import Counter
    modal = Counter(bucket(s) for s in segs).most_common(1)[0][0]
    tr = [s for s in segs if bucket(s) == modal]
    a = math.radians(modal)
    tdir = (math.cos(a), math.sin(a))       # along a tread
    nrm = (-tdir[1], tdir[0])               # climb axis (perp to tread)
    items = [{"s": s, "lat": s[4] * tdir[0] + s[5] * tdir[1],
              "c": s[4] * nrm[0] + s[5] * nrm[1]} for s in tr]
    items.sort(key=lambda d: d["lat"])
    # break the ladder into lateral bands at the widest lateral gap
    gi, gv = 0, -1.0
    for i in range(1, len(items)):
        d = items[i]["lat"] - items[i - 1]["lat"]
        if d > gv:
            gv, gi = d, i
    bands = [items[:gi], items[gi:]]
    dn = g.get("down_near")
    labels = dxf_stairs._dir_labels(path)     # up/down KIND from the text
    circles = dxf_stairs._term_circles(path)  # terminating side (floor-level end)
    cents = []
    for band in bands:
        if band:
            cents.append((sum(b["s"][4] for b in band) / len(band),
                          sum(b["s"][5] for b in band) / len(band)))
        else:
            cents.append(None)
    out = []
    for bi, band in enumerate(bands):
        if len(band) < 3:
            continue
        segset = [b["s"] for b in band]
        xs = [c for s in segset for c in (s[0], s[2])]
        ys = [c for s in segset for c in (s[1], s[3])]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        treads = [[[round(s[0], 1), round(s[1], 1)],
                   [round(s[2], 1), round(s[3], 1)]] for s in segset]
        cx, cy = cents[bi]
        sx, sy = nrm                          # climb axis; sign set below
        down = None
        # up/down KIND from the architect's Down/Up text nearest this band;
        # DIRECTION from the flight's terminating CIRCLE (owner, VR #14/#30:
        # each stair carries a circle on the side level with this floor - the
        # run travels AWAY from it). Text alone can't give direction on a
        # shared landing where both bands' labels sit together.
        best, bestd = None, 90.0
        for kind, (lx, ly) in labels:
            d = math.hypot(cx - lx, cy - ly)
            if d < bestd:
                best, bestd = kind, d
        circ, cd = None, 70.0
        for (qx, qy) in circles:
            d = math.hypot(cx - qx, cy - qy)
            if d < cd:
                circ, cd = (qx, qy), d
        if circ is not None:                  # travel AWAY from the circle
            qx, qy = circ
            s = 1.0 if ((cx - qx) * sx + (cy - qy) * sy) >= 0 else -1.0
            sx, sy = s * nrm[0], s * nrm[1]
        if best is not None:
            down = (best == "down")
        st = {"polygon": [[bx0, by0], [bx1, by0], [bx1, by1], [bx0, by1]],
              "treads": treads,
              "direction": [round(sx, 2), round(sy, 2)]}
        if "direction" in g and g.get("down_dir_from_gt"):
            st["direction"] = g["direction"]
        # FALLBACK: the manual down_near heuristic, only when no label is near
        if down is None and dn and cents[bi]:
            oc = cents[1 - bi]
            here = math.hypot(cents[bi][0] - dn[0], cents[bi][1] - dn[1])
            there = math.hypot(oc[0] - dn[0], oc[1] - dn[1]) if oc else 1e18
            down = here <= there
        if down:
            st["down"] = True
        out.append(st)
    return out


def add_stairs(plan, path, geom_layers, gt, stair_labels):
    """Label-gated stair detection. The architect labels every stairwell
    ("STAIR", plus "Down"/"Up to Attic"); we keep only tread-runs sitting
    inside a labeled stairwell (killing window-glazing false positives)
    and MERGE every run near one label into a single stair. A switchback
    is then just several runs under one label folded into one stairwell,
    however messy its landings. down/direction come from the GT sidecar
    (the plotted Up/Down text is unreadable geometry)."""
    runs = dxf_stairs.detect(path, set(geom_layers))
    if not stair_labels:
        stair_labels = [tuple(g["near"]) for g in (gt or {}).get("stairs", [])]
    R = 110.0                                     # run-to-label gate (inches)
    groups = {}
    for r in runs:
        cx, cy = r["centroid"]
        li, ld = None, R
        for i, (lx, ly) in enumerate(stair_labels):
            d = math.hypot(cx - lx, cy - ly)
            if d < ld:
                li, ld = i, d
        if li is not None:
            groups.setdefault(li, []).append(r)

    gt_stairs = (gt or {}).get("stairs", [])
    out = []
    for li, grp in groups.items():
        treads = [t for r in grp for t in r["treads"]]
        xs = [c for t in treads for c in (t[0][0], t[1][0])]
        ys = [c for t in treads for c in (t[0][1], t[1][1])]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        g = min(gt_stairs, key=lambda s: math.hypot(
            s["near"][0] - cx, s["near"][1] - cy), default=None)
        matched = g and math.hypot(g["near"][0] - cx,
                                   g["near"][1] - cy) <= g.get("tol", 90)
        if matched and g.get("drop"):
            continue
        if matched and g.get("split"):
            # a switchback well: emit its two flights (up + down) so the
            # viewer builds the landing + return flight instead of one run
            out.extend(_switchback_bands(path, set(geom_layers), g))
            continue
        # Direction / down: a matched GT entry is authoritative (keeps the
        # hand-tuned L1 switchback + garage runs exact); otherwise take the
        # architect's own Down/Up text indicator that dxf_stairs read off the
        # sheet (run["down"] / run["label_dir"]); else fall back to the
        # dominant run's raw climb axis with no level change.
        dom = max(grp, key=lambda r: len(r["treads"]))
        labr = next((r for r in grp if r.get("label_dir")), None)
        if matched:
            direction = g["direction"] if "direction" in g else dom["climb"]
            down = bool(g.get("down"))
        elif labr is not None:
            direction = labr["label_dir"]
            down = bool(labr.get("down"))
        else:
            direction = dom["climb"]
            down = False
        st = {"polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
              "treads": treads, "direction": direction}
        if down:
            st["down"] = True
        if matched and g.get("top"):
            st["top"] = g["top"]   # e.g. "slab": flight starts at the low slab
        out.append(st)
    # flush end: the architect's termination circle marks the tread that is
    # level with THIS floor's plane (the flight travels away from it, up or
    # down). The viewer anchors that end at elevation 0 (VR notes #14/#30).
    circles = dxf_stairs._term_circles(path)
    for st in out:
        poly = st["polygon"]
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        best, bd = None, 110.0
        for (qx, qy) in circles:
            d = math.hypot(cx - qx, cy - qy)
            if d < bd:
                best, bd = (qx, qy), d
        if best is not None:
            st["flush"] = [round(best[0], 1), round(best[1], 1)]
    # second-flight turn for an up-flight that switchbacks/quarter-turns to the
    # floor above ("back" = U-turn, "right"/"left" = quarter-turn). Supplied by
    # the GT `up_turns` sidecar (owner read from the plan, VR notes #58/#14).
    for oc in (gt.get("up_turns") or []):
        nx, ny = oc["near"]
        best, bd = None, oc.get("tol", 80)
        for st in out:
            poly = st["polygon"]
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            d = math.hypot(cx - nx, cy - ny)
            if d < bd:
                best, bd = st, d
        if best is not None:
            best["up_turn"] = oc.get("turn", "back")
    plan["stairs"] = out
    return len(out)


def add_fixtures(plan, path, fixture_layers):
    """INSERT block references on the fixtures layer become plan fixtures.
    The block NAME drives the viewer's stand-in (TOILET/TUB/CABINET/...,
    see buildFixture), the block definition's extent drives the footprint
    size, and the insert's rotation passes through. Owner-authored fixture
    blocks live on 1.<floor>FURN (VR notes #56/#40)."""
    lays = {l.strip().upper() for l in fixture_layers if l.strip()}
    if not lays:
        return 0
    doc = ezdxf.readfile(path)
    out = plan.get("fixtures") or []
    for e in doc.modelspace().query("INSERT"):
        if e.dxf.layer.upper() not in lays:
            continue
        xs, ys = [], []
        try:
            blk = doc.blocks[e.dxf.name]
            for be in blk:
                t = be.dxftype()
                if t == "LINE":
                    for p in (be.dxf.start, be.dxf.end):
                        xs.append(p.x); ys.append(p.y)
                elif t in ("CIRCLE", "ARC"):
                    c, r = be.dxf.center, be.dxf.radius
                    xs += [c.x - r, c.x + r]; ys += [c.y - r, c.y + r]
                elif t == "LWPOLYLINE":
                    for v in be.get_points("xy"):
                        xs.append(v[0]); ys.append(v[1])
        except Exception:
            pass
        if xs:
            w, d = max(xs) - min(xs), max(ys) - min(ys)
            ox, oy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        else:
            w, d, ox, oy = 24.0, 24.0, 0.0, 0.0
        sx = getattr(e.dxf, "xscale", 1.0) or 1.0
        sy = getattr(e.dxf, "yscale", 1.0) or 1.0
        rot = math.radians(getattr(e.dxf, "rotation", 0.0) or 0.0)
        ip = e.dxf.insert
        cxr = ox * sx * math.cos(rot) - oy * sy * math.sin(rot)
        cyr = ox * sx * math.sin(rot) + oy * sy * math.cos(rot)
        out.append({
            "name": e.dxf.name,
            "center": [round(ip.x + cxr, 2), round(ip.y + cyr, 2)],
            "size": [round(abs(w * sx), 2), round(abs(d * sy), 2)],
            "rotation": round(math.degrees(rot), 1),
        })
        print(f"fixture: {e.dxf.name} @({ip.x:.1f},{ip.y:.1f})")
    plan["fixtures"] = out
    return len(out)


def render_underlay(path, layers, plan, out_png, in_per_px=0.5, margin=40):
    """Rasterize the DXF geometry layers at plan scale for the viewer's
    'sheet' compare layer. Returns {file, x0, y1, in_per_px} mapping the
    image's top-left pixel to plan (x0, y1) with +y up (matching how the
    viewer lays the underlay under the model)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    xs = [c for w in plan["walls"] for c in (w["start"][0], w["end"][0])]
    ys = [c for w in plan["walls"] for c in (w["start"][1], w["end"][1])]
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    W = max(1, round((x1 - x0) / in_per_px))
    H = max(1, round((y1 - y0) / in_per_px))

    doc = ezdxf.readfile(path)
    lines, arcs = [], []
    for e in doc.modelspace():
        if e.dxf.layer not in layers:
            continue
        t = e.dxftype()
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            lines.append([(a[0], a[1]), (b[0], b[1])])
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points()]
            lines += [[pts[i], pts[i + 1]] for i in range(len(pts) - 1)]
        elif t == "ARC":
            c, r = e.dxf.center, e.dxf.radius
            a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
            span = (a1 - a0) % (2 * math.pi)
            steps = max(6, int(span / 0.2))
            pth = [(c[0] + r * math.cos(a0 + span * k / steps),
                    c[1] + r * math.sin(a0 + span * k / steps))
                   for k in range(steps + 1)]
            arcs += [[pth[k], pth[k + 1]] for k in range(len(pth) - 1)]

    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_axis_off()
    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)          # +y up
    ax.add_collection(LineCollection(lines, colors="#222", linewidths=1.1))
    ax.add_collection(LineCollection(arcs, colors="#555", linewidths=0.7))
    fig.savefig(out_png, dpi=100, facecolor="white")
    plt.close(fig)
    return {"file": out_png.split("/")[-1], "x0": round(x0, 2),
            "y1": round(y1, 2), "in_per_px": in_per_px}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--floor", default="1", help="floor tag, e.g. 1 or 2")
    ap.add_argument("--wall-layers", required=True)
    ap.add_argument("--dw-layers", required=True,
                    help="combined doors+windows layer(s)")
    ap.add_argument("--rmnames-layer", default="")
    ap.add_argument("--gt", default="")
    ap.add_argument("--fixture-layers", default="",
                    help="comma-separated INSERT layers; default 1.<floor>FURN")
    ap.add_argument("--wall-height", type=float, default=96.0)
    ap.add_argument("--underlay", default="",
                    help="also render a true-scale sheet PNG here")
    args = ap.parse_args()

    dw = args.dw_layers
    plan, report = X.extract(
        args.input, args.wall_layers.split(","), dw.split(","), dw.split(","),
        tol=2.0, max_wall=14.0, units="inches")
    for w in plan["walls"]:
        w["height"] = args.wall_height

    gt = json.load(open(args.gt)) if args.gt else {}

    n_drop = filter_disconnected(plan)

    # Re-derive openings from the architect's colour-coded door/window
    # symbols (red swing arcs = doors, green frames = windows, bare wall
    # gaps = cased openings) instead of the merge-and-infer classifier,
    # which over-merged whole wall runs and dropped arc-marked doors.
    # Done AFTER filter_disconnected so wall indices match the final walls.
    plan["openings"], op_report = dxf_openings.detect(
        args.input, args.wall_layers.split(","), dw.split(","), plan["walls"])
    report["openings"] = op_report

    # Semantic opening overrides (GT sidecar). The colour-coded symbols are
    # authoritative, but a handful of pass-throughs the owner reviewed carry a
    # fact the drawing can't (e.g. a wide gap the plotter drew a leaf on that
    # is really an open cased pass-through, VR note #25). Match a detected
    # opening whose centre is within `tol` (default 30") of `near`; then
    # `remove` it, or set `type` / `width` / `sill` / `shut`. Generic + minimal.
    for oc in (gt.get("openings") or []):
        nx, ny = oc["near"]
        best, bestd = None, oc.get("tol", 30)
        for o in plan["openings"]:
            w = plan["walls"][o["wall_index"]]
            (ax, ay), (bx, by) = w["start"], w["end"]
            p = o["position"]
            d = math.hypot(ax + (bx - ax) * p - nx, ay + (by - ay) * p - ny)
            if d < bestd:
                best, bestd = o, d
        if best is None:
            print(f"fix: opening near ({nx},{ny}) UNMATCHED")
            continue
        if oc.get("remove"):
            plan["openings"].remove(best)
            print(f"fix: opening near ({nx},{ny}) removed")
            continue
        for k in ("type", "width", "sill", "head"):
            if k in oc:
                best[k] = oc[k]
        if oc.get("shut"):
            best["shut"] = True
        print(f"fix: opening near ({nx},{ny}) -> {best.get('type')}")

    # Re-trace rooms with doorway bridges. A door can sit in the gap
    # BETWEEN two collinear wall segments (the dining room's north door);
    # that hole is wider than detect_rooms' junction healing, so the room
    # loop never closes and the room gets no floor. Bridge close,
    # collinear wall-end pairs before polygonizing.
    class _W:
        pass

    def _adapt(c0, c1, th):
        a = _W()
        a.c0, a.c1, a.thickness = tuple(c0), tuple(c1), th
        d = math.hypot(c1[0] - c0[0], c1[1] - c0[1]) or 1.0
        a.axis = ((c1[0] - c0[0]) / d, (c1[1] - c0[1]) / d)
        return a

    ads = [_adapt(w["start"], w["end"], w["thickness"])
           for w in plan["walls"]]
    ends = []
    for w in plan["walls"]:
        d = math.hypot(w["end"][0] - w["start"][0],
                       w["end"][1] - w["start"][1]) or 1.0
        ax = ((w["end"][0] - w["start"][0]) / d,
              (w["end"][1] - w["start"][1]) / d)
        ends.append((w["start"], ax, w["thickness"]))
        ends.append((w["end"], ax, w["thickness"]))
    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            (p, pax, pth), (q, qax, qth) = ends[i], ends[j]
            gap = math.hypot(q[0] - p[0], q[1] - p[1])
            if not (6.0 < gap <= 48.0):
                continue
            if abs(pax[0] * qax[0] + pax[1] * qax[1]) < 0.96:
                continue
            gx, gy = (q[0] - p[0]) / gap, (q[1] - p[1]) / gap
            if abs(gx * pax[0] + gy * pax[1]) < 0.9:
                continue          # ends are beside each other, not in line
            ads.append(_adapt(p, q, min(pth, qth)))
    # explicit GT bridges for corners the polygonizer misses (e.g. the
    # living/sunroom divider's top corner) - [[x0,y0,x1,y1], ...]
    for seg in gt.get("room_bridges", []):
        ads.append(_adapt((seg[0], seg[1]), (seg[2], seg[3]), 4.0))
    rooms2 = X.detect_rooms(ads, plan.get("fixtures", []),
                            plan.setdefault("warnings", []))
    if len(rooms2) >= len(plan.get("rooms", [])):
        plan["rooms"] = rooms2

    stair_labels = []
    if args.rmnames_layer:
        labels = room_labels(args.input, args.rmnames_layer)
        assign_names(plan, labels)
        named_garage = [r for r in plan["rooms"] if r.get("kind") == "garage"]
        report["garage_rooms"] = len(named_garage)
        # stairwell markers the architect places: STAIR + the flight tags
        stair_labels = [pos for name, pos in labels
                        if any(k in name.upper()
                               for k in ("STAIR", "DOWN", "ATTIC", "UP TO"))]
    # unlabeled flights the homeowner wants modeled anyway (the exterior
    # stoop off the dining door) are forced via the GT sidecar
    stair_labels += [tuple(s["near"]) for s in gt.get("stairs", [])
                     if s.get("force")]

    # treads live on the doors/windows layer; the wall layer only adds noise
    n_st = add_stairs(plan, args.input, dw.split(","), gt, stair_labels)

    fx_layers = args.fixture_layers or f"1.{args.floor}FURN"
    n_fx = add_fixtures(plan, args.input, fx_layers.split(","))

    # explicit room-kind overrides from the GT sidecar. Used where the
    # architectural reality isn't derivable from the label - e.g. the
    # sunken sunroom (VR #60) whose slab sits at grade like the garage.
    for rk in (gt.get("room_kinds") or []):
        n = 0
        if "near" in rk:
            nx, ny = rk["near"]
            best, bd = None, rk.get("tol", 80)
            for r in plan["rooms"]:
                poly = r["polygon"]
                rcx = sum(p[0] for p in poly) / len(poly)
                rcy = sum(p[1] for p in poly) / len(poly)
                d = math.hypot(rcx - nx, rcy - ny)
                if d < bd:
                    best, bd = r, d
            if best is not None:
                best["kind"] = rk["kind"]
                n = 1
        else:
            key = rk["name"].strip().upper()
            for r in plan["rooms"]:
                if (r.get("name") or "").strip().upper() == key:
                    r["kind"] = rk["kind"]
                    n += 1
        tgt = rk.get("name") or rk.get("near")
        print(f"fix: room_kind {tgt} -> {rk['kind']} ({n})" if n
              else f"fix: room_kind {tgt} UNMATCHED")

    # room material themes from the GT sidecar (owner's material photos).
    # Each entry: {"name": "KITCHEN"} (applies to every room with that name)
    # or {"near": [x, y], "tol": 60} (nearest room centroid), plus
    # "theme": {"wall": "#hex", "floor": "maple|slate|tile|wood|#hex"}.
    # The viewer paints that room's wall faces / floor accordingly.
    for rt in (gt.get("room_themes") or []):
        matched = []
        if "name" in rt:
            key = rt["name"].strip().upper()
            matched = [r for r in plan["rooms"]
                       if (r.get("name") or "").strip().upper() == key]
        elif "near" in rt:
            nx, ny = rt["near"]
            best, bd = None, rt.get("tol", 60)
            for r in plan["rooms"]:
                poly = r["polygon"]
                rcx = sum(p[0] for p in poly) / len(poly)
                rcy = sum(p[1] for p in poly) / len(poly)
                d = math.hypot(rcx - nx, rcy - ny)
                if d < bd:
                    best, bd = r, d
            matched = [best] if best else []
        if not matched:
            print(f"fix: room_theme {rt.get('name') or rt.get('near')} UNMATCHED")
        for r in matched:
            r["theme"] = rt.get("theme", {})
            print(f"fix: room_theme -> {r.get('name') or 'unnamed'}")

    spawn = default_camera(plan, (gt.get("camera") or {}).get("near"))
    if spawn:
        plan["cameras"] = [spawn]

    # neutral source tag only - the raw DXF's title block carries the
    # owner's name/address, so it is never committed to the public repo
    if args.underlay:
        u = render_underlay(args.input, set(args.wall_layers.split(",")
                                            + dw.split(",")), plan, args.underlay)
        plan["underlay"] = u
    elif os.path.exists(args.output):
        # without --underlay, keep the existing output's underlay registration
        # (the plotted-sheet rasters are hand-registered; a bare regen must not
        # strip their reference from the plan)
        try:
            with open(args.output) as f:
                prev = json.load(f).get("underlay")
            if prev:
                plan["underlay"] = prev
                print(f"underlay: carried forward {prev.get('file')}")
        except Exception:
            pass

    # homeowner-specified furnishings (fireplaces, cubbies...) ride the
    # fixtures pipeline: placement, rendering, and collision for free.
    # Entries without a vendored asset carry stub:"label" and render as a
    # labeled massing block in the viewer.
    for f in gt.get("furnishings", []):
        plan.setdefault("fixtures", []).append(dict(f))

    # Dedup the two fixture sources: extract's INSERT blocks (named, no
    # outline) and dxf_furnish's mined furnishings (outline linework) both
    # land in plan["fixtures"] and double up in dense corners. On overlap
    # keep the richer one - a real outline beats a box, and a named block
    # beats a generic FURN-DRAWN.
    def _ov(a, b):
        (ax, ay), (aw, ad) = a["center"], a["size"]
        (bx, by), (bw, bd) = b["center"], b["size"]
        ox = min(ax + aw / 2, bx + bw / 2) - max(ax - aw / 2, bx - bw / 2)
        oy = min(ay + ad / 2, by + bd / 2) - max(ay - ad / 2, by - bd / 2)
        if ox <= 0 or oy <= 0:
            return 0.0
        return ox * oy / max(1.0, min(aw * ad, bw * bd))

    def _keep(a, b):                       # which of two overlapping to keep
        ac, bc = a.get("cab"), b.get("cab")
        ag = a["name"].endswith("DRAWN")
        bg = b["name"].endswith("DRAWN")
        if ac and bg:                      # a cabinet run beats a bare box
            return a
        if bc and ag:
            return b
        ao, bo = bool(a.get("outline")), bool(b.get("outline"))
        if ao != bo:
            return a if ao else b
        if ag != bg:
            return b if ag else a
        return a
    fx = plan.get("fixtures", [])
    keep = [True] * len(fx)
    for i in range(len(fx)):
        # cabinet runs always survive: a base + its upper stack at the same
        # spot (they must not drop each other), and a long counter run must
        # not be dropped by a small sink/range that sits in it.
        if fx[i].get("cab"):
            continue
        for j in range(len(fx)):
            if i != j and keep[i] and keep[j] and _ov(fx[i], fx[j]) > 0.4 \
                    and _keep(fx[i], fx[j]) is fx[j]:
                keep[i] = False
                break
    dropped = keep.count(False)
    if dropped:
        plan["fixtures"] = [f for i, f in enumerate(fx) if keep[i]]
        report.setdefault("fixture_dedup", dropped)

    # Vendored 3D models attach by location so they survive regeneration:
    # a GT entry {"near":[x,y], "asset":"assets/sofa.glb"} tags the nearest
    # surviving fixture with an `asset` path. The viewer loads that GLB at
    # runtime, scales it to the fixture footprint, and falls back to the
    # schematic render if it can't (missing file / blocked host). Placement
    # stays DXF-authoritative - the model only replaces how the item looks.
    for a in gt.get("assets", []):
        nx, ny = a["near"]
        cand = [f for i, f in enumerate(plan.get("fixtures", []))]
        if not cand:
            print("fix: asset UNMATCHED (no fixtures)")
            continue
        tgt = min(cand, key=lambda f: math.hypot(
            f["center"][0] - nx, f["center"][1] - ny))
        d = math.hypot(tgt["center"][0] - nx, tgt["center"][1] - ny)
        tol = a.get("tol", 60.0)
        if d <= tol:
            tgt["asset"] = a["asset"]
            if "rotation" in a:            # reorient a model to face the room
                tgt["rotation"] = a["rotation"]
            print(f"fix: asset {a['asset']} -> {tgt['name']} ({d:.0f}in)")
        else:
            print(f"fix: asset {a['asset']} UNMATCHED "
                  f"(nearest {tgt['name']} {d:.0f}in > {tol:.0f})")

    if gt.get("palette"):
        plan["palette"] = gt["palette"]
    plan["source"] = {"kind": "dxf", "floor": args.floor}
    plan.setdefault("warnings", [])
    with open(args.output, "w") as f:
        json.dump(plan, f, indent=1)
    print(f"wrote {args.output}: walls={len(plan['walls'])} "
          f"openings={len(plan['openings'])} rooms={len(plan['rooms'])} "
          f"stairs={n_st} "
          f"named={sum(1 for r in plan['rooms'] if r.get('name'))}")


if __name__ == "__main__":
    main()

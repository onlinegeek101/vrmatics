#!/usr/bin/env python3
"""Convert a 2D architectural DXF floor plan into plan.json for the VR viewer.

Pure geometry, deterministic, no AI. The pipeline:

  1. Read LINE / LWPOLYLINE entities on the wall layer(s).
  2. Snap endpoints within `tolerance` so small gaps (messy corners) close.
  3. Pair parallel line segments that face each other within `max_wall`
     (perpendicular distance) into wall "pieces" with a centerline + thickness.
  4. Merge collinear pieces of the same wall; the gaps between pieces are
     candidate openings.
  5. Classify each gap from nearby geometry on the door/window layers:
     a door swing ARC or door INSERT -> door; window glazing LINEs running
     parallel to the wall (or a window INSERT) -> window. Wide unmatched
     gaps become full-height cased openings; narrow ones (breaks drafters
     leave where walls intersect) are filled back in.
  6. Emit plan.json; unpaired/orphan lines are reported, never fatal.

Usage:
    python extract.py input.dxf -o plan.json \
        [--wall-layers A-WALL] [--door-layers A-DOOR] \
        [--window-layers A-GLAZ] [--tolerance 2.0] [--max-wall 12.0]

Layer names are configurable so real CAD exports (DataCAD, AutoCAD) can be
swapped in. Matching is case-insensitive and xref-aware: `A-WALL` also
matches an xref-bound layer like `xref-house$0$A-WALL`.
"""
import argparse
import json
import math
import sys

import ezdxf

WALL_HEIGHT = 96.0      # 8'-0"
DOOR_HEAD = 80.0        # 6'-8"
WINDOW_SILL = 30.0
WINDOW_HEAD = 80.0

ANGLE_TOL_DEG = 2.0     # parallel test
MIN_WALL_THICK = 2.0    # ignore near-zero "thickness" (duplicate/trim lines)
MIN_WALL_LEN = 12.0     # drop merged "walls" shorter than this (jamb caps etc.)
MIN_OPENING = 18.0      # unmatched gaps below this are wall-intersection breaks
MIN_HINT_LINE = 6.0     # ignore tiny tick lines when hunting window glazing


# --------------------------------------------------------------------------
# small vector helpers
# --------------------------------------------------------------------------
def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def length(a):
    return math.hypot(a[0], a[1])


def unit(a):
    n = length(a)
    return (a[0] / n, a[1] / n) if n else (0.0, 0.0)


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


# --------------------------------------------------------------------------
# 1. read segments
# --------------------------------------------------------------------------
class Segment:
    __slots__ = ("a", "b", "handle")

    def __init__(self, a, b, handle=""):
        self.a = (round(a[0], 6), round(a[1], 6))
        self.b = (round(b[0], 6), round(b[1], 6))
        self.handle = handle

    def dir(self):
        return unit(sub(self.b, self.a))

    def length(self):
        return length(sub(self.b, self.a))


def layer_matches(layer, specs):
    """Case-insensitive layer match. A spec matches the layer exactly, or as
    the tail of an xref-bound name (`xref-house$0$A-WALL` matches `A-WALL`),
    so commands stay short even for messy real-world exports."""
    up = layer.upper()
    for s in specs:
        su = s.upper()
        if up == su or up.endswith("$" + su):
            return True
    return False


def read_wall_segments(msp, wall_layers):
    segs = []
    for e in msp:
        if not layer_matches(e.dxf.layer, wall_layers):
            continue
        t = e.dxftype()
        if t == "LINE":
            segs.append(Segment(
                (e.dxf.start.x, e.dxf.start.y),
                (e.dxf.end.x, e.dxf.end.y),
                e.dxf.handle,
            ))
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed and len(pts) > 2:
                pts.append(pts[0])
            for i in range(len(pts) - 1):
                if pts[i] != pts[i + 1]:
                    segs.append(Segment(pts[i], pts[i + 1], e.dxf.handle))
    # drop degenerate zero-length segments
    return [s for s in segs if s.length() > 1e-6]


def read_opening_hints(msp, door_layers, window_layers):
    """Collect evidence of doors/windows from the opening layers.

    Real exports differ: some place door/window BLOCKs (INSERT), others draw
    the symbols inline - a door swing as an ARC, window glazing as LINEs
    across the gap. All three become "hints" a wall gap can match against:

      prio 0  ARC       -> door   (swing; center = hinge point on the wall)
      prio 1  INSERT    -> door or window by layer
      prio 2  LINE      -> window (glazing; must run parallel to the wall)
    """
    hints = []
    for e in msp:
        layer = e.dxf.layer
        is_door = layer_matches(layer, door_layers)
        is_win = layer_matches(layer, window_layers)
        if not (is_door or is_win):
            continue
        t = e.dxftype()
        if t == "ARC":
            c = e.dxf.center
            hints.append({"pt": (c.x, c.y), "kind": "door", "prio": 0,
                          "r": e.dxf.radius})
        elif t == "INSERT":
            kind = "window" if (is_win and not is_door) else "door"
            p = e.dxf.insert
            hints.append({"pt": (p.x, p.y), "kind": kind, "prio": 1})
        elif t == "LINE":
            a = (e.dxf.start.x, e.dxf.start.y)
            b = (e.dxf.end.x, e.dxf.end.y)
            v = sub(b, a)
            ln = length(v)
            if ln < MIN_HINT_LINE:
                continue
            hints.append({
                "pt": ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2),
                "kind": "window", "prio": 2,
                "dir": unit(v), "len": ln,
            })
    return hints


# --------------------------------------------------------------------------
# 2. endpoint snapping
# --------------------------------------------------------------------------
def snap_endpoints(segs, tol):
    """Cluster all endpoints; move each endpoint to its cluster centroid.
    Returns the number of endpoints that actually moved (gaps snapped)."""
    points = []
    for s in segs:
        points.append(s.a)
        points.append(s.b)

    # simple O(n^2) union by proximity - fine for floor-plan sizes
    clusters = []  # list of [members]
    assigned = {}
    for i, p in enumerate(points):
        placed = False
        for ci, c in enumerate(clusters):
            cen = c["centroid"]
            if length(sub(p, cen)) <= tol:
                c["members"].append(p)
                sx = sum(m[0] for m in c["members"])
                sy = sum(m[1] for m in c["members"])
                n = len(c["members"])
                c["centroid"] = (sx / n, sy / n)
                assigned[i] = ci
                placed = True
                break
        if not placed:
            clusters.append({"members": [p], "centroid": p})
            assigned[i] = len(clusters) - 1

    snapped = 0
    for i, s in enumerate(segs):
        ca = clusters[assigned[2 * i]]["centroid"]
        cb = clusters[assigned[2 * i + 1]]["centroid"]
        if length(sub(ca, s.a)) > 1e-6:
            snapped += 1
        if length(sub(cb, s.b)) > 1e-6:
            snapped += 1
        s.a = (round(ca[0], 6), round(ca[1], 6))
        s.b = (round(cb[0], 6), round(cb[1], 6))
    return snapped


# --------------------------------------------------------------------------
# 3. pair parallel segments into wall pieces
# --------------------------------------------------------------------------
def parallel(u, v):
    return abs(cross(u, v)) <= math.sin(math.radians(ANGLE_TOL_DEG))


def project_interval(seg, origin, axis):
    """Return (t_lo, t_hi) of a segment's endpoints projected on `axis`."""
    ta = dot(sub(seg.a, origin), axis)
    tb = dot(sub(seg.b, origin), axis)
    return (min(ta, tb), max(ta, tb))


class WallPiece:
    __slots__ = ("c0", "c1", "thickness", "axis")

    def __init__(self, c0, c1, thickness):
        self.c0 = c0
        self.c1 = c1
        self.thickness = thickness
        self.axis = unit(sub(c1, c0))


def pair_segments(segs, max_wall):
    """Pair each segment with a facing parallel segment to build wall pieces.
    Returns (pieces, used_flags)."""
    n = len(segs)
    used = [False] * n
    pieces = []

    for i in range(n):
        si = segs[i]
        ui = si.dir()
        best = None
        for j in range(i + 1, n):
            if used[j]:
                continue
            sj = segs[j]
            uj = sj.dir()
            if not parallel(ui, uj):
                continue
            # perpendicular distance between the two infinite lines
            perp = (-ui[1], ui[0])
            d = abs(dot(sub(sj.a, si.a), perp))
            if d < MIN_WALL_THICK or d > max_wall:
                continue
            # projections must overlap along the wall direction
            lo_i, hi_i = project_interval(si, si.a, ui)
            lo_j, hi_j = project_interval(sj, si.a, ui)
            ov_lo = max(lo_i, lo_j)
            ov_hi = min(hi_i, hi_j)
            overlap = ov_hi - ov_lo
            if overlap <= max(1.0, 0.0):
                continue
            score = (d, -overlap)
            if best is None or score < best[0]:
                best = (score, j, d, ov_lo, ov_hi, perp)
        if best is None:
            continue

        _, j, d, ov_lo, ov_hi, perp = best
        used[i] = used[j] = True
        # side of sj relative to si along perp -> midline offset
        side = dot(sub(segs[j].a, si.a), perp)
        mid_off = perp[0] * (side / 2.0), perp[1] * (side / 2.0)
        base = si.a
        c0 = (base[0] + ui[0] * ov_lo + mid_off[0],
              base[1] + ui[1] * ov_lo + mid_off[1])
        c1 = (base[0] + ui[0] * ov_hi + mid_off[0],
              base[1] + ui[1] * ov_hi + mid_off[1])
        pieces.append(WallPiece(c0, c1, d))

    orphans = [segs[i] for i in range(n) if not used[i]]
    return pieces, orphans


# --------------------------------------------------------------------------
# 4. merge collinear pieces into walls; gaps between them = openings
# --------------------------------------------------------------------------
def collinear(p, q, tol):
    """Two pieces share the same infinite centerline?"""
    if not parallel(p.axis, q.axis):
        return False
    perp = (-p.axis[1], p.axis[0])
    # both endpoints of q lie on p's line
    d0 = abs(dot(sub(q.c0, p.c0), perp))
    d1 = abs(dot(sub(q.c1, p.c0), perp))
    return d0 <= tol and d1 <= tol


class Wall:
    def __init__(self, c0, c1, thickness):
        self.c0 = c0
        self.c1 = c1
        self.thickness = thickness
        self.gaps = []  # list of (center_dist, width)


def merge_pieces(pieces, tol):
    """Group collinear pieces, then order along the axis and record inter-piece
    gaps as openings. Returns list[Wall]."""
    groups = []
    for pc in pieces:
        for g in groups:
            if collinear(g[0], pc, tol):
                g.append(pc)
                break
        else:
            groups.append([pc])

    walls = []
    for g in groups:
        axis = g[0].axis
        origin = g[0].c0
        # project each piece to [lo, hi] along axis
        spans = []
        thick = []
        for pc in g:
            t0 = dot(sub(pc.c0, origin), axis)
            t1 = dot(sub(pc.c1, origin), axis)
            spans.append((min(t0, t1), max(t0, t1)))
            thick.append(pc.thickness)
        spans.sort()

        # merge overlapping/touching spans, remember gaps between them
        merged = [list(spans[0])]
        gaps = []
        for lo, hi in spans[1:]:
            last = merged[-1]
            if lo <= last[1] + tol:
                last[1] = max(last[1], hi)
            else:
                gaps.append((last[1], lo))  # (gap_start, gap_end)
                merged.append([lo, hi])

        wall_lo = merged[0][0]
        wall_hi = merged[-1][1]
        c0 = (origin[0] + axis[0] * wall_lo, origin[1] + axis[1] * wall_lo)
        c1 = (origin[0] + axis[0] * wall_hi, origin[1] + axis[1] * wall_hi)
        thickness = sum(thick) / len(thick)
        wall = Wall(c0, c1, thickness)
        for gs, ge in gaps:
            center = (gs + ge) / 2.0 - wall_lo
            width = ge - gs
            wall.gaps.append((center, width))
        walls.append(wall)
    return walls


def snap_wall_endpoints(walls, tol):
    """Cluster the centerline endpoints of all walls and snap each to its
    cluster centroid. This closes messy corners where two walls' centerlines
    should meet but miss by a small gap (within tolerance). Returns the number
    of endpoints moved."""
    endpoints = []
    for w in walls:
        endpoints.append(w.c0)
        endpoints.append(w.c1)

    clusters = []
    assigned = {}
    for i, p in enumerate(endpoints):
        for ci, c in enumerate(clusters):
            if length(sub(p, c["centroid"])) <= tol:
                c["members"].append(p)
                sx = sum(m[0] for m in c["members"])
                sy = sum(m[1] for m in c["members"])
                nn = len(c["members"])
                c["centroid"] = (sx / nn, sy / nn)
                assigned[i] = ci
                break
        else:
            clusters.append({"members": [p], "centroid": p})
            assigned[i] = len(clusters) - 1

    snapped = 0
    for i, w in enumerate(walls):
        c0 = clusters[assigned[2 * i]]["centroid"]
        c1 = clusters[assigned[2 * i + 1]]["centroid"]
        if length(sub(c0, w.c0)) > 1e-6:
            snapped += 1
        if length(sub(c1, w.c1)) > 1e-6:
            snapped += 1
        w.c0 = (round(c0[0], 6), round(c0[1], 6))
        w.c1 = (round(c1[0], 6), round(c1[1], 6))
    return snapped


# --------------------------------------------------------------------------
# 5. match gaps to door/window blocks and classify
# --------------------------------------------------------------------------
def classify_openings(walls, hints, tol):
    """Attach a type to each wall gap by matching nearby door/window hints.

    The match radius scales with the gap: a door swing's hinge (arc center)
    sits at one jamb, half the gap width from the gap center. Glazing-line
    hints must also run parallel to the wall and be a plausible fraction of
    the gap width, so a door leaf drawn perpendicular never reads as glass.

    Gaps with no evidence: wide ones become full-height cased openings
    (archways, pass-throughs, the garage opening); narrow ones are the
    breaks drafters leave where a crossing wall meets, and get filled
    back in (no opening emitted). Returns (openings, pass_through, filled).
    """
    used = [False] * len(hints)
    openings = []
    pass_through = 0
    filled = 0

    for wi, wall in enumerate(walls):
        axis = wall.axis
        wall_len = length(sub(wall.c1, wall.c0))
        for center, width in wall.gaps:
            if width < MIN_OPENING:
                filled += 1                   # intersection break: fill solid
                continue
            gp = (wall.c0[0] + axis[0] * center,
                  wall.c0[1] + axis[1] * center)
            radius = width * 0.75 + max(tol * 2.0, 4.0)
            best = None
            for k, h in enumerate(hints):
                if used[k]:
                    continue
                d = length(sub(h["pt"], gp))
                if d > radius:
                    continue
                if h["prio"] == 0:  # swing arc: radius ~ door leaf ~ gap width
                    if not (0.45 * width <= h["r"] <= 1.3 * width):
                        continue
                if h["prio"] == 2:  # glazing line: parallel + sized to gap
                    if not parallel(h["dir"], axis):
                        continue
                    if not (0.3 * width <= h["len"] <= 1.5 * width):
                        continue
                key = (h["prio"], d)
                if best is None or key < best[0]:
                    best = (key, k, h["kind"])

            if best is None:
                kind = "opening"              # cased opening / pass-through
                pass_through += 1
            else:
                _, k, kind = best
                used[k] = True

            if kind == "window":
                sill, head = WINDOW_SILL, WINDOW_HEAD
            else:                             # door and cased opening
                sill, head = 0.0, DOOR_HEAD
            position = center / wall_len if wall_len else 0.0
            openings.append({
                "wall_index": wi,
                "position": round(position, 4),
                "width": round(width, 2),
                "type": kind,
                "sill": sill,
                "head": head,
            })
    return openings, pass_through, filled


# add an axis property to Wall (used above)
Wall.axis = property(lambda self: unit(sub(self.c1, self.c0)))


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def extract(path, wall_layers, door_layers, window_layers, tol, max_wall):
    warnings = []
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    segs = read_wall_segments(msp, wall_layers)
    n_input = len(segs)

    snapped = snap_endpoints(segs, tol)
    pieces, orphans = pair_segments(segs, max_wall)
    walls = merge_pieces(pieces, tol)

    # drop merged "walls" too short to be real (paired jamb caps, trim marks)
    short = [w for w in walls if length(sub(w.c1, w.c0)) < MIN_WALL_LEN]
    walls = [w for w in walls if length(sub(w.c1, w.c0)) >= MIN_WALL_LEN]
    snapped += snap_wall_endpoints(walls, tol)

    hints = read_opening_hints(msp, door_layers, window_layers)
    openings, pass_through, filled = classify_openings(walls, hints, tol)

    for s in orphans:
        warnings.append(
            f"orphan line not paired into a wall: "
            f"({s.a[0]:.1f},{s.a[1]:.1f})->({s.b[0]:.1f},{s.b[1]:.1f})"
        )
    if short:
        warnings.append(
            f"{len(short)} paired piece(s) shorter than {MIN_WALL_LEN:.0f}\" "
            f"dropped (likely jamb caps, not walls)"
        )
    if pass_through:
        warnings.append(
            f"{pass_through} wide gap(s) had no door/window evidence; kept as "
            f"full-height cased openings"
        )

    plan = {
        "units": "inches",
        "walls": [
            {
                "start": [round(w.c0[0], 3), round(w.c0[1], 3)],
                "end": [round(w.c1[0], 3), round(w.c1[1], 3)],
                "thickness": round(w.thickness, 2),
                "height": WALL_HEIGHT,
            }
            for w in walls
        ],
        "openings": openings,
        "warnings": warnings,
    }

    kinds = {}
    for o in openings:
        kinds[o["type"]] = kinds.get(o["type"], 0) + 1
    report = {
        "input_segments": n_input,
        "walls_found": len(walls),
        "openings_matched": len(openings),
        "opening_kinds": kinds,
        "orphan_lines": len(orphans),
        "short_pieces_dropped": len(short),
        "gaps_snapped": snapped,
        "gaps_filled": filled,
        "opening_hints": len(hints),
    }
    return plan, report


def main():
    ap = argparse.ArgumentParser(description="DXF floor plan -> plan.json")
    ap.add_argument("input", help="input DXF file")
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--wall-layers", default="A-WALL",
                    help="comma-separated wall layer names")
    ap.add_argument("--door-layers", default="A-DOOR")
    ap.add_argument("--window-layers", default="A-GLAZ")
    ap.add_argument("--tolerance", type=float, default=2.0,
                    help="endpoint snap tolerance in drawing units (inches)")
    ap.add_argument("--max-wall", type=float, default=12.0,
                    help="max wall thickness for pairing parallel lines")
    args = ap.parse_args()

    def split(s):
        return [x.strip() for x in s.split(",") if x.strip()]

    try:
        plan, report = extract(
            args.input,
            set(split(args.wall_layers)),
            set(split(args.door_layers)),
            set(split(args.window_layers)),
            args.tolerance,
            args.max_wall,
        )
    except IOError:
        print(f"error: cannot read '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except ezdxf.DXFStructureError as exc:
        print(f"error: invalid DXF: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w") as f:
        json.dump(plan, f, indent=2)

    # Sibling .js copy so the viewer also works opened straight from disk
    # (file://), where browsers block fetch() of local JSON.
    embed_path = None
    if args.output.endswith(".json"):
        embed_path = args.output[:-5] + ".js"
        with open(embed_path, "w") as f:
            f.write("window.PLAN_TO_VR_EMBEDDED = ")
            json.dump(plan, f, indent=2)
            f.write(";\n")

    # summary report
    kinds = ", ".join(f"{v} {k}" for k, v in sorted(report["opening_kinds"].items()))
    print(f"Parsed {args.input} -> {args.output}")
    print(f"  input wall segments : {report['input_segments']}")
    print(f"  walls found         : {report['walls_found']}")
    print(f"  openings matched    : {report['openings_matched']}"
          + (f" ({kinds})" if kinds else "")
          + f" from {report['opening_hints']} hints")
    print(f"  gaps snapped        : {report['gaps_snapped']}")
    print(f"  gaps filled (breaks): {report['gaps_filled']}")
    print(f"  orphan lines skipped: {report['orphan_lines']}")
    if report["short_pieces_dropped"]:
        print(f"  short pieces dropped: {report['short_pieces_dropped']}")
    if plan["warnings"]:
        print(f"  warnings            : {len(plan['warnings'])} "
              f"(see 'warnings' in {args.output})")
    if embed_path:
        print(f"  embedded copy       : {embed_path} (lets the viewer open via file://)")


if __name__ == "__main__":
    main()

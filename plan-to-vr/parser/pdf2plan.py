#!/usr/bin/env python3
"""CAD-plotted PDF floor plan -> DXF -> plan.json.

Vector PDFs plotted from CAD (DataCAD, AutoCAD) keep every line segment as
exact coordinates, but lose layers, text (plotted as stroke fonts), and
entity types (arcs become short chords). This tool recovers enough of that
structure to feed the regular DXF pipeline:

  1. extract stroked line segments per page (undoing page /Rotate)
  2. classify by plot weight: heavy strokes are walls, light black strokes
     are symbols (door swings, glazing, fixtures), light gray is the
     existing-conditions xref and dimension text
  3. calibrate paper->world scale by wall-pair thickness: plots get printed
     at half size, so the title block scale cannot be trusted; try each
     hypothesis and keep the one whose paired wall thickness is plausible
     (the same geometry-first trick extract.py uses for DXF units)
  4. drop stray clusters (legend bars, title block art) far from the plan
  5. refit door swing arcs from chains of short chords (Kasa circle fit)
  6. flag window glazing: light segments running along a wall line
  7. emit a real DXF (A-WALL lines, A-DOOR arcs, A-GLAZ lines, inches)
  8. run extract.py's pipeline on it for walls/openings/rooms/footprint

Usage:
    python pdf2plan.py plan.pdf --page 0 -o plan.json [--dxf out.dxf]
                       [--ips auto|0.6667|1.3333] [--debug-png dbg.png]
"""
import argparse
import json
import math
import os
import sys

import ezdxf
import fitz  # PyMuPDF

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract as X

# paper points -> real inches hypotheses:
#   1/4" = 1'-0" plotted full size: 48 real in per paper in = 0.667 in/pt
#   the same sheet printed at half size (Arch D -> B): 96/72 = 1.333 in/pt
#   quarter-size print: 192/72
IPS_HYPOTHESES = (48.0 / 72.0, 96.0 / 72.0, 192.0 / 72.0)

WALL_MIN_WIDTH_PT = 0.42     # plot weight that separates walls from symbols
CLUSTER_JOIN_IN = 60.0       # wall segs closer than this are one structure
CLUSTER_MIN_LEN_IN = 400.0   # real wings carry many feet of wall line
CLUSTER_MIN_DIAG_IN = 200.0  # legend swatch stacks are compact
# arc refitting happens in paper space so the gates hold under any print
# scale; radii are only interpreted as door widths after calibration
ARC_CHORD_MAX_PT = 16.0
ARC_CHAIN_TOL_PT = 0.6
ARC_MIN_PTS = 6
ARC_MAX_RMS_PT = 0.5
ARC_RADIUS_IN = (16.0, 48.0)  # door leaves 16"-48" once scaled
ARC_SPAN_DEG = (40.0, 190.0)
ARC_VOTE_IN = (22.0, 44.0)    # radii typical enough to vote on print scale
GLAZ_LEN_IN = (16.0, 130.0)   # glazing runs the width of the window
GLAZ_WALL_DIST_IN = 10.0
GLAZ_EXTEND_IN = 140.0  # wall line extended for mid-band glazing
GLAZ_PARALLEL_DEG = 4.0
GRAY_MIN_LEN_IN = 12.0        # demo walls are dashed => short fragments
GRAY_TOUCH_IN = 8.0           # recovered walls must join the new network


def page_segments(page):
    """All stroked line segments in y-up page space (points), classified.

    get_drawings() reports raw (unrotated) coordinates; apply the page
    rotation matrix so the plan reads the way the sheet is meant to hang.
    Returns dict: class name -> [((x0,y0),(x1,y1)), ...]
    """
    rot = page.rotation_matrix
    H = page.rect.height

    def xf(p):
        q = fitz.Point(p.x, p.y) * rot
        return (q.x, H - q.y)   # display space is y-down; plans are y-up

    out = {"wall": [], "symbol": [], "gray": [], "poche": [], "arrow": []}
    for path in page.get_drawings():
        if "f" in path["type"]:
            f = path.get("fill")
            # small solid-black fills are arrowheads (dimension ends and
            # stair walk-line tips); keep their centroids
            if f and max(f) < 0.05:
                r = path["rect"]
                if max(r.width, r.height) <= 14.0:
                    c = xf(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
                    out["arrow"].append(c)
            # wall poche plots as a neutral gray fill; arrowheads are
            # black, the sheet logo is tinted, paper knockouts are white
            if f and abs(f[0] - f[1]) < 0.02 and abs(f[1] - f[2]) < 0.02 \
                    and 0.3 <= f[0] <= 0.95:
                poly = []
                for it in path["items"]:
                    if it[0] == "l":
                        poly.append(xf(it[1]))
                        poly.append(xf(it[2]))
                if len(poly) >= 6:
                    out["poche"].append(poly)
        if "s" not in path["type"]:
            continue
        col = path.get("color") or (0, 0, 0)
        w = path.get("width") or 0.0
        gray = max(col) > 0.2   # plotted black is (0,0,0); xref is 0.4 gray
        if gray:
            key = "gray"
        elif w >= WALL_MIN_WIDTH_PT:
            key = "wall"
        else:
            key = "symbol"
        for it in path["items"]:
            if it[0] == "l":
                out[key].append((xf(it[1]), xf(it[2])))
    return out


def to_segs(pairs, scale):
    return [X.Segment((a[0] * scale, a[1] * scale),
                      (b[0] * scale, b[1] * scale)) for a, b in pairs]


DASH_MAX_PIECE_PT = 6.0      # a dash is a short piece...
DASH_MIN_PIECES = 4          # ...repeated along a line...
DASH_MAX_SOLIDITY = 0.82     # ...with air between the pieces


def split_dashed(pairs):
    """Separate dashed linework (demo walls, hidden/overhead lines) from
    solid strokes, in paper space.

    Renovation sheets carry a second, historical drawing in dashes -
    walls to demolish, roof lines overhead - and every downstream stage
    is cleaner without it. A segment is dashed only when it is SHORT and
    belongs to a merged collinear run of several short pieces whose
    inked fraction is low; long solid members sharing the line (a real
    wall continuing where a dashed run ends) never qualify.
    Returns (solid_pairs, dashed_pairs).
    """
    RUN_BREAK = 10.0             # gap that ends a dashed run, points
    buckets = {}                 # (axis, line offset) -> [(lo, hi, idx)]
    diag = []
    for idx, (a, b) in enumerate(pairs):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        if L > DASH_MAX_PIECE_PT or L == 0:
            continue
        if abs(dy) <= 0.3:       # horizontal piece on line y
            key = ("h", round(a[1] / 0.8))
            lo, hi = sorted((a[0], b[0]))
        elif abs(dx) <= 0.3:     # vertical piece on line x
            key = ("v", round(a[0] / 0.8))
            lo, hi = sorted((a[1], b[1]))
        else:
            diag.append(idx)     # diagonal dashes are rare; keep them
            continue
        buckets.setdefault(key, []).append((lo, hi, idx))

    dashed_idx = set()
    for pieces in buckets.values():
        if len(pieces) < DASH_MIN_PIECES:
            continue
        pieces.sort()
        run = []
        for lo, hi, k in pieces + [(1e18, 1e18, -1)]:
            if run and lo - run[-1][1] > RUN_BREAK:
                if len(run) >= DASH_MIN_PIECES:
                    extent = run[-1][1] - run[0][0]
                    inked = sum(h - l for l, h, _ in run)
                    if extent > 0 and inked / extent <= DASH_MAX_SOLIDITY:
                        dashed_idx.update(kk for _, _, kk in run)
                run = []
            if k >= 0:
                run.append((lo, hi, k))

    solid, dashed = [], []
    for idx, pair in enumerate(pairs):
        (dashed if idx in dashed_idx else solid).append(pair)
    return solid, dashed


def calibrate_scale(wall_pairs, paper_arcs, forced=None):
    """Pick inches-per-point so walls come out wall-thick and door swings
    door-sized.

    Prints get made at half or quarter size without anyone updating the
    title block, so measure instead of trusting it. Two independent
    signals vote:
      - paired parallel wall lines must sit 3"-14" apart, ideally near a
        5.5" stud wall (plots fragment walls at every opening, so no
        length-ratio requirement like detect_units uses on DXF)
      - refit door-swing arcs must land at plausible leaf radii; a swing
        that reads 20" under one hypothesis reads 40" under the next, so
        counting radii inside the common 22"-44" band separates them
    """
    if forced:
        return forced, "forced"
    best = None
    for ips in IPS_HYPOTHESES:
        segs = to_segs(wall_pairs, ips)
        pieces, _ = X.pair_segments(segs, 14.0)
        if len(pieces) < 4:
            continue
        th = sorted(p.thickness for p in pieces)
        median = th[len(th) // 2]
        if not 3.0 <= median <= 14.0:
            continue
        # real walls exist: several pieces must be much longer than thick
        long_pieces = sum(
            1 for p in pieces
            if X.length(X.sub(p.c1, p.c0)) >= 8.0 * p.thickness)
        if long_pieces < 3:
            continue
        votes = sum(1 for a in paper_arcs
                    if ARC_VOTE_IN[0] <= a["r"] * ips <= ARC_VOTE_IN[1])
        score = votes * 2.0 - abs(median - 5.5)
        if best is None or score > best[0]:
            best = (score, ips, median, len(pieces), votes)
    if best is None:
        raise SystemExit(
            "could not calibrate scale: no hypothesis yields plausible "
            "wall thicknesses; pass --ips explicitly")
    _, ips, median, n, votes = best
    return ips, (f"median wall {median:.1f}\" across {n} pairs, "
                 f"{votes} door-radius arcs agree")


def main_cluster(segs, join=CLUSTER_JOIN_IN):
    """Keep wall segments that belong to the building, drop sheet furniture.

    Legend swatches and title-block rules are also plotted at wall weight,
    so connectivity alone cannot separate them: openings legitimately cut
    wall runs into far-apart fragments (a 16' garage door leaves a bigger
    hole than the gap between the plan and the legend). Two rules instead:
      - a cluster is structure if it carries real length and real extent
        (many feet of wall line spread over a real footprint)
      - small clusters are rescued if they sit inside the structure's
        bounding box (jamb stubs between garage doors, chimney), while
        compact clusters outside it (legend, title art) stay dropped
    """
    n = len(segs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def boxes_near(s, t, d):
        ax0 = min(s.a[0], s.b[0]) - d; ax1 = max(s.a[0], s.b[0]) + d
        ay0 = min(s.a[1], s.b[1]) - d; ay1 = max(s.a[1], s.b[1]) + d
        bx0 = min(t.a[0], t.b[0]);     bx1 = max(t.a[0], t.b[0])
        by0 = min(t.a[1], t.b[1]);     by1 = max(t.a[1], t.b[1])
        return ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) != find(j) and boxes_near(segs[i], segs[j], join):
                parent[find(i)] = find(j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(segs[i])

    def cluster_stats(g):
        total = sum(X.length(X.sub(s.b, s.a)) for s in g)
        xs = [c[0] for s in g for c in (s.a, s.b)]
        ys = [c[1] for s in g for c in (s.a, s.b)]
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        return total, diag, (min(xs), min(ys), max(xs), max(ys))

    main, rest = [], []
    bbox = None
    for g in groups.values():
        total, diag, bb = cluster_stats(g)
        if total >= CLUSTER_MIN_LEN_IN and diag >= CLUSTER_MIN_DIAG_IN:
            main.extend(g)
            bbox = bb if bbox is None else (
                min(bbox[0], bb[0]), min(bbox[1], bb[1]),
                max(bbox[2], bb[2]), max(bbox[3], bb[3]))
        else:
            rest.append(g)

    kept = list(main)
    if bbox:
        m = 24.0
        for g in rest:
            _, _, bb = cluster_stats(g)
            if (bb[0] >= bbox[0] - m and bb[2] <= bbox[2] + m and
                    bb[1] >= bbox[1] - m and bb[3] <= bbox[3] + m):
                kept.extend(g)
    return kept, n - len(kept)


def chain_segments(segs, tol=0.4):
    """Join short chords end-to-end into polylines (dash/arc reassembly)."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))

    ends = {}
    for i, s in enumerate(segs):
        for p in (s.a, s.b):
            ends.setdefault(key(p), []).append(i)

    seen = [False] * len(segs)
    chains = []
    for i, s in enumerate(segs):
        if seen[i]:
            continue
        seen[i] = True
        chain = [s.a, s.b]
        for head in (0, 1):
            while True:
                tip = chain[0] if head == 0 else chain[-1]
                nxt = None
                for j in ends.get(key(tip), []):
                    if not seen[j]:
                        nxt = j
                        break
                if nxt is None:
                    break
                seen[nxt] = True
                t = segs[nxt]
                da = math.dist(t.a, tip)
                p = t.b if da <= math.dist(t.b, tip) else t.a
                if head == 0:
                    chain.insert(0, p)
                else:
                    chain.append(p)
        chains.append(chain)
    return chains


def fit_circle(pts):
    """Kasa least-squares circle fit -> (cx, cy, r, rms)."""
    n = len(pts)
    sx = sum(p[0] for p in pts) / n
    sy = sum(p[1] for p in pts) / n
    u = [p[0] - sx for p in pts]
    v = [p[1] - sy for p in pts]
    suu = sum(a * a for a in u); svv = sum(a * a for a in v)
    suv = sum(a * b for a, b in zip(u, v))
    suuu = sum(a ** 3 for a in u); svvv = sum(a ** 3 for a in v)
    suvv = sum(a * b * b for a, b in zip(u, v))
    svuu = sum(b * a * a for a, b in zip(u, v))
    det = suu * svv - suv * suv
    if abs(det) < 1e-9:
        return None
    cx = (suvv + suuu) / 2 * svv - (svuu + svvv) / 2 * suv
    cy = (svuu + svvv) / 2 * suu - (suvv + suuu) / 2 * suv
    cx /= det; cy /= det
    r = math.sqrt(cx * cx + cy * cy + (suu + svv) / n)
    cx += sx; cy += sy
    rms = math.sqrt(sum((math.dist(p, (cx, cy)) - r) ** 2 for p in pts) / n)
    return cx, cy, r, rms


def find_door_arcs_paper(symbol_pairs):
    """Refit door swings from chord chains, in paper (point) space.

    Working in points keeps the chord/rms gates independent of the print
    scale, so the same fits can vote on the scale itself. Radii and
    centers are interpreted in inches later by multiplying with ips.
    """
    segs = [X.Segment(a, b) for a, b in symbol_pairs
            if math.dist(a, b) <= ARC_CHORD_MAX_PT]
    arcs = []
    for chain in chain_segments(segs, tol=ARC_CHAIN_TOL_PT):
        pts = list(chain)
        if len(pts) < ARC_MIN_PTS:
            continue
        # chains pick up the door leaf and jamb ticks along with the swing;
        # shed the worst point and refit instead of rejecting outright
        fit = fit_circle(pts)
        drops = max(2, len(pts) // 3)
        while fit and fit[3] > ARC_MAX_RMS_PT and drops > 0 \
                and len(pts) > ARC_MIN_PTS:
            cx, cy, r, _ = fit
            worst = max(range(len(pts)),
                        key=lambda i: abs(math.dist(pts[i], (cx, cy)) - r))
            pts.pop(worst)
            drops -= 1
            fit = fit_circle(pts)
        if not fit:
            continue
        cx, cy, r, rms = fit
        if rms > ARC_MAX_RMS_PT:
            continue
        angs = sorted(math.atan2(p[1] - cy, p[0] - cx) for p in pts)
        # widest gap between successive point angles marks the arc's ends
        gaps = [(angs[(i + 1) % len(angs)] - angs[i]) % (2 * math.pi)
                for i in range(len(angs))]
        big = max(range(len(gaps)), key=lambda i: gaps[i])
        a0 = angs[(big + 1) % len(angs)]
        a1 = angs[big]
        span = math.degrees((a1 - a0) % (2 * math.pi))
        if not ARC_SPAN_DEG[0] <= span <= ARC_SPAN_DEG[1]:
            continue
        arcs.append({"center": (cx, cy), "r": r,
                     "a0": math.degrees(a0), "a1": math.degrees(a1)})
    return arcs


def scale_arcs(paper_arcs, ips):
    """Keep paper arcs that are door-sized under the chosen scale."""
    out = []
    for a in paper_arcs:
        r = a["r"] * ips
        if ARC_RADIUS_IN[0] <= r <= ARC_RADIUS_IN[1]:
            out.append({"center": (a["center"][0] * ips,
                                   a["center"][1] * ips),
                        "r": r, "a0": a["a0"], "a1": a["a1"]})
    return out


def find_glazing(symbol_segs, wall_segs, panels=()):
    """Light segments running along a wall line = window glazing.

    The wall line is extended past each segment's ends when testing
    proximity - the glazing of a wide window band sits mid-gap, far from
    any finite wall piece, but exactly on the interrupted wall's line.
    Door panels (garage doors, sliders) also lie on that line, so any
    piece inside a flagged panel span is excluded. Counter fronts and
    cabinet faces along interior walls still slip through; the footprint
    pass cleans those up after extraction.
    """
    near_wall = []
    for s in symbol_segs:
        v = X.sub(s.b, s.a)
        L = X.length(v)
        if not GLAZ_LEN_IN[0] <= L <= GLAZ_LEN_IN[1]:
            continue
        u = X.unit(v)
        mid = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
        if any(min(pa[0], pb[0]) - 4 <= mid[0] <= max(pa[0], pb[0]) + 4 and
               min(pa[1], pb[1]) - 4 <= mid[1] <= max(pa[1], pb[1]) + 4
               for pa, pb in panels):
            continue
        for w in wall_segs:
            wv = X.sub(w.b, w.a)
            wl = X.length(wv)
            if wl < 6.0:
                continue
            wu = X.unit(wv)
            cosang = abs(u[0] * wu[0] + u[1] * wu[1])
            if cosang < math.cos(math.radians(GLAZ_PARALLEL_DEG)):
                continue
            t = ((mid[0] - w.a[0]) * wu[0] + (mid[1] - w.a[1]) * wu[1])
            t = max(-GLAZ_EXTEND_IN, min(wl + GLAZ_EXTEND_IN, t))
            foot = (w.a[0] + wu[0] * t, w.a[1] + wu[1] * t)
            if math.dist(mid, foot) <= GLAZ_WALL_DIST_IN:
                near_wall.append(s)
                break
    return near_wall


# symbol-weight wall recovery (sunroom window bands, porch knee walls)
# the floor is high because pairing prefers the thinnest partner: shorter
# runs (window pane sections ~30") would steal a band face at a smaller
# spacing than the true opposite face and shred the band into confetti
SYM_RUN_MIN_IN = 50.0        # merged run must be this long to be a wall
SYM_RUN_GAP_IN = 6.0         # mullion breaks bridged when merging runs
SYM_RUN_SOLID = 0.75         # solid fraction: dashed lines stay rejected
SYM_TOUCH_IN = 8.0           # both run ends must reach the wall network


def merge_runs(segs, gap=SYM_RUN_GAP_IN, lateral=0.75):
    """Merge collinear segments into runs, bridging small gaps.

    Returns [(a, b, solid_fraction)] where solid_fraction is how much of
    the run's extent is actually inked - dashed lines merge into runs
    too, but their fraction stays low and the caller drops them.
    """
    used = [False] * len(segs)
    runs = []
    for i, s in enumerate(segs):
        if used[i]:
            continue
        u = X.unit(X.sub(s.b, s.a))
        group = [s]
        used[i] = True
        changed = True
        while changed:
            changed = False
            lo = min(min(q[0] * u[0] + q[1] * u[1] for q in (t.a, t.b))
                     for t in group)
            hi = max(max(q[0] * u[0] + q[1] * u[1] for q in (t.a, t.b))
                     for t in group)
            ref = group[0].a
            for j, t in enumerate(segs):
                if used[j]:
                    continue
                tu = X.unit(X.sub(t.b, t.a))
                if abs(tu[0] * u[0] + tu[1] * u[1]) < 0.999:
                    continue
                mid = ((t.a[0] + t.b[0]) / 2, (t.a[1] + t.b[1]) / 2)
                lat = abs((mid[0] - ref[0]) * -u[1] + (mid[1] - ref[1]) * u[0])
                if lat > lateral:
                    continue
                t0 = min(t.a[0] * u[0] + t.a[1] * u[1],
                         t.b[0] * u[0] + t.b[1] * u[1])
                t1 = max(t.a[0] * u[0] + t.a[1] * u[1],
                         t.b[0] * u[0] + t.b[1] * u[1])
                if t0 > hi + gap or t1 < lo - gap:
                    continue
                group.append(t)
                used[j] = True
                changed = True
        lo = min(min(q[0] * u[0] + q[1] * u[1] for q in (t.a, t.b))
                 for t in group)
        hi = max(max(q[0] * u[0] + q[1] * u[1] for q in (t.a, t.b))
                 for t in group)
        if hi - lo < 1.0:
            continue
        inked = sum(X.length(X.sub(t.b, t.a)) for t in group)
        ref = group[0].a
        base = ref[0] * u[0] + ref[1] * u[1]
        a = (ref[0] + u[0] * (lo - base), ref[1] + u[1] * (lo - base))
        b = (ref[0] + u[0] * (hi - base), ref[1] + u[1] * (hi - base))
        runs.append((a, b, min(1.0, inked / (hi - lo))))
    return runs


def recover_symbol_walls(symbol_segs, wall_segs):
    """Walls drawn at symbol weight: sunroom window bands, glazed porches.

    These plot as thin double lines the wall classifier skips, in a layer
    shared with furniture, stairs, appliances and section symbols. The
    guards, in order:
      - merged runs must be long and mostly inked (dashes fail solidity)
      - runs must pair at wall thickness, like any wall
      - ladder runs (a third parallel line at the same spacing: stair
        treads, decking) are rejected
      - pieces overlapping an already-detected wall are drawn detail
        (backsplashes, cabinet faces), not structure
      - hatched pieces are section symbols (the steel-beam callout)
      - both piece ends must reach the wall network
    Returns (wall edge lines, panel spans): the gap-infill rejects are
    door panels drawn inside openings, and knowing where they are lets
    the opening classifier tell a paneled garage door from a window band.
    """
    panels = []
    cands = []
    for a, b, solid in merge_runs(
            [s for s in symbol_segs
             if X.length(X.sub(s.b, s.a)) >= 2.0]):
        if math.dist(a, b) >= SYM_RUN_MIN_IN and solid >= SYM_RUN_SOLID:
            cands.append(X.Segment(a, b))
    if not cands:
        return [], panels
    pieces, _ = X.pair_segments(list(cands), 14.0)

    def perp(u):
        return (-u[1], u[0])

    def near_seg(pt, s, tol):
        ax, ay = s.a
        vx, vy = s.b[0] - ax, s.b[1] - ay
        L2 = vx * vx + vy * vy
        t = 0.0 if not L2 else max(0.0, min(1.0, ((pt[0] - ax) * vx +
                                                  (pt[1] - ay) * vy) / L2))
        return math.dist(pt, (ax + vx * t, ay + vy * t)) <= tol

    # hatch density map: short diagonal strokes mark section symbols
    diags = []
    for s in symbol_segs:
        L = X.length(X.sub(s.b, s.a))
        if 0.5 <= L <= 8.0:
            u = X.unit(X.sub(s.b, s.a))
            if 0.25 <= abs(u[0]) <= 0.97:
                diags.append(((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))

    kept = []
    for p in pieces:
        u = X.unit(X.sub(p.c1, p.c0))
        n = perp(u)
        L = X.length(X.sub(p.c1, p.c0))
        mid = ((p.c0[0] + p.c1[0]) / 2, (p.c0[1] + p.c1[1]) / 2)
        # ladder: another candidate parallel at ~thickness beyond a face
        ladder = False
        for s in cands:
            su = X.unit(X.sub(s.b, s.a))
            if abs(su[0] * u[0] + su[1] * u[1]) < 0.985:
                continue
            smid = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
            d = abs((smid[0] - mid[0]) * n[0] + (smid[1] - mid[1]) * n[1])
            along = abs((smid[0] - mid[0]) * u[0] + (smid[1] - mid[1]) * u[1])
            if along > L / 2 + 6:
                continue
            if 0.6 * p.thickness <= d - p.thickness / 2 <= 1.6 * p.thickness \
                    and X.length(X.sub(s.b, s.a)) >= 0.5 * L:
                ladder = True
                break
        if ladder:
            continue
        # overlap with detected walls: drawn detail on the wall, not a
        # wall. Even partial overlap disqualifies - a wall-weight run
        # interrupted by garage doors still owns its line, and doubling
        # it here shreds the pairing downstream
        samples = [(p.c0[0] + (p.c1[0] - p.c0[0]) * t,
                    p.c0[1] + (p.c1[1] - p.c0[1]) * t)
                   for t in (0.1, 0.3, 0.5, 0.7, 0.9)]
        hits = sum(1 for q in samples
                   if any(near_seg(q, s, 4.0) for s in wall_segs))
        if hits >= 2:
            continue
        # hatch: section symbol, not structure
        n_hatch = sum(1 for hx, hy in diags
                      if abs((hx - mid[0]) * u[0] + (hy - mid[1]) * u[1])
                      <= L / 2 + 2
                      and abs((hx - mid[0]) * n[0] + (hy - mid[1]) * n[1])
                      <= p.thickness + 4)
        if n_hatch >= 6:
            continue
        # gap infill: garage/entry door panels plot as thin double lines
        # inside an opening, collinear with the interrupted wall run. If
        # long wall strokes continue the piece's line on BOTH sides, the
        # piece is a panel in that run's gap, not new structure (a
        # sunroom band that extends the building only has wall on one
        # side of it)
        lo = min(X.dot(p.c0, u), X.dot(p.c1, u))
        hi = max(X.dot(p.c0, u), X.dot(p.c1, u))
        before = after = False
        for s in wall_segs:
            su = X.unit(X.sub(s.b, s.a))
            if abs(su[0] * u[0] + su[1] * u[1]) < 0.999:
                continue
            if X.length(X.sub(s.b, s.a)) < 18.0:
                continue
            smid = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
            lat = abs((smid[0] - mid[0]) * n[0] + (smid[1] - mid[1]) * n[1])
            if lat > 3.5:
                continue
            s0, s1 = X.dot(s.a, u), X.dot(s.b, u)
            if max(s0, s1) <= lo + 6:
                before = True
            if min(s0, s1) >= hi - 6:
                after = True
        if before and after:
            panels.append((p.c0, p.c1))
            continue
        kept.append(p)

    # connectivity: both ends must reach the network (walls or other
    # accepted pieces), which floating furniture outlines never do
    def end_ok(pt, others):
        for s in wall_segs:
            if near_seg(pt, s, SYM_TOUCH_IN):
                return True
        for q in others:
            if q is not None and near_seg(pt, X.Segment(q.c0, q.c1),
                                          SYM_TOUCH_IN):
                return True
        return False

    alive = list(kept)
    changed = True
    while changed:
        changed = False
        for i, p in enumerate(alive):
            if p is None:
                continue
            others = [q for j, q in enumerate(alive) if j != i]
            if not (end_ok(p.c0, others) and end_ok(p.c1, others)):
                alive[i] = None
                changed = True
    final = [p for p in alive if p is not None]

    lines = []
    for p in final:
        u = X.unit(X.sub(p.c1, p.c0))
        n = perp(u)
        t = p.thickness / 2
        lines.append(X.Segment((p.c0[0] + n[0] * t, p.c0[1] + n[1] * t),
                               (p.c1[0] + n[0] * t, p.c1[1] + n[1] * t)))
        lines.append(X.Segment((p.c0[0] - n[0] * t, p.c0[1] - n[1] * t),
                               (p.c1[0] - n[0] * t, p.c1[1] - n[1] * t)))
    return lines, panels


def default_camera(plan, prefer=None):
    """Spawn the walkthrough just inside the main entry.

    The main entry is the widest exterior hinged door (footprint probe:
    one side in, one side out) - or, when `prefer` gives a plan-inch
    point, the exterior door nearest it (homeowner-pinned entry). Doors
    marked shut never win. The camera stands two feet inside, facing
    into the house - the view a visitor gets stepping in.
    """
    fp = plan.get("footprint") or []
    if len(fp) < 3:
        return None
    best = None
    for o in plan["openings"]:
        if o["type"] != "door" or o.get("shut"):
            continue
        w = plan["walls"][o["wall_index"]]
        dx = w["end"][0] - w["start"][0]
        dy = w["end"][1] - w["start"][1]
        L = math.hypot(dx, dy) or 1.0
        gx = w["start"][0] + dx * o["position"]
        gy = w["start"][1] + dy * o["position"]
        off = w["thickness"] / 2 + 4.0
        nx, ny = -dy / L * off, dx / L * off
        in1 = point_in_poly(gx + nx, gy + ny, fp)
        in2 = point_in_poly(gx - nx, gy - ny, fp)
        if in1 == in2:
            continue                       # interior door
        sgn = 1.0 if in1 else -1.0         # inward normal
        score = (-math.hypot(gx - prefer[0], gy - prefer[1]) if prefer
                 else o["width"])
        if best is None or score > best[0]:
            ux, uy = -dy / L * sgn, dx / L * sgn
            px = gx + ux * (w["thickness"] / 2 + 24.0)
            py = gy + uy * (w["thickness"] / 2 + 24.0)
            # viewer yaw: 0 faces plan north (+y); positive per
            # dolly.rotation.y, forward = (-sin yaw, +cos yaw)
            yaw = math.degrees(math.atan2(-ux, uy))
            best = (score, px, py, yaw)
    if best is None:
        return None
    return {
        "position": [round(best[1], 2), round(best[2], 2)],
        "rotation": round(best[3], 2),
        "is_primary": True,
    }


def point_in_poly(px, py, poly):
    hit = False
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        if (ay > py) != (by > py):
            x = ax + (py - ay) * (bx - ax) / (by - ay)
            if x > px:
                hit = not hit
    return hit


def _op_center(plan, o):
    w = plan["walls"][o["wall_index"]]
    ax, ay = w["start"]
    bx, by = w["end"]
    return (ax + (bx - ax) * o["position"], ay + (by - ay) * o["position"])


def apply_corrections(plan, fixes):
    """Fold in homeowner ground truth that no geometry rule can infer:
    which stair runs descend, sheet ambiguities (a band that is really
    window-wall-door), labeled dimensions, walls the linework breaks but
    reality doesn't. Matching is by proximity in plan inches so the fixes
    survive regeneration; every application (or miss) is logged."""
    for fx in fixes.get("stairs") or []:
        tx, ty = fx["near"]
        best, bd = None, 1e9
        for st in plan.get("stairs") or []:
            xs = [p[0] for p in st["polygon"]]
            ys = [p[1] for p in st["polygon"]]
            d = math.hypot((min(xs) + max(xs)) / 2 - tx,
                           (min(ys) + max(ys)) / 2 - ty)
            if d < bd:
                bd, best = d, st
        if best is None or bd > 90:
            print(f"fix: stair near {fx['near']}: NO MATCH ({bd:.0f}\" off)")
            continue
        if "split_y" in fx:
            y0 = fx["split_y"]
            halves = []
            for key in ("south", "north"):
                props = fx.get(key)
                if props is None:
                    continue
                treads = [t for t in best["treads"]
                          if ((t[0][1] + t[1][1]) / 2 < y0) == (key == "south")]
                if not treads:
                    continue
                xs = [q[0] for t in treads for q in t]
                ys = [q[1] for t in treads for q in t]
                st2 = {"polygon": [[min(xs), min(ys)], [max(xs), min(ys)],
                                   [max(xs), max(ys)], [min(xs), max(ys)]],
                       "treads": treads}
                st2.update(props)
                halves.append(st2)
            plan["stairs"].remove(best)
            plan["stairs"].extend(halves)
            print(f"fix: stair near {fx['near']}: split at y={y0} "
                  f"into {len(halves)} runs")
        else:
            for k in ("down", "direction"):
                if k in fx:
                    best[k] = fx[k]
            print(f"fix: stair near {fx['near']}: "
                  + ", ".join(f"{k}={fx[k]}" for k in ("down", "direction")
                              if k in fx))
    for fx in fixes.get("openings") or []:
        tx, ty = fx["near"]
        best, bd = None, 1e9
        for o in plan["openings"]:
            cx, cy = _op_center(plan, o)
            d = math.hypot(cx - tx, cy - ty)
            if d < bd:
                bd, best = d, o
        if best is None or bd > 30:
            print(f"fix: opening near {fx['near']}: NO MATCH ({bd:.0f}\" off)")
            continue
        if fx.get("remove"):
            plan["openings"].remove(best)
            print(f"fix: opening near {fx['near']}: removed "
                  f"({best['type']} {best['width']}\")")
            continue
        applied = []
        if "width" in fx:
            best["width"] = fx["width"]
            applied.append(f"width={fx['width']}")
        if "type" in fx:
            best["type"] = fx["type"]
            applied.append(f"type={fx['type']}")
        if fx.get("shut"):
            best["shut"] = True
            applied.append("shut")
        print(f"fix: opening near {fx['near']}: " + ", ".join(applied))


def fix_window_sides(plan, panels=()):
    """Swap misread opening types using the footprint as the arbiter.

    Glazing evidence from a plot is noisy in both directions: counter
    fronts along interior walls read as glass, while some real windows
    plot only jamb ticks and match nothing. The footprint decides which
    is which topologically - probe a point on each side of the gap:
      - one side in, one side out  -> a facade gap: windows stay, and
        hint-less holes become windows (an outside wall never has an
        open hole) - EXCEPT gaps holding a door panel (the thin double
        line drawn inside garage doors and sliders), which stay open
      - both sides inside          -> an interior gap: 'windows' here
        are counter lines, so they become the cased openings they are
    Distance to the boundary is NOT a substitute: walls flanking an
    interior notch (garage/house junction) sit near the outline yet
    face inside on both sides. Width is not a substitute for the panel
    test either: window bands run wider than single garage doors.
    """
    fp = plan.get("footprint") or []
    if len(fp) < 3:
        return 0, 0

    def inside(px, py):
        return point_in_poly(px, py, fp)

    demoted = promoted = 0
    for o in plan["openings"]:
        if o["type"] not in ("window", "opening"):
            continue
        w = plan["walls"][o["wall_index"]]
        gx = w["start"][0] + (w["end"][0] - w["start"][0]) * o["position"]
        gy = w["start"][1] + (w["end"][1] - w["start"][1]) * o["position"]
        dx = w["end"][0] - w["start"][0]
        dy = w["end"][1] - w["start"][1]
        L = math.hypot(dx, dy) or 1.0
        off = w["thickness"] / 2 + 4.0
        nx, ny = -dy / L * off, dx / L * off
        sides = inside(gx + nx, gy + ny) + inside(gx - nx, gy - ny)
        paneled = any(
            min(pa[0], pb[0]) - 8 <= gx <= max(pa[0], pb[0]) + 8 and
            min(pa[1], pb[1]) - 8 <= gy <= max(pa[1], pb[1]) + 8
            for pa, pb in panels)
        if o["type"] == "window" and sides == 2:
            o["type"] = "opening"
            o["sill"] = 0.0
            demoted += 1
        elif o["type"] == "opening" and sides == 1 and not paneled:
            o["type"] = "window"
            o["sill"] = X.WINDOW_SILL
            o["head"] = X.WINDOW_HEAD
            promoted += 1
    return demoted, promoted


def poche_wall_edges(poche_polys, ips, black_segs):
    """Wall lines from poche fill polygons, where strokes don't cover them.

    The plot fills every wall that survives the renovation - existing gray
    and new dark alike - with a neutral gray poche polygon, while demo
    walls, furniture, fixtures and dimension art get no fill. That makes
    the fill layer the authoritative wall mask: the long edges of each
    poche polygon are exactly the wall faces, no matter which stroke
    weight drew them. New-construction faces are already drawn at wall
    weight, so only edges the stroke set does NOT cover are added -
    otherwise every wall face doubles up and pairing falls apart.
    """
    def covered(a, b):
        L = math.dist(a, b)
        n = max(2, int(L / 6.0))
        hit = 0
        for i in range(n + 1):
            t = i / n
            p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            for s in black_segs:
                sx, sy = s.a
                vx, vy = s.b[0] - sx, s.b[1] - sy
                L2 = vx * vx + vy * vy
                u = 0.0 if not L2 else max(0.0, min(1.0, (
                    (p[0] - sx) * vx + (p[1] - sy) * vy) / L2))
                if math.dist(p, (sx + vx * u, sy + vy * u)) <= 1.5:
                    hit += 1
                    break
        return hit / (n + 1) >= 0.5

    lines = []
    for poly in poche_polys:
        pts = [(p[0] * ips, p[1] * ips) for p in poly]
        for i in range(0, len(pts) - 1, 2):
            a, b = pts[i], pts[i + 1]
            if math.dist(a, b) >= GRAY_MIN_LEN_IN and not covered(a, b):
                lines.append(X.Segment(a, b))
    return lines


STAIR_TREAD_LEN = (24.0, 78.0)   # tread line length, inches
STAIR_SPACING = (8.0, 14.0)      # tread-to-tread spacing in plan
STAIR_MIN_TREADS = 4


def find_stairs(symbol_segs, gray_segs, wall_segs, arrows=()):
    """Stair runs: evenly spaced parallel tread lines (the ladder pattern
    every wall-recovery guard rejects, captured on purpose this time).

    Returns [{polygon, treads, direction?}] with the run rectangle and
    each tread as a segment, in inches. Handles horizontal and vertical
    runs; break-symbol gaps inside a run are bridged by allowing one
    missing tread, and runs split by a break symbol or landing are merged
    when they continue each other. A run must hug a wall along at least
    one side - sofa cushion seams and other furniture striping float in
    open floor instead. The walk-line arrow, when present, gives the
    run's pointing direction (which end the arrow aims at - up vs down
    still lives only in the unreadable plotted text).
    """
    def wall_beside(poly):
        (x0, y0), (x2, y2) = poly[0], poly[2]
        xa, xb = sorted((x0, x2))
        ya, yb = sorted((y0, y2))
        for s in wall_segs:
            if abs(s.a[0] - s.b[0]) <= 1.0:       # vertical wall stroke
                sy = sorted((s.a[1], s.b[1]))
                if min(sy[1], yb) - max(sy[0], ya) >= 0.6 * (yb - ya) and \
                        (abs(s.a[0] - xa) <= 8 or abs(s.a[0] - xb) <= 8):
                    return True
            if abs(s.a[1] - s.b[1]) <= 1.0:       # horizontal wall stroke
                sx = sorted((s.a[0], s.b[0]))
                if min(sx[1], xb) - max(sx[0], xa) >= 0.6 * (xb - xa) and \
                        (abs(s.a[1] - ya) <= 8 or abs(s.a[1] - yb) <= 8):
                    return True
        return False
    def collect(cands, horiz):
        # index treads by their cross-axis extent so runs group together
        treads = []
        for s in cands:
            dx = abs(s.b[0] - s.a[0])
            dy = abs(s.b[1] - s.a[1])
            L = math.hypot(dx, dy)
            if not STAIR_TREAD_LEN[0] <= L <= STAIR_TREAD_LEN[1]:
                continue
            if horiz and dy > 1.0:
                continue
            if not horiz and dx > 1.0:
                continue
            lo, hi = (sorted((s.a[0], s.b[0])) if horiz
                      else sorted((s.a[1], s.b[1])))
            pos = s.a[1] if horiz else s.a[0]
            treads.append((lo, hi, pos, s))
        treads.sort(key=lambda t: t[2])
        used = [False] * len(treads)
        runs = []
        for i in range(len(treads)):
            if used[i]:
                continue
            chain = [treads[i]]
            used[i] = True
            while True:
                last = chain[-1]
                nxt = None
                for j in range(len(treads)):
                    if used[j]:
                        continue
                    lo, hi, pos, _ = treads[j]
                    gap = pos - last[2]
                    if gap <= 0.5:
                        continue
                    if gap > 2.2 * STAIR_SPACING[1]:
                        break    # sorted: nothing closer will follow
                    ov = min(hi, last[1]) - max(lo, last[0])
                    if ov < 0.6 * min(hi - lo, last[1] - last[0]):
                        continue
                    if STAIR_SPACING[0] <= gap <= STAIR_SPACING[1] or \
                            2 * STAIR_SPACING[0] <= gap <= 2 * STAIR_SPACING[1]:
                        nxt = j
                        break
                if nxt is None:
                    break
                chain.append(treads[nxt])
                used[nxt] = True
            if len(chain) < STAIR_MIN_TREADS:
                continue
            lo = min(t[0] for t in chain)
            hi = max(t[1] for t in chain)
            p0, p1 = chain[0][2], chain[-1][2]
            if horiz:
                poly = [[lo, p0], [hi, p0], [hi, p1], [lo, p1]]
                tl = [[[t[0], t[2]], [t[1], t[2]]] for t in chain]
            else:
                poly = [[p0, lo], [p0, hi], [p1, hi], [p1, lo]]
                tl = [[[t[2], t[0]], [t[2], t[1]]] for t in chain]
            runs.append({
                "polygon": [[round(x, 2), round(y, 2)] for x, y in poly],
                "treads": [[[round(v, 2) for v in q] for q in t]
                           for t in tl],
                "_horiz": horiz,     # treads horizontal => run climbs in y
            })
        return runs

    cands = symbol_segs + gray_segs
    runs = collect(cands, True) + collect(cands, False)

    def bbox(r):
        xs = [q[0] for q in r["polygon"]]
        ys = [q[1] for q in r["polygon"]]
        return min(xs), min(ys), max(xs), max(ys)

    # the walk line: a long line along the run's centerline whose tip
    # carries an arrowhead. It both points the run and vouches for runs
    # whose flanks are stringers rather than modeled walls (dimension
    # arrows elsewhere never sit on a run's centerline)
    def walk_line(r):
        x0, y0, x1, y1 = bbox(r)
        w_, h_ = x1 - x0, y1 - y0
        horiz_run = r["_horiz"]       # treads horizontal => climbs in y
        best = None
        for sg in symbol_segs:
            mx = (sg.a[0] + sg.b[0]) / 2
            my = (sg.a[1] + sg.b[1]) / 2
            if not (x0 - 4 <= mx <= x1 + 4 and y0 - 4 <= my <= y1 + 4):
                continue
            dxs = abs(sg.b[0] - sg.a[0])
            dys = abs(sg.b[1] - sg.a[1])
            L = math.hypot(dxs, dys)
            if horiz_run:
                if dxs > dys * 0.35 or L < 0.45 * h_:
                    continue
                lat = abs(mx - (x0 + x1) / 2)
                if lat > 0.3 * w_:
                    continue
            else:
                if dys > dxs * 0.35 or L < 0.45 * w_:
                    continue
                lat = abs(my - (y0 + y1) / 2)
                if lat > 0.3 * h_:
                    continue
            # resolve the pointing end. Two drafting conventions: a
            # solid fill is either the arrowhead itself (dimension
            # style) or the start-of-travel dot, with an open stroke-V
            # at the actual tip. A V of short diagonals wins; a lone
            # fill on one end is then the start marker.
            u = X.unit(X.sub(sg.b, sg.a))

            def fill_at(tip, d):
                for ah in arrows:
                    rel = (ah[0] - tip[0], ah[1] - tip[1])
                    along = rel[0] * d[0] + rel[1] * d[1]
                    lat = abs(rel[0] * -d[1] + rel[1] * d[0])
                    if -2.0 <= along <= 10.0 and lat <= 3.0:
                        return True
                return False

            def vee_at(tip):
                n = 0
                for t in symbol_segs:
                    for e in (t.a, t.b):
                        if math.dist(e, tip) > 3.0:
                            continue
                        tv = X.unit(X.sub(t.b, t.a))
                        c = abs(tv[0] * u[0] + tv[1] * u[1])
                        tl = X.length(X.sub(t.b, t.a))
                        if 2.0 <= tl <= 14.0 and 0.3 <= c <= 0.99:
                            n += 1
                        break
                return n >= 2

            fa = fill_at(sg.a, (-u[0], -u[1])) if arrows else False
            fb = fill_at(sg.b, u) if arrows else False
            va, vb = vee_at(sg.a), vee_at(sg.b)
            tip = tail = None
            if va != vb:
                tip, tail = (sg.a, sg.b) if va else (sg.b, sg.a)
            elif fa != fb:
                tip, tail = (sg.a, sg.b) if fa else (sg.b, sg.a)
            if tip is None:
                continue
            if best is None or L > best[0]:
                best = (L, X.unit(X.sub(tip, tail)))
        return best

    def has_break(r):
        # the floor cut plane is drawn as a zigzag polyline crossing the
        # run where it continues below - i.e. a DOWN flight. The zigzag
        # is a thin band: wide across the run, a few inches along it.
        # (Door-swing arc chords are also short diagonals, but their
        # chains spread as far along the run as across it.)
        x0, y0, x1, y1 = bbox(r)
        horiz = r["_horiz"]
        ix0, iy0, ix1, iy1 = x0, y0, x1, y1
        if horiz:
            iy0 -= 42; iy1 += 42
        else:
            ix0 -= 42; ix1 += 42
        cand = [sg for sg in symbol_segs
                if X.length(X.sub(sg.b, sg.a)) <= 30.0
                and ix0 - 1 <= (sg.a[0] + sg.b[0]) / 2 <= ix1 + 1
                and iy0 - 1 <= (sg.a[1] + sg.b[1]) / 2 <= iy1 + 1]
        for chain in chain_segments(list(cand), tol=1.2):
            xs = [q[0] for q in chain]
            ys = [q[1] for q in chain]
            if horiz:
                across = max(xs) - min(xs)
                along = max(ys) - min(ys)
                cross_w = x1 - x0
            else:
                across = max(ys) - min(ys)
                along = max(xs) - min(xs)
                cross_w = y1 - y0
            if along <= 14.0 and 1.0 <= along and \
                    across >= 0.45 * cross_w:
                return True
        return False

    for r in runs:
        r["_down"] = has_break(r)

    # merge runs the break symbol split: same cross extent, small gap,
    # and the same up/down character (never fuse an up flight with the
    # down flight beside it)
    merged = True
    while merged:
        merged = False
        for i in range(len(runs)):
            for j in range(i + 1, len(runs)):
                a, b = bbox(runs[i]), bbox(runs[j])
                same_x = abs(a[0] - b[0]) <= 8 and abs(a[2] - b[2]) <= 8
                same_y = abs(a[1] - b[1]) <= 8 and abs(a[3] - b[3]) <= 8
                gap_y = max(a[1], b[1]) - min(a[3], b[3])
                gap_x = max(a[0], b[0]) - min(a[2], b[2])
                if runs[i]["_down"] != runs[j]["_down"]:
                    continue
                if (same_x and gap_y <= 3 * STAIR_SPACING[1]) or \
                        (same_y and gap_x <= 3 * STAIR_SPACING[1]):
                    x0 = min(a[0], b[0]); y0 = min(a[1], b[1])
                    x1 = max(a[2], b[2]); y1 = max(a[3], b[3])
                    runs[i] = {
                        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                        "treads": runs[i]["treads"] + runs[j]["treads"],
                        "_horiz": runs[i]["_horiz"],
                        "_down": runs[i]["_down"],
                    }
                    runs.pop(j)
                    merged = True
                    break
            if merged:
                break


    kept = []
    for r in runs:
        wl = walk_line(r)
        if not (wall_beside(r["polygon"]) or wl):
            continue
        if wl:
            r["direction"] = [round(wl[1][0], 4), round(wl[1][1], 4)]
        if r["_down"]:
            r["down"] = True      # cut by the floor plane: descends
        r.pop("_horiz", None)
        r.pop("_down", None)
        kept.append(r)
    return kept


CHIMNEY_SIDE_IN = (24.0, 96.0)


def find_chimneys(symbol_segs, wall_segs):
    """Masonry masses: a closed symbol-weight rect with coursing rungs.

    The chimney plots as a rectangle bumped out from an exterior wall,
    capped at the far end and striped with full-width ledger lines. Too
    thick to pair as a wall, it would otherwise vanish from the model.
    The full-width requirement on the rungs is what rejects lookalikes:
    roof/eave tick pairs also stand off the wall at chimney-ish spacing,
    but the only horizontals crossing them are the wall lines themselves,
    which run far past the pair.
    """
    runs = merge_runs([s for s in symbol_segs
                       if X.length(X.sub(s.b, s.a)) >= 2.0])
    def vert(r):
        return abs(r[0][0] - r[1][0]) < 1.0
    def horz(r):
        return abs(r[0][1] - r[1][1]) < 1.0
    vs = [r for r in runs if vert(r)
          and CHIMNEY_SIDE_IN[0] <= abs(r[0][1] - r[1][1]) <= CHIMNEY_SIDE_IN[1]]
    hs = [r for r in runs if horz(r)]

    fixtures = []
    for i, v1 in enumerate(vs):
        for v2 in vs[i + 1:]:
            wdt = abs(v1[0][0] - v2[0][0])
            if not CHIMNEY_SIDE_IN[0] <= wdt <= CHIMNEY_SIDE_IN[1]:
                continue
            y1 = sorted((v1[0][1], v1[1][1]))
            y2 = sorted((v2[0][1], v2[1][1]))
            if abs(y1[0] - y2[0]) > 6 or abs(y1[1] - y2[1]) > 6:
                continue
            x0, x1_ = sorted((v1[0][0], v2[0][0]))
            # caps and rungs must span the rect and not run past it
            spans = []
            for h in hs:
                hx = sorted((h[0][0], h[1][0]))
                if abs(hx[0] - x0) <= 4 and abs(hx[1] - x1_) <= 4 \
                        and y1[0] - 4 <= h[0][1] <= y1[1] + 4:
                    spans.append(h[0][1])
            if not 3 <= len(spans) <= 7:
                # cap plus a few coursing rungs; a stair run inside a
                # footprint notch mimics the pattern but has a tread
                # every foot, far more lines than masonry coursing
                continue
            # masonry is drawn hollow except for coursing: appliance
            # stacks (washer/dryer/sink) mimic the rect+rungs pattern
            # but carry their own interior vertical edges
            hollow = True
            for r in runs:
                if not vert(r) or math.dist(r[0], r[1]) < 12:
                    continue
                rx = r[0][0]
                ry = sorted((r[0][1], r[1][1]))
                if x0 + 3 < rx < x1_ - 3 and \
                        min(ry[1], y1[1]) - max(ry[0], y1[0]) >= \
                        0.3 * (y1[1] - y1[0]):
                    hollow = False
                    break
            if not hollow:
                continue
            if max(spans) < y1[1] - 6 and min(spans) > y1[0] + 6:
                continue            # no cap at either end
            # slots between two parallel walls are stairwells or chases,
            # never chimneys - masonry abuts a wall on one end at most
            flank = [False, False]
            for s in wall_segs:
                if abs(s.a[0] - s.b[0]) > 1.0:
                    continue          # want walls parallel to the sides
                sy = sorted((s.a[1], s.b[1]))
                if min(sy[1], y1[1]) - max(sy[0], y1[0]) < \
                        0.5 * (y1[1] - y1[0]):
                    continue
                if -14 <= s.a[0] - x0 <= 2:
                    flank[0] = True
                if -2 <= s.a[0] - x1_ <= 14:
                    flank[1] = True
            if flank[0] and flank[1]:
                continue
            # must abut the wall network
            corners = [(x0, y1[0]), (x1_, y1[0]), (x0, y1[1]), (x1_, y1[1])]
            def near_wall(pt):
                for s in wall_segs:
                    ax, ay = s.a
                    vx, vy = s.b[0] - ax, s.b[1] - ay
                    L2 = vx * vx + vy * vy
                    t = 0.0 if not L2 else max(0.0, min(1.0, (
                        (pt[0] - ax) * vx + (pt[1] - ay) * vy) / L2))
                    if math.dist(pt, (ax + vx * t, ay + vy * t)) <= 10.0:
                        return True
                return False
            if not any(near_wall(c) for c in corners):
                continue
            fixtures.append({
                "name": "CHIMNEY",
                "center": [round((x0 + x1_) / 2, 2),
                           round((y1[0] + y1[1]) / 2, 2)],
                "rotation": 0.0,
                "size": [round(wdt, 2), round(y1[1] - y1[0], 2)],
                "height": 0.0,   # filled in with the wall height later
            })
    return fixtures


def write_dxf(path, wall_segs, arcs, glaz_segs, stairs=()):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 1   # inches
    for name, color in (("A-WALL", 7), ("A-DOOR", 1), ("A-GLAZ", 5),
                        ("A-STRS", 3)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    for s in wall_segs:
        msp.add_line(s.a, s.b, dxfattribs={"layer": "A-WALL"})
    for a in arcs:
        msp.add_arc(a["center"], a["r"], a["a0"], a["a1"],
                    dxfattribs={"layer": "A-DOOR"})
    for s in glaz_segs:
        msp.add_line(s.a, s.b, dxfattribs={"layer": "A-GLAZ"})
    for st in stairs:
        pts = st["polygon"] + [st["polygon"][0]]
        msp.add_lwpolyline(pts, dxfattribs={"layer": "A-STRS"})
        for t in st["treads"]:
            msp.add_line(tuple(t[0]), tuple(t[1]),
                         dxfattribs={"layer": "A-STRS"})
    doc.saveas(path)


def debug_png(path, wall_segs, arcs, glaz_segs, dropped_note=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    fig, ax = plt.subplots(figsize=(16, 11), dpi=110)
    ax.add_collection(LineCollection(
        [[s.a, s.b] for s in wall_segs], colors="black", linewidths=0.9))
    ax.add_collection(LineCollection(
        [[s.a, s.b] for s in glaz_segs], colors="tab:blue", linewidths=0.8))
    for a in arcs:
        c = plt.Circle(a["center"], a["r"], fill=False,
                       color="tab:red", linewidth=0.8)
        ax.add_patch(c)
    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_title(f"walls={len(wall_segs)} doors={len(arcs)} "
                 f"glaz={len(glaz_segs)} {dropped_note}")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def export_underlay(page, plan, ips, out_png, margin=50.0):
    """Save a raster crop of the sheet around the plan, plus the metadata
    the viewer needs to lay it under the model at true scale.

    The crop is the wall bounding box plus a margin, which conveniently
    leaves the title block and its personal details out of the image.
    """
    import numpy as np
    from matplotlib.image import imsave

    xs = [c for w in plan["walls"] for c in (w["start"][0], w["end"][0])]
    ys = [c for w in plan["walls"] for c in (w["start"][1], w["end"][1])]
    if not xs:
        return None
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    dpi = 144.0
    k = dpi / 72.0
    pix = page.get_pixmap(dpi=int(dpi))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    H = page.rect.height
    px0 = max(0, int(x0 / ips * k))
    px1 = min(pix.width, int(x1 / ips * k))
    py0 = max(0, int((H - y1 / ips) * k))
    py1 = min(pix.height, int((H - y0 / ips) * k))
    if px1 <= px0 or py1 <= py0:
        return None
    imsave(out_png, img[py0:py1, px0:px1])
    return {
        "file": os.path.basename(out_png),
        # plan inches of the image top-left corner + inches per pixel
        "x0": round(px0 / k * ips, 3),
        "y1": round((H - py0 / k) * ips, 3),
        "in_per_px": round(ips / k, 6),
    }


def main():
    ap = argparse.ArgumentParser(description="CAD-plotted PDF -> plan.json")
    ap.add_argument("input")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--dxf", default="", help="also save the generated DXF")
    ap.add_argument("--ips", default="auto",
                    help="inches per PDF point, or 'auto' to calibrate")
    ap.add_argument("--wall-height", type=float, default=96.0)
    ap.add_argument("--debug-png", default="")
    ap.add_argument("--underlay", default="",
                    help="save a sheet raster crop for the viewer underlay")
    ap.add_argument("--fix", default="",
                    help="ground-truth corrections JSON (homeowner review); "
                         "see corrections/README")
    args = ap.parse_args()

    doc = fitz.open(args.input)
    page = doc[args.page]
    raw = page_segments(page)
    print(f"page {args.page}: wall-weight segs {len(raw['wall'])}, "
          f"symbol segs {len(raw['symbol'])}, gray segs {len(raw['gray'])}")

    # strip dashed historical linework (demo walls, roof/overhead lines)
    # before anything downstream sees it
    ndash = {}
    for key in ("wall", "symbol", "gray"):
        raw[key], gone = split_dashed(raw[key])
        ndash[key] = len(gone)
    print(f"dashed pieces filtered: wall {ndash['wall']}, "
          f"symbol {ndash['symbol']}, gray {ndash['gray']}")

    paper_arcs = find_door_arcs_paper(raw["symbol"])
    forced = None if args.ips == "auto" else float(args.ips)
    ips, why = calibrate_scale(raw["wall"], paper_arcs, forced)
    print(f"scale: {ips:.4f} real inches per point ({why})")

    stroke_walls = to_segs(raw["wall"], ips)
    poche = poche_wall_edges(raw["poche"], ips, stroke_walls)
    print(f"wall edges from poche fills: {len(poche)} uncovered "
          f"({len(raw['poche'])} filled wall polygons)")

    # recover symbol-weight walls against the UNclustered stroke set: the
    # posts a sunroom band ends on are themselves tiny clusters that only
    # survive the cluster filter once the band has extended the main bbox
    # over them - so recovery runs first, clustering last
    symbol_segs = to_segs(raw["symbol"], ips)
    sym_walls, panels = recover_symbol_walls(symbol_segs, stroke_walls + poche)
    print(f"symbol-weight walls recovered: {len(sym_walls) // 2}; "
          f"door panels flagged: {len(panels)}")

    wall_segs, dropped = main_cluster(stroke_walls + poche + sym_walls)
    print(f"wall segs kept {len(wall_segs)}, dropped {dropped} "
          f"(legend/title-block clusters)")

    arcs = scale_arcs(paper_arcs, ips)
    glaz = find_glazing(symbol_segs, wall_segs, panels)
    print(f"door swing arcs refit: {len(arcs)}; glazing lines: {len(glaz)}")

    gray_in = to_segs(raw["gray"], ips)
    arrows_in = [(a[0] * ips, a[1] * ips) for a in raw["arrow"]]
    stairs = find_stairs(symbol_segs, gray_in, wall_segs, arrows_in)
    print(f"stair runs detected: {len(stairs)}")

    dxf_path = args.dxf or (os.path.splitext(args.output)[0] + ".dxf")
    write_dxf(dxf_path, wall_segs, arcs, glaz, stairs)
    print(f"wrote {dxf_path}")

    if args.debug_png:
        debug_png(args.debug_png, wall_segs, arcs, glaz)
        print(f"wrote {args.debug_png}")

    plan, report = X.extract(
        dxf_path, ["A-WALL"], ["A-DOOR"], ["A-GLAZ"],
        tol=2.0, max_wall=14.0, units="inches")
    for w in plan["walls"]:
        w["height"] = args.wall_height
    plan["stairs"] = stairs
    fixes = json.load(open(args.fix)) if args.fix else {}
    if (fixes.get("fixtures") or {}).get("remove_chimneys"):
        # homeowner review: the rect+rungs masses on these sheets are
        # exterior steps / a sunroom connection, not chimneys
        chimneys = []
        print("fix: chimney detector disabled for this sheet")
    else:
        chimneys = find_chimneys(symbol_segs, wall_segs)
    fp = plan.get("footprint") or []
    if len(fp) >= 3:
        # a chimney bumps out of the building; appliance stacks and
        # window symbols that mimic its rect+rungs pattern sit inside
        chimneys = [c for c in chimneys
                    if not point_in_poly(c["center"][0], c["center"][1], fp)]
    else:
        chimneys = []
    if chimneys:
        print(f"chimney masses kept (outside footprint): {len(chimneys)}")
    for c in chimneys:
        c["height"] = args.wall_height
    plan["fixtures"] = (plan.get("fixtures") or []) + chimneys

    n_dem, n_pro = fix_window_sides(plan, panels)
    if n_dem or n_pro:
        print(f"window/opening fixes: {n_dem} interior windows demoted, "
              f"{n_pro} exterior gaps promoted to windows")
        # garage classification ran inside extract(), before these type
        # fixes - a slider that just became a window may have qualified a
        # room as a garage. Reclassify against the corrected openings.
        class _W:
            pass
        adapters = []
        for w in plan["walls"]:
            a = _W()
            a.c0 = tuple(w["start"])
            a.c1 = tuple(w["end"])
            a.thickness = w["thickness"]
            adapters.append(a)
        for r in plan["rooms"]:
            if r["kind"] == "garage":
                r["kind"] = "room"
        X.classify_garages(plan["rooms"], adapters, plan["openings"],
                           plan["warnings"], plan["footprint"])
    if fixes:
        apply_corrections(plan, fixes)
        plan.setdefault("warnings", []).append(
            "homeowner corrections applied: " + os.path.basename(args.fix))
    # camera last: shut doors and corrected openings change the entry pick
    spawn = default_camera(plan, (fixes.get("camera") or {}).get("near"))
    if spawn:
        plan["cameras"] = [spawn]
    plan["source"] = {
        "kind": "pdf", "page": args.page,
        "inches_per_point": round(ips, 4),
    }
    if args.underlay:
        meta = export_underlay(page, plan, ips, args.underlay)
        if meta:
            plan["underlay"] = meta
            print(f"wrote {args.underlay} (sheet underlay)")

    with open(args.output, "w") as f:
        json.dump(plan, f, indent=1)
    print(f"wrote {args.output}")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()

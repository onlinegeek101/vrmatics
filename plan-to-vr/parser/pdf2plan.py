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

    out = {"wall": [], "symbol": [], "gray": [], "poche": []}
    for path in page.get_drawings():
        if "f" in path["type"]:
            f = path.get("fill")
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


def find_glazing(symbol_segs, wall_segs):
    """Light segments running along a wall line = window glazing.

    Counter fronts and cabinet faces also run along walls and can't be
    told from glass here; interior false windows are cleaned up after
    extraction instead, where the footprint says which walls face out.
    """
    near_wall = []
    for s in symbol_segs:
        v = X.sub(s.b, s.a)
        L = X.length(v)
        if not GLAZ_LEN_IN[0] <= L <= GLAZ_LEN_IN[1]:
            continue
        u = X.unit(v)
        mid = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
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
            t = max(0.0, min(wl, t))
            foot = (w.a[0] + wu[0] * t, w.a[1] + wu[1] * t)
            if math.dist(mid, foot) <= GLAZ_WALL_DIST_IN:
                near_wall.append(s)
                break
    return near_wall


GARAGE_DOOR_MIN = 96.0


def fix_window_sides(plan):
    """Swap misread opening types using the footprint as the arbiter.

    Glazing evidence from a plot is noisy in both directions: counter
    fronts along interior walls read as glass, while some real windows
    plot only jamb ticks and match nothing. The footprint disambiguates:
      - interior 'windows' become the cased openings they are
      - exterior hint-less gaps become windows (an outside wall never
        has an open hole), unless they are garage-door wide
    Rare true interior windows and hint-less exterior doors lose out;
    both beat glass in a hallway or a hole in the facade.
    """
    fp = plan.get("footprint") or []
    if len(fp) < 3:
        return 0, 0

    def fp_dist(gx, gy):
        best = 1e9
        for i in range(len(fp)):
            ax, ay = fp[i]
            bx, by = fp[(i + 1) % len(fp)]
            vx, vy = bx - ax, by - ay
            L2 = vx * vx + vy * vy
            t = 0.0 if not L2 else max(0.0, min(1.0, (
                (gx - ax) * vx + (gy - ay) * vy) / L2))
            best = min(best, math.hypot(gx - (ax + vx * t),
                                        gy - (ay + vy * t)))
        return best

    demoted = promoted = 0
    for o in plan["openings"]:
        if o["type"] not in ("window", "opening"):
            continue
        w = plan["walls"][o["wall_index"]]
        gx = w["start"][0] + (w["end"][0] - w["start"][0]) * o["position"]
        gy = w["start"][1] + (w["end"][1] - w["start"][1]) * o["position"]
        exterior = fp_dist(gx, gy) <= w["thickness"] + 6.0
        if o["type"] == "window" and not exterior:
            o["type"] = "opening"
            o["sill"] = 0.0
            demoted += 1
        elif o["type"] == "opening" and exterior \
                and o["width"] < GARAGE_DOOR_MIN:
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


def write_dxf(path, wall_segs, arcs, glaz_segs):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 1   # inches
    for name, color in (("A-WALL", 7), ("A-DOOR", 1), ("A-GLAZ", 5)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    for s in wall_segs:
        msp.add_line(s.a, s.b, dxfattribs={"layer": "A-WALL"})
    for a in arcs:
        msp.add_arc(a["center"], a["r"], a["a0"], a["a1"],
                    dxfattribs={"layer": "A-DOOR"})
    for s in glaz_segs:
        msp.add_line(s.a, s.b, dxfattribs={"layer": "A-GLAZ"})
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
    args = ap.parse_args()

    doc = fitz.open(args.input)
    page = doc[args.page]
    raw = page_segments(page)
    print(f"page {args.page}: wall-weight segs {len(raw['wall'])}, "
          f"symbol segs {len(raw['symbol'])}, gray segs {len(raw['gray'])}")

    paper_arcs = find_door_arcs_paper(raw["symbol"])
    forced = None if args.ips == "auto" else float(args.ips)
    ips, why = calibrate_scale(raw["wall"], paper_arcs, forced)
    print(f"scale: {ips:.4f} real inches per point ({why})")

    stroke_walls = to_segs(raw["wall"], ips)
    poche = poche_wall_edges(raw["poche"], ips, stroke_walls)
    print(f"wall edges from poche fills: {len(poche)} uncovered "
          f"({len(raw['poche'])} filled wall polygons)")
    wall_segs, dropped = main_cluster(stroke_walls + poche)
    print(f"wall segs kept {len(wall_segs)}, dropped {dropped} "
          f"(legend/title-block clusters)")

    symbol_segs = to_segs(raw["symbol"], ips)
    arcs = scale_arcs(paper_arcs, ips)
    glaz = find_glazing(symbol_segs, wall_segs)
    print(f"door swing arcs refit: {len(arcs)}; glazing lines: {len(glaz)}")

    dxf_path = args.dxf or (os.path.splitext(args.output)[0] + ".dxf")
    write_dxf(dxf_path, wall_segs, arcs, glaz)
    print(f"wrote {dxf_path}")

    if args.debug_png:
        debug_png(args.debug_png, wall_segs, arcs, glaz)
        print(f"wrote {args.debug_png}")

    plan, report = X.extract(
        dxf_path, ["A-WALL"], ["A-DOOR"], ["A-GLAZ"],
        tol=2.0, max_wall=14.0, units="inches")
    for w in plan["walls"]:
        w["height"] = args.wall_height
    n_dem, n_pro = fix_window_sides(plan)
    if n_dem or n_pro:
        print(f"window/opening fixes: {n_dem} interior windows demoted, "
              f"{n_pro} exterior gaps promoted to windows")
    plan["source"] = {
        "kind": "pdf", "page": args.page,
        "inches_per_point": round(ips, 4),
    }
    with open(args.output, "w") as f:
        json.dump(plan, f, indent=1)
    print(f"wrote {args.output}")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()

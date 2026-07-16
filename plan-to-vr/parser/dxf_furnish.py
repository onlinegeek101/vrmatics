#!/usr/bin/env python3
"""Mine the architect DXF for drawn furnishings -> stub entries.

Everything placed here is read off the drawing itself - no photo guessing,
no hand placement. Sources, in order of authority:

 1. Text labels sitting ON the items they describe ("3'6" x 8' island",
    "36\" gas range", "Soaking tub", "5' vanity", "DW"...). A keyword
    catalog maps each to a footprint; labels that carry dimensions are
    parsed. The label's insert point is only approximate, so each label is
    SNAPPED to the nearby linework cluster whose bbox best matches the
    expected footprint - the cluster only re-centers the item, the size
    stays the parsed/catalog size (never grows past 1.3x of it).
 2. Linework clusters on the doors/windows layer (and the FURN layer):
    fixtures and furniture are drawn as islands of lines/arcs/ellipses.
    Union-find on entity proximity, INCLUDING near-wall clusters (toilets,
    tubs, showers, vanities and fridges all sit against walls). Door swing
    arcs, window glazing bands, stair-tread ladders and rug outlines are
    excluded; what remains is classified heuristically (toilet, tub,
    shower, vanity, fridge, bed...) or kept as generic FURN-DRAWN.
 3. HATCH masses on the doors/windows layer that straddle a wall are the
    chimney masses -> FURN-FIREPLACE.

Writes the proposals into the GT sidecar's "furnishings" with
"auto": true (re-runs replace only auto entries; hand-placed ones and
homeowner labels are never touched, and mined items that land on a manual
entry are dropped - the manual placement is authoritative).

Usage: dxf_furnish.py plan.dxf corrections.json --prefix 1.1 \
           --wall-layer 1.1WALL [--plan viewer/plans/home-l1.json]
"""
import argparse
import collections
import json
import math
import re

import ezdxf

# keyword -> (name, (w, d) or None = parse from label, height); first match
CATALOG = [
    ("ISLAND", "FURN-ISLAND", None, 36.0),
    ("GAS RANGE", "FURN-RANGE", (30, 26), 36.0),
    ("RANGE", "FURN-RANGE", (30, 26), 36.0),
    ("SOAKING TUB", "FURN-TUB", (60, 32), 24.0),
    ("TUB/SHOWER", "FURN-TUB", (60, 30), 24.0),
    ("TUB", "FURN-TUB", (60, 30), 24.0),
    ("SHOWER", "FURN-SHOWER", None, 80.0),
    ("VANITY", "FURN-VANITY", None, 34.0),
    ("SINK", "FURN-SINK", (30, 22), 36.0),
    ("DW", "FURN-DISHWASHER", (24, 24), 34.0),
    ("REFR", "FURN-FRIDGE", (36, 30), 70.0),
    ("FRIDGE", "FURN-FRIDGE", (36, 30), 70.0),
    ("REF", "FURN-FRIDGE", (36, 30), 70.0),
    ("WASHER", "FURN-WASHER", (27, 27), 38.0),
    ("DRYER", "FURN-DRYER", (27, 27), 38.0),
    ("COFFEE", "FURN-COFFEE-BAR", (24, 16), 36.0),
    ("BROOM CAB", "FURN-BROOM-CAB", (16, 24), 84.0),
    ("BENCH", "FURN-BENCH", (48, 18), 18.0),
]
# labels that contain a catalog keyword by accident but are not items
NOT_ITEMS = ("LOW WALL", "NEW WINDOW", "NEW STRUCTURAL", "LINEN/LAUNDRY",
             "CASED OPENING")
# default footprints for parse-from-label items when the label has no dims
PARSE_DEFAULT = {"FURN-ISLAND": (96, 42), "FURN-SHOWER": (36, 36),
                 "FURN-VANITY": (60, 22)}

PAIR = re.compile(r"(\d+)'\s*-?\s*(\d+)?\"?\s*x\s*(\d+)'\s*-?\s*(\d+)?", re.I)
SINGLE = re.compile(r"(\d+)'\s*-?\s*(\d+)?\"?")


def parse_size(name, text):
    m = PAIR.search(text)
    if m:
        a = int(m.group(1)) * 12 + int(m.group(2) or 0)
        b = int(m.group(3)) * 12 + int(m.group(4) or 0)
        return (max(a, b), min(a, b))
    if name == "FURN-VANITY":
        m = SINGLE.search(text)
        if m:
            return (int(m.group(1)) * 12 + int(m.group(2) or 0), 22)
    return PARSE_DEFAULT.get(name, (36, 36))


# ---------------------------------------------------------------- geometry

def ent_points(e):
    """Sample points along an entity (for clustering + bboxes)."""
    t = e.dxftype()
    if t == "LINE":
        a, b = e.dxf.start, e.dxf.end
        return [(a[0], a[1]), ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2),
                (b[0], b[1])]
    if t == "ARC":
        c, r = e.dxf.center, e.dxf.radius
        a0 = math.radians(e.dxf.start_angle)
        a1 = math.radians(e.dxf.end_angle)
        if a1 < a0:
            a1 += 2 * math.pi
        n = max(3, int((a1 - a0) / 0.5))
        return [(c[0] + r * math.cos(a0 + (a1 - a0) * i / n),
                 c[1] + r * math.sin(a0 + (a1 - a0) * i / n))
                for i in range(n + 1)]
    if t == "CIRCLE":
        c, r = e.dxf.center, e.dxf.radius
        return [(c[0] + r * math.cos(a), c[1] + r * math.sin(a))
                for a in (0, 0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6)]
    if t == "ELLIPSE":
        try:
            return [(p[0], p[1]) for p in e.flattening(0.5)]
        except Exception:
            return []
    if t == "LWPOLYLINE":
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        out = []
        for i in range(len(pts) - 1):
            out += [pts[i], ((pts[i][0] + pts[i + 1][0]) / 2,
                             (pts[i][1] + pts[i + 1][1]) / 2)]
        if pts:
            out.append(pts[-1])
        return out
    return []


def arc_span(e):
    return (e.dxf.end_angle - e.dxf.start_angle) % 360


def eff_color(e, doc):
    c = e.dxf.color
    if c in (0, 256):  # byblock / bylayer
        try:
            return doc.layers.get(e.dxf.layer).dxf.color
        except Exception:
            return 7
    return c


class Walls:
    def __init__(self, msp, wall_layer):
        self.segs = [(e.dxf.start[0], e.dxf.start[1],
                      e.dxf.end[0], e.dxf.end[1])
                     for e in msp.query("LINE") if e.dxf.layer == wall_layer]
        xs = [c for s in self.segs for c in (s[0], s[2])]
        ys = [c for s in self.segs for c in (s[1], s[3])]
        self.bbox = (min(xs), max(xs), min(ys), max(ys)) if xs else None

    def near(self, x, y, thr=6.5):
        for (x0, y0, x1, y1) in self.segs:
            dx, dy = x1 - x0, y1 - y0
            L2 = dx * dx + dy * dy or 1.0
            t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / L2))
            if math.hypot(x - x0 - t * dx, y - y0 - t * dy) < thr:
                return True
        return False

    def crosses(self, x0, x1, y0, y1):
        """Does a wall segment pass through the interior of this bbox?"""
        for (ax, ay, bx, by) in self.segs:
            for t in (0.25, 0.5, 0.75):
                px, py = ax + (bx - ax) * t, ay + (by - ay) * t
                if x0 + 2 < px < x1 - 2 and y0 + 2 < py < y1 - 2:
                    return True
        return False


def is_door_arc(e):
    if e.dxftype() != "ARC":
        return False
    return 16.0 <= e.dxf.radius <= 46.0 and 30.0 <= arc_span(e) <= 115.0


def door_leaf_lines(msp, layers, door_arcs):
    """Lines hinged at a door arc's center (the drawn door leaf)."""
    hinges = [(e.dxf.center[0], e.dxf.center[1], e.dxf.radius)
              for e in door_arcs]
    out = set()
    for e in msp.query("LINE"):
        if e.dxf.layer not in layers:
            continue
        (x0, y0, _), (x1, y1, _) = e.dxf.start, e.dxf.end
        ln = math.hypot(x1 - x0, y1 - y0)
        for (hx, hy, r) in hinges:
            if ln < 0.65 * r or ln > 1.2 * r:
                continue
            if (math.hypot(x0 - hx, y0 - hy) < 3.0
                    or math.hypot(x1 - hx, y1 - hy) < 3.0):
                out.add(id(e))
                break
    return out


# ---------------------------------------------------------------- clusters

class Cluster:
    def __init__(self, ents, doc):
        xs, ys = [], []
        self.ents = ents
        self.n = len(ents)
        self.arcs = []
        self.ellipses = 0
        self.circles = 0
        self.diagonals = 0
        self.long_diagonals = 0  # full-bbox X braces (roofs, skylights)
        self.segs = 0            # drawn-segment count (polylines expanded)
        self.lines = []          # (angle mod 180 deg, length)
        self.colors = collections.Counter()
        for (e, pts) in ents:
            for p in pts:
                xs.append(p[0])
                ys.append(p[1])
            self.colors[eff_color(e, doc)] += 1
            t = e.dxftype()
            self.segs += 1
            if t == "ARC":
                self.arcs.append((e.dxf.radius, arc_span(e)))
            elif t == "ELLIPSE":
                self.ellipses += 1
            elif t == "CIRCLE":
                self.circles += 1
            elif t == "LWPOLYLINE":
                self.segs += max(0, len(e.get_points("xy")) - 2)
                if e.closed:
                    self.segs += 1
            elif t == "LINE":
                (x0, y0, _), (x1, y1, _) = e.dxf.start, e.dxf.end
                ln = math.hypot(x1 - x0, y1 - y0)
                ang = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180
                self.lines.append((ang, ln))
                if ln > 15 and 15 <= min(ang % 90, 90 - ang % 90) <= 75:
                    self.diagonals += 1
                    if ln > 70:
                        self.long_diagonals += 1
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.w = self.x1 - self.x0
        self.d = self.y1 - self.y0
        self.cx = (self.x0 + self.x1) / 2
        self.cy = (self.y0 + self.y1) / 2

    @property
    def dims(self):
        return (min(self.w, self.d), max(self.w, self.d))

    def outline(self, cx=None, cy=None, cap=140):
        """The actual drawn segments, centre-relative (plan inches), so the
        viewer renders the true DXF furniture symbol (ShareCAD parity)
        instead of a bounding box. cx/cy override the centre (a label item
        may recentre on a slid sub-box)."""
        cx = self.cx if cx is None else cx
        cy = self.cy if cy is None else cy
        segs = []
        for (e, pts) in self.ents:
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.4:
                    continue
                segs.append([round(a[0] - cx, 1), round(a[1] - cy, 1),
                             round(b[0] - cx, 1), round(b[1] - cy, 1)])
        return segs[:cap]

    def is_ladder(self):
        """Stair treads / exterior steps: a run of parallel lines of
        near-equal length dominating the cluster."""
        if len(self.lines) < 3:
            return False
        for (a0, l0) in self.lines:
            if l0 < 30:
                continue
            group = [ln for (a, ln) in self.lines
                     if abs((a - a0 + 90) % 180 - 90) < 4
                     and abs(ln - l0) < 0.22 * max(ln, l0)]
            if len(group) >= 3 and len(self.lines) - len(group) <= 4:
                return True          # bare tread ladder
            if len(group) >= 4 and len(group) >= 0.42 * len(self.lines):
                return True          # tread ladder with stringers/arrows
        return False


def gather_pool(msp, doc, layers, notes_layer, walls, max_line=110.0):
    """Entities eligible for furniture clustering. Long lines are rug
    outlines / counter runs / leaders - they chain unrelated items, out."""
    door_arcs = [e for e in msp.query("ARC")
                 if e.dxf.layer in layers and is_door_arc(e)]
    leaves = door_leaf_lines(msp, layers, door_arcs)
    pool = []
    for e in msp:
        lay = e.dxf.layer
        loose = lay == notes_layer and eff_color(e, doc) == 11
        if lay not in layers and not loose:
            continue
        t = e.dxftype()
        if t not in ("LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE"):
            continue
        if t == "ARC" and is_door_arc(e):
            continue
        if id(e) in leaves:
            continue
        if t == "LINE":
            (x0, y0, _), (x1, y1, _) = e.dxf.start, e.dxf.end
            if math.hypot(x1 - x0, y1 - y0) > max_line:
                continue
            if walls.near((x0 + x1) / 2, (y0 + y1) / 2):
                continue        # glazing bands, jambs, sills
        pts = ent_points(e)
        if pts:
            pool.append((e, pts))
    return pool


def find_clusters(pool, doc, link=5.0):
    n = len(pool)
    par = list(range(n))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a

    grid = collections.defaultdict(list)
    for i, (e, pts) in enumerate(pool):
        for p in pts:
            grid[(int(p[0] // link), int(p[1] // link))].append((i, p))
    for (gx, gy), items in grid.items():
        neigh = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neigh += grid.get((gx + dx, gy + dy), [])
        for i, p in items:
            for j, q in neigh:
                if find(i) != find(j) and math.hypot(
                        p[0] - q[0], p[1] - q[1]) < link:
                    par[find(i)] = find(j)
    comps = collections.defaultdict(list)
    for i in range(n):
        comps[find(i)].append(pool[i])
    return [Cluster(v, doc) for v in comps.values()]


# ---------------------------------------------------------------- rooms

def load_plan(plan_path):
    rooms, footprint, stair_boxes = [], None, []
    if plan_path:
        try:
            plan = json.load(open(plan_path))
            footprint = plan.get("footprint")
            for s in plan.get("stairs", []):
                poly = s.get("polygon")
                if poly:
                    xs = [q[0] for q in poly]
                    ys = [q[1] for q in poly]
                    stair_boxes.append(((min(xs) + max(xs)) / 2,
                                        (min(ys) + max(ys)) / 2,
                                        max(xs) - min(xs),
                                        max(ys) - min(ys)))
            for r in plan.get("rooms", []):
                if r.get("name") and r.get("polygon"):
                    rooms.append((r["name"].upper(), r["polygon"],
                                  r.get("area", 0)))
            rooms.sort(key=lambda r: r[2])
        except Exception:
            pass
    return rooms, footprint, stair_boxes


def in_poly(poly, x, y):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def room_at(rooms, x, y):
    for name, poly, _area in rooms:
        if in_poly(poly, x, y):
            return name
    return ""


# ---------------------------------------------------------------- labels

def _clip_seg(ax, ay, bx, by, x0, x1, y0, y1):
    """Liang-Barsky clip of a segment to an axis-aligned box; None if out."""
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return None
    return [ax + t0 * dx, ay + t0 * dy, ax + t1 * dx, ay + t1 * dy]


def crop_segments(msp, layers, px, py, reach, box=None):
    """Raw drawn segments near a point (incl. near-wall), absolute plan
    inches - for label items / the fireplace that never formed a cluster,
    so they still render their real linework instead of a catalog box.
    `box` = (halfw, halfd) clips each segment to the item footprint, so an
    appliance never inherits the full length of the counter run it sits
    on (the washer/dryer spanning-bar bug)."""
    hw, hd = box if box else (reach, reach)
    x0, x1, y0, y1 = px - hw, px + hw, py - hd, py + hd
    segs = []
    for e in msp.query("LINE ARC CIRCLE ELLIPSE LWPOLYLINE"):
        if e.dxf.layer not in layers:
            continue
        pts = ent_points(e)
        if len(pts) < 2:
            continue
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        if math.hypot(mx - px, my - py) > reach:
            continue
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.4:
                continue
            cl = _clip_seg(a[0], a[1], b[0], b[1], x0, x1, y0, y1)
            if cl and math.hypot(cl[2] - cl[0], cl[3] - cl[1]) >= 0.4:
                segs.append(cl)
    return segs


def rel_outline(segs, cx, cy, cap=160):
    return [[round(s[0] - cx, 1), round(s[1] - cy, 1),
             round(s[2] - cx, 1), round(s[3] - cy, 1)] for s in segs[:cap]]


def label_items(msp, text_layers, clusters, red_clusters, kitchen_pts,
                geom_layers=None):
    labels = []
    for e in msp.query("TEXT"):
        if e.dxf.layer not in text_layers:
            continue
        t = (e.dxf.text or "").strip()
        up = t.upper()
        if any(w in up for w in NOT_ITEMS):
            continue
        for kw, name, size, h in CATALOG:
            if kw not in up:
                continue
            if size is None:
                size = parse_size(name, t)
            if abs(e.dxf.rotation % 180 - 90) < 30:  # label rotated with item
                size = (size[1], size[0])
            labels.append([name, size, h, t,
                           e.dxf.insert[0], e.dxf.insert[1]])
            break

    # candidate (label, cluster) matches, best score first, each cluster
    # claimed once - two "Sink" labels must not land on the same basin
    cands = []
    pool = clusters + red_clusters
    for li, (name, (we, de), h, t, px, py) in enumerate(labels):
        for c in pool:
            dist = math.hypot(c.cx - px, c.cy - py)
            if dist > max(60, we, de):
                continue
            if min(c.w, c.d) < 4:
                continue
            score = min(
                max(abs(math.log(w / we)), abs(math.log(d / de)))
                for (w, d) in ((c.w, c.d), (c.d, c.w)))
            if score < 0.42:
                cands.append((score + dist / 200.0, li, id(c), c, None))
                continue
            # counter/cabinet RUN: one axis fits, the other is a long run -
            # slide the expected box along the run to the label
            for axis in (0, 1):
                fit = (c.w, c.d)[axis]
                run = (c.d, c.w)[axis]
                ex = min(we, de) if fit <= run else max(we, de)
                ey = max(we, de) if fit <= run else min(we, de)
                if c.segs < 4 or not (0.66 < fit / ex < 1.5) \
                        or run < 1.5 * ey:
                    continue
                if axis == 0:
                    pos = (c.cx, min(max(py, c.y0 + ey / 2), c.y1 - ey / 2),
                           ex, ey)
                else:
                    pos = (min(max(px, c.x0 + ey / 2), c.x1 - ey / 2), c.cy,
                           ey, ex)
                d2 = math.hypot(pos[0] - px, pos[1] - py)
                if d2 < max(48, ex, ey):
                    cands.append((0.2 + abs(math.log(fit / ex))
                                  + d2 / 200.0, li, id(c), c, pos))
                break
    cands.sort(key=lambda r: r[0])
    placed, claimed = {}, set()
    for (score, li, cid, c, pos) in cands:
        if li in placed or cid in claimed:
            continue
        placed[li] = (c, pos)
        claimed.add(cid)

    out = []
    for li, (name, (we, de), h, t, px, py) in enumerate(labels):
        c, pos = placed.get(li, (None, None))
        if pos is not None:
            cx, cy, w, d = pos
        elif c is not None:
            cx, cy = c.cx, c.cy
            w, d = ((we, de) if (c.w >= c.d) == (we >= de) else (de, we))
            w, d = min(w, 1.3 * max(we, de)), min(d, 1.3 * max(we, de))
        elif name in ("FURN-VANITY", "FURN-SINK"):
            cx, cy, w, d = snap_basins(px, py, (we, de), clusters,
                                       reach=55 if name == "FURN-VANITY"
                                       else 40)
        else:
            cx, cy, w, d = px, py, we, de
        entry = {"name": name, "center": [round(cx), round(cy)],
                 "size": [round(w), round(d)], "height": h,
                 "rotation": 0, "auto": True,
                 "stub": f"{t} (DXF note label)"}
        if c is not None:
            entry["outline"] = c.outline(cx, cy)
        elif geom_layers:
            crop = crop_segments(msp, geom_layers, cx, cy,
                                 max(w, d, 36) * 0.8,
                                 box=(w / 2 + 4, d / 2 + 4))
            if len(crop) >= 4:
                entry["outline"] = rel_outline(crop, cx, cy)
        out.append(entry)
        if name in ("FURN-RANGE", "FURN-SINK", "FURN-DISHWASHER",
                    "FURN-ISLAND"):
            kitchen_pts.append((cx, cy))
    return out


def snap_basins(px, py, size, clusters, reach=55):
    """A sink/vanity whose counter merged into bigger linework still has
    its drawn basin(s): center the item on the basins."""
    bowls = [c for c in clusters
             if 10 <= c.dims[0] <= 24 and c.dims[1] <= 26
             and (c.ellipses or c.arcs or c.circles)
             and math.hypot(c.cx - px, c.cy - py) < reach]
    if not bowls:
        return px, py, size[0], size[1]
    x0 = min(c.x0 for c in bowls)
    x1 = max(c.x1 for c in bowls)
    y0 = min(c.y0 for c in bowls)
    y1 = max(c.y1 for c in bowls)
    w, d = max(size), min(size)
    if y1 - y0 > x1 - x0:       # counter runs north-south
        w, d = d, w
    return (x0 + x1) / 2, (y0 + y1) / 2, w, d


# ---------------------------------------------------------------- fireplace

def hatch_bbox(e):
    xs, ys = [], []
    for path in e.paths:
        if hasattr(path, "vertices"):
            for v in path.vertices:
                xs.append(v[0])
                ys.append(v[1])
        else:
            for edge in path.edges:
                if hasattr(edge, "start"):
                    xs += [edge.start[0], edge.end[0]]
                    ys += [edge.start[1], edge.end[1]]
                elif hasattr(edge, "center"):
                    xs.append(edge.center[0])
                    ys.append(edge.center[1])
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


def fireplace_items(msp, dw_layer, walls):
    """Hatched chimney masses straddling a wall -> FURN-FIREPLACE."""
    boxes = [b for e in msp.query("HATCH") if e.dxf.layer == dw_layer
             for b in [hatch_bbox(e)] if b]
    groups = []
    for b in boxes:
        for g in groups:
            if (b[0] < g[1] + 14 and g[0] < b[1] + 14
                    and b[2] < g[3] + 14 and g[2] < b[3] + 14):
                g[0], g[1] = min(g[0], b[0]), max(g[1], b[1])
                g[2], g[3] = min(g[2], b[2]), max(g[3], b[3])
                break
        else:
            groups.append(list(b))
    out = []
    for (x0, x1, y0, y1) in groups:
        if x1 - x0 < 24 or y1 - y0 < 24:
            continue
        if not walls.crosses(x0, x1, y0, y1):
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        crop = crop_segments(msp, {dw_layer}, cx, cy,
                             max(x1 - x0, y1 - y0) / 2 + 10)
        out.append({"name": "FURN-FIREPLACE",
                    "center": [round(cx), round(cy)],
                    "size": [round(x1 - x0), round(y1 - y0)], "height": 96.0,
                    "rotation": 0, "auto": True,
                    "outline": rel_outline(crop, cx, cy),
                    "stub": "fireplace: hatched chimney masses straddling "
                            "the wall (DXF)"})
    return out


# ---------------------------------------------------------------- classify

def classify(c, room, kitchen_pts):
    """Heuristic identification of an unlabeled fixture cluster."""
    lo, hi = c.dims
    bathy = ("BATH" in room or "TUB" in room or "SHOWER" in room
             or "LINEN" in room)
    if len(c.arcs) >= 10 and 16 <= lo <= 24 and 24 <= hi <= 32:
        return "FURN-TOILET", 30.0, "toilet (arc-drawn bowl + tank)"
    if c.diagonals >= 2 and 28 <= lo <= 54 and hi <= 76 and bathy:
        return "FURN-SHOWER", 80.0, "shower (boxed pan, diagonals to drain)"
    if (c.arcs or c.ellipses) and 26 <= lo <= 36 and 52 <= hi <= 72 and bathy:
        return "FURN-TUB", 24.0, "bathtub (rounded outline)"
    if bathy and 14 <= lo <= 26 and 30 <= hi <= 84:
        return "FURN-VANITY", 34.0, "vanity (counter run in bathroom)"
    if (c.colors.get(1, 0) + c.colors.get(11, 0) > 0.7 * c.n
            and 28 <= lo <= 45 and 40 <= hi <= 60 and kitchen_pts
            and min(math.hypot(c.cx - px, c.cy - py)
                    for (px, py) in kitchen_pts) < 150):
        return "FURN-FRIDGE", 70.0, "refrigerator (boxed mass in kitchen run)"
    if 70 <= lo <= 100 and 80 <= hi <= 110 and "BEDROOM" in room:
        return "FURN-BED", 26.0, "bed (drawn with nightstands)"
    return "FURN-DRAWN", 30.0, "furniture drawn on the sheet (unlabeled)"


def covered(cx, cy, w, d, taken, ratio=0.45):
    """Is >=ratio of this box's area already inside a taken box?"""
    for (tx, ty, tw, td) in taken:
        ox = min(cx + w / 2, tx + tw / 2) - max(cx - w / 2, tx - tw / 2)
        oy = min(cy + d / 2, ty + td / 2) - max(cy - d / 2, ty - td / 2)
        if ox > 0 and oy > 0 and ox * oy > ratio * min(w * d, tw * td):
            return True
    return False


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf")
    ap.add_argument("gt")
    ap.add_argument("--prefix", default="1.1",
                    help="layer prefix for this floor (1.1, 1.2 ...)")
    ap.add_argument("--wall-layer", default="",
                    help="walls layer (default <prefix>WALL)")
    ap.add_argument("--plan", default="",
                    help="plan json for room/footprint context")
    args = ap.parse_args()

    pfx = args.prefix
    wall_layer = args.wall_layer or pfx + "WALL"
    dw_layer, furn_layer = pfx + "DRWDWS", pfx + "FURN"
    notes_layer, rm_layer = pfx + "NOTES", pfx + "RMNAMES"

    doc = ezdxf.readfile(args.dxf)
    msp = doc.modelspace()
    walls = Walls(msp, wall_layer)
    rooms, footprint, stair_boxes = load_plan(args.plan)
    gt = json.load(open(args.gt))
    manual = [f for f in gt.get("furnishings", []) if not f.get("auto")]

    pool = gather_pool(msp, doc, {dw_layer, furn_layer}, notes_layer, walls)
    clusters = find_clusters(pool, doc)
    red_pool = [(e, pts) for (e, pts) in pool if eff_color(e, doc) in (1, 11)]
    red_clusters = find_clusters(red_pool, doc)

    kitchen_pts = []
    items = label_items(msp, {notes_layer, rm_layer}, clusters, red_clusters,
                        kitchen_pts,
                        geom_layers={dw_layer, furn_layer, notes_layer})
    items += fireplace_items(msp, dw_layer, walls)

    # manual placements are authoritative: drop mined items landing on them
    manual_boxes = [(f["center"][0], f["center"][1],
                     f["size"][0], f["size"][1]) for f in manual]
    items = [f for f in items
             if not covered(f["center"][0], f["center"][1],
                            f["size"][0], f["size"][1], manual_boxes)]

    taken = manual_boxes + [
        (f["center"][0], f["center"][1], f["size"][0], f["size"][1])
        for f in items]
    for s in gt.get("stairs", []):
        if s.get("bbox"):
            x0, x1, y0, y1 = s["bbox"]
            taken.append(((x0 + x1) / 2, (y0 + y1) / 2,
                          abs(x1 - x0), abs(y1 - y0)))
    taken += stair_boxes

    def inside(x, y):
        if footprint:
            return in_poly(footprint, x, y)
        wx0, wx1, wy0, wy1 = walls.bbox
        return wx0 - 10 < x < wx1 + 10 and wy0 - 10 < y < wy1 + 10

    def consider(c, depth=0):
        if c.segs < 4 and not (c.segs == 3 and 18 <= c.dims[0]
                               and c.dims[1] <= 60):
            return
        if c.dims[0] < 14:
            return
        if c.dims[1] > 135 and depth == 0:
            # over-long chains: a stray connector (rug edge, leader) is
            # bridging separate pieces - drop long lines and re-cluster
            keep = []
            for (e, pts) in c.ents:
                if e.dxftype() == "LINE":
                    (x0, y0, _), (x1, y1, _) = e.dxf.start, e.dxf.end
                    if math.hypot(x1 - x0, y1 - y0) > 60:
                        continue
                keep.append((e, pts))
            for sub in find_clusters(keep, doc):
                consider(sub, depth + 1)
            return
        if c.dims[1] > 155:                       # room/roof/porch chains
            return
        if c.dims[0] <= 10 and c.dims[1] >= 20:   # window bands
            return
        if c.long_diagonals >= 2:                 # roof/skylight X-braces
            return
        if c.is_ladder():                         # stair treads, ext. steps
            return
        if not inside(c.cx, c.cy):
            return
        if covered(c.cx, c.cy, c.w, c.d, taken):
            return
        room = room_at(rooms, c.cx, c.cy)
        name, h, why = classify(c, room, kitchen_pts)
        items.append({"name": name, "center": [round(c.cx), round(c.cy)],
                      "size": [round(c.w), round(c.d)], "height": h,
                      "rotation": 0, "auto": True, "outline": c.outline(),
                      "stub": f"{why}{' in ' + room.title() if room else ''}"})
        taken.append((c.cx, c.cy, c.w, c.d))

    for c in sorted(clusters, key=lambda c: -c.n):
        consider(c)

    items = [f for f in items if inside(f["center"][0], f["center"][1])]

    gt["furnishings"] = manual + items
    json.dump(gt, open(args.gt, "w"), indent=1)
    print(f"kept {len(manual)} manual, mined {len(items)} auto:")
    for f in items:
        print(f"  {f['name']:18s} at ({f['center'][0]:5d},{f['center'][1]:5d})"
              f" {f['size'][0]:3d}x{f['size'][1]:<3d}  {f['stub'][:52]}")


if __name__ == "__main__":
    main()

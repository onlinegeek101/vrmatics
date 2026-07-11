#!/usr/bin/env python3
"""Convert one floor of a Zillow Indoor Dataset (ZInD) tour into plan.json
for the VR viewer.

ZInD (github.com/zillow/zind) ships one `zind_data.json` per home with:

  scale_meters_per_coordinate.floor_XX   float|None - coords -> meters
  merger.floor_XX.complete_room_YY.partial_room_ZZ.pano_NN
      per-pano room layouts in LOCAL coords (camera at origin, camera
      height normalized to 1.0) plus a floor_plan_transformation
      (translation/rotation/scale) into the GLOBAL floor coords; W/D/O
      are flat lists where each element is a triplet of points:
      [left, right, (bottom, top)] - only the first two are in-plane.
  redraw.floor_XX.room_YY                the cleaned-up final floor plan:
      room polygons + doors/windows as endpoint PAIRS (no openings),
      in the same GLOBAL coords as the merger.

Redraw room polygons trace the INTERIOR face of each wall, so adjacent
rooms do not share edges - their facing edges run parallel, separated by
the wall thickness. The pipeline therefore:

  1. Pick a floor, resolve the meters-per-coordinate scale (estimate it
     from door widths if the calibration is missing), convert to inches.
  2. Take room polygons from `redraw` (fallback: merger layout_complete
     of each complete room's primary pano).
  3. Pair facing parallel edges of DIFFERENT rooms into shared interior
     walls (centerline midway, thickness = measured separation); edge
     spans left unpaired become exterior walls, pushed outward by half
     the exterior thickness so the room face stays put.
  4. Map W/D/O segments (redraw doors/windows + merger openings) onto
     the nearest wall as position-fraction + width; dedupe the copies
     annotated from both sides of a shared wall.
  5. Emit plan.json (inches) plus extra "rooms" and "cameras" arrays;
     camera = pano's floor_plan_transformation translation/rotation.

Usage:
    python zind2plan.py zind_data.json -o plan.json [--floor 0]

`--floor` is an index into the sorted floor ids (0 = first floor found)
or an explicit id like `floor_01`. Missing fields warn, never crash.
"""
import argparse
import json
import math
import sys

METERS_TO_INCHES = 39.3701

WALL_HEIGHT = 96.0          # fallback when ceiling heights are unusable
DOOR_HEAD = 80.0            # 6'-8"
WINDOW_SILL = 30.0
WINDOW_HEAD = 80.0

INTERIOR_THICKNESS = 4.5    # default when a shared wall's gap is degenerate
EXTERIOR_THICKNESS = 6.0    # guess for unpaired (exterior/unscanned) edges

ANGLE_TOL_DEG = 2.0         # parallel test for facing edges
MAX_WALL = 14.0             # max separation (in) to pair facing edges
COINCIDENT_TOL = 1.0        # below this the two edges are the same line
MIN_OVERLAP = 3.0           # min facing overlap (in) worth a shared wall
MIN_WALL_LEN = 2.0          # drop leftover exterior slivers below this
MERGE_PERP_TOL = 1.0        # collinear-merge: max offset from shared line
MERGE_GAP_TOL = 2.0         # collinear-merge: max endpoint gap along line
OPENING_SNAP = 6.0          # extra reach (in) when snapping W/D/O to walls
ASSUMED_DOOR_M = 0.81       # ~32" door, for scale estimation fallback

SILL_HEAD = {               # type -> (sill, head), inches
    "door": (0.0, DOOR_HEAD),
    "window": (WINDOW_SILL, WINDOW_HEAD),
    "opening": (0.0, DOOR_HEAD),
}


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


def parallel(u, v):
    return abs(cross(u, v)) <= math.sin(math.radians(ANGLE_TOL_DEG))


def to_global(points, tr):
    """Apply a ZInD floor_plan_transformation (local -> global floor coords).

    Matches zind/code/transformations.py: p' = scale * R(rotation) @ p + t
    with R a counter-clockwise rotation by `rotation` degrees.
    """
    th = math.radians(tr.get("rotation", 0.0))
    s = tr.get("scale", 1.0)
    tx, ty = tr.get("translation", (0.0, 0.0))
    c, sn = math.cos(th), math.sin(th)
    return [((x * c - y * sn) * s + tx, (x * sn + y * c) * s + ty)
            for x, y in points]


# --------------------------------------------------------------------------
# 1. floor selection + scale resolution
# --------------------------------------------------------------------------
def list_floors(data):
    """Union of floor ids across redraw and merger, sorted."""
    ids = set(data.get("redraw", {}) or {}) | set(data.get("merger", {}) or {})
    return sorted(ids)


def pick_floor(data, floor_arg, warnings):
    floors = list_floors(data)
    if not floors:
        warnings.append("no floors found in redraw or merger")
        return None
    if floor_arg in floors:                       # explicit id, e.g. floor_01
        return floor_arg
    try:
        idx = int(floor_arg)
    except ValueError:
        warnings.append(f"unknown floor '{floor_arg}', using '{floors[0]}'")
        return floors[0]
    if not 0 <= idx < len(floors):
        warnings.append(
            f"floor index {idx} out of range (have {floors}), using index 0")
        idx = 0
    return floors[idx]


def resolve_scale(data, floor_id, warnings):
    """Meters per ZInD coordinate for this floor.

    `scale_meters_per_coordinate.floor_XX` can be None (calibration failed
    for some tours). Fall back to assuming the median redraw door is a
    standard 32" door - crude, but keeps the output usable, with a warning.
    """
    scales = data.get("scale_meters_per_coordinate") or {}
    scale = scales.get(floor_id) if isinstance(scales, dict) else None
    if isinstance(scale, (int, float)) and scale > 0:
        return float(scale), "meters"

    door_widths = []
    for room in (data.get("redraw", {}).get(floor_id) or {}).values():
        for seg in room.get("doors", []):
            if len(seg) == 2:
                door_widths.append(length(sub(seg[1], seg[0])))
    if door_widths:
        door_widths.sort()
        median = door_widths[len(door_widths) // 2]
        if median > 0:
            est = ASSUMED_DOOR_M / median
            warnings.append(
                f"scale_meters_per_coordinate missing for {floor_id}; "
                f"estimated {est:.3f} m/coord assuming 32\" doors")
            return est, "unknown (scale estimated from door widths)"
    warnings.append(
        f"scale_meters_per_coordinate missing for {floor_id} and no doors "
        f"to estimate from; assuming 1 coordinate = 1 meter")
    return 1.0, "unknown (assumed meters)"


# --------------------------------------------------------------------------
# 2. rooms
# --------------------------------------------------------------------------
def polygon_points(vertices):
    """Normalize a ZInD polygon: tuples, no repeated closing vertex."""
    pts = [tuple(p) for p in vertices]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def read_rooms(data, floor_id, warnings):
    """Room polygons in GLOBAL floor coords + a label for each.

    Prefer `redraw` (the cleaned-up plan). If it is absent for this floor,
    rebuild rooms from the merger: for each complete room take the first
    primary pano's layout_complete (fallback layout_raw) moved to global.
    Returns list of {"polygon": [...], "kind": str}.
    """
    rooms = []
    redraw = (data.get("redraw") or {}).get(floor_id)
    if redraw:
        for room_id in sorted(redraw):
            room = redraw[room_id]
            pts = polygon_points(room.get("vertices", []))
            if len(pts) < 3:
                warnings.append(f"redraw {room_id}: degenerate polygon skipped")
                continue
            pins = room.get("pins", [])
            kind = pins[0].get("label", "room") if pins else "room"
            rooms.append({"polygon": pts, "kind": kind,
                          "labels": [p.get("label", "") for p in pins]})
        return rooms, "redraw"

    warnings.append(f"no redraw geometry for {floor_id}; "
                    f"rebuilding rooms from merger layouts")
    for cr_id, cr in sorted(((data.get("merger") or {}).get(floor_id) or {}).items()):
        best = None
        for pr in cr.values():
            for pano in pr.values():
                if not isinstance(pano, dict) or not pano.get("is_primary"):
                    continue
                layout = pano.get("layout_complete") or pano.get("layout_raw")
                if layout and "floor_plan_transformation" in pano:
                    best = (layout, pano["floor_plan_transformation"])
                    break
            if best:
                break
        if best is None:
            warnings.append(f"merger {cr_id}: no usable primary layout")
            continue
        layout, tr = best
        pts = polygon_points(to_global(layout.get("vertices", []), tr))
        if len(pts) >= 3:
            label = pano.get("label", "room") or "room"
            rooms.append({"polygon": pts, "kind": label, "labels": [label]})
    return rooms, "merger"


# --------------------------------------------------------------------------
# 3. facing-edge pairing -> walls
# --------------------------------------------------------------------------
class Edge:
    """One directed polygon edge with its outward normal and coverage
    bookkeeping (which spans along it already belong to a shared wall)."""
    __slots__ = ("a", "b", "room", "axis", "len", "outward", "covered")

    def __init__(self, a, b, room, ccw):
        self.a = a
        self.b = b
        self.room = room
        self.axis = unit(sub(b, a))
        self.len = length(sub(b, a))
        # left normal of a CCW polygon points inward, so outward is right
        n = (self.axis[1], -self.axis[0])
        self.outward = n if ccw else (-n[0], -n[1])
        self.covered = []       # [lo, hi] spans already paired into walls

    def point(self, t):
        return (self.a[0] + self.axis[0] * t, self.a[1] + self.axis[1] * t)


def signed_area(pts):
    return sum(cross(pts[i], pts[(i + 1) % len(pts)])
               for i in range(len(pts))) / 2.0


def room_edges(rooms):
    edges = []
    for ri, room in enumerate(rooms):
        pts = room["polygon"]
        ccw = signed_area(pts) > 0
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            if length(sub(b, a)) > 1e-6:
                edges.append(Edge(a, b, ri, ccw))
    return edges


def pair_edges(edges):
    """Turn facing parallel edges of different rooms into shared walls.

    For every edge pair (different rooms, parallel, mutually on each
    other's outward side, separated by <= MAX_WALL) the overlapping span
    becomes one wall: centerline midway between the faces, thickness =
    the measured separation. Both edges mark the span as covered so the
    leftover pass does not emit it again. Near-zero separation means the
    source already stored centerlines - dedupe to one wall with the
    default interior thickness.
    """
    walls = []
    n = len(edges)
    for i in range(n):
        ei = edges[i]
        for j in range(i + 1, n):
            ej = edges[j]
            if ei.room == ej.room or not parallel(ei.axis, ej.axis):
                continue
            perp = ei.outward
            d = dot(sub(ej.a, ei.a), perp)      # separation along ei.outward
            if d < -COINCIDENT_TOL or d > MAX_WALL:
                continue                        # behind us, or too far apart
            # facing check: ej must look back toward ei (unless coincident)
            if d > COINCIDENT_TOL and dot(ej.outward, ei.outward) > -0.5:
                continue
            # overlap of the two edges along ei's axis
            t0 = dot(sub(ej.a, ei.a), ei.axis)
            t1 = dot(sub(ej.b, ei.a), ei.axis)
            lo, hi = max(0.0, min(t0, t1)), min(ei.len, max(t0, t1))
            if hi - lo < MIN_OVERLAP:
                continue
            sep = abs(d)
            thickness = sep if sep > COINCIDENT_TOL else INTERIOR_THICKNESS
            off = sep / 2.0
            c0 = ei.point(lo)
            c1 = ei.point(hi)
            walls.append({
                "start": (c0[0] + perp[0] * off, c0[1] + perp[1] * off),
                "end": (c1[0] + perp[0] * off, c1[1] + perp[1] * off),
                "thickness": thickness,
            })
            ei.covered.append((lo, hi))
            # project the same span onto ej for its coverage bookkeeping
            s0 = dot(sub(ei.point(lo), ej.a), ej.axis)
            s1 = dot(sub(ei.point(hi), ej.a), ej.axis)
            ej.covered.append((min(s0, s1), max(s0, s1)))
    return walls


def merge_collinear_spans(spans):
    """Merge collinear near-adjacent segments into single spans.

    Two spans merge when they lie on the same infinite line (both endpoints
    within MERGE_PERP_TOL perpendicular) and their intervals along that line
    touch, overlap, or gap by no more than MERGE_GAP_TOL. This welds the
    chains of tiny uncovered exterior slivers (left where facing-edge
    coverage is fragmented) back into continuous walls instead of letting
    the length filter punch holes to the outside. `spans` is a list of
    (p0, p1) tuples; returns merged (p0, p1) tuples.
    """
    groups = []   # {"origin", "axis", "intervals": [[lo, hi], ...]}
    for p0, p1 in spans:
        axis = unit(sub(p1, p0))
        if axis == (0.0, 0.0):
            continue
        placed = False
        for g in groups:
            if not parallel(axis, g["axis"]):
                continue
            perp = (-g["axis"][1], g["axis"][0])
            if abs(dot(sub(p0, g["origin"]), perp)) > MERGE_PERP_TOL:
                continue
            if abs(dot(sub(p1, g["origin"]), perp)) > MERGE_PERP_TOL:
                continue
            t0 = dot(sub(p0, g["origin"]), g["axis"])
            t1 = dot(sub(p1, g["origin"]), g["axis"])
            g["intervals"].append([min(t0, t1), max(t0, t1)])
            placed = True
            break
        if not placed:
            groups.append({"origin": p0, "axis": axis,
                           "intervals": [[0.0, length(sub(p1, p0))]]})

    merged = []
    for g in groups:
        intervals = sorted(g["intervals"])
        out = [list(intervals[0])]
        for lo, hi in intervals[1:]:
            if lo <= out[-1][1] + MERGE_GAP_TOL:
                out[-1][1] = max(out[-1][1], hi)
            else:
                out.append([lo, hi])
        o, ax = g["origin"], g["axis"]
        for lo, hi in out:
            merged.append(((o[0] + ax[0] * lo, o[1] + ax[1] * lo),
                           (o[0] + ax[0] * hi, o[1] + ax[1] * hi)))
    return merged


def leftover_walls(edges, exterior_thickness):
    """Spans of each edge not covered by a shared wall become exterior
    walls, pushed outward by half the thickness so the interior face of
    the wall stays on the room polygon. Collinear adjacent spans are merged
    first so fragmented coverage does not shred a straight exterior wall
    into slivers; only what is still shorter than MIN_WALL_LEN after
    merging gets dropped. Returns (walls, n_slivers)."""
    spans = []
    for e in edges:
        covered = sorted(e.covered)
        merged = []
        for lo, hi in covered:
            if merged and lo <= merged[-1][1] + COINCIDENT_TOL:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        gaps = []
        cursor = 0.0
        for lo, hi in merged:
            if lo - cursor > 1e-6:
                gaps.append((cursor, lo))
            cursor = max(cursor, hi)
        if e.len - cursor > 1e-6:
            gaps.append((cursor, e.len))
        off = exterior_thickness / 2.0
        for lo, hi in gaps:
            c0, c1 = e.point(lo), e.point(hi)
            spans.append((
                (c0[0] + e.outward[0] * off, c0[1] + e.outward[1] * off),
                (c1[0] + e.outward[0] * off, c1[1] + e.outward[1] * off),
            ))

    walls = []
    slivers = 0
    for p0, p1 in merge_collinear_spans(spans):
        if length(sub(p1, p0)) < MIN_WALL_LEN:
            slivers += 1
            continue
        walls.append({"start": p0, "end": p1,
                      "thickness": exterior_thickness})
    return walls, slivers


# --------------------------------------------------------------------------
# 4. W/D/O segments -> openings on walls
# --------------------------------------------------------------------------
def read_wdo_segments(data, floor_id, scale_in, rooms_source, warnings):
    """Collect W/D/O as ((x,y),(x,y)) segments in inches, GLOBAL coords.

    Doors/windows come from redraw (endpoint pairs). Openings only exist
    in the merger layouts, where each W/D/O is a triplet of points -
    [left, right, (bottom, top)] - so only the first two are taken. When
    rooms came from the merger (no redraw), doors/windows are read from
    the merger too.
    """
    segments = []   # (kind, p0, p1)

    def add(kind, p0, p1):
        p0 = (p0[0] * scale_in, p0[1] * scale_in)
        p1 = (p1[0] * scale_in, p1[1] * scale_in)
        if length(sub(p1, p0)) > 1e-6:
            segments.append((kind, p0, p1))

    redraw = (data.get("redraw") or {}).get(floor_id) or {}
    if rooms_source == "redraw":
        for room_id, room in sorted(redraw.items()):
            for key, kind in (("doors", "door"), ("windows", "window")):
                for seg in room.get(key, []):
                    if len(seg) == 2:
                        add(kind, seg[0], seg[1])
                    else:
                        warnings.append(
                            f"redraw {room_id}: malformed {key} entry skipped")

    # merger: openings always; doors/windows too if redraw was unavailable
    merger_kinds = [("openings", "opening")]
    if rooms_source != "redraw":
        merger_kinds += [("doors", "door"), ("windows", "window")]
    for cr_id, cr in sorted(((data.get("merger") or {}).get(floor_id) or {}).items()):
        for pr in cr.values():
            for pano in pr.values():
                if not isinstance(pano, dict) or not pano.get("is_primary"):
                    continue
                layout = pano.get("layout_complete") or pano.get("layout_raw")
                tr = pano.get("floor_plan_transformation")
                if not layout or not tr:
                    continue
                for key, kind in merger_kinds:
                    flat = layout.get(key, [])
                    if len(flat) % 3:
                        warnings.append(
                            f"merger {cr_id}: {key} list not a multiple of 3")
                        continue
                    for k in range(0, len(flat), 3):
                        p0, p1 = to_global(flat[k:k + 2], tr)
                        add(kind, p0, p1)
                break   # one primary pano per complete room is enough
            else:
                continue
            break
    return segments


def collect_vertical_hints(data, floor_id, scale_in, warnings):
    """Measured sill/head heights for every W/D/O annotation, in inches.

    Merger layouts store each element as [left, right, (bottom, top)] where
    the third entry is vertical extent normalized to camera height 1.0;
    inches = (1 + v) * pano_scale * scale_in. Returns
    [{"center": (x,y) inches, "kind": ..., "sill": in, "head": in}].
    """
    hints = []
    for cr in sorted(((data.get("merger") or {}).get(floor_id) or {}).values(),
                     key=str):
        for pr in cr.values():
            for pano in pr.values():
                if not isinstance(pano, dict) or not pano.get("is_primary"):
                    continue
                layout = pano.get("layout_complete") or pano.get("layout_raw")
                tr = pano.get("floor_plan_transformation")
                if not layout or not tr:
                    continue
                vscale = tr.get("scale", 1.0) * scale_in
                for key, kind in (("doors", "door"), ("windows", "window"),
                                  ("openings", "opening")):
                    flat = layout.get(key, [])
                    if len(flat) % 3:
                        continue
                    for k in range(0, len(flat), 3):
                        vert = flat[k + 2]
                        if not (isinstance(vert, (list, tuple))
                                and len(vert) >= 2):
                            continue
                        p0, p1 = to_global(flat[k:k + 2], tr)
                        cx = (p0[0] + p1[0]) / 2.0 * scale_in
                        cy = (p0[1] + p1[1]) / 2.0 * scale_in
                        sill = (1.0 + vert[0]) * vscale
                        head = (1.0 + vert[1]) * vscale
                        if head <= sill:
                            continue
                        hints.append({"center": (cx, cy), "kind": kind,
                                      "sill": max(0.0, sill), "head": head})
    return hints


def apply_vertical_hints(openings, walls, hints, warnings):
    """Replace assumed sill/head values with measured ones.

    Each opening takes the average of matching hints (same broad kind,
    center within 24"). Door/opening sills snap to 0 when nearly there.
    Falls back to the assumed values when no annotation is close.
    """
    unhinted = 0
    for o in openings:
        w = walls[o["wall_index"]]
        axis = unit(sub(w["end"], w["start"]))
        t = o["position"] * length(sub(w["end"], w["start"]))
        c = (w["start"][0] + axis[0] * t, w["start"][1] + axis[1] * t)
        sills, heads = [], []
        for h in hints:
            same = h["kind"] == o["type"] or \
                "opening" in (h["kind"], o["type"])
            if same and length(sub(h["center"], c)) <= 24.0:
                sills.append(h["sill"])
                heads.append(h["head"])
        if not sills:
            unhinted += 1
            continue
        sill = sum(sills) / len(sills)
        head = sum(heads) / len(heads)
        if o["type"] != "window" and sill < 2.0:
            sill = 0.0
        o["sill"] = round(sill, 1)
        o["head"] = round(head, 1)
    if unhinted:
        warnings.append(
            f"{unhinted} opening(s) had no measured vertical annotation; "
            f"standard heights assumed")
    return unhinted


def map_openings(segments, walls, warnings):
    """Snap each W/D/O segment to the nearest wall as an opening.

    Match = midpoint projects inside the wall span and sits within half
    the wall thickness (+ slack) of the centerline. Shared walls are
    annotated from both rooms, so near-identical openings of the same
    type on the same wall are deduped.
    """
    openings = []
    unmapped = 0
    duplicates = 0
    for kind, p0, p1 in segments:
        mid = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
        width = length(sub(p1, p0))
        best = None
        for wi, w in enumerate(walls):
            axis = unit(sub(w["end"], w["start"]))
            wl = length(sub(w["end"], w["start"]))
            t = dot(sub(mid, w["start"]), axis)
            if t < -OPENING_SNAP or t > wl + OPENING_SNAP:
                continue
            perp = abs(cross(axis, sub(mid, w["start"])))
            if perp > w["thickness"] / 2.0 + OPENING_SNAP:
                continue
            if not parallel(axis, unit(sub(p1, p0))):
                continue
            if best is None or perp < best[0]:
                best = (perp, wi, min(max(t, 0.0), wl), wl)
        if best is None:
            unmapped += 1
            continue
        _, wi, t, wl = best
        position = t / wl if wl else 0.0
        # duplicate = same wall + overlapping span + same type; an 'opening'
        # also counts as a duplicate of a door/window there (the merger can
        # re-annotate a passage the redraw already drew as a door)
        dup = any(
            o["wall_index"] == wi
            and (o["type"] == kind or "opening" in (o["type"], kind))
            and abs(o["position"] - position) * wl < max(width, o["width"]) / 2.0
            for o in openings)
        if dup:
            duplicates += 1
            continue
        sill, head = SILL_HEAD[kind]
        openings.append({
            "wall_index": wi,
            "position": round(position, 4),
            "width": round(width, 2),
            "type": kind,
            "sill": sill,
            "head": head,
        })
    if unmapped:
        warnings.append(
            f"{unmapped} W/D/O segment(s) matched no wall and were skipped "
            f"(for 'opening' this usually means the plan is already open "
            f"there - no wall to cut)")
    return openings, duplicates


# --------------------------------------------------------------------------
# 5. cameras + wall height
# --------------------------------------------------------------------------
def read_cameras(data, floor_id, scale_in, warnings):
    """One camera per pano on this floor.

    A pano's floor_plan_transformation moves its local frame (camera at
    the origin) into global floor coords, so the camera position is just
    the translation. `rotation` is degrees counter-clockwise; 0 means the
    pano's forward (image center) axis points along plan +Y.
    Also returns the median ceiling height in inches (0 if unknown):
    ceiling_height is normalized to camera height 1.0 in the pano's local
    frame, so meters = ceiling_height * pano_scale * meters_per_coord.
    """
    cameras = []
    heights = []
    for cr_id, cr in sorted(((data.get("merger") or {}).get(floor_id) or {}).items()):
        for pr in cr.values():
            for pano in pr.values():
                if not isinstance(pano, dict):
                    continue
                tr = pano.get("floor_plan_transformation")
                if not tr or "translation" not in tr:
                    warnings.append(f"merger {cr_id}: pano without "
                                    f"floor_plan_transformation skipped")
                    continue
                tx, ty = tr["translation"]
                cam = {
                    "position": [round(tx * scale_in, 3),
                                 round(ty * scale_in, 3)],
                    "rotation": round(tr.get("rotation", 0.0), 2),
                    "pano": (pano.get("image_path") or "").split("/")[-1],
                    "is_primary": bool(pano.get("is_primary")),
                    "room": pano.get("label", ""),
                    # camera height above floor: normalized 1.0 by definition
                    "height": round(tr.get("scale", 1.0) * scale_in, 1),
                }
                ch = pano.get("ceiling_height")
                if isinstance(ch, (int, float)) and ch > 0:
                    ceil_in = ch * tr.get("scale", 1.0) * scale_in
                    heights.append(ceil_in)
                    cam["_ceiling"] = ceil_in     # stripped before emit
                cameras.append(cam)
    heights.sort()
    median_h = heights[len(heights) // 2] if heights else 0.0
    return cameras, median_h


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


def assign_measured_heights(rooms, walls, cameras, default_h):
    """Per-room ceilings and per-wall heights from the pano measurements.

    Each room's ceiling is the median measured ceiling of the panos shot
    inside it. Each wall takes the tallest ceiling of the rooms it borders
    (sampled just off both faces at the midpoint), so walls always reach
    their rooms' ceilings. Everything falls back to default_h.
    """
    for room in rooms:
        ceils = [c["_ceiling"] for c in cameras
                 if "_ceiling" in c and
                 point_in_polygon(c["position"], room["polygon"])]
        if ceils:
            ceils.sort()
            m = ceils[len(ceils) // 2]
            if 60.0 <= m <= 240.0:
                room["ceiling"] = round(m, 1)

    for w in walls:
        axis = unit(sub(w["end"], w["start"]))
        perp = (-axis[1], axis[0])
        mid = ((w["start"][0] + w["end"][0]) / 2.0,
               (w["start"][1] + w["end"][1]) / 2.0)
        off = w["thickness"] / 2.0 + 3.0
        borders = []
        for side in (1.0, -1.0):
            sp = (mid[0] + perp[0] * off * side, mid[1] + perp[1] * off * side)
            for room in rooms:
                if "ceiling" in room and point_in_polygon(sp, room["polygon"]):
                    borders.append(room["ceiling"])
                    break
        w["height"] = round(max(borders), 1) if borders else default_h

    for c in cameras:
        c.pop("_ceiling", None)


# --------------------------------------------------------------------------
# 6. building footprint + garage classification
# --------------------------------------------------------------------------
FOOTPRINT_SIMPLIFY = 1.0
GARAGE_MIN_AREA = 250.0 * 144.0   # 250 sq ft in sq inches
GARAGE_MIN_OPENING = 90.0         # opening width (in) suggesting a car door
GARAGE_EDGE_TOL = 12.0            # opening center must sit this close to room

# ZInD pin labels that carry no real information; only these may be
# overridden by the geometric garage rule
GENERIC_ROOM_KINDS = {"room", "unknown", "undefined", ""}


def compute_footprint(walls, warnings):
    """Building footprint: buffer each wall centerline by half its thickness
    (square caps), union everything, take the largest polygon's exterior
    ring, simplified. Returns [[x, y], ...] in inches without the closing
    duplicate point. Never raises; warns and returns [] on failure."""
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
    except ImportError:
        warnings.append("shapely not installed; footprint skipped")
        return []
    try:
        bufs = [LineString([w["start"], w["end"]])
                .buffer(w["thickness"] / 2.0, cap_style="square")
                for w in walls]
        merged = unary_union(bufs)
        polys = list(getattr(merged, "geoms", [merged]))
        polys = [p for p in polys if p.geom_type == "Polygon" and p.area > 0]
        if not polys:
            warnings.append("footprint failed: wall union produced no polygon")
            return []
        largest = max(polys, key=lambda p: p.area)
        ring = largest.exterior.simplify(FOOTPRINT_SIMPLIFY)
        coords = list(ring.coords)[:-1]   # drop closing duplicate
        if len(coords) < 3:
            warnings.append("footprint failed: degenerate exterior ring")
            return []
        return [[round(x, 3), round(y, 3)] for x, y in coords]
    except Exception as exc:
        warnings.append(f"footprint failed: {exc}")
        return []


def classify_garages(rooms, walls, openings, warnings):
    """Garage detection on top of the ZInD pin labels.

    A pin that already says garage is normalized to exactly \"garage\".
    Otherwise the geometric rule may override ONLY a generic/unknown pin
    kind: area >= 250 sq ft AND some opening at least 90\" wide sits on the
    room boundary (its center - taken from the owning wall's centerline at
    the position fraction - lies within 12\" of the polygon's exterior).
    Mutates room dicts in place; never raises."""
    try:
        from shapely.geometry import Point, Polygon
    except ImportError:
        warnings.append("shapely not installed; garage classification skipped")
        return
    try:
        centers = []
        for o in openings:
            if o["width"] < GARAGE_MIN_OPENING:
                continue
            w = walls[o["wall_index"]]
            axis = unit(sub(w["end"], w["start"]))
            t = o["position"] * length(sub(w["end"], w["start"]))
            centers.append((w["start"][0] + axis[0] * t,
                            w["start"][1] + axis[1] * t))
        for room in rooms:
            kind = str(room.get("kind", "")).strip().lower()
            if "garage" in kind:
                room["kind"] = "garage"
                continue
            if kind not in GENERIC_ROOM_KINDS or not centers:
                continue
            poly = Polygon(room["polygon"])
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
            if poly.area < GARAGE_MIN_AREA:
                continue
            if any(poly.exterior.distance(Point(c)) <= GARAGE_EDGE_TOL
                   for c in centers):
                room["kind"] = "garage"
    except Exception as exc:
        warnings.append(f"garage classification failed: {exc}")


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def convert(path, floor_arg, exterior_thickness):
    warnings = []
    with open(path) as f:
        data = json.load(f)

    floors = list_floors(data)
    floor_id = pick_floor(data, floor_arg, warnings)
    if floor_id is None:
        return None, None
    if len(floors) > 1:
        warnings.append(
            f"tour has {len(floors)} floors ({', '.join(floors)}); "
            f"converted '{floor_id}' - rerun with --floor for the others")

    scale_m, drawing_units = resolve_scale(data, floor_id, warnings)
    scale_in = scale_m * METERS_TO_INCHES   # ZInD coordinate -> inches

    rooms, rooms_source = read_rooms(data, floor_id, warnings)
    rooms_in = [
        {"polygon": [[round(x * scale_in, 3), round(y * scale_in, 3)]
                     for x, y in r["polygon"]],
         "kind": r["kind"], "labels": r["labels"]}
        for r in rooms
    ]

    edges = room_edges(
        [{"polygon": [tuple(p) for p in r["polygon"]]} for r in rooms_in])
    shared = pair_edges(edges)
    exterior, slivers = leftover_walls(edges, exterior_thickness)
    walls = shared + exterior

    cameras, ceiling_in = read_cameras(data, floor_id, scale_in, warnings)
    wall_height = round(ceiling_in, 1) if 60.0 <= ceiling_in <= 240.0 \
        else WALL_HEIGHT
    if wall_height == WALL_HEIGHT and ceiling_in:
        warnings.append(f"implausible ceiling height ({ceiling_in:.0f}\"), "
                        f"using default {WALL_HEIGHT:.0f}\"")

    segments = read_wdo_segments(data, floor_id, scale_in, rooms_source,
                                 warnings)
    openings, duplicates = map_openings(segments, walls, warnings)

    # measured accuracy pass: real per-opening verticals, per-room ceilings,
    # per-wall heights, true camera/eye heights - assumptions only as fallback
    hints = collect_vertical_hints(data, floor_id, scale_in, warnings)
    unhinted = apply_vertical_hints(openings, walls, hints, warnings)
    assign_measured_heights(rooms_in, walls, cameras, wall_height)
    cam_heights = sorted(c["height"] for c in cameras if c.get("height"))
    eye_height = cam_heights[len(cam_heights) // 2] if cam_heights else None

    classify_garages(rooms_in, walls, openings, warnings)
    footprint = compute_footprint(walls, warnings)

    if slivers:
        warnings.append(f"{slivers} exterior sliver(s) shorter than "
                        f"{MIN_WALL_LEN:.0f}\" dropped")

    plan = {
        "units": "inches",
        "drawing_units": drawing_units,
        "scale_meters_per_coordinate": scale_m,
        "floor": floor_id,
        "floors_available": floors,
        "walls": [
            {
                "start": [round(w["start"][0], 3), round(w["start"][1], 3)],
                "end": [round(w["end"][0], 3), round(w["end"][1], 3)],
                "thickness": round(w["thickness"], 2),
                "height": w.get("height", wall_height),
            }
            for w in walls
        ],
        "openings": openings,
        "rooms": rooms_in,
        "footprint": footprint,
        "cameras": cameras,
        "eye_height": eye_height,
        "warnings": warnings,
    }

    kinds = {}
    for o in openings:
        kinds[o["type"]] = kinds.get(o["type"], 0) + 1
    room_kinds = {}
    for r in rooms_in:
        room_kinds[r["kind"]] = room_kinds.get(r["kind"], 0) + 1
    report = {
        "floor": floor_id,
        "floors": len(floors),
        "rooms_source": rooms_source,
        "rooms": len(rooms_in),
        "room_kinds": room_kinds,
        "footprint_points": len(footprint),
        "walls": len(walls),
        "shared_walls": len(shared),
        "exterior_walls": len(exterior),
        "openings": len(openings),
        "opening_kinds": kinds,
        "wdo_duplicates_merged": duplicates,
        "cameras": len(cameras),
        "wall_height": wall_height,
        "vertical_hints": len(hints),
        "unhinted_openings": unhinted,
        "eye_height": eye_height,
    }
    flip_plan_y(plan)
    return plan, report


def flip_plan_y(plan):
    """ZInD global floor coordinates are y-DOWN (image convention). Blind
    side-by-side comparison of our renders against the tour's own panoramas
    showed every view mirrored left-right, and Zillow's rendered floor-plan
    PNG confirmed it (patio north of the bonus room; our y-up read put it
    south). Negate y everywhere and adjust camera headings (theta -> 180 -
    theta) so the output is a true y-up plan matching the real house.
    """
    for w in plan["walls"]:
        w["start"][1] = -w["start"][1]
        w["end"][1] = -w["end"][1]
    for r in plan.get("rooms", []):
        for v in r["polygon"]:
            v[1] = -v[1]
    for p in plan.get("footprint", []):
        p[1] = -p[1]
    for c in plan.get("cameras", []):
        c["position"][1] = -c["position"][1]
        c["rotation"] = round((180.0 - c.get("rotation", 0.0)) % 360.0, 2)


def main():
    ap = argparse.ArgumentParser(
        description="ZInD tour (zind_data.json) -> plan.json")
    ap.add_argument("input", help="ZInD zind_data.json for one home")
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--floor", default="0",
                    help="floor index (0-based, sorted) or id like floor_01")
    ap.add_argument("--exterior-thickness", type=float,
                    default=EXTERIOR_THICKNESS,
                    help="thickness guess (inches) for unpaired edges")
    args = ap.parse_args()

    try:
        plan, report = convert(args.input, args.floor,
                               args.exterior_thickness)
    except IOError:
        print(f"error: cannot read '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if plan is None:
        print("error: no floors found in input", file=sys.stderr)
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

    kinds = ", ".join(f"{v} {k}"
                      for k, v in sorted(report["opening_kinds"].items()))
    print(f"Parsed {args.input} -> {args.output}")
    print(f"  floor               : {report['floor']} "
          f"(1 of {report['floors']} in tour)")
    room_kinds = ", ".join(
        f"{v} {k}" for k, v in sorted(report["room_kinds"].items()))
    print(f"  rooms               : {report['rooms']} "
          f"(from {report['rooms_source']})"
          + (f" ({room_kinds})" if room_kinds else ""))
    print(f"  footprint           : {report['footprint_points']} points")
    print(f"  walls               : {report['walls']} "
          f"({report['shared_walls']} shared, "
          f"{report['exterior_walls']} exterior)")
    print(f"  openings            : {report['openings']}"
          + (f" ({kinds})" if kinds else "")
          + (f", {report['wdo_duplicates_merged']} double-annotated merged"
             if report["wdo_duplicates_merged"] else ""))
    print(f"  cameras (panos)     : {report['cameras']}")
    print(f"  wall height         : {report['wall_height']}\" (per-wall from room ceilings)")
    print(f"  measured verticals  : {report['vertical_hints']} annotations, "
          f"{report['unhinted_openings']} opening(s) fell back to defaults")
    if report.get("eye_height"):
        print(f"  eye height          : {report['eye_height']}\" (median camera)")
    if plan["warnings"]:
        print(f"  warnings            : {len(plan['warnings'])} "
              f"(see 'warnings' in {args.output})")
    if embed_path:
        print(f"  embedded copy       : {embed_path} "
              f"(lets the viewer open via file://)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Convert one storey of an IFC building model into plan.json for the
VR viewer.

IFC (ISO 16739) is the BIM exchange schema: a typed object graph where
walls, doors, windows, spaces and furniture are first-class products with
real 3D geometry, so - unlike the DXF/ZInD parsers - nothing needs to be
guessed from linework. The pipeline:

  1. List IfcBuildingStorey sorted by Elevation; pick one (--storey by
     index or name, default = the storey containing the most walls).
     Products belong to a storey via IfcRelContainedInSpatialStructure
     (furniture is usually contained in an IfcSpace, which aggregates up
     to its storey); anything unplaced falls back to a z-range test.
  2. Mesh every product with ifcopenshell.geom using USE_WORLD_COORDS so
     vertices arrive in world space; model units -> inches.
  3. Walls: project vertices to XY, fit the minimum rotated rectangle
     (shapely; PCA fallback) - long axis is the centerline, short side
     the thickness (clamped 2..24"), bbox z-extent the height. One wall
     per IfcWall / IfcWallStandardCase, no merging.
  4. Doors/windows: snap the world bbox onto the host wall's centerline
     (IfcRelFillsElement when present, else nearest wall like
     zind2plan.map_openings) for position/width; sill/head from bbox z
     minus the storey elevation. Door meshes include frame trim, so the
     schedule's OverallWidth/OverallHeight win whenever the projected
     size disagrees by more than a frame's worth (wild disagreement is
     warned about, mild ones are silently trimmed).
  5. Rooms: IfcSpace vertices near the space's base z, convex hull
     (concave spaces are approximated - warned). kind from LongName;
     ceiling = space z-extent when plausible.
  6. Fixtures: IfcFurnishingElement (+ IfcFlowTerminal when the file has
     any): oriented world bbox using the object placement's rotation.
  7. footprint = union of wall rectangles, largest exterior ring (same
     as zind2plan.compute_footprint).

Usage:
    python ifc2plan.py model.ifc -o plan.json [--storey 0|"Level 1"]

Missing fields warn, never crash.
"""
import argparse
import json
import math
import sys

METERS_TO_INCHES = 39.3701

WALL_HEIGHT = 96.0          # fallback when a wall has no usable z-extent
MIN_THICKNESS = 2.0         # clamp range for the obb short dimension
MAX_THICKNESS = 24.0
OPENING_SNAP = 12.0         # reach (in) when snapping a door/window to a wall
SOFFIT_BASE = 72.0          # wall bases higher than this above the storey
                            # floor are soffits/chases, not plan walls
FRAME_TOL = 1.0             # beyond this, OverallWidth/Height overrides bbox
WILD_TOL = 12.0             # beyond this the disagreement is worth a warning
BASE_Z_TOL = 2.0            # space verts within this of min z form the floor
MIN_CEILING = 60.0          # plausible room/wall height range (inches)
MAX_CEILING = 240.0
FOOTPRINT_SIMPLIFY = 1.0

ROOM_KINDS = (              # LongName substring -> plan room kind
    ("bath", "bath"),
    ("kitchen", "kitchen"),
    ("laundry", "laundry"),
    ("utility", "laundry"),
    ("garage", "garage"),
)


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
# 1. storey selection + product assignment
# --------------------------------------------------------------------------
def unit_scale(model, warnings):
    """Model length unit -> meters (1.0 for the common all-SI case)."""
    try:
        from ifcopenshell.util.unit import calculate_unit_scale
        return float(calculate_unit_scale(model))
    except Exception as exc:
        warnings.append(f"unit scale detection failed ({exc}); assuming meters")
        return 1.0


def storey_elevation_in(storey, scale_in):
    e = getattr(storey, "Elevation", None)
    return float(e) * scale_in if isinstance(e, (int, float)) else 0.0


def list_storeys(model, scale_in):
    """IfcBuildingStorey entities sorted by Elevation."""
    return sorted(model.by_type("IfcBuildingStorey"),
                  key=lambda s: storey_elevation_in(s, scale_in))


def space_storey(space):
    """Walk a space's decomposition/containment up to its storey."""
    obj, hops = space, 0
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


def map_products_to_storeys(model, warnings):
    """element id -> IfcBuildingStorey via ContainedInStructure.

    Furniture is commonly contained in an IfcSpace instead of the storey;
    resolve those through the space's aggregation chain. Spaces themselves
    are mapped too (they decompose the storey rather than being contained).
    """
    out = {}
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        structure = rel.RelatingStructure
        storey = structure if structure.is_a("IfcBuildingStorey") \
            else space_storey(structure)
        if storey is None:
            continue
        for el in rel.RelatedElements:
            out[el.id()] = storey
    for space in model.by_type("IfcSpace"):
        storey = space_storey(space)
        if storey is not None:
            out[space.id()] = storey
    return out


def pick_storey(storeys, storey_arg, wall_counts, scale_in, warnings):
    if storey_arg is not None:
        for s in storeys:
            if (s.Name or "") == storey_arg:
                return s
        try:
            idx = int(storey_arg)
            if 0 <= idx < len(storeys):
                return storeys[idx]
            warnings.append(f"storey index {idx} out of range "
                            f"(have {len(storeys)}); using default")
        except ValueError:
            warnings.append(f"unknown storey '{storey_arg}'; using default")
    # default: the storey with the most walls (ties -> lowest elevation)
    return max(storeys, key=lambda s: (wall_counts.get(s.id(), 0),
                                       -storey_elevation_in(s, scale_in)))


def assign_storey(el, storey_map, storeys, bbox, scale_in):
    """Explicit containment first; else the storey whose elevation band
    contains the element's base z (bbox is ((min3), (max3)) in inches)."""
    st = storey_map.get(el.id())
    if st is not None:
        return st
    if bbox is None or not storeys:
        return None
    z0 = bbox[0][2] + 1.0    # nudge up so slab-level bases don't round down
    chosen = storeys[0]
    for s in storeys:
        if storey_elevation_in(s, scale_in) <= z0:
            chosen = s
    return chosen


# --------------------------------------------------------------------------
# 2. geometry meshing (world coords, inches)
# --------------------------------------------------------------------------
def make_geom_settings():
    import ifcopenshell.geom
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)   # bake placements into verts
    return settings


def shape_verts(model_settings, el, scale_in, warnings, faces=False):
    """World-space vertices (N x 3 numpy array, inches); None on failure."""
    import ifcopenshell.geom
    import numpy as np
    try:
        shape = ifcopenshell.geom.create_shape(model_settings, el)
    except Exception as exc:
        warnings.append(f"{el.is_a()} #{el.id()} ({el.Name or 'unnamed'}): "
                        f"geometry failed ({exc})")
        return (None, None) if faces else None
    v = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3) * scale_in
    if v.size == 0:
        warnings.append(f"{el.is_a()} #{el.id()}: empty geometry")
        return (None, None) if faces else None
    if faces:
        f = np.array(shape.geometry.faces, dtype=int).reshape(-1, 3)
        return v, f
    return v


def bbox_of(verts):
    return ((float(verts[:, 0].min()), float(verts[:, 1].min()),
             float(verts[:, 2].min())),
            (float(verts[:, 0].max()), float(verts[:, 1].max()),
             float(verts[:, 2].max())))


# --------------------------------------------------------------------------
# 3. walls
# --------------------------------------------------------------------------
def wall_from_verts(verts, warnings, label):
    """Oriented 2D footprint of a wall mesh -> centerline + thickness.

    Minimum rotated rectangle of the XY vertex cloud (rotating calipers
    via shapely); the long sides give the axis, the short dimension the
    thickness. This beats the 'longest horizontal edge' heuristic because
    triangulated top faces contribute long diagonal edges. PCA fallback
    when shapely is unavailable or the footprint is degenerate.
    """
    import numpy as np
    xy = np.unique(np.round(verts[:, :2], 3), axis=0)
    axis = None
    corners = None
    if len(xy) >= 3:
        try:
            from shapely.geometry import MultiPoint
            rect = MultiPoint([tuple(p) for p in xy]).minimum_rotated_rectangle
            if rect.geom_type == "Polygon":
                corners = list(rect.exterior.coords)[:4]
        except Exception as exc:
            warnings.append(f"{label}: min-rect failed ({exc}); using PCA")
    if corners is not None:
        e1 = sub(corners[1], corners[0])
        e2 = sub(corners[2], corners[1])
        axis = unit(e1) if length(e1) >= length(e2) else unit(e2)
    if axis is None:                       # PCA / degenerate fallback
        centered = xy - xy.mean(axis=0)
        if len(xy) >= 2:
            _, _, vt = np.linalg.svd(centered, full_matrices=False)
            axis = unit((float(vt[0][0]), float(vt[0][1])))
        if axis is None or axis == (0.0, 0.0):
            warnings.append(f"{label}: degenerate footprint skipped")
            return None
    perp = (-axis[1], axis[0])
    t = xy @ np.array(axis)
    p = xy @ np.array(perp)
    t0, t1 = float(t.min()), float(t.max())
    p0, p1 = float(p.min()), float(p.max())
    thickness = p1 - p0
    clamped = min(max(thickness, MIN_THICKNESS), MAX_THICKNESS)
    if abs(clamped - thickness) > 0.05:
        warnings.append(f"{label}: thickness {thickness:.1f}\" clamped "
                        f"to {clamped:.1f}\"")
    pm = (p0 + p1) / 2.0
    height = float(verts[:, 2].max() - verts[:, 2].min())
    if not MIN_CEILING * 0.3 <= height <= MAX_CEILING * 1.5:
        height = WALL_HEIGHT
    return {
        "start": (axis[0] * t0 + perp[0] * pm, axis[1] * t0 + perp[1] * pm),
        "end": (axis[0] * t1 + perp[0] * pm, axis[1] * t1 + perp[1] * pm),
        "thickness": clamped,
        "height": height,
    }


def wall_storey(el, storey_map, storeys, bb, scale_in):
    """Storey a wall belongs to, correcting floating containment.

    Containment wins, except when the wall's base floats well above that
    storey's floor: multi-storey stair enclosures are commonly contained
    in their base-constraint level even though they stand in the storey
    above. Such walls move to the storey whose elevation band overlaps
    most of their z-extent."""
    st = assign_storey(el, storey_map, storeys, bb, scale_in)
    if st is None or bb is None:
        return st
    z0, z1 = bb[0][2], bb[1][2]
    if z0 - storey_elevation_in(st, scale_in) <= SOFFIT_BASE:
        return st
    best, best_ov = st, -1.0
    for i, s in enumerate(storeys):
        lo = storey_elevation_in(s, scale_in)
        hi = storey_elevation_in(storeys[i + 1], scale_in) \
            if i + 1 < len(storeys) else float("inf")
        ov = min(z1, hi) - max(z0, lo)
        if ov > best_ov:
            best, best_ov = s, ov
    return best


def build_walls(model, settings, storey, storey_map, storeys, scale_in,
                elev_in, warnings):
    """One plan wall per IfcWall on the chosen storey.

    Walls whose base still floats well above the storey floor after the
    reassignment (roof chases, skylight shafts, soffits over stairs) are
    dropped: the plan schema has no wall base elevation, so the viewer
    would wrongly ground them and wall off the space beneath. Returns
    (walls, id->index map for opening host lookup)."""
    walls, index_of = [], {}
    floated = 0
    for el in model.by_type("IfcWall"):     # includes IfcWallStandardCase
        verts = shape_verts(settings, el, scale_in, warnings)
        if verts is None:
            continue
        bb = bbox_of(verts)
        if wall_storey(el, storey_map, storeys, bb, scale_in) != storey:
            continue
        if bb[0][2] - elev_in > SOFFIT_BASE:
            floated += 1
            continue
        w = wall_from_verts(verts, warnings,
                            f"wall #{el.id()} ({el.Name or 'unnamed'})")
        if w is None:
            continue
        index_of[el.id()] = len(walls)
        walls.append(w)
    if floated:
        warnings.append(f"{floated} wall(s) with bases more than "
                        f"{SOFFIT_BASE:.0f}\" above the storey floor "
                        f"dropped (soffits / roof chases)")
    return walls, index_of


# --------------------------------------------------------------------------
# 4. doors / windows -> openings
# --------------------------------------------------------------------------
def host_wall_id(el):
    """The wall an IfcDoor/IfcWindow fills, via its opening element."""
    try:
        for fills in getattr(el, "FillsVoids", None) or []:
            opening = fills.RelatingOpeningElement
            for voids in getattr(opening, "VoidsElements", None) or []:
                host = voids.RelatingBuildingElement
                if host is not None and host.is_a("IfcWall"):
                    return host.id()
    except Exception:
        pass
    return None


def snap_to_wall(center, walls, candidates):
    """Nearest wall (same test as zind2plan.map_openings): the center must
    project inside the span (+snap) and sit within half the thickness
    (+snap) of the centerline. Returns (wall_index, t, wall_len) or None."""
    best = None
    for wi in candidates:
        w = walls[wi]
        axis = unit(sub(w["end"], w["start"]))
        wl = length(sub(w["end"], w["start"]))
        t = dot(sub(center, w["start"]), axis)
        if t < -OPENING_SNAP or t > wl + OPENING_SNAP:
            continue
        perp = abs(cross(axis, sub(center, w["start"])))
        if perp > w["thickness"] / 2.0 + OPENING_SNAP:
            continue
        if best is None or perp < best[0]:
            best = (perp, wi, min(max(t, 0.0), wl), wl)
    return best[1:] if best else None


def build_openings(model, settings, storey, storey_map, storeys, walls,
                   wall_index_of, scale_in, elev_in, warnings):
    """IfcDoor/IfcWindow on the storey -> openings on their walls."""
    import numpy as np
    openings = []
    unmapped = trimmed = 0
    for ifc_type, kind in (("IfcDoor", "door"), ("IfcWindow", "window")):
        for el in model.by_type(ifc_type):
            verts = shape_verts(settings, el, scale_in, warnings)
            if verts is None:
                continue
            bb = bbox_of(verts)
            if assign_storey(el, storey_map, storeys, bb,
                             scale_in) != storey:
                continue
            center = ((bb[0][0] + bb[1][0]) / 2.0, (bb[0][1] + bb[1][1]) / 2.0)
            host = wall_index_of.get(host_wall_id(el))
            hit = snap_to_wall(center, walls,
                               [host] if host is not None
                               else range(len(walls)))
            if hit is None and host is None:
                unmapped += 1
                warnings.append(f"{kind} #{el.id()} ({el.Name or 'unnamed'}) "
                                f"matched no wall; skipped")
                continue
            if hit is None:      # host known but center off its span: clamp
                w = walls[host]
                axis = unit(sub(w["end"], w["start"]))
                wl = length(sub(w["end"], w["start"]))
                hit = (host, min(max(dot(sub(center, w["start"]), axis),
                                     0.0), wl), wl)
            wi, t, wl = hit
            w = walls[wi]
            axis = unit(sub(w["end"], w["start"]))
            proj = verts[:, :2] @ np.array(axis)
            width = float(proj.max() - proj.min())
            sill = max(0.0, bb[0][2] - elev_in)
            head = bb[1][2] - elev_in

            # schedule data beats the mesh: door/window meshes include frame
            # trim, so OverallWidth/OverallHeight override mild disagreement;
            # wild disagreement means the projection went wrong - warn.
            ow = getattr(el, "OverallWidth", None)
            if isinstance(ow, (int, float)) and ow > 0:
                ow *= scale_in
                if abs(width - ow) > WILD_TOL:
                    warnings.append(
                        f"{kind} #{el.id()}: projected width {width:.1f}\" "
                        f"wildly disagrees with OverallWidth {ow:.1f}\"; "
                        f"using the schedule value")
                    width = ow
                elif abs(width - ow) > FRAME_TOL:
                    trimmed += 1
                    width = ow
            oh = getattr(el, "OverallHeight", None)
            if isinstance(oh, (int, float)) and oh > 0:
                oh *= scale_in
                if abs((head - sill) - oh) > WILD_TOL:
                    warnings.append(
                        f"{kind} #{el.id()}: projected height "
                        f"{head - sill:.1f}\" wildly disagrees with "
                        f"OverallHeight {oh:.1f}\"; using the schedule value")
                    head = sill + oh
                elif abs((head - sill) - oh) > FRAME_TOL:
                    trimmed += 1
                    head = sill + oh
            openings.append({
                "wall_index": wi,
                "position": round(t / wl if wl else 0.0, 4),
                "width": round(width, 2),
                "type": kind,
                "sill": round(sill, 1),
                "head": round(head, 1),
            })
    if trimmed:
        warnings.append(f"{trimmed} projected opening dimension(s) included "
                        f"frame trim; replaced with OverallWidth/Height")
    return openings, unmapped


# --------------------------------------------------------------------------
# 5. rooms from IfcSpace
# --------------------------------------------------------------------------
def room_kind(space):
    name = " ".join(str(x) for x in (space.LongName, space.Name,
                                     space.ObjectType) if x).lower()
    for key, kind in ROOM_KINDS:
        if key in name:
            return kind
    return "room"


def build_rooms(model, settings, storey, storey_map, storeys, scale_in,
                warnings):
    """IfcSpace footprints: base-z vertices -> convex hull polygon."""
    import numpy as np
    rooms = []
    approximated = 0
    for space in model.by_type("IfcSpace"):
        verts = shape_verts(settings, space, scale_in, warnings)
        if verts is None:
            continue
        if assign_storey(space, storey_map, storeys, bbox_of(verts),
                         scale_in) != storey:
            continue
        label = space.LongName or space.Name or "room"
        z0 = float(verts[:, 2].min())
        base = verts[np.abs(verts[:, 2] - z0) <= BASE_Z_TOL][:, :2]
        base = np.unique(np.round(base, 3), axis=0)
        if len(base) < 3:
            warnings.append(f"space #{space.id()} ({label}): "
                            f"degenerate footprint skipped")
            continue
        try:
            from shapely.geometry import MultiPoint
            hull = MultiPoint([tuple(p) for p in base]).convex_hull
            if hull.geom_type != "Polygon":
                warnings.append(f"space #{space.id()} ({label}): "
                                f"collinear footprint skipped")
                continue
            poly = [[round(x, 3), round(y, 3)]
                    for x, y in list(hull.exterior.coords)[:-1]]
            if len(base) > len(poly):
                approximated += 1     # hull dropped verts: likely concave
        except ImportError:
            warnings.append("shapely not installed; room footprints are "
                            "raw axis-aligned boxes")
            poly = [[round(float(base[:, 0].min()), 3), round(float(base[:, 1].min()), 3)],
                    [round(float(base[:, 0].max()), 3), round(float(base[:, 1].min()), 3)],
                    [round(float(base[:, 0].max()), 3), round(float(base[:, 1].max()), 3)],
                    [round(float(base[:, 0].min()), 3), round(float(base[:, 1].max()), 3)]]
        room = {"polygon": poly, "kind": room_kind(space),
                "name": space.Name or "", "label": str(label)}
        zext = float(verts[:, 2].max()) - z0
        if MIN_CEILING <= zext <= MAX_CEILING:
            room["ceiling"] = round(zext, 1)
        rooms.append(room)
    if approximated:
        warnings.append(f"{approximated} space(s) had concave footprints "
                        f"approximated by their convex hull")
    return rooms


# --------------------------------------------------------------------------
# 6. fixtures
# --------------------------------------------------------------------------
def placement_rotation_deg(el):
    """Z-rotation of the object placement (degrees CCW), 0 on any trouble."""
    try:
        from ifcopenshell.util.placement import get_local_placement
        m = get_local_placement(el.ObjectPlacement)
        return math.degrees(math.atan2(float(m[1][0]), float(m[0][0])))
    except Exception:
        return 0.0


def build_fixtures(model, settings, storey, storey_map, storeys, scale_in,
                   warnings):
    """Furniture and terminals -> oriented-box fixture stand-ins."""
    import numpy as np
    fixtures = []
    types = ["IfcFurnishingElement"]
    try:
        if model.by_type("IfcFlowTerminal"):
            types.append("IfcFlowTerminal")
    except Exception:
        pass
    for ifc_type in types:
        for el in model.by_type(ifc_type):
            verts = shape_verts(settings, el, scale_in, warnings)
            if verts is None:
                continue
            bb = bbox_of(verts)
            if assign_storey(el, storey_map, storeys, bb,
                             scale_in) != storey:
                continue
            rot = placement_rotation_deg(el)
            r = math.radians(rot)
            axis = np.array((math.cos(r), math.sin(r)))
            perp = np.array((-math.sin(r), math.cos(r)))
            t = verts[:, :2] @ axis
            p = verts[:, :2] @ perp
            tc, pc = (t.min() + t.max()) / 2.0, (p.min() + p.max()) / 2.0
            center = tc * axis + pc * perp
            fixtures.append({
                "name": el.Name or el.ObjectType or ifc_type,
                "center": [round(float(center[0]), 3),
                           round(float(center[1]), 3)],
                "rotation": round(rot % 360.0, 2),
                "size": [round(float(t.max() - t.min()), 2),
                         round(float(p.max() - p.min()), 2)],
                "height": round(float(verts[:, 2].max() - verts[:, 2].min()), 1),
            })
    return fixtures


# --------------------------------------------------------------------------
# 7. building footprint (mirrors zind2plan.compute_footprint)
# --------------------------------------------------------------------------
def compute_footprint(walls, warnings):
    """Buffer each wall centerline by half its thickness (square caps),
    union, take the largest polygon's exterior ring, simplified."""
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
        coords = list(ring.coords)[:-1]
        if len(coords) < 3:
            warnings.append("footprint failed: degenerate exterior ring")
            return []
        return [[round(x, 3), round(y, 3)] for x, y in coords]
    except Exception as exc:
        warnings.append(f"footprint failed: {exc}")
        return []


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def convert(path, storey_arg):
    import ifcopenshell
    warnings = []
    model = ifcopenshell.open(path)

    scale_in = unit_scale(model, warnings) * METERS_TO_INCHES
    storeys = list_storeys(model, scale_in)
    if not storeys:
        warnings.append("no IfcBuildingStorey found")
        return None, None
    storey_map = map_products_to_storeys(model, warnings)

    settings = make_geom_settings()

    # wall census per storey (containment only - cheap, no meshing) both to
    # pick the default storey and for the summary line
    wall_counts = {}
    for el in model.by_type("IfcWall"):
        st = storey_map.get(el.id())
        if st is not None:
            wall_counts[st.id()] = wall_counts.get(st.id(), 0) + 1

    storey = pick_storey(storeys, storey_arg, wall_counts, scale_in, warnings)
    elev_in = storey_elevation_in(storey, scale_in)

    walls, wall_index_of = build_walls(model, settings, storey, storey_map,
                                       storeys, scale_in, elev_in, warnings)
    openings, unmapped = build_openings(model, settings, storey, storey_map,
                                        storeys, walls, wall_index_of,
                                        scale_in, elev_in, warnings)
    rooms = build_rooms(model, settings, storey, storey_map, storeys,
                        scale_in, warnings)
    fixtures = build_fixtures(model, settings, storey, storey_map, storeys,
                              scale_in, warnings)
    footprint = compute_footprint(walls, warnings)

    plan = {
        "units": "inches",
        "drawing_units": "meters" if abs(scale_in - METERS_TO_INCHES) < 1e-6
        else f"model units x {scale_in / METERS_TO_INCHES:.6g} m",
        "storey": storey.Name or "",
        "storey_elevation": round(elev_in, 2),
        "storeys_available": [
            {"name": s.Name or "", "elevation": round(
                storey_elevation_in(s, scale_in), 2),
             "walls": wall_counts.get(s.id(), 0)}
            for s in storeys
        ],
        "walls": [
            {
                "start": [round(w["start"][0], 3), round(w["start"][1], 3)],
                "end": [round(w["end"][0], 3), round(w["end"][1], 3)],
                "thickness": round(w["thickness"], 2),
                "height": round(w["height"], 1),
            }
            for w in walls
        ],
        "openings": openings,
        "rooms": rooms,
        "fixtures": fixtures,
        "footprint": footprint,
        "warnings": warnings,
    }

    kinds = {}
    for o in openings:
        kinds[o["type"]] = kinds.get(o["type"], 0) + 1
    room_kinds = {}
    for r in rooms:
        room_kinds[r["kind"]] = room_kinds.get(r["kind"], 0) + 1
    report = {
        "storey": storey.Name or "",
        "elevation": elev_in,
        "storeys": [(s.Name or "", storey_elevation_in(s, scale_in),
                     wall_counts.get(s.id(), 0)) for s in storeys],
        "walls": len(walls),
        "openings": len(openings),
        "opening_kinds": kinds,
        "unmapped_openings": unmapped,
        "rooms": len(rooms),
        "room_kinds": room_kinds,
        "fixtures": len(fixtures),
        "footprint_points": len(footprint),
    }
    return plan, report


def main():
    ap = argparse.ArgumentParser(
        description="IFC building model -> plan.json (one storey)")
    ap.add_argument("input", help="IFC file (IFC2X3 / IFC4)")
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--storey", default=None,
                    help="storey index (0-based, sorted by elevation) or "
                         "name like 'Level 1'; default = most walls")
    args = ap.parse_args()

    try:
        plan, report = convert(args.input, args.storey)
    except IOError:
        print(f"error: cannot read '{args.input}'", file=sys.stderr)
        sys.exit(1)
    if plan is None:
        print("error: no storeys found in input", file=sys.stderr)
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

    storeys = ", ".join(f"{name or '?'} ({elev:.0f}\", {n} walls)"
                        for name, elev, n in report["storeys"])
    kinds = ", ".join(f"{v} {k}"
                      for k, v in sorted(report["opening_kinds"].items()))
    room_kinds = ", ".join(
        f"{v} {k}" for k, v in sorted(report["room_kinds"].items()))
    print(f"Parsed {args.input} -> {args.output}")
    print(f"  storey              : {report['storey']} "
          f"(elev {report['elevation']:.1f}\")")
    print(f"  storeys available   : {storeys}")
    print(f"  walls               : {report['walls']}")
    print(f"  openings            : {report['openings']}"
          + (f" ({kinds})" if kinds else "")
          + (f", {report['unmapped_openings']} unmapped"
             if report["unmapped_openings"] else ""))
    print(f"  rooms               : {report['rooms']}"
          + (f" ({room_kinds})" if room_kinds else ""))
    print(f"  fixtures            : {report['fixtures']}")
    print(f"  footprint           : {report['footprint_points']} points")
    if plan["warnings"]:
        print(f"  warnings            : {len(plan['warnings'])} "
              f"(see 'warnings' in {args.output})")
    if embed_path:
        print(f"  embedded copy       : {embed_path} "
              f"(lets the viewer open via file://)")


if __name__ == "__main__":
    main()

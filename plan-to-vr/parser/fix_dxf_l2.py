#!/usr/bin/env python3
"""One-off layer corrections for the floor-2 architect DXF (A1.2).

The A1.2 export has a handful of entities drawn on the WALLS layer that
are not walls (confirmed with the homeowner against the sheet):

  * two door symbols (red swing arc + green leaf/jambs) -> 1.2DRWDWS
  * the hall bath tub and the shower stall               -> 1.2FURN
  * a red demolition wall stub in the bedroom            -> 1.2DEMO
  * two single "structure over" indicator lines          -> deleted

This script is the revert path: it never modifies its input, it writes a
corrected COPY, and re-running it on the pristine original reproduces the
same output. To revert, simply regenerate the plan from the original.

Usage:
    python fix_dxf_l2.py original-A1.2.dxf corrected-A1.2.dxf
"""
import math
import sys

import ezdxf

WALLS = "1.2WALLS"


def _mid(e):
    t = e.dxftype()
    if t == "LINE":
        return ((e.dxf.start[0] + e.dxf.end[0]) / 2,
                (e.dxf.start[1] + e.dxf.end[1]) / 2)
    if t in ("ARC", "CIRCLE"):
        return (e.dxf.center[0], e.dxf.center[1])
    if t == "LWPOLYLINE":
        pts = list(e.get_points())
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts))
    if t == "POINT":
        return (e.dxf.location[0], e.dxf.location[1])
    return None


def _linelen(e):
    return math.hypot(e.dxf.end[0] - e.dxf.start[0],
                      e.dxf.end[1] - e.dxf.start[1])


def _line_matches(e, a, b, tol=2.0):
    for (p, q) in ((a, b), (b, a)):
        if (math.hypot(e.dxf.start[0] - p[0], e.dxf.start[1] - p[1]) < tol
                and math.hypot(e.dxf.end[0] - q[0], e.dxf.end[1] - q[1]) < tol):
            return True
    return False


# entities to delete: the two single "structure over" indicator lines
DELETE_LINES = [((-197.3, 80.7), (-34.3, 80.7)),
                ((-47.8, 70.2), (99.2, 70.2))]

# door symbols: everything green (leaf/jambs) + the red arc + short red
# jamb caps inside each box moves to the doors/windows layer
DOOR_BOXES = [(2, 48, -99, -85),      # hall door, arc hinge (12,-93)
              (-73, -37, -48, -15)]   # bath door, arc hinge (-46,-41)

# tub: outlines are green/red-family, plus its arcs / faucet / points
TUB_BOX = (202, 244, 28, 66)
# shower: pan curves + enclosure sides + drain are ACI red (1)
SHOWER_BOX = (212, 263, -100, -10)
# demolition wall stub in the bedroom (red closed polyline)
DEMO_BOX = (-74, -59, -87, -64)

# erroneous all-red door symbol (homeowner: not a real door). Every valid
# door is a red arc + GREEN leaf; this one at hinge (163,-78) is drawn
# entirely red - leftover linework, not a door. Arc + red leaf -> demo.
BAD_DOOR_BOX = (150, 200, -82, -40)


def in_box(pt, box):
    x0, x1, y0, y1 = box
    return pt is not None and x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1


def main(src, dst):
    doc = ezdxf.readfile(src)
    msp = doc.modelspace()
    moved = {"1.2DRWDWS": 0, "1.2FURN": 0, "1.2DEMO": 0}
    deleted = []

    for e in list(msp):
        t = e.dxftype()
        pt = _mid(e)

        # the erroneous all-red door lives on the doors/windows layer
        if (e.dxf.layer == "1.2DRWDWS" and in_box(pt, BAD_DOOR_BOX)
                and e.dxf.color in (1, 11)
                and (t == "ARC" or (t == "LINE" and _linelen(e) > 6))):
            e.dxf.layer = "1.2DEMO"
            moved["1.2DEMO"] += 1
            continue

        if e.dxf.layer != WALLS:
            continue

        if t == "LINE" and any(_line_matches(e, a, b)
                               for a, b in DELETE_LINES):
            deleted.append(e)
            continue

        for box in DOOR_BOXES:
            if in_box(pt, box):
                if (t == "ARC" and e.dxf.color == 1) \
                        or (t == "LINE" and e.dxf.color == 3) \
                        or (t == "LINE" and e.dxf.color == 1
                            and _linelen(e) < 8):
                    e.dxf.layer = "1.2DRWDWS"
                    moved["1.2DRWDWS"] += 1

        if in_box(pt, TUB_BOX):
            if (t in ("ARC", "CIRCLE", "LWPOLYLINE", "POINT")
                    or (t == "LINE" and e.dxf.color in (3, 11))):
                e.dxf.layer = "1.2FURN"
                moved["1.2FURN"] += 1
        elif in_box(pt, SHOWER_BOX):
            if e.dxf.color == 1 and t in ("LINE", "CIRCLE"):
                e.dxf.layer = "1.2FURN"
                moved["1.2FURN"] += 1
        elif in_box(pt, DEMO_BOX):
            if e.dxf.color == 1:
                e.dxf.layer = "1.2DEMO"
                moved["1.2DEMO"] += 1

    for e in deleted:
        msp.delete_entity(e)

    doc.saveas(dst)
    print(f"wrote {dst}: moved {moved}, deleted {len(deleted)} "
          f"indicator line(s)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

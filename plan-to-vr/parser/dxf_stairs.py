#!/usr/bin/env python3
"""Detect stair runs from clean DXF linework (evenly-spaced tread ladders).

Architect DXF draws stair treads as a run of parallel, similar-length,
evenly-spaced lines - far cleaner than the plotted-PDF case, so a direct
geometric detector works. Returns run dicts shaped like the viewer's
plan["stairs"]: {polygon, treads:[[[x,y],[x,y]],...], direction?, down?}.

The architect also writes a small "Down" / "Up" / "Up to Attic" text beside
each flight naming which way it goes FROM THIS floor - the authoritative
indicator of which side is level to this floor. In the native DXF that text
is real, readable TEXT/MTEXT (unlike the plotted-PDF case), so we read it and
tag each run with `down` (bool) and a signed climb `label_dir`. A GT sidecar
can still override per flight where the label is ambiguous or absent.
"""
import math
from collections import defaultdict

import ezdxf


def _lines(path, layers):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace().query("LINE"):
        if e.dxf.layer in layers:
            a, b = e.dxf.start, e.dxf.end
            out.append((a[0], a[1], b[0], b[1]))
    return out


def _angle(seg):
    a = math.atan2(seg[3] - seg[1], seg[2] - seg[0]) % math.pi
    return a


def _dir_labels(path):
    """Read the architect's up/down flight tags. Returns list of
    (kind, (x, y)) where kind is "down" or "up". Kept deliberately strict
    (whole-word DOWN/DN/UP/UP TO.../ATTIC) so unrelated notes elsewhere on
    the sheet ("New window", a dimension string) never masquerade as a stair
    direction; proximity to a detected flight gates it further in detect()."""
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace():
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        t = (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text)
        u = t.strip().upper().rstrip(".")
        if u in ("DOWN", "DN"):
            kind = "down"
        elif u == "UP" or u == "ATTIC" or u.startswith("UP TO"):
            kind = "up"
        else:
            continue
        p = e.dxf.insert
        out.append((kind, (p[0], p[1])))
    return out


def _term_circles(path):
    """The architect marks each flight's TERMINATING side - the end that is
    level with THIS floor - with a small annotation circle (r~4in) on the
    notes/room-name layer, sitting on the flight's travel line (verified in
    ShareCAD, VR notes #14/#30). The run travels (descends if down, climbs if
    up) AWAY from that circle. This is the authoritative termination/direction
    indicator; the Down/Up text only says which KIND, not which end is the
    floor - and on a shared landing both flights' text sits together, so the
    text position can't give direction. Returns [(x, y)]."""
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace().query("CIRCLE"):
        lyr = e.dxf.layer.upper()
        if ("NOTES" in lyr or "RMNAMES" in lyr) and 2.0 <= e.dxf.radius <= 8.0:
            c = e.dxf.center
            out.append((c[0], c[1]))
    return out


def _annotate_directions(runs, labels, circles=None, maxd=80.0, circd=70.0):
    """Tag each run with `down` (from the nearest Down/Up text) and a signed
    `label_dir` along its climb axis. DIRECTION comes from the flight's
    terminating CIRCLE when one is near: the run travels AWAY from the circle
    (which marks the end level with this floor). Only if no circle is near does
    it fall back to the label position (down away from label, up toward it).
    Circle-first fixes shared-landing stairs where both flights' text sits at
    the same landing yet the runs' floor-ends are marked by their own circles
    (VR #14/#30)."""
    circles = circles or []
    for r in runs:
        cx, cy = r["centroid"]
        best, bestd = None, maxd
        for kind, (lx, ly) in labels:
            d = math.hypot(cx - lx, cy - ly)
            if d < bestd:
                best, bestd = (kind, (lx, ly)), d
        if best is None:
            continue
        kind, (lx, ly) = best
        ux, uy = r["climb"]
        circ, cd = None, circd
        for (qx, qy) in circles:
            d = math.hypot(cx - qx, cy - qy)
            if d < cd:
                circ, cd = (qx, qy), d
        if circ is not None:
            qx, qy = circ                        # travel AWAY from the circle
            s = 1.0 if ((cx - qx) * ux + (cy - qy) * uy) >= 0 else -1.0
        else:
            s0 = 1.0 if ((lx - cx) * ux + (ly - cy) * uy) >= 0 else -1.0
            s = -s0 if kind == "down" else s0
        r["down"] = (kind == "down")
        r["label_dir"] = [round(s * ux, 2), round(s * uy, 2)]
    return runs


def detect(path, layers, tread_min=22, tread_max=80, spacing_lo=7,
           spacing_hi=15, min_treads=3):
    """Two-level clustering makes switchbacks fall out naturally:
        angle bucket -> LATERAL BAND -> chain along the climb.
    Treads in one flight share a lateral centre (they stack along the
    climb); the two halves of a U-stair sit in two lateral bands, so
    each becomes its own run instead of being merged into one."""
    segs = _lines(path, layers)
    treads = [s for s in segs
              if tread_min <= math.hypot(s[2] - s[0], s[3] - s[1]) <= tread_max]
    buckets = defaultdict(list)
    for s in treads:
        buckets[round(math.degrees(_angle(s)) / 5) * 5].append(s)

    runs = []
    for ang, group in buckets.items():
        tdir = (math.cos(math.radians(ang)), math.sin(math.radians(ang)))
        nrm = (-tdir[1], tdir[0])            # climb axis (perp to tread)
        items = []
        for s in group:
            mx, my = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
            L = math.hypot(s[2] - s[0], s[3] - s[1])
            items.append({"c": mx * nrm[0] + my * nrm[1],       # climb pos
                          "lat": mx * tdir[0] + my * tdir[1],    # lateral pos
                          "L": L, "seg": s, "mx": mx, "my": my})
        # 0) spatially cluster treads before lateral banding. Lateral banding
        #    alone is GLOBAL within an angle bucket, so an unrelated vertical
        #    line elsewhere in the plan (a window mullion, a stray fragment)
        #    that happens to share a lateral value bridges two distant flights
        #    into one band - and when two stacked flights share climb positions
        #    the collision then swallows one flight entirely. Grouping treads by
        #    centre proximity first keeps each stairwell self-contained; a
        #    U-stair's two flights (~half a tread-length apart laterally) stay
        #    in one cluster and are split by the lateral pass below.
        CLUSTER = 72.0
        n = len(items)
        parent = list(range(n))

        def _find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a

        for i in range(n):
            for j in range(i + 1, n):
                if _find(i) == _find(j):
                    continue
                if math.hypot(items[i]["mx"] - items[j]["mx"],
                              items[i]["my"] - items[j]["my"]) <= CLUSTER:
                    parent[_find(i)] = _find(j)
        clusters = {}
        for i in range(n):
            clusters.setdefault(_find(i), []).append(items[i])

        bands = []
        for cluster in clusters.values():
            # 1) split cluster into lateral bands (one flight per band): sort by
            #    lateral centre, break where the gap exceeds ~half a tread
            cluster.sort(key=lambda d: d["lat"])
            cur = []
            for it in cluster:
                if cur and it["lat"] - cur[-1]["lat"] > 0.6 * it["L"]:
                    bands.append(cur); cur = []
                cur.append(it)
            if cur:
                bands.append(cur)
        # 2) within a band, chain evenly-spaced treads along the climb
        for band in bands:
            band.sort(key=lambda d: d["c"])
            i = 0
            while i < len(band):
                chain = [band[i]]
                j = i + 1
                while j < len(band):
                    gap = band[j]["c"] - chain[-1]["c"]
                    if gap < spacing_lo:
                        j += 1
                        continue
                    if gap > spacing_hi:
                        break
                    med = sorted(c["L"] for c in chain)[len(chain) // 2]
                    if 0.55 * med <= band[j]["L"] <= 1.8 * med:
                        chain.append(band[j]); j += 1
                    else:
                        break
                if len(chain) >= min_treads:
                    runs.append(_build([c["seg"] for c in chain], nrm))
                i = j if j > i + 1 else i + 1
    _annotate_directions(runs, _dir_labels(path), _term_circles(path))
    return runs


def _build(segset, nrm):
    treads = [[[round(s[0], 1), round(s[1], 1)],
               [round(s[2], 1), round(s[3], 1)]] for s in segset]
    xs = [c for s in segset for c in (s[0], s[2])]
    ys = [c for s in segset for c in (s[1], s[3])]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return {
        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "treads": treads,
        "n": len(treads),
        "climb": [round(nrm[0], 2), round(nrm[1], 2)],
        "centroid": [round((x0 + x1) / 2, 1), round((y0 + y1) / 2, 1)],
    }


if __name__ == "__main__":
    import sys, json
    runs = detect(sys.argv[1], set(sys.argv[2].split(",")))
    for r in runs:
        print(f"run @ {r['centroid']}  treads={r['n']}  climb={r['climb']}  "
              f"bbox={r['polygon'][0]}..{r['polygon'][2]}")

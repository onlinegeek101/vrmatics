#!/usr/bin/env python3
"""Detect stair runs from clean DXF linework (evenly-spaced tread ladders).

Architect DXF draws stair treads as a run of parallel, similar-length,
evenly-spaced lines - far cleaner than the plotted-PDF case, so a direct
geometric detector works. Returns run dicts shaped like the viewer's
plan["stairs"]: {polygon, treads:[[[x,y],[x,y]],...], direction?, down?}.
Direction / down come from a ground-truth sidecar (the plotted "Up/Down"
text is unreadable geometry), matched by run centroid.
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

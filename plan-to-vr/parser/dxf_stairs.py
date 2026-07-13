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
           spacing_hi=15, min_treads=4):
    segs = _lines(path, layers)
    treads = [s for s in segs
              if tread_min <= math.hypot(s[2] - s[0], s[3] - s[1]) <= tread_max]
    # bucket by orientation (0 or 90-ish) rounded to 5 deg
    buckets = defaultdict(list)
    for s in treads:
        buckets[round(math.degrees(_angle(s)) / 5) * 5].append(s)

    runs = []
    used = set()
    for ang, group in buckets.items():
        tdir = (math.cos(math.radians(ang)), math.sin(math.radians(ang)))
        nrm = (-tdir[1], tdir[0])            # climb axis (perp to tread)
        # each tread: climb-projection, lateral span [lo,hi], length, seg
        items = []
        for s in group:
            mx, my = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
            L = math.hypot(s[2] - s[0], s[3] - s[1])
            lat = mx * tdir[0] + my * tdir[1]
            items.append({"c": mx * nrm[0] + my * nrm[1],
                          "lo": lat - L / 2, "hi": lat + L / 2,
                          "L": L, "seg": s})
        items.sort(key=lambda d: d["c"])
        i = 0
        while i < len(items):
            chain = [items[i]]
            clat = [items[i]["lo"], items[i]["hi"]]        # running lateral span
            j = i + 1
            while j < len(items):
                cand = items[j]
                gap = cand["c"] - chain[-1]["c"]
                if gap < spacing_lo:               # duplicate line
                    j += 1
                    continue
                if gap > spacing_hi:
                    break
                # a real next tread must STACK: lateral overlap with the run,
                # and comparable length (not a stray glazing line)
                ov = min(clat[1], cand["hi"]) - max(clat[0], cand["lo"])
                med = sorted(c["L"] for c in chain)[len(chain) // 2]
                if ov > 0.4 * min(med, cand["L"]) and \
                        0.55 * med <= cand["L"] <= 1.8 * med:
                    chain.append(cand)
                    clat[0] = min(clat[0], cand["lo"])
                    clat[1] = max(clat[1], cand["hi"])
                    j += 1
                else:
                    break
            if len(chain) >= min_treads:
                segset = [c["seg"] for c in chain]
                key = tuple(sorted(id(s) for s in segset))
                if key not in used:
                    used.add(key)
                    runs.append(_build(segset, nrm))
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

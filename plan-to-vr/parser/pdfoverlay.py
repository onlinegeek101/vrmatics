#!/usr/bin/env python3
"""Overlay a generated plan.json onto the source PDF sheet for auditing.

Draws the plan's wall faces and opening spans, mapped back through the
exact transform pdf2plan used (page rotation + inches-per-point), on top
of a raster of the original page. Registration is implicit: if the
extraction is right, every colored line sits on a drawn line; anything
floating off the drawing is a defect, anything drawn but uncovered is a
miss.

Usage:
    python pdfoverlay.py binder.pdf plan.json --page 0 -o overlay.png
                         [--dpi 150] [--crop x0,y0,x1,y1]  (plan inches)
"""
import argparse
import json
import math

import fitz
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("plan")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("-o", "--output", default="overlay.png")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--crop", default="", help="x0,y0,x1,y1 in plan inches")
    args = ap.parse_args()

    plan = json.load(open(args.plan))
    ips = (plan.get("source") or {}).get("inches_per_point")
    if not ips:
        raise SystemExit("plan has no source.inches_per_point; regenerate "
                         "with pdf2plan.py")

    doc = fitz.open(args.pdf)
    page = doc[args.page]
    pix = page.get_pixmap(dpi=args.dpi)
    import numpy as np
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]

    H = page.rect.height
    k = args.dpi / 72.0

    def to_px(x_in, y_in):
        # inverse of pdf2plan.page_segments: plan inches -> display pt -> px
        return (x_in / ips * k, (H - y_in / ips) * k)

    fig, ax = plt.subplots(figsize=(pix.width / 100, pix.height / 100),
                           dpi=100)
    ax.imshow(img, extent=(0, pix.width, pix.height, 0))

    wall_lines, open_lines, open_cols = [], [], []
    C = {"door": (1, 0.1, 0.1), "window": (0.1, 0.35, 1),
         "opening": (1, 0.6, 0)}
    walls = plan["walls"]
    for w in walls:
        ax_, ay = w["start"]; bx, by = w["end"]
        dx, dy = bx - ax_, by - ay
        L = (dx * dx + dy * dy) ** 0.5 or 1
        nx, ny = -dy / L * w["thickness"] / 2, dx / L * w["thickness"] / 2
        for s in (1, -1):
            wall_lines.append([to_px(ax_ + nx * s, ay + ny * s),
                               to_px(bx + nx * s, by + ny * s)])
    for o in plan["openings"]:
        w = walls[o["wall_index"]]
        ax_, ay = w["start"]; bx, by = w["end"]
        dx, dy = bx - ax_, by - ay
        L = (dx * dx + dy * dy) ** 0.5 or 1
        ux, uy = dx / L, dy / L
        mx, my = ax_ + dx * o["position"], ay + dy * o["position"]
        h = o["width"] / 2
        open_lines.append([to_px(mx - ux * h, my - uy * h),
                           to_px(mx + ux * h, my + uy * h)])
        open_cols.append(C.get(o["type"], C["opening"]))

    ax.add_collection(LineCollection(
        wall_lines, colors=[(1, 0.1, 0.7)], linewidths=1.1, alpha=0.85))
    ax.add_collection(LineCollection(
        open_lines, colors=open_cols, linewidths=2.2, alpha=0.85))
    # stairs: run outline + treads
    for st in plan.get("stairs") or []:
        poly = st["polygon"] + [st["polygon"][0]]
        pxs = [to_px(x, y) for x, y in poly]
        ax.plot([q[0] for q in pxs], [q[1] for q in pxs],
                color=(0, 0.75, 0.85), linewidth=1.4, alpha=0.9)
        ax.add_collection(LineCollection(
            [[to_px(*t[0]), to_px(*t[1])] for t in st["treads"]],
            colors=[(0, 0.75, 0.85)], linewidths=0.8, alpha=0.8))

    # door swings: hinge-anchored quarter arc + open leaf
    import numpy as np
    for o in plan["openings"]:
        if o["type"] != "door" or not o.get("hinge"):
            continue
        w = walls[o["wall_index"]]
        ax_, ay = w["start"]; bx, by = w["end"]
        dx, dy = bx - ax_, by - ay
        L = (dx * dx + dy * dy) ** 0.5 or 1
        ux, uy = dx / L, dy / L
        mx, my = ax_ + dx * o["position"], ay + dy * o["position"]
        half = o["width"] / 2
        hx = mx + ux * half * o["hinge"]      # hinge jamb
        hy = my + uy * half * o["hinge"]
        away = math.atan2(uy, ux)             # toward the far jamb
        if o["hinge"] > 0:
            away += math.pi
        # rotate the leaf from along-the-wall toward the swing-side
        # normal; the rotation sense flips with the hinge end
        angs = [away + t * o["swing"] * (1 if o["hinge"] < 0 else -1)
                for t in np.linspace(0, math.pi / 2, 24)]
        leaf = min(o.get("leaf") or o["width"], o["width"])
        pts = [(hx + leaf * math.cos(a),
                hy + leaf * math.sin(a)) for a in angs]
        ax.plot([to_px(x, y)[0] for x, y in pts],
                [to_px(x, y)[1] for x, y in pts],
                color=(0.9, 0, 0.4), linewidth=1.0, alpha=0.9)
        ax.plot([to_px(hx, hy)[0], to_px(*pts[-1])[0]],
                [to_px(hx, hy)[1], to_px(*pts[-1])[1]],
                color=(0.9, 0, 0.4), linewidth=1.2, alpha=0.9)

    for f in plan.get("fixtures") or []:
        cx, cy = f["center"]
        w2, d2 = f["size"][0] / 2, f["size"][1] / 2
        box = [to_px(cx - w2, cy - d2), to_px(cx + w2, cy - d2),
               to_px(cx + w2, cy + d2), to_px(cx - w2, cy + d2),
               to_px(cx - w2, cy - d2)]
        ax.plot([q[0] for q in box], [q[1] for q in box],
                color=(0, 0.7, 0.3), linewidth=1.6, alpha=0.9)

    if args.crop:
        x0, y0, x1, y1 = [float(v) for v in args.crop.split(",")]
        p0, p1 = to_px(x0, y1), to_px(x1, y0)   # y flips
        ax.set_xlim(p0[0], p1[0]); ax.set_ylim(p1[1], p0[1])
    else:
        ax.set_xlim(0, pix.width); ax.set_ylim(pix.height, 0)
    ax.axis("off")
    fig.savefig(args.output, bbox_inches="tight", pad_inches=0)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()

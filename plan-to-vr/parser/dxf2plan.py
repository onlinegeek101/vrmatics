#!/usr/bin/env python3
"""Build a viewer plan.json from a native architect DXF (one floor).

Unlike pdf2plan (which reconstructs geometry from a plotted raster), the
architect's DXF is clean layered vector CAD, so this is thin: run the
shared extractor for walls/doors/windows/rooms, then augment with what
the DXF gives us for free that the PDF never did -

  * real room NAMES (the RMNAMES text layer) -> room labels + kinds,
    which also drives garage split-level and per-room floor materials;
  * stair runs detected straight from the tread ladders (dxf_stairs),
    with down / direction / level from a small ground-truth sidecar
    (the "Up/Down" text is unreadable geometry, same as the PDF);
  * the DEMO layer is simply excluded, so historical/demo linework never
    pollutes the model (no dash-filtering heuristics needed).

Usage:
    python dxf2plan.py plan.dxf -o home-l1.json --floor 1 \
        --gt corrections/dxf-l1.json
"""
import argparse
import json
import math

import ezdxf

import extract as X
import dxf_stairs
from pdf2plan import default_camera, point_in_poly


def room_labels(path, layer):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace():
        if e.dxftype() in ("TEXT", "MTEXT") and e.dxf.layer == layer:
            t = (e.plain_text() if e.dxftype() == "MTEXT"
                 else e.dxf.text).strip()
            p = e.dxf.insert
            if t:
                out.append((t, (p[0], p[1])))
    return out


NAME_KIND = [
    ("GARAGE", "garage"), ("BATH", "bath"), ("LAUNDRY", "laundry"),
    ("KITCHEN", "kitchen"), ("MUDROOM", "mud"), ("CLOSET", "closet"),
    ("PANTRY", "pantry"), ("BEDROOM", "bedroom"), ("LIVING", "living"),
    ("DINING", "dining"), ("SUNROOM", "sun"), ("PORCH", "porch"),
    ("STAIR", "stair"), ("HALL", "hall"), ("PLAY", "play"),
    ("LINEN", "closet"), ("WALK-IN", "closet"), ("COAT", "closet"),
]


def kind_for(name):
    up = name.upper()
    for key, kind in NAME_KIND:
        if key in up:
            return kind
    return "room"


def assign_names(plan, labels):
    """Give each detected room the label whose point sits inside it (else
    the nearest label), and a kind derived from that name."""
    for r in plan.get("rooms", []):
        poly = r["polygon"]
        best, bestd = None, 1e18
        for name, (lx, ly) in labels:
            if point_in_poly(lx, ly, poly):
                best, bestd = name, -1
                break
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            d = math.hypot(cx - lx, cy - ly)
            if d < bestd:
                best, bestd = name, d
        if best is not None:
            r["name"] = best.replace("  ", " ")
            k = kind_for(best)
            # keep bath/laundry/garage/kitchen (the viewer styles them);
            # generic rooms stay "room"
            if k in ("garage", "bath", "laundry", "kitchen"):
                r["kind"] = k


def add_stairs(plan, path, geom_layers, gt):
    runs = dxf_stairs.detect(path, set(geom_layers))
    gt_stairs = (gt or {}).get("stairs", [])
    out = []
    for r in runs:
        cx, cy = r["centroid"]
        # a GT entry matched by centroid supplies down / direction / drop
        # and can veto a run (exterior steps we don't model)
        g = min(gt_stairs, key=lambda s: math.hypot(
            s["near"][0] - cx, s["near"][1] - cy), default=None)
        matched = g and math.hypot(g["near"][0] - cx,
                                   g["near"][1] - cy) <= (g.get("tol", 60))
        if matched and g.get("drop"):
            continue                      # GT says: not a stair (exterior)
        st = {"polygon": r["polygon"], "treads": r["treads"]}
        if matched:
            if "direction" in g:
                st["direction"] = g["direction"]
            else:
                st["direction"] = r["climb"]
            if g.get("down"):
                st["down"] = True
        else:
            st["direction"] = r["climb"]
        out.append(st)
    plan["stairs"] = out
    return len(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--floor", default="1", help="floor tag, e.g. 1 or 2")
    ap.add_argument("--wall-layers", required=True)
    ap.add_argument("--dw-layers", required=True,
                    help="combined doors+windows layer(s)")
    ap.add_argument("--rmnames-layer", default="")
    ap.add_argument("--gt", default="")
    ap.add_argument("--wall-height", type=float, default=96.0)
    args = ap.parse_args()

    dw = args.dw_layers
    plan, report = X.extract(
        args.input, args.wall_layers.split(","), dw.split(","), dw.split(","),
        tol=2.0, max_wall=14.0, units="inches")
    for w in plan["walls"]:
        w["height"] = args.wall_height

    gt = json.load(open(args.gt)) if args.gt else {}

    if args.rmnames_layer:
        labels = room_labels(args.input, args.rmnames_layer)
        assign_names(plan, labels)
        # re-run garage classification now that names set the garage kind
        named_garage = [r for r in plan["rooms"] if r.get("kind") == "garage"]
        report["garage_rooms"] = len(named_garage)

    # treads live on the doors/windows layer; the wall layer only adds noise
    n_st = add_stairs(plan, args.input, dw.split(","), gt)

    spawn = default_camera(plan, (gt.get("camera") or {}).get("near"))
    if spawn:
        plan["cameras"] = [spawn]

    # neutral source tag only - the raw DXF's title block carries the
    # owner's name/address, so it is never committed to the public repo
    plan["source"] = {"kind": "dxf", "floor": args.floor}
    plan.setdefault("warnings", [])
    with open(args.output, "w") as f:
        json.dump(plan, f, indent=1)
    print(f"wrote {args.output}: walls={len(plan['walls'])} "
          f"openings={len(plan['openings'])} rooms={len(plan['rooms'])} "
          f"stairs={n_st} "
          f"named={sum(1 for r in plan['rooms'] if r.get('name'))}")


if __name__ == "__main__":
    main()

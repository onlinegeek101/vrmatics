#!/usr/bin/env python3
"""Re-download the sample floor plan DXF (in case you don't want it in git).

Source: https://github.com/jscad/sample-files (MIT license)
  dxf/dxf-parser/floorplan.dxf

It is a real single-story residential plan ("Bishop-Overland") exported from
AutoCAD 2004 with xref-bound layers - walls drawn as parallel line pairs on
`xref-Bishop-Overland-08$0$A-WALL`, door swings as arcs and window glazing as
lines on `...$A-OPENING`, plus dimensions, notes, and fixtures as noise.
Drawing units are inches.

Usage:
    python fetch_sample.py [-o floorplan.dxf]
"""
import argparse
import urllib.request

URL = ("https://raw.githubusercontent.com/jscad/sample-files/"
       "master/dxf/dxf-parser/floorplan.dxf")


def main():
    ap = argparse.ArgumentParser(description="Download the sample DXF.")
    ap.add_argument("-o", "--output", default="floorplan.dxf")
    args = ap.parse_args()

    print(f"Fetching {URL} ...")
    with urllib.request.urlopen(URL) as resp:
        data = resp.read()
    with open(args.output, "wb") as f:
        f.write(data)
    print(f"Wrote {args.output} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()

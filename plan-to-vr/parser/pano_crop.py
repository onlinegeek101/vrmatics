#!/usr/bin/env python3
"""Crop perspective views out of a ZInD equirect pano at given yaw offsets."""
import json
import math
import sys

import numpy as np
from PIL import Image

pano_path, out_prefix = sys.argv[1], sys.argv[2]
yaws = [float(y) for y in sys.argv[3].split(",")]

W_OUT, H_OUT, HFOV = 800, 500, 90.0

img = np.asarray(Image.open(pano_path))
Hp, Wp = img.shape[:2]

half = math.tan(math.radians(HFOV / 2))
xs = np.linspace(-half, half, W_OUT)
ys = np.linspace(half * H_OUT / W_OUT, -half * H_OUT / W_OUT, H_OUT)
gx, gy = np.meshgrid(xs, ys)
gz = np.ones_like(gx)                    # forward = +z in cam frame

for yaw in yaws:
    r = math.radians(yaw)
    # rotate about vertical: positive yaw looks to the RIGHT in the pano
    rx = gx * math.cos(r) + gz * math.sin(r)
    rz = -gx * math.sin(r) + gz * math.cos(r)
    theta = np.arctan2(rx, rz)           # longitude, 0 = pano center
    norm = np.sqrt(rx * rx + gy * gy + rz * rz)
    phi = np.arcsin(gy / norm)           # latitude
    u = ((theta / (2 * math.pi)) + 0.5) * Wp
    v = (0.5 - phi / math.pi) * Hp
    ui = np.clip(u.astype(int), 0, Wp - 1)
    vi = np.clip(v.astype(int), 0, Hp - 1)
    out = img[vi, ui]
    Image.fromarray(out).save(f"{out_prefix}_yaw{int(yaw):+04d}.png")
    print(f"wrote {out_prefix}_yaw{int(yaw):+04d}.png")

"""Front/side preview of a drawlogic body (PGB1/PGB2) in PES2012's bind pose.

    python3 pgb_preview.py <body dir> <out.png>

Z-buffered triangle raster (numpy, flat texture sample per triangle centroid
UV): a check that the re-posed geometry is one connected figure with the
right textures, before a boot.
"""
import os
import struct
import sys

import numpy as np
from PIL import Image

PX = 512
VIEWS = ((0, 1, 2, 1), (2, 1, 0, -1))   # (horizontal axis, vertical axis, depth axis, horizontal sign)


def load(d):
    b = open(os.path.join(d, 'body.bin'), 'rb').read()
    magic, nv, ni, stride = struct.unpack_from('<4I', b)
    if magic == 0x32424750:
        nsub, flags = struct.unpack_from('<2I', b, 16)
        subs = [struct.unpack_from('<4I', b, 24 + 16 * k) for k in range(nsub)]
        at, isz = 24 + 16 * nsub, 4
    else:
        subs, at, isz = [(0, ni, 0, 0)], 16, 2
    v = np.frombuffer(b, np.uint8, nv * stride, at).reshape(nv, stride)
    pos = v[:, :12].copy().view(np.float32).reshape(nv, 3)
    uv = v[:, 64:72].copy().view(np.float32).reshape(nv, 2)
    idx = np.frombuffer(b, np.uint32 if isz == 4 else np.uint16, ni, at + nv * stride).astype(np.int64)
    texs = {}
    for _, _, t, _ in subs:
        p = os.path.join(d, 'body_%d.tex' % t) if magic == 0x32424750 else os.path.join(d, 'body.tex')
        tb = open(p, 'rb').read()
        _, w, h, _ = struct.unpack_from('<4I', tb)
        texs[t] = np.frombuffer(tb, np.uint8, w * h * 4, 16).reshape(h, w, 4)[:, :, [2, 1, 0]]
    return pos, uv, idx, subs, texs


def render(pos, uv, idx, subs, texs, view):
    hx, vy, dz, sg = view
    img = np.full((PX, PX, 3), 40, np.uint8)
    zb = np.full((PX, PX), -1e9)
    lo, hi = pos.min(0), pos.max(0)
    span = max(hi[hx] - lo[hx], hi[vy] - lo[vy]) * 1.05
    cx, cy = (lo[hx] + hi[hx]) / 2, (lo[vy] + hi[vy]) / 2
    def scr(p):
        return ((sg * (p[..., hx] - cx) / span + 0.5) * PX, (0.5 - (p[..., vy] - cy) / span) * PX)
    for first, count, t, _ in subs:
        tri = idx[first:first + count].reshape(-1, 3)
        tex = texs[t]
        for a, b, c in tri:
            P = pos[[a, b, c]]
            xs, ys = scr(P)
            x0, x1 = int(max(0, xs.min())), int(min(PX - 1, xs.max()))
            y0, y1 = int(max(0, ys.min())), int(min(PX - 1, ys.max()))
            if x1 < x0 or y1 < y0:
                continue
            u, v = uv[[a, b, c]].mean(0)
            col = tex[int((v % 1) * (tex.shape[0] - 1)), int((u % 1) * (tex.shape[1] - 1))]
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            d = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (xs[2] - xs[0]) * (ys[1] - ys[0])
            if abs(d) < 1e-9:
                continue
            w1 = ((gx - xs[0]) * (ys[2] - ys[0]) - (xs[2] - xs[0]) * (gy - ys[0])) / d
            w2 = ((xs[1] - xs[0]) * (gy - ys[0]) - (gx - xs[0]) * (ys[1] - ys[0])) / d
            w0 = 1 - w1 - w2
            m = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            z = w0 * P[0, dz] + w1 * P[1, dz] + w2 * P[2, dz]
            z = z * (1 if sg > 0 else -1) * (1 if view[2] == 2 else 1)
            sub = zb[y0:y1 + 1, x0:x1 + 1]
            m &= z > sub
            sub[m] = z[m]
            img[y0:y1 + 1, x0:x1 + 1][m] = col
    return img


def main(d, out):
    data = load(d)
    ims = [render(*data, view=v) for v in VIEWS]
    Image.fromarray(np.hstack(ims)).save(out)


if __name__ == '__main__':
    main(*sys.argv[1:3])

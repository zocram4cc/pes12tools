"""Offline renders of a UVStep body: numpy mirror of custom_ps.hlsl.

    python3 tools/render_uvstep.py <body dir> <out.png> [t0,t1,...]

Reads the real body.bin + body_<k>.tex (PGT1 or DDS, like pgb_preview.py)
and draws the front face of every SUB_UVSTEP submesh at the given times
(seconds), uniform and timing modes through tools/uvstep.py's mirror
(frame_at/tile_uv/timing_lookup). Not a game screenshot: it proves the
trailer, the textures and the frame math agree before a boot.
"""
import io
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'dist', 'pes2012-blender',
                                'pes2012_starter_pack'))
import uvstep  # noqa: E402
import pgb2  # noqa: E402

PX = 256


def load(d):
    from PIL import Image
    import numpy as np
    b = open(os.path.join(d, 'body.bin'), 'rb').read()
    parsed = pgb2.parse(b)
    subs = parsed['subs']
    v = np.array([(w['pos'], w['uv0'], w['uv1']) for w in parsed['verts']],
                 dtype=object)
    pos = np.array([w['pos'] for w in parsed['verts']], float)
    uv0 = np.array([w['uv0'] for w in parsed['verts']], float)
    uv1 = np.array([w['uv1'] for w in parsed['verts']], float)
    idx = np.array(parsed['idx'], np.int64)
    texs = {}
    slots = {int(s['tex']) for s in subs}
    slots |= {int(p['timing_tex']) for p in pgb2.parse_ustep(parsed['trailer']).values()
              if p['timing_tex'] != pgb2.USTP_NO_TIMING_TEX}
    for t in sorted(slots):
        tb = open(os.path.join(d, 'body_%d.tex' % t), 'rb').read()
        if tb[:4] == b'DDS ':
            texs[t] = np.asarray(Image.open(
                io.BytesIO(tb)).convert('RGBA')).astype(int)
            continue
        _m, w, h, _n = struct.unpack_from('<4I', tb)
        texs[t] = np.frombuffer(tb, np.uint8, w * h * 4, 16).reshape(
            h, w, 4)[:, :, [2, 1, 0, 3]].astype(int)
    return parsed, pos, uv0, uv1, idx, texs


def sample(tex, uv):
    """Nearest sample with wrap (the DLL forces WRAP on both stages)."""
    h, w = tex.shape[:2]
    x = int((uv[0] % 1.0) * w) % w
    y = int((uv[1] % 1.0) * h) % h
    return tex[y, x]


def render_face(parsed, pos, uv0, uv1, idx, texs, t, only=None):
    """Front orthographic view (z+) of the body's SUB_UVSTEP submeshes at t.
    only: file sub index to draw alone (None = every UVStep sub)."""
    from PIL import Image
    import numpy as np
    ustep = pgb2.parse_ustep(parsed['trailer'])
    timing_imgs = {}
    img = np.full((PX, PX, 3), 40, np.uint8)
    zb = np.full((PX, PX), -1e9)
    for si, s in enumerate(parsed['subs']):
        if only is not None and si != only:
            continue
        if not s['flags'] & pgb2.SUB_UVSTEP or si not in ustep:
            continue
        p = ustep[si]
        tri = idx[s['first']:s['first'] + s['count']].reshape(-1, 3)
        # each sub framed on its own centre: the test model's cubes stand side by side
        cx, cy = (pos[tri.ravel(), 0].min() + pos[tri.ravel(), 0].max()) / 2, (pos[tri.ravel(), 1].min() + pos[tri.ravel(), 1].max()) / 2
        tex = texs[s['tex']]
        ttex = None
        if p['use_timing'] and p['timing_tex'] != uvstep.NO_TIMING_TEX:
            if p['timing_tex'] not in timing_imgs:
                tb = texs.get(p['timing_tex'])
                timing_imgs[p['timing_tex']] = tb
            ttex = timing_imgs[p['timing_tex']]
        for a, b, c in tri:
            P = pos[[a, b, c]]
            xs = ((P[:, 0] - cx) * 0.9 / 0.55 + 0.5) * PX
            ys = (0.5 - (P[:, 1] - cy) * 0.9 / 0.55) * PX
            x0, x1 = int(max(0, xs.min())), int(min(PX - 1, xs.max()))
            y0, y1 = int(max(0, ys.min())), int(min(PX - 1, ys.max()))
            if x1 < x0 or y1 < y0:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5,
                                 np.arange(y0, y1 + 1) + 0.5)
            d = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (xs[2] - xs[0]) * (ys[1] - ys[0])
            if abs(d) < 1e-9:
                continue
            w1 = ((gx - xs[0]) * (ys[2] - ys[0]) - (xs[2] - xs[0]) * (gy - ys[0])) / d
            w2 = ((xs[1] - xs[0]) * (gy - ys[0]) - (gx - xs[0]) * (ys[1] - ys[0])) / d
            w0 = 1 - w1 - w2
            m = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            z = w0 * P[0, 2] + w1 * P[1, 2] + w2 * P[2, 2]
            for yy in range(y0, y1 + 1):
                for xx in range(x0, x1 + 1):
                    if not m[yy - y0, xx - x0] or z[yy - y0, xx - x0] <= zb[yy, xx]:
                        continue
                    bw = (w0[yy - y0, xx - x0], w1[yy - y0, xx - x0],
                          w2[yy - y0, xx - x0])
                    mu = sum(bw[k] * uv0[[a, b, c][k]] for k in range(3))
                    tu = sum(bw[k] * uv1[[a, b, c][k]] for k in range(3))
                    if p['use_timing']:
                        r, al = _timing_px(ttex, tu, t, p)
                        frame, vis = uvstep.frame_at(p, t, r, al)
                    else:
                        frame, vis = uvstep.frame_at(p, t)
                    if not vis:
                        continue
                    guv = uvstep.tile_uv(p, tuple(mu), frame)
                    col = sample(tex, guv)
                    if col[3] == 0:
                        continue
                    zb[yy, xx] = z[yy - y0, xx - x0]
                    img[yy, xx] = col[:3]
    return Image.fromarray(img)


def _timing_px(ttex, tu, t, p):
    """Nearest timing texel at the scrolled UVs (mirrors timing_lookup)."""
    h, w = ttex.shape[:2]
    ou = (t / p['sec_u']) if p['sec_u'] > 0.0 else 0.0
    x = int(((tu[0] + ou) % 1.0) * w) % w
    y = int((tu[1] % 1.0) * h) % h
    r, _g, _b, a = (int(v) for v in ttex[y, x])
    return r, a


def main(d, out, times):
    from PIL import Image
    parsed, pos, uv0, uv1, idx, texs = load(d)
    ims = [render_face(parsed, pos, uv0, uv1, idx, texs, t) for t in times]
    sheet = Image.new('RGB', (PX * len(ims), PX), (40, 40, 40))
    for k, im in enumerate(ims):
        sheet.paste(im, (k * PX, 0))
    sheet.save(out)
    print('%s: %s' % (out, ', '.join('t=%.1fs' % t for t in times)))


if __name__ == '__main__':
    times = [float(x) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 else [0.0, 1.0, 2.0, 3.0]
    main(sys.argv[1], sys.argv[2], times)

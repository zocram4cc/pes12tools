"""Build the UVStep offline test model: numbered frames on a cube.

Writes work_visual/UVStep/model/{body.bin,body_0.tex,body_1.tex}: one
SUB_UVSTEP submesh (uniform 2x2 cycle, 4s) whose base texture is the frame
grid, and one timing-mode submesh (same grid + timing strip) with TC1 = a
left/right split (u<0.5 -> frame 0's band, u>=0.5 -> frame 2's band).
Synthetic geometry only; nothing PES-derived.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _p in (('..', 'dist', 'pes2012-blender'), ('..', 'pes2012-blender')):   # repo / dist layout
    sys.path.insert(0, os.path.join(HERE, *_p, 'pes2012_starter_pack'))
import uvstep  # noqa: E402
import pgb2  # noqa: E402

VIS = os.path.join(HERE, '..', 'work_visual', 'UVStep')
OUT = os.path.join(VIS, 'model')


def cube(side=0.5):
    """8 corners, 12 triangles, per-face UVs 0..1, all on slot 0."""
    c = side / 2
    P = [(-c, -c, -c), (c, -c, -c), (c, c, -c), (-c, c, -c),
         (-c, -c, c), (c, -c, c), (c, c, c), (-c, c, c)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6),
             (0, 4, 5), (0, 5, 1), (2, 6, 7), (2, 7, 3),
             (0, 3, 7), (0, 7, 4), (1, 5, 6), (1, 6, 2)]
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0),
           (1.0, 1.0), (0.0, 1.0)]
    vert_data, tris = {}, []
    for tri in faces:
        corners = []
        for k, pi in enumerate(tri):
            key = (pi, k)
            if key not in vert_data:
                vert_data[key] = dict(
                    pos=P[pi], nrm=(0.0, 0.0, 1.0), tan=(1.0, 0.0, 0.0),
                    bin=(0.0, 1.0, 0.0), uv0=uvs[k], uv1=uvs[k],
                    infl={0: 1.0}, face={}, hand={})
            corners.append(key)
        tris.append(tuple(corners))
    return vert_data, tris


TIMING_CUBE_OFFSET_M = (0.7, 0.0, 0.0)   # beside the uniform cube, not inside it (09-10: they z-fought in game)


def timing_uvs(vert_data):
    """Timing UV map for the timing-mode cube: u<0.5 half reads frame 0's
    band, u>=0.5 half frame 2's (the strip is 4 equal bands); the cube sits
    TIMING_CUBE_OFFSET_M beside the uniform one."""
    out = {}
    for key, d in vert_data.items():
        u = 0.125 if d['uv0'][0] < 0.5 else 0.625
        c = dict(d)
        c['uv1'] = (u, 0.5)
        c['pos'] = tuple(p + o for p, o in zip(d['pos'], TIMING_CUBE_OFFSET_M))
        out[key] = c
    return out

def main():
    from PIL import Image
    grid = Image.open(os.path.join(VIS, 'grid.png')).convert('RGBA')
    timing = Image.open(os.path.join(VIS, 'timing.png')).convert('RGBA')
    os.makedirs(OUT, exist_ok=True)
    vd, tris = cube()
    vd2 = timing_uvs(vd)
    # pack_body keys verts per material table: offset the timing copy's keys
    vd2 = {('t',) + k: d for k, d in vd2.items()}
    tris2 = [tuple(('t',) + c for c in t) for t in tris]
    merged = dict(vd)
    merged.update(vd2)
    uni = uvstep.make_params(tile_u=2, tile_v=2, tiles_used=4,
                             sec_cycle=4.0)
    tch = uvstep.make_params(tile_u=2, tile_v=2, tiles_used=4,
                             use_timing=True, sec_u=4.0, timing_tex=1)
    out = pgb2.pack_body(
        {0: tris, 1: tris2}, merged,
        {0: pgb2.SUB_UVSTEP | pgb2.SUB_TWOSIDED,
         1: pgb2.SUB_UVSTEP | pgb2.SUB_TWOSIDED},
        {0: 0, 1: 0}, {0: False, 1: False}, 0, b'',
        {0: uni, 1: tch})
    open(os.path.join(OUT, 'body.bin'), 'wb').write(out)
    _write_tex(grid, os.path.join(OUT, 'body_0.tex'))
    _write_tex(timing, os.path.join(OUT, 'body_1.tex'))
    p = pgb2.parse(out)
    print('model: %d verts %d subs ustep=%s' % (
        len(p['verts']), len(p['subs']), pgb2.parse_ustep(p['trailer'])))




def _write_tex(im, path):
    w, h = im.size
    mips, cw, ch = [], w, h
    cur = im
    while True:
        r, g, b, a = cur.split()
        mips.append(Image.merge('RGBA', (b, g, r, a)).tobytes())
        if cw == 1 and ch == 1:
            break
        cw, ch = max(1, cw // 2), max(1, ch // 2)
        cur = cur.resize((cw, ch), Image.LANCZOS)
    with open(path, 'wb') as o:
        o.write(struct.pack('<4I', pgb2.TEX_MAGIC, w, h, len(mips)))
        for m in mips:
            o.write(m)


if __name__ == '__main__':
    from PIL import Image  # noqa: E402
    main()

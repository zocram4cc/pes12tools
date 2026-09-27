"""PES2014-eFootball kit textures -> PES2012 (PES2008-13 layout) kitserver kits.

    python3 pes15_kits.py <PES21 Data dir> <Kit Textures dir> <kits dir> <pes12 team id>

Writes <kits dir>/<tid>/{pa,pb,ga,gb}.tex (+ .png to look at) from the export's
u0XXXp1/p2/g1/g2.dds; drawlogic writes them into the game's kit texture.

The two layouts are joined through the bodies that wear them, not by
hand-drawn panels:

  PES2012  dt0c.img #3, block 0 = the LOD0 kit (45 KTMDL sections). The kit
           texture rides TEXCOORD1 (TEXCOORD0 is a detail map): shirt,
           collar, sleeves, shorts, socks, each a chart of the 1024x512 sheet.
  PES2021  the stock pieces that wear a u0XXXpN sheet (tools/pes21_import
           pes_base_body): bibs.fmdl mod_latest_uni_shirts (torso),
           undershirt.fmdl torso_mat (sleeves), pants_out_sub, socks_middle.
           Same UV layout as PES14-20's kit sheets.

The PES21 pieces are re-posed into PES2012's bind (fmdl_to_pes12's rigid
per-bone re-pose), then every PES2012 texel is traced to the body surface and
across to the nearest PES21 surface point of the same garment facing the same
way; that point's UV is where the texel reads from. The inverse table (PES21
UV -> PES2012 UV) re-maps the UVs of 4cc models that paint the kit sheet
(pes15_to_pes12.py), so they wear whatever kit the game has bound.

Both tables are derived from the user's own game files and cached in
kitmap/ next to this script (built once, never distributed).
"""
import glob
import math
import os
import struct
import sys

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get('PES12_VENDOR', os.path.join(HERE, 'vendor')))
import afs  # noqa: E402
import ktmdl  # noqa: E402
import fmdl_to_pes12 as F  # noqa: E402
import retarget  # noqa: E402
import FmdlFile  # noqa: E402

GAME = os.environ.get('PES12_GAME', os.path.join(HERE, 'game'))
KIT_IMG, KIT_ENTRY = os.path.join(GAME, 'img', 'dt0c.img'), 3
CACHE = os.environ.get('PES12_KITMAP', os.path.join(HERE, 'kitmap'))
PES12_W, PES12_H = 1024, 512          # kserv kit.png (kitserver manual 6.2)
PES15_GRID = 1024                     # inverse table resolution over the square PES15 sheet

# dt0c #3 block 0 sections by garment, with each stride's (normal, TEXCOORD1)
# byte offsets - the declarations grabbed from the in-match draws (26-09):
# stride 80/96: NORMAL @28, TEXCOORD1 @72; stride 72 (socks): NORMAL @20,
# TEXCOORD1 @64. Sleeves: 29-32 short (x to 0.41), 35/38-40 long (to 0.74),
# each with its own box on the sheet; 33/34/36/37 are left-arm variants whose
# UVs spread over the torso chart as well, left out.
PES12_PARTS = {
    'shirt': [6, 9, 10, 11, 12, 13, 14],   # 6 = collar, cut from the torso chart
    'sleeves': [29, 30, 31, 32, 35, 38, 39, 40],
    'shorts': [5],
    'socks': [18],
}
PES12_OFFSETS = {80: (28, 72), 96: (28, 72), 72: (20, 64)}

# PES21 garment pieces (path under the extracted Data, material or None = all)
PES21_CPK = {'common': ('dt00_x64.cpk.bak', 'common_package'),
             'shirts': ('dt32_g4.cpk', 'parts/undershirt'),
             'bibs': ('dt32_g4.cpk', 'parts/bibs'),
             'eyelash': ('dt00_x64.cpk.bak', 'face_eyelash_alp')}
# Engine-side textures 4cc models name instead of shipping (PES15 path ->
# PES21 ftex, same art): without them the eyelash cards draw grey.
ENGINE_TEXTURES = {
    'model/character/face/common/face_eyelash.dds':
        'Asset/model/character/common/sourceimages/#windx11/face_eyelash_alp.ftex',
}
PES21_PARTS = {
    'shirt': ('Asset/model/character/parts/bibs/scenes/#Win/bibs.fmdl', 'mod_latest_uni_shirts'),
    'sleeves': ('Asset/model/character/parts/undershirt/scenes/#Win/undershirt.fmdl', 'torso_mat'),
    'shorts': ('cp/**/pants_out_sub.fmdl', None),
    'socks': ('cp/**/socks_middle.fmdl', None),
}
SAMPLE_SPACING_M = 0.002    # surface sample spacing for the nearest-point search
NEAREST_K = 12              # candidates checked for a same-facing match
FILL_ITER = 24              # gutter dilation passes (keeps mips from bleeding)
KIT_SLOTS = {'p1': 'pa', 'p2': 'pb', 'g1': 'ga', 'g2': 'gb'}


# ---------- geometry ----------

def strip_tris(idx):
    t = [(idx[j], idx[j + 1], idx[j + 2]) for j in range(len(idx) - 2)]
    return np.array([x for x in t if len(set(x)) == 3], dtype=np.int64).reshape(-1, 3)


def pes12_kit():
    """{part: (P, N, UV, tris)} of the stock LOD0 kit, in PES12 bind space."""
    b = ktmdl.unwesys(afs.read(KIT_IMG, KIT_ENTRY))
    secs = ktmdl.sections(b, b.find(ktmdl.MAGIC))
    out = {}
    for part, ids in PES12_PARTS.items():
        Ps, Ns, UVs, Ts, base = [], [], [], [], 0
        for k in ids:
            s = secs[k]
            st, n = s['stride'], s['verts']
            no, uo = PES12_OFFSETS[st]
            v = np.frombuffer(b, np.uint8, n * st, s['vert_offset']).reshape(n, st)
            Ps.append(v[:, 0:12].copy().view(np.float32).reshape(n, 3))
            Ns.append(v[:, no:no + 12].copy().view(np.float32).reshape(n, 3))
            UVs.append(v[:, uo:uo + 8].copy().view(np.float32).reshape(n, 2))
            Ts.append(strip_tris(np.frombuffer(b, np.uint16, s['indices'], s['index_offset']).astype(np.int64)) + base)
            base += n
        out[part] = tuple(np.concatenate(x) for x in (Ps, Ns, UVs, Ts))
    return out


def extract_pes21(data_dir, work):
    import cpk
    import fpk
    for name, (arc, pattern) in PES21_CPK.items():
        marker = os.path.join(work, '.' + name)
        if not os.path.exists(marker):
            cpk.extract(os.path.join(data_dir, arc), work, pattern=pattern)
            open(marker, 'w').close()
    cp = os.path.join(work, 'Asset/model/character/#Win/common_package.fpk')
    if not os.path.isdir(os.path.join(work, 'cp')):
        fpk.extract(cp, os.path.join(work, 'cp'))


def engine_texture(path):
    """PES engine texture path -> PIL image from the cached PES21 extract, or None."""
    rel = ENGINE_TEXTURES.get(path or '')
    src = rel and os.path.join(CACHE, 'pes21', rel)
    if not src or not os.path.exists(src):
        return None
    import io
    import ftex
    return Image.open(io.BytesIO(ftex.to_dds(open(src, 'rb').read()))).convert('RGBA')


def repose(p, n, top, p12):
    """fmdl_to_pes12's rigid per-bone re-pose of a point and its normal."""
    pos, nrm = np.zeros(3), np.zeros(3)
    for fox, w in top:
        a = retarget.PES_RENDER_BIND[fox][0]
        q = retarget.PES_ALIGN.get(fox, (0.0, 0.0, 0.0, 1.0))
        r = retarget._q_rot(q, tuple(pi - ai for pi, ai in zip(p, a)))
        t = p12[F.FOX_TO_PES12[fox]]
        pos += w * (np.array(t) + np.array(r))
        nrm += w * np.array(retarget._q_rot(q, n))
    return pos, nrm / (np.linalg.norm(nrm) or 1.0)


def pes21_part(path, material):
    """-> (P, N, UV, tris) re-posed into PES12 bind."""
    import json
    table = json.load(open(F.BONES_JSON))
    p12 = {b['bone']: b['pos'] for b in table['bones']}
    f = FmdlFile.FmdlFile()
    f.readFile(path)
    bind = {b.name: (b.globalPosition.x, b.globalPosition.y, b.globalPosition.z) for b in f.bones}
    P, N, UV, T = [], [], [], []
    for m in f.meshes:
        if material and m.materialInstance.name != material:
            continue
        base = len(P)
        index_of = {id(v): k for k, v in enumerate(m.vertices)}
        for v in m.vertices:
            infl = {}
            for bone, w in v.boneMapping.items():
                fox = F.main_bone(bone.name, bind)
                infl[fox] = infl.get(fox, 0.0) + w
            infl = {b: w for b, w in infl.items() if w >= F.MIN_WEIGHT} or infl
            tw = sum(infl.values()) or 1.0
            p, n = repose((v.position.x, v.position.y, v.position.z), (v.normal.x, v.normal.y, v.normal.z),
                          [(b, w / tw) for b, w in infl.items()], p12)
            P.append(p)
            N.append(n)
            UV.append((v.uv[0].u, v.uv[0].v))
        T += [[base + index_of[id(fv)] for fv in fa.vertices] for fa in m.faces]
    return np.array(P), np.array(N), np.array(UV), np.array(T, dtype=np.int64)


def face_normals(P, T):
    n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    return n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)


def surface_samples(P, T, attrs, normals):
    """Area-proportional barycentric samples -> (points, attrs, normals)."""
    a = np.linalg.norm(np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]]), axis=1) / 2
    n = np.maximum(1, np.ceil(a / SAMPLE_SPACING_M ** 2)).astype(int)
    tri = np.repeat(np.arange(len(T)), n)
    r1, r2 = np.random.default_rng(0).random((2, len(tri)))
    s = np.sqrt(r1)
    w = np.stack([1 - s, s * (1 - r2), s * r2], 1)
    pts = sum(w[:, [k]] * P[T[tri, k]] for k in range(3))
    at = sum(w[:, [k]] * attrs[T[tri, k]] for k in range(3))
    return pts, at, normals[tri]


# Garments of the two games differ in cut (PES2012's shorts reach 9 cm lower,
# y 0.61 vs 0.70): each PES21 garment is scaled per axis onto the PES2012 one's
# box first, so a hem meets a hem instead of the lower shorts all reading the
# PES21 hem's last row. Percentiles, not extremes: the bibs carry a stray
# flap to z 0.22 that would skew a min/max box.
BOX_PCT = (1, 99)


def fit_box(P, target):
    lo, hi = np.percentile(P, BOX_PCT, axis=0)
    tlo, thi = np.percentile(target, BOX_PCT, axis=0)
    return (P - lo) / (hi - lo) * (thi - tlo) + tlo


def raster(UV, T, W, H, attrs):
    """Rasterize triangles in UV space -> (H, W, dims) barycentric-interpolated attrs + mask."""
    out = np.zeros((H, W, attrs.shape[1]), np.float32)
    mask = np.zeros((H, W), bool)
    for t in T:
        uv = UV[t] * [W, H]
        x0, y0 = np.floor(uv.min(0)).astype(int)
        x1, y1 = np.ceil(uv.max(0)).astype(int)
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W - 1), min(y1, H - 1)
        if x1 < x0 or y1 < y0:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = uv
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            continue
        l1 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / d
        l2 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / d
        l3 = 1 - l1 - l2
        inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if not inside.any():
            continue
        val = l1[..., None] * attrs[t[0]] + l2[..., None] * attrs[t[1]] + l3[..., None] * attrs[t[2]]
        sub = out[y0:y1 + 1, x0:x1 + 1]
        sub[inside] = val[inside]
        mask[y0:y1 + 1, x0:x1 + 1] |= inside
    return out, mask


def nearest_same_facing(tree, normals, pts, nrm):
    """Index of the nearest sample whose normal agrees with nrm (thin cloth:
    front and back of a shirt are millimetres apart)."""
    d, ii = tree.query(pts, k=NEAREST_K)
    agree = np.einsum('qkj,qj->qk', normals[ii], nrm) > 0
    pick = np.where(agree.any(1), agree.argmax(1), 0)
    return ii[np.arange(len(pts)), pick]

def dilate(grid, mask):
    """Grow valid texels into the gutters (nearest valid neighbour)."""
    for _ in range(FILL_ITER):
        if mask.all():
            break
        grown, gm = grid.copy(), mask.copy()
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            sm = np.roll(mask, (dy, dx), (0, 1))
            sg = np.roll(grid, (dy, dx), (0, 1))
            take = sm & ~gm
            grown[take] = sg[take]
            gm |= take
        grid, mask = grown, gm
    return grid, mask


def build_maps(data_dir):
    """-> (fwd: (H12, W12, 2) PES15 uv per PES12 texel, inv: (G, G, 2) PES12 uv per PES15 texel)."""
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, 'kitmap.npz')
    if os.path.exists(cached):
        z = np.load(cached)
        return z['fwd'], z['inv']
    if not data_dir:
        raise SystemExit('kit layout tables not built yet: pass the PES2021 Data dir (--pes21=)')
    work = os.path.join(CACHE, 'pes21')
    extract_pes21(data_dir, work)
    k12 = pes12_kit()
    fwd = np.zeros((PES12_H, PES12_W, 2), np.float32)
    fmask = np.zeros((PES12_H, PES12_W), bool)
    inv = np.zeros((PES15_GRID, PES15_GRID, 2), np.float32)
    imask = np.zeros((PES15_GRID, PES15_GRID), bool)
    for part, (rel, mat) in PES21_PARTS.items():
        P21, N21, UV21, T21 = pes21_part(glob.glob(os.path.join(work, rel), recursive=True)[0], mat)
        P12, N12, UV12, T12 = k12[part]
        P21 = fit_box(P21, P12)
        n21 = face_normals(P21, T21)
        if np.einsum('ij,ij->i', n21, N21[T21[:, 0]]).mean() < 0:
            n21 = -n21
        n12 = face_normals(P12, T12)
        if np.einsum('ij,ij->i', n12, N12[T12[:, 0]]).mean() < 0:
            n12 = -n12
        # PES12 texel -> body point -> nearest PES21 point of the same garment
        pts21, uv21, nn21 = surface_samples(P21, T21, UV21, n21)
        tree21 = cKDTree(pts21)
        vn21 = np.zeros_like(P21)
        np.add.at(vn21, T21.ravel(), np.repeat(n21, 3, 0))
        attrs = np.concatenate([P12, N12], 1)
        g, m = raster(UV12, T12, PES12_W, PES12_H, attrs)
        q = nearest_same_facing(tree21, nn21, g[m][:, :3], g[m][:, 3:])
        fwd[m] = uv21[q]
        fmask |= m
        # PES15 texel -> PES21 body point -> nearest PES12 point of the garment
        pts12, uv12, nn12 = surface_samples(P12, T12, UV12, n12)
        tree12 = cKDTree(pts12)
        g, m = raster(UV21, T21, PES15_GRID, PES15_GRID, np.concatenate([P21, vn21], 1))
        q = nearest_same_facing(tree12, nn12, g[m][:, :3], g[m][:, 3:])
        inv[m] = uv12[q]
        imask |= m
        print('%-8s pes15 texels %6d' % (part, m.sum()))
    fwd, _ = dilate(fwd, fmask)
    inv, _ = dilate(inv, imask)
    np.savez_compressed(cached, fwd=fwd, inv=inv)
    return fwd, inv


# ---------- kits ----------

def sample(img, uv):
    """Bilinear sample of an (h, w, c) array at normalized uv (..., 2)."""
    h, w = img.shape[:2]
    x = np.clip(uv[..., 0] * w - 0.5, 0, w - 1)
    y = np.clip(uv[..., 1] * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    return (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x1] * fx * (1 - fy)
            + img[y1, x0] * (1 - fx) * fy + img[y1, x1] * fx * fy)


def convert_kit(dds, fwd):
    src = np.asarray(Image.open(dds).convert('RGB'), np.float32)
    return Image.fromarray(np.clip(sample(src, fwd), 0, 255).astype(np.uint8), 'RGB')


def install(data_dir, textures, kits_dir, tid):
    """Every u0XXX{p,g}N.dds in <textures> -> <kits_dir>/<tid>/<slot>.tex, the
    PES2012-layout sheet drawlogic writes over the kit texture the game binds
    for that team (kserv's GDB cannot serve the DLC teams: selecting one
    crashes the game, 26-09)."""
    fwd, _ = build_maps(data_dir)
    if data_dir:
        extract_pes21(data_dir, os.path.join(CACHE, 'pes21'))
    out = os.path.join(kits_dir, str(tid))
    os.makedirs(out, exist_ok=True)
    done = []
    for f in sorted(os.listdir(textures)):
        stem = os.path.splitext(f)[0]
        if not f.lower().endswith('.dds') or len(stem) != 7 or stem[5:] not in KIT_SLOTS:
            continue
        slot = KIT_SLOTS[stem[5:]]
        png = os.path.join(out, slot + '.png')
        convert_kit(os.path.join(textures, f), fwd).save(png)
        F.write_tex(png, os.path.join(out, slot + '.tex'))
        done.append(slot)
    print('team %d kits: %s' % (tid, ' '.join(done)))


if __name__ == '__main__':
    install(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))

"""PES2014-eFootball kit textures -> PES2012 (PES2008-13 layout) kitserver kits.

    python3 pes15_kits.py <PES21 Data dir> <Kit Textures dir> <kits dir> <pes12 team id> [<PES2015 Data dir>]

Writes <kits dir>/<tid>/{pa,pb,ga,gb}.tex (+ .png to look at) from the export's
u0XXXp1/p2/g1/g2.dds; drawlogic writes them into the game's kit texture. The
export's own sheets go beside them untouched as <slot>_hi.dds.

The two layouts are joined through the bodies that wear them, not by
hand-drawn panels:

  PES2012  dt0c.img #3, block 0 = the LOD0 kit (45 KTMDL sections). The kit
           texture rides TEXCOORD1 (TEXCOORD0 is a detail map): shirt,
           collar, sleeves, shorts, socks, each a chart of the 1024x512 sheet.
  PES2015  the kit garments PES itself dresses a player in (dt32/dt35
           uniform): shirt_out_high + collar_001 (torso), sleeve_short_001,
           sleeve_long_001, referee_pants_001, socks_middle_high. The 4cc packs paint
           their sheets for these: PES2021's own sleeve_short covers only the
           top of the sleeve chart (no cuff, badge cut off, 30-09).

The PES2015 pieces are re-posed into PES2012's bind (fmdl_to_pes12's rigid
per-bone re-pose). The forced-UV table (fwd.bin, kitforce) traces every texel
of every kit model in dt0c.img along the stock surface normal onto them (see
build_fwd) - the upper body in PES2015's own arms-down bind instead, the
stock points moved there (arms_down); the PES2012-layout sheet (<slot>.tex) and the inverse table (PES21
UV -> PES2012 UV) trace each texel to the nearest PES2015 surface point of the
same garment facing the same way. The inverse table re-maps the UVs of 4cc
models that paint the kit sheet (pes15_to_pes12.py), so they wear whatever kit
the game has bound.

Both tables are derived from the user's own game files and cached in
kitmap/ next to this script (PES12_KITMAP; built once, never distributed).
"""
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
    'sleeves': [29, 30, 31, 32],
    'sleeves_long': [35, 38, 39, 40],
    'shorts': [5],
    'socks': [18],
}
PES12_OFFSETS = {80: (28, 72), 96: (28, 72), 72: (20, 64)}

# PES21 extracts: the engine textures below (face_eyelash) and common_package
PES21_CPK = {'common': ('dt00_x64.cpk.bak', 'common_package'),
             'eyelash': ('dt00_x64.cpk.bak', 'face_eyelash_alp')}
# Engine-side textures 4cc models name instead of shipping (PES15 path ->
# PES21 ftex, same art): without them the eyelash cards draw grey.
ENGINE_TEXTURES = {
    'model/character/face/common/face_eyelash.dds':
        'Asset/model/character/common/sourceimages/#windx11/face_eyelash_alp.ftex',
}
# PES2015 kit garments (paths under the extracted Data). PES21's bibs/undershirt
# used before put the pack's tie and badge on the stock shoulders and armpits
# (red splinters PES never shows, 30-09).
PES15_CPK = {'dt32': ('dt32.cpk', 'character1/model/character/uniform/nocloth/'),
             'dt35': ('dt35.cpk', 'character0/model/character/uniform/')}
UNI1 = 'common/character1/model/character/uniform/'
UNI0 = 'common/character0/model/character/uniform/'
PES15_PARTS = {
    'shirt': [UNI1 + 'nocloth/shirt_out_high.model', UNI0 + 'nocloth/collar_001.model'],
    'sleeves': [UNI0 + 'nocloth/sleeve_short_001.model'],
    'sleeves_long': [UNI0 + 'nocloth/sleeve_long_001.model'],
    # cloth/pants_NNN are cloth-sim models; the nocloth shorts PES ships are
    # these (the 4cc patch's nocloth pants_001 is a byte-level copy: 672 verts)
    'shorts': [UNI0 + 'nocloth/referee_pants_001.model'],
    'socks': [UNI1 + 'nocloth/socks_middle_high.model'],
}
SAMPLE_SPACING_M = 0.002    # surface sample spacing for the nearest-point search
NEAREST_K = 12              # candidates checked for a same-facing match
FILL_ITER = 24              # gutter dilation passes (keeps mips from bleeding)
KIT_SLOTS = {'p1': 'pa', 'p2': 'pb', 'g1': 'ga', 'g2': 'gb'}
HI_KIT_SUFFIX = '_hi.dds'    # full-resolution source sheet beside <slot>.tex (drawlogic HI_KIT_SUFFIX)


# ---------- geometry ----------

def strip_tris(idx):
    t = [(idx[j], idx[j + 1], idx[j + 2]) for j in range(len(idx) - 2)]
    return np.array([x for x in t if len(set(x)) == 3], dtype=np.int64).reshape(-1, 3)


def pes12_sections(ids):
    """{section: (P, N, UV, tris)} of stock LOD0 kit sections, in PES12 bind space."""
    b = ktmdl.unwesys(afs.read(KIT_IMG, KIT_ENTRY))
    secs = ktmdl.sections(b, b.find(ktmdl.MAGIC))
    out = {}
    for k in ids:
        s = secs[k]
        st, n = s['stride'], s['verts']
        no, uo = PES12_OFFSETS[st]
        v = np.frombuffer(b, np.uint8, n * st, s['vert_offset']).reshape(n, st)
        out[k] = (v[:, 0:12].copy().view(np.float32).reshape(n, 3),
                  v[:, no:no + 12].copy().view(np.float32).reshape(n, 3),
                  v[:, uo:uo + 8].copy().view(np.float32).reshape(n, 2),
                  strip_tris(np.frombuffer(b, np.uint16, s['indices'], s['index_offset']).astype(np.int64)))
    return out


def pes12_kit():
    """{part: (P, N, UV, tris)} of the stock LOD0 kit, in PES12 bind space."""
    out = {}
    for part, ids in PES12_PARTS.items():
        got = pes12_sections(ids)
        base = np.cumsum([0] + [len(got[k][0]) for k in ids])[:-1]
        out[part] = tuple(np.concatenate([got[k][c] for k in ids]) for c in range(3)) \
            + (np.concatenate([got[k][3] + b for k, b in zip(ids, base)]),)
    return out


def extract_cpks(table, data_dir, work):
    """{name: (cpk, pattern)} -> files under work, once (a marker per name)."""
    sys.path.insert(0, F.VENDOR)
    import cpk
    for name, (arc, pattern) in table.items():
        marker = os.path.join(work, '.' + name)
        if not os.path.exists(marker):
            cpk.extract(os.path.join(data_dir, arc), work, pattern=pattern)
            open(marker, 'w').close()


def extract_pes21(data_dir, work):
    sys.path.insert(0, F.VENDOR)
    import fpk
    extract_cpks(PES21_CPK, data_dir, work)
    cp = os.path.join(work, 'Asset/model/character/#Win/common_package.fpk')
    if not os.path.isdir(os.path.join(work, 'cp')):
        fpk.extract(cp, os.path.join(work, 'cp'))


def engine_texture(path):
    """PES engine texture path -> PIL image from the cached PES21 extract, or None."""
    rel = ENGINE_TEXTURES.get(path or '')
    src = rel and os.path.join(CACHE, 'pes21', rel)
    if not src or not os.path.exists(src):
        return None
    sys.path.insert(0, F.VENDOR)
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


# The forced-UV trace (build_fwd) meets the upper body in PES2015's own,
# arms-down bind instead of re-posing PES2015's garments into PES2012's
# T-pose: re-posed, the shirt's chest carried arm weight (0.13 at |x| 0.07,
# 0.31 at 0.11-0.13, 0.88 at 0.21; sk_upperarm's alignment is a 45 deg
# turn), which bent the chest logos, and the shirt and sleeve, skinned apart,
# sheared the 3 stripes at the shoulder seam (owner, 02-10). The stock points
# go arms-down instead, by one smooth field: identity on the chest, the
# inverse of the upper-arm re-pose along the arm, smoothstep between by |x|.
# 0.14: the chest logos sit inside it; 0.28: the turn spread over the
# shoulder (renders 02-10: a sharp blend at 0.14-0.20 folded the stripes).
ARM_BLEND_X = (0.14, 0.28)
UPPER_BODY_PARTS = ('shirt', 'sleeves', 'sleeves_long')   # PES15_PARTS keys traced arms-down
UPPER_ARMS = (('sk_upperarm_r', -1), ('sk_upperarm_l', 1))  # Fox bone, side (sign of x)
UPPER_SECTION_KINDS = ('torso', 'collar', 'sleeves', 'sleeves_long')   # section_kind bases traced arms-down


def arm_weight(P):
    """0 on the chest .. 1 along the arms, smoothstep over ARM_BLEND_X in |x|."""
    s = np.clip((np.abs(P[:, 0]) - ARM_BLEND_X[0]) / (ARM_BLEND_X[1] - ARM_BLEND_X[0]), 0, 1)
    return s * s * (3 - 2 * s)


def arms_down(P, N):
    """Stock PES2012-bind points and normals -> PES2015's arms-down bind
    (ARM_BLEND_X): the inverse of repose for a point on sk_upperarm, faded in."""
    import json
    p12 = {b['bone']: b['pos'] for b in json.load(open(F.BONES_JSON))['bones']}
    w = arm_weight(P)[:, None]
    out_p, out_n = P.copy(), N.copy()
    for fox, side in UPPER_ARMS:
        m = np.sign(P[:, 0]) == side
        a, t = np.array(retarget.PES_RENDER_BIND[fox][0]), np.array(p12[F.FOX_TO_PES12[fox]])
        x, y, z, qw = retarget.PES_ALIGN[fox]
        u = -np.array([x, y, z])                    # the inverse rotation: conjugate
        rot = lambda v: v + 2 * np.cross(u, np.cross(u, v) + qw * v)
        full_p, full_n = a + rot(P[m] - t), rot(N[m])
        out_p[m] = (1 - w[m]) * P[m] + w[m] * full_p
        n = (1 - w[m]) * N[m] + w[m] * full_n
        out_n[m] = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)
    return out_p, out_n


def upper_fit(raw_shirt, stock_shirt):
    """The shirt's box fit (raw PES2015 shirt onto the arms-down stock shirt),
    faded out along the arms (arm_weight): fitted on the chest, the sleeves
    as authored, so every upper-body garment takes the same map and their
    seams stay closed (the full fit pulled the shoulders 3 cm in, 02-10)."""
    fit = box_map(raw_shirt, stock_shirt)
    def f(X):
        w = arm_weight(X)[:, None]
        return (1 - w) * fit(X) + w * X
    return f


def pes15_part(path, raw=False):
    """PES2015 .model garment -> (P, N, UV, tris) re-posed into PES12 bind,
    or in its own bind with raw."""
    import json
    import pes15_to_pes12 as C
    table = json.load(open(F.BONES_JSON))
    p12 = {b['bone']: b['pos'] for b in table['bones']}
    m = C.load_model(path)
    bind = {b.name: C.joint(b) for b in m.bones}
    P, N, UV, T = [], [], [], []
    for mesh in m.meshes:
        bones = mesh.boneGroup.bones if mesh.boneGroup else []
        base = len(P)
        index_of = {id(v): k for k, v in enumerate(mesh.vertices)}
        for v in mesh.vertices:
            p, n = (v.position.x, v.position.y, v.position.z), (v.normal.x, v.normal.y, v.normal.z)
            if not raw:
                infl = {}
                for bi, w in (v.boneMapping or {}).items():
                    try:     # pes15_to_pes12: stale indices past the group, unmapped helpers
                        fox = F.main_bone(bones[bi].name, bind)
                    except (IndexError, KeyError):
                        continue
                    infl[fox] = infl.get(fox, 0.0) + w
                infl = {b: w for b, w in infl.items() if w >= F.MIN_WEIGHT} or infl
                tw = sum(infl.values()) or 1.0
                p, n = repose(p, n, [(b, w / tw) for b, w in infl.items()], p12)
            P.append(p)
            N.append(n)
            UV.append((v.uv[0].u, v.uv[0].v))
        T += [[base + index_of[id(fv)] for fv in fa.vertices] for fa in mesh.faces]
    return np.array(P), np.array(N), np.array(UV), np.array(T, dtype=np.int64)


# PES2015 garments carry inner faces (collar and cuff turn-ups): a stock point
# matched onto one reads the inside of the garment. Few per garment (30-09:
# shirt 64 of 3158, sleeves 19 of 900, long sleeves 129 of 1836).
LINING_PROBE_M = 0.015    # look this far in front of a face along its normal ...
LINING_RADIUS_M = 0.010   # ... for the garment's own surface: found = the face is lining


def drop_lining(P, T, n):
    """Triangles of a garment minus those with its own shell right in front."""
    c = P[T].mean(1)
    tree = cKDTree(c)
    hit = np.array([len(x) > 0 for x in tree.query_ball_point(c + n * LINING_PROBE_M, LINING_RADIUS_M)])
    return T[~hit], n[~hit]


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
# y 0.61 vs 0.70 on PES21's pants_out_sub): each PES2015 garment is scaled per
# axis onto the PES2012 one's box first, so a hem meets a hem instead of the
# lower shorts all reading the reference hem's last row. Percentiles, not
# extremes, so a stray flap cannot skew the box.
BOX_PCT = (1, 99)
# Only where the cut differs: the re-posed PES2015 sleeves already sit within
# 1-2 cm of PES2012's, and boxing them onto the whole part (armpit to cuff)
# lifts the tube 3-4 cm at the cuff, onto the PES2015 underside (31 % of the
# stock sleeve read the grey cuff lining, 30-09).
FIT_BOX_PARTS = {'shirt', 'shorts', 'socks'}


def box_map(P, target):
    """The per-axis map of P's percentile box onto target's (fit_box)."""
    lo, hi = np.percentile(P, BOX_PCT, axis=0)
    tlo, thi = np.percentile(target, BOX_PCT, axis=0)
    return lambda Q: (Q - lo) / (hi - lo) * (thi - tlo) + tlo


def fit_box(P, target):
    return box_map(P, target)(P)


def raster_each(UV, T, W, H, attrs):
    """Per triangle in UV space -> (ys, xs, barycentric-interpolated attrs) of the texels it covers."""
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
        iy, ix = np.nonzero(inside)
        yield iy + y0, ix + x0, val[inside]


def raster(UV, T, W, H, attrs):
    """Rasterize triangles in UV space -> (H, W, dims) barycentric-interpolated attrs + mask
    (a texel two triangles cover keeps the last)."""
    out = np.zeros((H, W, attrs.shape[1]), np.float32)
    mask = np.zeros((H, W), bool)
    for ys, xs, val in raster_each(UV, T, W, H, attrs):
        out[ys, xs] = val
        mask[ys, xs] = True
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


def pes15_work(pes15):
    """The extracted PES2015 garments' folder (extracted from <pes15> once)."""
    work = os.path.join(CACHE, 'pes15')
    if not pes15 and not all(os.path.exists(os.path.join(work, '.' + n)) for n in PES15_CPK):
        raise SystemExit('kit layout tables not built yet: pass the PES2015 Data dir (--pes15=)')
    if pes15:
        extract_cpks(PES15_CPK, pes15, work)
    return work


def pes15_garment(work, part, P12, raw=False, fit=None):
    """A part's PES2015 garments re-posed into PES12 bind (or in their own
    with raw), box-fitted onto the stock part's points P12 (FIT_BOX_PARTS) or
    mapped by fit, lining dropped -> (P21, UV21, T21, n21)."""
    got = [pes15_part(os.path.join(work, rel), raw) for rel in PES15_PARTS[part]]
    base = np.cumsum([0] + [len(g[0]) for g in got])[:-1]
    P21, N21, UV21 = (np.concatenate([g[k] for g in got]) for k in range(3))
    T21 = np.concatenate([g[3] + b for g, b in zip(got, base)])
    if fit is not None:
        P21 = fit(P21)
    elif part in FIT_BOX_PARTS:
        P21 = fit_box(P21, P12)
    n21 = face_normals(P21, T21)
    if np.einsum('ij,ij->i', n21, N21[T21[:, 0]]).mean() < 0:
        n21 = -n21
    keep = len(T21)
    T21, n21 = drop_lining(P21, T21, n21)
    print('%-8s lining faces dropped %d of %d' % (part, keep - len(T21), keep))
    return P21, UV21, T21, n21


# ---------- the forced-UV table (kitforce) ----------
#
# One PES2012 texel can stand for several body points: torso variants share a
# chart (sections 9/10 and 13/14, 6140 texels over 1 cm apart), the collar
# strip meets the long-sleeve charts, the left-arm variants spread over the
# torso chart, and dt0c.img holds 128 kit models with 63 collar shapes over
# 35 shared torso pieces. A one-value table keeps whichever section was
# rastered last and every other one reads it (02-10: 85 of the shirt's 113
# vertices more than 32 px off sat on such texels - the bib, collar and
# crest). So each texel keeps up to FWD_MAX_CAND candidates, (PES14+ uv, the
# stock point it was traced from), from every distinct kit section of every
# kit model; kitforce takes the candidate nearest the vertex it remaps.
#
# A stock point is traced by projection along its normal onto the PES2015
# garment (the first same-facing hit within FWD_RAY_M either way, its uv
# interpolated on the hit triangle), not by nearest point: the stock torso
# sits 19 mm inside the box-fitted PES2015 shirt (up to 54), and across that
# gap the nearest point under the neck was the collar garment, so the bib
# read the collar strip (02-10, trace map of the in-game torso). Each ray
# takes the outermost hit over its garments - the layer PES draws on top: a
# torso (shared by up to 48 collars) traces to the PES2015 shirt with its own
# collar_001, which fills the shirt body's 125 mm placket hole at the front
# centre and covers the sheet's shoulder boxes beside the neck (taken as the
# nearest hit, the shoulder tops showed the scraps PES hides there, 02-10); a
# stock collar to the shirt body plus the PES2015 collar its shape fits best
# (mean distance, all 105 PES2015 collars in the shirt's box fit).
# Candidates closer than this are one surface: normal projection maps two
# near-parallel surfaces a centimetre apart onto practically the same PES2015
# point, and the 128 kit models' body variants sit 5-20 mm apart (at 5 mm 3.7 M
# candidates overflowed FWD_MAX_CAND, 02-10). Real conflicts are further apart:
# the shorts' legs, the collar strip vs the long sleeves, front vs back.
FWD_SAME_SURFACE_M = 0.02
FWD_MAX_CAND = 3             # candidates kept per texel (02-10: 1 for 438k texels, 2 for 45k, 3 for 156, 4 for 10)
FWD_MM_PER_M = 1000          # candidate positions are stored in mm, int16
FWD_RAY_M = 0.08             # projection reach either side of the stock surface (gap measured up to 54 mm)
FWD_RAY_PROBES = 9           # points along the ray whose nearest triangles are tested ...
FWD_RAY_K = 16               # ... this many each
FWD_RAY_CHUNK = 4096         # rays per vectorised batch
COLLAR_GLOB = UNI0 + 'nocloth/collar_'   # PES2015 collar garments: collar_001 .. collar_105
SHIRT_BODY = UNI1 + 'nocloth/shirt_out_high.model'
# PES2012's turn-ups (the shorts' hem, sleeve cuffs, shirt hems, collars)
# are a second, inward-facing layer whose vertices repeat the outer shell's
# position AND uv: the texel is the outer layer's too. Traced along its own
# inward normal the lining reads PES14+ lining or hem band, and whichever
# layer rastered first claimed the shared texels - on the right leg the
# lining, so the outer back of the leg read the lavender hem band (owner,
# 02-10; 1122 such twins over the 9 shorts sections, 5852 in torso ones).
TWIN_POS_M, TWIN_UV = 0.001, 1e-4         # coincident and the same texel
LINING_MIN_CORNERS = 2                    # inner-twin corners that make a stock triangle lining


def inner_twins(P, N, UV, T):
    """bool per vertex: the inward member of each coincident, same-uv,
    opposite-normal vertex pair of a stock section (a turn-up's lining).
    Garments are convex across a vertex's own edges: along each edge an
    outward normal turns with the edge (sum (n_j - n_i).(p_j - p_i) > 0),
    the lining's inward one against it. Neighbourhood centroids misread the
    crotch, where both legs pull it between them."""
    pairs = np.array(sorted(cKDTree(P).query_pairs(TWIN_POS_M)), np.int64).reshape(-1, 2)
    inner = np.zeros(len(P), bool)
    if not len(pairs):
        return inner
    a, b = pairs[:, 0], pairs[:, 1]
    twin = (np.abs(UV[a] - UV[b]).max(1) < TWIN_UV) & ((N[a] * N[b]).sum(1) < 0)
    a, b = a[twin], b[twin]
    e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    turn = ((N[e[:, 1]] - N[e[:, 0]]) * (P[e[:, 1]] - P[e[:, 0]])).sum(1)
    convex = np.zeros(len(P))
    np.add.at(convex, e[:, 0], turn)
    np.add.at(convex, e[:, 1], turn)
    inner[np.where(convex[a] > convex[b], b, a)] = True
    return inner



FWD_BIN = os.path.join(CACHE, 'fwd.bin')   # drawlogic's copy of fwd (kitforce)
FWD_MAGIC = 0x32445746                    # 'FWD2': u32 magic, w, h, k; then h x w x k FWD_REC
FWD_REC = np.dtype([('u', '<f4'), ('v', '<f4'), ('x', '<i2'), ('y', '<i2'), ('z', '<i2'), ('ok', '<i2')])


KIT_MODEL_MIN_SECTIONS = 30   # a kit model's first block has 34-46 sections; other dt0c models far fewer
DEDUP_POS_PER_M, DEDUP_UV = 1e4, 1e5   # sections equal to 0.1 mm and 1e-5 uv are one section


def kit_sections():
    """Every distinct kit section (strides of PES12_OFFSETS) of every block of
    every kit model in dt0c.img -> [(P, N, UV, tris, stride)], deduplicated by
    position and uv (variants differ in normals, weights or bytes alone)."""
    import hashlib
    seen, out = set(), []
    for e in range(len(afs.entries(KIT_IMG))):
        try:
            b = ktmdl.unwesys(afs.read(KIT_IMG, e))
        except Exception:     # not a WESYS model entry
            continue
        at = b.find(ktmdl.MAGIC)
        if at < 0 or len(ktmdl.sections(b, at)) < KIT_MODEL_MIN_SECTIONS:
            continue
        while at >= 0:
            for s in ktmdl.sections(b, at):
                st, n = s['stride'], s['verts']
                if st not in PES12_OFFSETS:
                    continue
                no, uo = PES12_OFFSETS[st]
                v = np.frombuffer(b, np.uint8, n * st, s['vert_offset']).reshape(n, st)
                P = v[:, 0:12].copy().view(np.float32).reshape(n, 3)
                UV = v[:, uo:uo + 8].copy().view(np.float32).reshape(n, 2)
                if not (np.isfinite(P).all() and np.isfinite(UV).all()):
                    continue
                key = hashlib.md5(np.round(P * DEDUP_POS_PER_M).astype(np.int32).tobytes()
                                  + np.round(UV * DEDUP_UV).astype(np.int32).tobytes()).digest()
                if key in seen:
                    continue
                seen.add(key)
                T = strip_tris(np.frombuffer(b, np.uint16, s['indices'], s['index_offset']).astype(np.int64))
                out.append((P, v[:, no:no + 12].copy().view(np.float32).reshape(n, 3), UV, T, st))
            at = b.find(ktmdl.MAGIC, at + 1)
    return out


# Section kinds by where the section sits on the stock body (PES2012 bind, m)
SOCKS_TOP_Y = 0.6           # socks end below the knee (y 0.06..0.51)
SHORTS_TOP_Y = 1.1          # shorts y 0.61..1.07
NECK_BOTTOM_Y = 1.35        # collars and neck pieces start at y 1.38..1.46
NECK_HALF_X = 0.25          # ... within |x| 0.10
LONG_SLEEVE_X = 0.5         # short sleeves reach |x| 0.41, long ones 0.74
TORSO_CHART_U = 0.6         # the PES2012 torso chart is u < 0.6; sleeve charts are u > 0.62


def section_kind(P, UV, stride):
    """-> 'socks' | 'shorts' | 'collar' | 'torso' | 'sleeves' | 'sleeves_long', plus
    '+torso' for arm pieces whose UVs also cover the torso chart."""
    y0, y1, ax = P[:, 1].min(), P[:, 1].max(), np.abs(P[:, 0]).max()
    if y1 < SOCKS_TOP_Y:
        return 'socks'
    if y1 < SHORTS_TOP_Y and stride != 96:
        return 'shorts'
    if y0 > NECK_BOTTOM_Y and ax < NECK_HALF_X:
        return 'collar'
    if ax < NECK_HALF_X:
        return 'torso'
    arm = 'sleeves_long' if ax > LONG_SLEEVE_X else 'sleeves'
    return arm + '+torso' if UV[:, 0].min() < TORSO_CHART_U else arm


def garment_mesh(P21, T21, UV21, n21):
    """A PES2015 garment ready for project(): (P, T, UV, face normals, centroid tree)."""
    return P21, T21, UV21, n21, cKDTree(P21[T21].mean(1))


def project(mesh, pts, nrm):
    """PES14+ uv where each stock point's outward ray (direction nrm) meets the
    garment (Moller-Trumbore, same-facing hits within FWD_RAY_M either way) at
    its outermost hit - the layer PES draws on top, e.g. a collar over the
    shirt body -> (uv, hit, signed t of the hit; -inf where none)."""
    P, T, UV, fn, tree = mesh
    nrm = nrm / (np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12)
    uv, hit, dist = np.zeros((len(pts), 2)), np.zeros(len(pts), bool), np.full(len(pts), -np.inf)
    steps = np.linspace(-FWD_RAY_M, FWD_RAY_M, FWD_RAY_PROBES)
    for s in range(0, len(pts), FWD_RAY_CHUNK):
        p, d = pts[s:s + FWD_RAY_CHUNK], nrm[s:s + FWD_RAY_CHUNK]
        c = np.concatenate([tree.query(p + d * t, k=FWD_RAY_K)[1] for t in steps], 1)
        a = P[T[c, 0]]
        e1, e2 = P[T[c, 1]] - a, P[T[c, 2]] - a
        dd = d[:, None, :]
        pv = np.cross(dd, e2)
        det = (e1 * pv).sum(-1)
        inv = np.where(np.abs(det) > 1e-12, 1 / np.where(det == 0, 1, det), 0)
        tv = p[:, None, :] - a
        u = (tv * pv).sum(-1) * inv
        qv = np.cross(tv, e1)
        v = (dd * qv).sum(-1) * inv
        t = (e2 * qv).sum(-1) * inv
        ok = (inv != 0) & (u >= 0) & (v >= 0) & (u + v <= 1) & (np.abs(t) <= FWD_RAY_M) & ((fn[c] * dd).sum(-1) > 0)
        j = np.where(ok, t, -np.inf).argmax(1)
        i = np.arange(len(p))
        h = ok[i, j]
        cj, uj, vj = c[i, j], u[i, j], v[i, j]
        uv[s:s + FWD_RAY_CHUNK] = ((1 - uj - vj)[:, None] * UV[T[cj, 0]] + uj[:, None] * UV[T[cj, 1]] + vj[:, None] * UV[T[cj, 2]])
        hit[s:s + FWD_RAY_CHUNK] = h
        dist[s:s + FWD_RAY_CHUNK] = np.where(h, t[i, j], -np.inf)
    return uv, hit, dist


# Torso rays keep the normal's horizontal direction but not its tilt: along
# the full normal a chest point's PES2015 height depended on how its normal
# tilts with the chest's curvature, which differs across x, so the chest logos
# (adidas, crest) leaned towards the centre (owner, 02-10). Rays fanned from
# the body axis were worse: the PES2015 shirt front sits 5 cm behind the stock
# chest (z 0.07 vs 0.12), so they squeezed the chest onto the middle of the
# front and magnified the art. The ray keeps the normal's vertical part cubed:
# 0.2 on the chest -> 0.008, 0.9 on a shoulder top -> 0.73, continuous.
RAY_VERTICAL_POWER = 3


def torso_rays(nrm):
    """Ray directions for torso points: the normal's horizontal direction,
    with its vertical part raised to RAY_VERTICAL_POWER."""
    n = nrm / (np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12)
    r = n * [1, 0, 1]
    r /= np.linalg.norm(r, axis=1, keepdims=True) + 1e-12
    c = n[:, 1] ** RAY_VERTICAL_POWER
    d = r * np.sqrt(1 - c ** 2)[:, None]
    d[:, 1] = c
    return d


def pes15_collars(work, fit):
    """Every PES2015 collar garment in its own bind, mapped as the shirt (fit)
    -> [(name, garment mesh, surface sample tree)]."""
    folder = os.path.join(work, os.path.dirname(COLLAR_GLOB))
    out = []
    for f in sorted(os.listdir(folder)):
        if not f.startswith(os.path.basename(COLLAR_GLOB)) or not f.endswith('.model'):
            continue
        P, N, UV, T = pes15_part(os.path.join(folder, f), raw=True)
        P = fit(P)
        n = face_normals(P, T)
        if np.einsum('ij,ij->i', n, N[T[:, 0]]).mean() < 0:
            n = -n
        T, n = drop_lining(P, T, n)
        out.append((f, garment_mesh(P, T, UV, n), cKDTree(surface_samples(P, T, UV, n)[0])))
    return out


def build_fwd(pes15=None):
    """-> (H12, W12, FWD_MAX_CAND) FWD_REC: per PES2012 texel, the PES14+ uv
    of each distinct stock surface point that uses it."""
    cached = os.path.join(CACHE, 'fwd2.npz')
    if os.path.exists(cached):
        return np.load(cached)['rec']
    work = pes15_work(pes15)
    k12 = pes12_kit()
    meshes, clouds = {}, {}
    # The upper body is traced in PES2015's own bind, the stock points moved
    # arms-down (arms_down); every upper-body garment takes the shirt's fit
    # (upper_fit). Shorts and socks: re-posed and box-fitted.
    raw = [pes15_part(os.path.join(work, rel), raw=True) for rel in PES15_PARTS['shirt']]
    ufit = upper_fit(np.concatenate([r[0] for r in raw]), arms_down(*k12['shirt'][:2])[0])
    for part in PES15_PARTS:                  # 'shirt' = body + collar_001, as PES2015 dresses it
        upper = part in UPPER_BODY_PARTS
        P21, UV21, T21, n21 = pes15_garment(work, part, k12[part][0], raw=upper, fit=ufit if upper else None)
        meshes[part] = garment_mesh(P21, T21, UV21, n21)
        clouds[part] = surface_samples(P21, T21, UV21, n21)
    # the shirt body alone: collars trace to it plus their own collar
    P, N, UV, T = pes15_part(os.path.join(work, SHIRT_BODY), raw=True)
    P = ufit(P)
    n = face_normals(P, T)
    if np.einsum('ij,ij->i', n, N[T[:, 0]]).mean() < 0:
        n = -n
    T, n = drop_lining(P, T, n)
    meshes['body'], clouds['body'] = garment_mesh(P, T, UV, n), surface_samples(P, T, UV, n)
    collars = pes15_collars(work, ufit)
    print('fwd: %d PES2015 collars' % len(collars))
    rec = np.zeros((PES12_H, PES12_W, FWD_MAX_CAND), FWD_REC)
    pos = np.full((PES12_H, PES12_W, FWD_MAX_CAND, 3), np.inf, np.float32)
    cnt = np.zeros((PES12_H, PES12_W), np.int32)
    dropped, twins, kinds, stats = 0, 0, {}, {}
    sections = kit_sections()
    for P12, N12, UV12, T12, stride in sections:
        kind = section_kind(P12, UV12, stride)
        kinds[kind] = kinds.get(kind, 0) + 1
        # torso: the PES2015 shirt with its own collar (the shirt body has a
        # 125 mm hole at the front centre, y 1.36-1.49, where that collar's
        # placket sits); collar: the shirt body plus the PES2015 collar its
        # shape fits best; sleeves: their sleeve plus the shirt - PES2012's
        # shirt/sleeve seam is not PES2015's, and traced to the sleeve alone a
        # sleeve point by the shoulder read the sleeve's stripes where PES2015
        # has the shirt's, breaking the shoulder stripes at PES2012's seam
        # (owner, 02-10). Each ray takes the outermost garment hit along it.
        if kind == 'collar':
            fit = [np.mean(tree.query(arms_down(P12, N12)[0])[0]) for _f, _m, tree in collars]
            m = collars[int(np.argmin(fit))][1]
            parts, near = [meshes['body'], m], [clouds['body'], surface_samples(*m[:4])]
        else:
            base = kind.split('+')[0]
            names = ['shirt'] if base == 'torso' else [base, 'shirt'] if base.startswith('sleeves') else [base]
            parts, near = [meshes[k] for k in names], [clouds[k] for k in names]
        # every triangle's own texels (sections overlap themselves: the shorts'
        # legs, the collar's two layers); only a point no candidate within
        # FWD_SAME_SURFACE_M stands for yet becomes one, and only those are traced.
        # A turn-up's lining claims none: its texels are its outer twin's
        # (inner_twins). A lining triangle has most corners twinned: the fan at
        # the shorts lining's top edge meets at an apex whose twin test is a tie.
        new = []
        lining = inner_twins(P12, N12, UV12, T12)[T12].sum(1) >= LINING_MIN_CORNERS
        twins += int(lining.sum())
        for ys, xs, v in raster_each(UV12, T12[~lining], PES12_W, PES12_H, np.concatenate([P12, N12], 1)):
            p = v[:, :3]
            seen = (np.linalg.norm(pos[ys, xs] - p[:, None], axis=2) < FWD_SAME_SURFACE_M).any(1)
            room = cnt[ys, xs] < FWD_MAX_CAND
            dropped += int((~seen & ~room).sum())
            add = ~seen & room
            ys, xs, v = ys[add], xs[add], v[add]
            slot = cnt[ys, xs]
            pos[ys, xs, slot] = v[:, :3]
            cnt[ys, xs] += 1
            new.append((ys, xs, slot, v))
        if not new or not sum(len(x[0]) for x in new):
            continue
        ys, xs, slot, allv = (np.concatenate([x[i] for x in new]) for i in range(4))
        p12, n12 = allv[:, :3], allv[:, 3:]
        if kind.split('+')[0] in UPPER_SECTION_KINDS:
            p12, n12 = arms_down(p12, n12)    # traced in PES2015's bind; stored at the stock point
        rays = torso_rays(n12) if kind == 'torso' else n12
        best, bdist = np.zeros((len(allv), 2)), np.full(len(allv), -np.inf)
        for g in parts:                       # several garments: the outermost hit along the ray
            uv, _h, dist = project(g, p12, rays)
            take = dist > bdist
            best[take], bdist[take] = uv[take], dist[take]
        bhit = np.isfinite(bdist)
        if (~bhit).any():                     # no garment along the normal: nearest same-facing point
            pts21, uv21, nn21 = (np.concatenate([c[i] for c in near]) for i in range(3))
            miss = ~bhit
            best[miss] = uv21[nearest_same_facing(cKDTree(pts21), nn21, p12[miss], n12[miss])]
        st = stats.setdefault(kind, [0, 0]); st[0] += len(allv); st[1] += int(bhit.sum())
        r = rec[ys, xs, slot]
        r['u'], r['v'] = best[:, 0], best[:, 1]
        r['x'], r['y'], r['z'] = (np.round(allv[:, c] * FWD_MM_PER_M).astype(np.int16) for c in range(3))
        r['ok'] = 1
        rec[ys, xs, slot] = r
    print('fwd: %d sections %s; %d turn-up lining triangles left to their outer twins' % (len(sections), kinds, twins))
    print('fwd: new candidates traced / hit a garment along the normal: %s'
          % {k: '%d/%d' % (v[0], v[1]) for k, v in stats.items()})
    print('fwd: candidates per texel %s, %d dropped over %d' % (np.bincount(cnt.ravel()).tolist(), dropped, FWD_MAX_CAND))
    # gutters: copy the nearest traced texel's candidates (kitforce rejects them
    # by position when they belong to another part of the body)
    have = cnt > 0
    for _ in range(FILL_ITER):
        if have.all():
            break
        grown, gh = rec.copy(), have.copy()
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            sh, sr = np.roll(have, (dy, dx), (0, 1)), np.roll(rec, (dy, dx), (0, 1))
            take = sh & ~gh
            grown[take] = sr[take]
            gh |= take
        rec, have = grown, gh
    np.savez_compressed(cached, rec=rec)
    return rec


def write_fwd(rec):
    """fwd -> <PES12_KITMAP>/fwd.bin (drawlogic reads 4cc-players/kitmap/fwd.bin): drawlogic re-maps the stock kit's
    UVs onto the PES14+ sheet per vertex with it (forced kit UVs)."""
    h, w, k = rec.shape
    with open(FWD_BIN, 'wb') as o:
        o.write(struct.pack('<4I', FWD_MAGIC, w, h, k))
        o.write(np.ascontiguousarray(rec).tobytes())


def build_maps(pes15=None):
    """-> (fwd: (H12, W12, 2) PES15 uv per PES12 texel, inv: (G, G, 2) PES12 uv per PES15 texel).
    pes15: the PES2015 Data dir, needed only while the cache is not built."""
    return _maps(pes15)[:2]


def _maps(pes15):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, 'kitmap.npz')
    if os.path.exists(cached):
        z = np.load(cached)
        if 'mask' in z.files:
            return z['fwd'], z['inv'], z['mask']
    work = os.path.join(CACHE, 'pes15')
    if not pes15 and not all(os.path.exists(os.path.join(work, '.' + n)) for n in PES15_CPK):
        raise SystemExit('kit layout tables not built yet: pass the PES2015 Data dir (--pes15=)')
    if pes15:
        extract_cpks(PES15_CPK, pes15, work)
    k12 = pes12_kit()
    fwd = np.zeros((PES12_H, PES12_W, 2), np.float32)
    fmask = np.zeros((PES12_H, PES12_W), bool)
    inv = np.zeros((PES15_GRID, PES15_GRID, 2), np.float32)
    imask = np.zeros((PES15_GRID, PES15_GRID), bool)
    for part in PES15_PARTS:
        P12, N12, UV12, T12 = k12[part]
        P21, UV21, T21, n21 = pes15_garment(work, part, P12)
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
    np.savez_compressed(cached, fwd=fwd, inv=inv, mask=imask)
    return fwd, inv, imask


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


DDS_MAGIC = b'DDS '


def install(data_dir, textures, kits_dir, tid, pes15=None):
    """Every u0XXX{p,g}N.dds in <textures> -> <kits_dir>/<tid>/<slot>.tex, the
    PES2012-layout sheet drawlogic writes over the kit texture the game binds
    for that team (kserv's GDB cannot serve the DLC teams: selecting one
    crashes the game, 26-09)."""
    fwd, _ = build_maps(pes15)
    write_fwd(build_fwd(pes15))
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
        src = os.path.join(textures, f)
        if open(src, 'rb').read(len(DDS_MAGIC)) != DDS_MAGIC:
            # /u/'s u0000g1.dds is no DDS at all (packs ship broken files)
            print('skipped %s: not a DDS' % f)
            continue
        png = os.path.join(out, slot + '.png')
        convert_kit(src, fwd).save(png)
        F.write_tex(png, os.path.join(out, slot + '.tex'))
        # The pack's own sheet, untouched (write_tex copies a power-of-two
        # DXT DDS byte for byte): drawlogic draws the stock kit with forced
        # PES14+ UVs and custom models with the pack's own, both on it. A
        # gutter fill here (30-09 to 02-10) smeared /g/'s circuit over the
        # neck, sleeve and shorts edges the stock kit shows.
        from pes15_to_pes12 import write_tex as write_dds
        write_dds(src, os.path.join(out, slot + HI_KIT_SUFFIX))
        done.append(slot)
    print('team %d kits: %s' % (tid, ' '.join(done)))


if __name__ == '__main__':
    install(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] if len(sys.argv) > 5 else None)

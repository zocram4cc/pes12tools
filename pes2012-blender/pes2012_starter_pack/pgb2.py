"""PGB2 custom body codec: drawlogic.dll's per-player body format.

Pure python, no Blender. Provenance: tools/pes15_to_pes12.py docstring and
(header magic/nv/ni/stride/nsub/keep, submesh table,
80-byte vertices, u32 indices, body_<k>.tex PGT1 textures).

Layout:
  u32 magic 'PGB2', nv, ni, stride(80), nsub, keep (stock pieces drawn, PIECES bits)
  nsub x (u32 firstIndex, u32 indexCount, u32 texture, u32 subFlags)
  nv x 80-byte vertices, ni x u32 indices

Vertex (struct '<3f3f4B3f3f3f2f2f', tools/fmdl_to_pes12.py):
  POSITION f3 | BLENDWEIGHT f3 (n-1 explicit) | BLENDINDICES ubyte4 |
  NORMAL f3 | BINORMAL f3 | TANGENT f3 | TEXCOORD0 f2 | TEXCOORD1 f2

Weight convention (tools/fmdl_to_pes12.py skin_pack, PES2012's skin VS):
blend index 0 takes the remainder 1 - (w0 + w1 + w2), index k + 1 takes
w[k]; the heaviest influence sits in index 0 and padding repeats it.
"""
import struct

MAGIC = 0x32424750  # 'PGB2'
STRIDE = 80
TEX_MAGIC = 0x31544750  # 'PGT1'
VERT_FMT = '<3f3f4B3f3f3f2f2f'

# stock pieces a body can keep drawn: drawlogic PIECE_NAMES, same bit order
PIECES = ('shorts', 'shirt', 'sleeves', 'socks', 'neck', 'gloves', 'head', 'boots', 'other', 'skin', 'hands')

SUB_ALPHATEST, SUB_BLEND, SUB_TWOSIDED, SUB_NOZWRITE = 1, 2, 4, 8
SUB_KIT, SUB_OUTLINE, SUB_FACE = 16, 32, 64
# drawlogic's own pixel shaders and hair pass (dllprobe/drawlogic.cpp SUB_*)
SUB_SHADELESS, SUB_TOON, SUB_HAIR = 1 << 16, 1 << 17, 1 << 18
SUB_REF_SHIFT = 8  # bits 8-15: alpha-test ref (pass alpha > ref)

MAX_INFLUENCES = 4
MIN_WEIGHT = 1.5 / 255  # one 8-bit step is 4cc authoring noise (tools/fmdl_to_pes12.py)

# BLENDINDICES palette slots in order. Slots 0-18 invert probe/
# body349b2_bones.json's bone->slot map through tools/fmdl_to_pes12.py's
# FOX_TO_PES12 names; slots 19/20 are the finger bones (FINGER_SLOT there,
# dllprobe/kitmap.h CU_SRC_*).
SLOT_BONES = (
    'sk_thigh_r', 'sk_leg_r', 'dsk_hip', 'sk_thigh_l', 'sk_leg_l',
    'sk_foot_l', 'sk_foot_r', 'sk_hand_r', 'sk_forearm_r', 'sk_upperarm_r',
    'sk_shoulder_r', 'sk_hand_l', 'sk_forearm_l', 'sk_upperarm_l',
    'sk_shoulder_l', 'sk_belly', 'sk_chest', 'sk_neck', 'sk_head',
    'fingers_l', 'fingers_r',
)
SLOT_OF = {name: i for i, name in enumerate(SLOT_BONES)}

# PES2012 head joint: probe/body349b2_bones.json bone 14. SUB_FACE vertices
# are stored head-local (position minus this); import adds it back.
HEAD_POS = (0.0, 1.65, -0.005)

# SUB_FACE verts index the 27-slot face palette (tools/pes12_rig.py
# face_rig: stock face BIN's face-packet bonePalette), not SLOT_BONES.
# Groups are named face_00..face_26 in palette order.
FACE_GROUP_FMT = 'face_%02d'
N_FACE_SLOTS = 27


def influences_to_slots(infl):
    """[(slot, weight)] -> (4 slots, 3 explicit f32 weights). The shader
    gives slot 0 the remainder and slot k + 1 explicit weight k, so the
    heaviest influence goes first and padding repeats it."""
    top = sorted(infl.items(), key=lambda kv: -kv[1])[:MAX_INFLUENCES]
    tw = sum(w for _, w in top) or 1.0
    top = [(s, w / tw) for s, w in top]
    slots = [s for s, _ in top]
    slots += [slots[0]] * (MAX_INFLUENCES - len(slots))
    ws = [w for _, w in top][1:] + [0.0] * MAX_INFLUENCES
    return tuple(slots[:MAX_INFLUENCES]), tuple(ws[:3])


def slots_to_influences(slots, explicit):
    """Inverse: slot 0 takes the remainder (1 - sum(explicit))."""
    full = (1.0 - sum(explicit),) + tuple(explicit)
    out = {}
    for s, w in zip(slots, full):
        out[s] = out.get(s, 0.0) + w
    return {s: w for s, w in out.items() if w > 0.0}


def pack_vertex(pos, infl, nrm, bin_, tan, uv0, uv1=None):
    slots, ws = influences_to_slots(dict(infl))
    return struct.pack(VERT_FMT, *pos, *ws, *slots, *nrm, *bin_, *tan,
                       *uv0, *(uv1 or uv0))


def unpack_vertex(buf, off=0):
    f = struct.unpack_from(VERT_FMT, buf, off)
    return dict(pos=tuple(f[0:3]), slots=tuple(f[6:10]),
                weights=tuple(f[3:6]), nrm=tuple(f[10:13]),
                bin=tuple(f[13:16]), tan=tuple(f[16:19]),
                uv0=tuple(f[19:21]), uv1=tuple(f[21:23]))


def parse(data):
    """bytes -> dict(keep, subs, verts, idx); verts are unpack_vertex dicts."""
    magic, nv, ni, stride, nsub, keep = struct.unpack_from('<6I', data, 0)
    if magic != MAGIC:
        raise ValueError('not a PGB2 body')
    if stride != STRIDE:
        raise ValueError('stride %d != %d' % (stride, STRIDE))
    subs = [struct.unpack_from('<4I', data, 24 + 16 * k) for k in range(nsub)]
    vbase = 24 + 16 * nsub
    verts = [unpack_vertex(data, vbase + STRIDE * k) for k in range(nv)]
    ibase = vbase + STRIDE * nv
    idx = list(struct.unpack_from('<%dI' % ni, data, ibase))
    return dict(keep=keep, subs=[dict(first=s[0], count=s[1], tex=s[2],
                                      flags=s[3]) for s in subs],
                verts=verts, idx=idx)


def build(keep, verts, idx, subs):
    """verts: unpack_vertex-style dicts (pos/slots/weights/...); returns bytes."""
    nv, ni = len(verts), len(idx)
    if nv > 0xFFFFFFFF or ni > 0xFFFFFFFF:
        raise ValueError('body too large')
    out = bytearray(struct.pack('<6I', MAGIC, nv, ni, STRIDE, len(subs), keep))
    for s in subs:
        out += struct.pack('<4I', s['first'], s['count'], s['tex'], s['flags'])
    for v in verts:
        out += struct.pack(VERT_FMT, *v['pos'], *v['weights'], *v['slots'],
                           *v['nrm'], *v['bin'], *v['tan'], *v['uv0'], *v['uv1'])
    out += struct.pack('<%dI' % ni, *idx)
    return bytes(out)


def pack_body(tris_by_mat, vert_data, mat_flags, mat_tex, mat_face, keep):
    """Pure assembly: per-vert data + triangle soup -> PGB2 bytes.
    Blender's export_body is a thin adapter reading mesh state into these
    plain dicts; tests/ drives this directly against p272101.

    vert_data: {vi: dict(pos, nrm, tan, bin, uv0, uv1,
                         infl={slot: w} body slots, face={slot: w} face slots)}.
    tris_by_mat: {material_index: [(a, b, c)]}. One submesh per used
    material slot, blended ones last (tools/pes15_to_pes12.py).
    mat_face: {material_index: bool} SUB_FACE submeshes: positions stored
    head-local (minus HEAD_POS) and weights on the face palette.
    """
    order = sorted(tris_by_mat,
                   key=lambda mi: bool(mat_flags.get(mi, 0) & SUB_BLEND))
    verts, idx, subs = [], [], []
    for mi in order:
        flags = mat_flags.get(mi, 0)
        face = mat_face.get(mi, bool(flags & SUB_FACE))
        first = len(idx)
        for tri in tris_by_mat[mi]:
            for vi in tri:
                d = vert_data[vi]
                infl = d['face'] if face else d['infl']
                slots, ws = influences_to_slots(infl or {0: 1.0})
                p = d['pos']
                if face:
                    p = (p[0] - HEAD_POS[0], p[1] - HEAD_POS[1],
                         p[2] - HEAD_POS[2])
                verts.append(dict(pos=p, slots=slots, weights=ws, nrm=d['nrm'],
                                  bin=d.get('bin', (0.0, 0.0, 1.0)),
                                  tan=d.get('tan', (1.0, 0.0, 0.0)),
                                  uv0=d['uv0'], uv1=d['uv1']))
                idx.append(len(verts) - 1)
        subs.append(dict(first=first, count=len(idx) - first,
                         tex=mat_tex.get(mi, 0), flags=flags))
    return build(keep, verts, idx, subs)


def round_trip(data):
    """Parse-compare helper: returns (parsed, rebuilt_bytes)."""
    p = parse(data)
    return p, build(p['keep'], p['verts'], p['idx'], p['subs'])


# --- PGT1 textures (body_<k>.tex): u32 'PGT1', w, h, nmips, BGRA8 mips ---

def parse_tex(data):
    magic, w, h, nmips = struct.unpack_from('<4I', data, 0)
    if magic != TEX_MAGIC:
        raise ValueError('not a PGT1 texture')
    mips, off = [], 16
    # mip sizes halve per level down to 1x1 (tools/fmdl_to_pes12.py write_tex)
    ww, hh = w, h
    for _ in range(nmips):
        n = ww * hh * 4
        mips.append(bytes(data[off:off + n]))
        if len(mips[-1]) != n:
            raise ValueError('truncated mip')
        off += n
        ww, hh = max(1, ww // 2), max(1, hh // 2)
    return dict(w=w, h=h, mips=mips)


def build_tex(w, h, mips):
    out = bytearray(struct.pack('<4I', TEX_MAGIC, w, h, len(mips)))
    for m in mips:
        out += m
    return bytes(out)

"""PES2015 4cc face folder (Faces/<pid> - <name>/) -> PES2012 custom body.

    python3 pes15_to_pes12.py <face folder> <kit texture.dds> <out_dir>

Reads every *.model in the folder with the4chancup/pes-model-blender's
ModelFile.py (no Blender needed), materials from face.xml -> *.mtl (PES15
layout) or materials.mtl (PES16+ faceneck layout), and writes the
drawlogic.dll PGB2 format:

  u32 'PGB2', nv, ni, stride(80), nsub, flags
  nsub x (u32 firstIndex, u32 indexCount, u32 texture, u32 subFlags)
  nv x 80-byte vertices (fmdl_to_pes12.py's layout), ni x u32 indices
  body_<texture>.tex per texture (PGT1, see fmdl_to_pes12.write_tex)

flags: which stock parts the model replaces (drawlogic MODE_*): 0 BODY all of
them, 1 HEAD the head only (face-slot players: head, hair and accessories over
the stock body), 2 KIT all but shirt/shorts/socks/boots, 3 BOOTS all but the
boots. <game>/kitserver/4cc-players/custom/<name>/mode (head|body|kit|boots)
overrides it.
subFlags (drawlogic SUB_*), from the material's .mtl states: bit0 alpha test
(ref = bits 8-15, pass alpha > ref), bit1 alpha blend, bit2 two-sided, bit3 no
depth write, bit4 kit slot (UVs on PES2012's kit sheet; drawlogic binds the
kit being worn), bit5 toon outline shell (drawn depth-biased), bit6 face part:
head-local vertices on the PES2012 face palette, drawn at the game's face draw
so jaw, lips, eyelids and brows animate (pes12_rig.py). Blended submeshes are written last so they blend over the rest.

Skeleton: PES15 bone matrices are inverse binds whose joints sit exactly on
PES21's render bind (sk_upperarm_l 0.195, 1.467, 0.033 in both), so the
re-pose of fmdl_to_pes12 applies unchanged.
"""
import math
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fmdl_to_pes12 as F  # noqa: E402  (bone tables, re-pose, texture writer)
import retarget  # noqa: E402  (via fmdl_to_pes12's path setup)
import pes15_kits as K  # noqa: E402  (kit layout tables)
import pes12_rig  # noqa: E402  (face rig from the user's game)

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    'pes_model_file', os.environ.get('PES12_MODEL_FILE',
                                     os.path.join(HERE, 'vendor', 'ModelFile.py')))
MF = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MF)

BODY2_MAGIC = 0x32424750        # 'PGB2'
MODE_BODY, MODE_HEAD, MODE_KIT, MODE_BOOTS = 0, 1, 2, 3
MODE_NAMES = ('body', 'head', 'kit', 'boots')
# A hidden-body model reaching the floor brings its own feet; one stopping
# above it stands in PES's boots (Stallman: y 0.07.., real boots id 1; Terry:
# y -0.17.., boots id 55 = none).
OWN_FEET_Y = 0.05
SUB_ALPHATEST, SUB_BLEND, SUB_TWOSIDED, SUB_NOZWRITE, SUB_KIT, SUB_OUTLINE, SUB_FACE = 1, 2, 4, 8, 16, 32, 64
# toon outline shells (inverted hulls) by material name: drawlogic pushes them
# back in depth so they only show past the silhouette
OUTLINE_MATERIAL = re.compile(r'outline', re.I)
SUB_REF_SHIFT = 8
# PES15's Hair shader does not take opacity from the diffuse alpha (hair_col
# alpha: median 0, 90th percentile 39 on DEVELOPERS, 27-09): drawn with its
# alphablend state the hair vanished. Drawn opaque and two-sided instead.
OPAQUE_SHADERS = ('Hair',)
SHADER_KEY = '_shader'              # parse_mtl stores the material's shader among its states
KIT_TEXTURE = '<kit slot>'     # tex_index key of the kit-slot stand-in texture
MAX_TEX_PX = 512                # textures downscaled: 22 bodies must fit a 32-bit process
WHOLE_BODY_NAMES = re.compile(r'parts_body|oral_kit|oral_nagi')
WHOLE_BODY_MIN_VERTS = 20000    # a single model this big is a body, not an accessory
SKIP_MATERIALS = re.compile(r'occlusion|antiblur|antiglow', re.I)
SKIP_FILES = re.compile(r'antiglow|antiblur', re.I)   # PES-side render tricks, no geometry to show
GREY_BGRA = (128, 128, 128, 255)
# A sampler path with no file name: how 4cc packs switch a template mesh off
# (von braun's face under his box head, Riemann Zeta's, Yukari's oral part).
NO_TEXTURE = './.dds'
HIDDEN_Y = -100.0               # 4cc packs park unused parts ~1000 m below the pitch


def load_model(path):
    r = MF.readModelFile(path, MF.ParserSettings())
    return r[0] if isinstance(r, tuple) else r


def joint(bone):
    """Inverse-bind 3x4 (rows) -> world joint position."""
    m = bone.matrix
    r = [m[0:3], m[4:7], m[8:11]]
    t = [m[3], m[7], m[11]]
    return tuple(-sum(r[k][i] * t[k] for k in range(3)) for i in range(3))


# PES2012's in-match body has one bone per hand; its own hands are modelled
# curled. A 4cc PES15 hand is authored flat on finger bones this game does not
# have, so it is bent into PES's relaxed hand before being baked onto the hand
# bone: the flexion of PES's normal.gani (mcp 15,
# pip 32, dip 20 degrees). The thumb is left as authored.
# ponytail: one fixed pose; per-player grips (fists, keepers) would need the finger bones
FINGER_CURL_DEG = {'mcp': 15.0, 'pip': 32.0, 'dip': 20.0}
FINGER_CHAIN = ('dip', 'pip', 'mcp')    # distal first, each about its own joint
FINGER_BONE = re.compile(r'skh_(index|middle|ring|pinky)_(mcp|pip|dip)_([lr])$')


def _rot_axis(v, axis, deg):
    """Rodrigues: v rotated deg about the unit axis."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    dot = sum(a * b for a, b in zip(axis, v))
    cr = (axis[1] * v[2] - axis[2] * v[1], axis[2] * v[0] - axis[0] * v[2], axis[0] * v[1] - axis[1] * v[0])
    return tuple(v[k] * c + cr[k] * s + axis[k] * dot * (1 - c) for k in range(3))


def curl_fingers(p, n, raw, joints):
    """(p, n) in PES15 space with each finger influence bent about its joints.
    Flexion axis = the anim-pose Z (fingers +-X, back of hand +Y after
    retarget.PES_ALIGN) taken back into the render pose; the left hand bends
    by -angle about it, the right by +angle, so both close palm-down."""
    tw = sum(w for _, w in raw)
    if not tw or not any(FINGER_BONE.match(name) for name, _ in raw):
        return p, n
    op, on = [0.0] * 3, [0.0] * 3
    for name, w in raw:
        q, qn = p, n
        m = FINGER_BONE.match(name)
        if m:
            finger, joint_name, side = m.groups()
            hq = retarget.PES_ALIGN.get('sk_hand_' + side, (0.0, 0.0, 0.0, 1.0))
            axis = retarget._q_rot((-hq[0], -hq[1], -hq[2], hq[3]), (0.0, 0.0, 1.0))
            sign = -1.0 if side == 'l' else 1.0
            for j in FINGER_CHAIN[FINGER_CHAIN.index(joint_name):]:
                jp = joints.get('skh_%s_%s_%s' % (finger, j, side))
                if jp is None:
                    continue
                d = _rot_axis(tuple(a - b for a, b in zip(q, jp)), axis, sign * FINGER_CURL_DEG[j])
                q = tuple(a + b for a, b in zip(jp, d))
                qn = _rot_axis(qn, axis, sign * FINGER_CURL_DEG[j])
        for k in range(3):
            op[k] += w / tw * q[k]
            on[k] += w / tw * qn[k]
    return tuple(op), tuple(on)


_kit_maps = []


def kit_maps():
    """(fwd, inv) from pes15_kits' cache; the team importer builds it first."""
    if not _kit_maps:
        path = os.path.join(K.CACHE, 'kitmap.npz')
        if not os.path.exists(path):
            raise SystemExit('no %s: run pes15_kits.py (or the team importer) first' % path)
        z = __import__('numpy').load(path)
        _kit_maps.extend((z['fwd'], z['inv']))
    return _kit_maps


def kit_fwd():
    return kit_maps()[0]


def kit_uv(u, v):
    """PES14+ kit-sheet UV -> PES2012 kit-sheet UV (nearest texel of the
    inverse table; UVs wrap like the sampler)."""
    inv = kit_maps()[1]
    g = inv.shape[0]
    x, y = int((u % 1.0) * g) % g, int((v % 1.0) * g) % g
    return float(inv[y, x, 0]), float(inv[y, x, 1])


def read_bytes(path):
    """File contents; PES's WESYS wrapper (zlib) removed - 4cc exports ship
    some face.xml/.mtl/.dds files still wrapped (72102 Use Case for Defending)."""
    import ktmdl
    return ktmdl.unwesys(open(path, 'rb').read())


def read_text(path):
    return read_bytes(path).decode('utf-8', 'replace')


def parse_mtl(path):
    """-> {material: (diffuse path or None, {state: value})}"""
    out = {}
    if not os.path.exists(path):
        return out
    root = ET.fromstring(re.sub(r'<\?xml[^>]*>', '', read_text(path)))
    for m in root.iter('material'):
        diff = None
        for s in m.iter('sampler'):
            if s.get('name') in ('DiffuseMap', 'BaseMap', 'ColorMap'):
                diff = s.get('path')
        states = {s.get('name'): s.get('value') for s in m.iter('state')}
        states[SHADER_KEY] = m.get('shader')
        out[m.get('name')] = (diff, states)
    return out


def sub_flags(states):
    """PES15 material states -> drawlogic submesh flags."""
    f = 0
    if states.get(SHADER_KEY) in OPAQUE_SHADERS:
        return SUB_TWOSIDED
    if states.get('alphatest') == '1':
        f |= SUB_ALPHATEST | (int(states.get('alpharef') or 0) << SUB_REF_SHIFT)
    if states.get('alphablend') == '1':
        f |= SUB_BLEND
    if states.get('twosided') in ('1', '2'):
        f |= SUB_TWOSIDED
    if states.get('zwrite') == '0':
        f |= SUB_NOZWRITE
    return f


def model_materials(folder):
    """-> {model file: {material: (diffuse, states)}}. face.xml names the mtl
    for the models it lists; the standard hair_high it does not list takes its
    materials from every .mtl in the folder."""
    xml = os.path.join(folder, 'face.xml')
    models = sorted(f for f in os.listdir(folder) if f.endswith('.model'))
    shared = {}
    for f in sorted(os.listdir(folder)):
        if f.endswith('.mtl'):
            try:
                shared.update(parse_mtl(os.path.join(folder, f)))
            except ET.ParseError:   # 4cc folders carry stale/binary .mtl copies no model uses
                print('skipping unparseable %s' % f)
    if os.path.exists(xml):
        text = read_text(xml)
        mapping = {}
        for m in re.finditer(r'<model[^>]*path="\./([^"]+)"[^>]*material="\./([^"]+)"', text, re.S):
            pat = re.compile('^' + re.escape(m.group(1)).replace('\\*', '.*') + '$')
            for f in models:
                if pat.match(f):
                    mapping[f] = parse_mtl(os.path.join(folder, m.group(2)))
        # PES loads what face.xml lists plus the standard hair; anything else
        # in the folder is a leftover it never draws (Terry's unweighted
        # oral_head, the grey "modD_phone" hair_d copied into four folders),
        # which here piled up at the origin or z-fought the hair.
        return {f: mapping.get(f, shared) for f in models if f in mapping or STANDARD_HAIR.match(f)}
    return {f: shared for f in models}


# face.xml model types whose geometry is authored in the head bone's own
# space and carries no weights: PES pins it to sk_head (No Time For Love's
# oral_hair, y -0.15..0.17 around the head joint; placed at the origin it lay
# on the pitch).
HEAD_TYPES = ('head',)
STANDARD_HAIR = re.compile(r'hair_high_.*\.model$')   # PES15 loads it without a face.xml entry
HEAD_BONE = 'sk_head'


def model_types(folder):
    """-> {model file: face.xml type} (face_neck, parts, uniform, gloveL, head, ...)."""
    xml = os.path.join(folder, 'face.xml')
    if not os.path.exists(xml):
        return {}
    text = read_text(xml)
    models = sorted(f for f in os.listdir(folder) if f.endswith('.model'))
    out = {}
    for m in re.finditer(r'<model[^>]*type="([^"]+)"[^>]*path="\./([^"]+)"', text, re.S):
        pat = re.compile('^' + re.escape(m.group(2)).replace('\\*', '.*') + '$')
        for f in models:
            if pat.match(f):
                out[f] = m.group(1)
    return out


def head_to_world(folder):
    """sk_head's local -> render-pose world transform, from the inverse bind
    of whichever model in the folder carries the bone."""
    for f in sorted(os.listdir(folder)):
        if not f.endswith('.model'):
            continue
        for b in load_model(os.path.join(folder, f)).bones:
            if b.name == HEAD_BONE:
                m = b.matrix
                r = [m[0:3], m[4:7], m[8:11]]
                t = [m[3], m[7], m[11]]
                # inverse bind maps world -> local: local = R w + t, so w = R^T (local - t)
                return lambda p: tuple(sum(r[k][i] * (p[k] - t[k]) for k in range(3)) for i in range(3))
    return None


# Face part. PES2012's face rig (pes12_rig.face_rig): joints in head-local
# space, bone 0 = skull (moves exactly with the body head bone), 25-28 the
# eye/neck controllers (left alone: our eyes are not split per eyeball).
FACE_SKULL_BONE = 0
FACE_CONTROLLER_BONES = (25, 26, 27, 28)
FACE_BONE = re.compile(r'skf_')
FACE_SIDE_EPS_M = 0.005     # |x| below this is the centre line
FACE_MATCH_MAX_M = 0.03     # a PES15 face bone farther than this from every PES2012 one rides the skull


def face_slots(bind, rig):
    """{PES15 skf_* bone: PES2012 face palette slot}, nearest joint on the same
    side of the face; head-local = render bind minus sk_head (the head's
    PES_ALIGN is identity)."""
    head = bind.get(HEAD_BONE)
    if head is None:
        return {}
    pal = rig['palette']
    cands = [(pal.index(b), rig['joints'][b]) for b in pal if b not in FACE_CONTROLLER_BONES and b != FACE_SKULL_BONE]
    side = lambda x: 0 if abs(x) < FACE_SIDE_EPS_M else (1 if x > 0 else -1)
    out = {}
    for name, j in bind.items():
        if not FACE_BONE.match(name):
            continue
        loc = [a - b for a, b in zip(j, head)]
        best = min(((sum((a - c) ** 2 for a, c in zip(loc, cj)), sl) for sl, cj in cands if side(cj[0]) == side(loc[0])),
                   default=(1e9, None))
        out[name] = best[1] if best[0] <= FACE_MATCH_MAX_M ** 2 else pal.index(FACE_SKULL_BONE)
    return out


def pack_face_vertex(pos, head_pos, raw, fslots, skull_slot, rest):
    """The face-local copy of a head-only vertex: position minus the PES2012
    head joint, weights on face palette slots (PES2012 convention: n bones =
    n-1 explicit weights, padding repeats the last slot). rest = the packed
    normal/binormal/tangent/uv tail of the body vertex."""
    w = {}
    for name, wt in raw:
        sl = fslots.get(name, skull_slot)
        w[sl] = w.get(sl, 0.0) + wt
    if not w:
        w = {skull_slot: 1.0}
    top = sorted(w.items(), key=lambda kv: -kv[1])[:F.MAX_INFLUENCES]
    tw = sum(x for _, x in top) or 1.0
    slots = [sl for sl, _ in top]
    ws = [x / tw for _, x in top][:-1]
    slots += [slots[-1]] * (F.MAX_INFLUENCES - len(slots))
    ws += [0.0] * (F.MAX_INFLUENCES - len(ws))
    return struct.pack('<3f3f4B', *[a - b for a, b in zip(pos, head_pos)], *ws[:3], *slots) + rest


def resolve_texture(folder, diffuse, material, kit_dds):
    """Local ./file.dds; engine paths (model/character/uniform/...) -> the team kit."""
    if diffuse and diffuse.startswith('./'):
        # PES runs on Windows, where ./temple.dds opens Terry's Temple.dds
        want = diffuse[2:].lower()
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if f.lower() == want and os.path.getsize(p) > 128:
                return p
    if (material.startswith(('uni_', 'kit')) or (diffuse or '').startswith('model/character/uniform')) and kit_dds:
        return kit_dds  # engine uniform texture (dummy_kit.dds, ...) = the team kit
    if 'skin' in material:
        for cand in ('skin_color.dds', 'face.dds'):
            p = os.path.join(folder, cand)
            if os.path.exists(p):
                return p
    return None


def write_tex(src, dst):
    from PIL import Image
    if src is None:
        im = Image.new('RGBA', (4, 4), GREY_BGRA[2::-1] + (255,))
    elif isinstance(src, Image.Image):
        im = src.convert('RGBA')
    else:
        im = Image.open(__import__('io').BytesIO(read_bytes(src))).convert('RGBA')
    if max(im.size) > MAX_TEX_PX:
        s = MAX_TEX_PX / max(im.size)
        im = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))), Image.LANCZOS)
    tmp = dst + '.png'
    im.save(tmp)
    F.write_tex(tmp, dst)
    os.remove(tmp)


def convert(folder, kit_dds, out_dir, hide_body=None):
    """hide_body: the PES export's word on whether PES hides its body under
    this model (FPC); None = judge from the geometry."""
    table = __import__('json').load(open(F.BONES_JSON))
    slot_of = {b['bone']: b['slot'] for b in table['bones']}
    p12 = {b['bone']: b['pos'] for b in table['bones']}
    mats = model_materials(folder)
    types = model_types(folder)
    head_world = head_to_world(folder)
    rig = pes12_rig.load_face_rig()
    skull_slot = rig['palette'].index(FACE_SKULL_BONE)
    head_p12 = p12[F.FOX_TO_PES12[HEAD_BONE]]
    verts, idx, subs, textures = [], [], [], []
    tex_index = {}
    geometry_whole = False
    for fname, mtl in mats.items():
        if SKIP_FILES.search(fname):
            continue
        m = load_model(os.path.join(folder, fname))
        nverts = sum(len(x.vertices) for x in m.meshes)
        if WHOLE_BODY_NAMES.search(fname) or nverts >= WHOLE_BODY_MIN_VERTS:
            geometry_whole = True
        bind = {b.name: joint(b) for b in m.bones}
        on_head = types.get(fname) in HEAD_TYPES and head_world is not None
        fslots = face_slots(bind, rig)
        for mesh in m.meshes:
            if SKIP_MATERIALS.search(mesh.material or '') or len(mesh.faces) < 1:
                continue
            diffuse, states = mtl.get(mesh.material, (None, {}))
            if diffuse == NO_TEXTURE:
                continue
            tex = resolve_texture(folder, diffuse, mesh.material or '', kit_dds)
            # The kit slot: PES paints the team's kit sheet onto these. Their
            # UVs move from the PES14+ sheet to PES2012's (pes15_kits inverse
            # table) and drawlogic binds the kit the player is wearing; the
            # stored texture is only the stand-in for kit 1.
            kit_slot = kit_dds is not None and tex == kit_dds
            engine = K.engine_texture(diffuse) if tex is None else None
            if kit_slot:
                tex = KIT_TEXTURE
            elif engine is not None:
                tex = diffuse
            if tex not in tex_index:
                tex_index[tex] = len(textures)
                textures.append(K.convert_kit(kit_dds, kit_fwd()) if kit_slot else engine if engine is not None else tex)
            base = len(verts)
            bones = mesh.boneGroup.bones if mesh.boneGroup else []
            index_of = {id(v): k for k, v in enumerate(mesh.vertices)}
            face_of = {}                 # body vertex index -> face-local copy (bytes)
            for v in mesh.vertices:
                infl, raw = {}, []
                for bi, w in (v.boneMapping or {}).items():
                    if bi >= len(bones):  # 4cc exports carry stale indices past the bone group
                        continue
                    name = bones[bi].name
                    try:
                        fox = F.main_bone(name, bind)
                    except KeyError:
                        continue
                    key = F.slot_key(name, fox)
                    infl[key] = infl.get(key, 0.0) + w
                    raw.append((name, w))
                if not infl and on_head:
                    infl = {(HEAD_BONE, False): 1.0}
                if not infl:  # static geometry rides the nearest main joint
                    p = (v.position.x, v.position.y, v.position.z)
                    fox = min(F.FOX_TO_PES12, key=lambda b: sum((a - c) ** 2 for a, c in zip(p, retarget.PES_RENDER_BIND[b][0]))
                              if b in retarget.PES_RENDER_BIND else 1e9)
                    infl = {(fox, False): 1.0}
                infl = {b: w for b, w in infl.items() if w >= F.MIN_WEIGHT} or infl
                top = sorted(infl.items(), key=lambda kv: -kv[1])[:F.MAX_INFLUENCES]
                tw = sum(w for _, w in top) or 1.0
                top = [(b, w / tw) for b, w in top]
                p = (v.position.x, v.position.y, v.position.z)
                n = (v.normal.x, v.normal.y, v.normal.z) if v.normal else (0.0, 1.0, 0.0)
                if on_head and not raw:
                    o = head_world((0.0, 0.0, 0.0))
                    p = head_world(p)
                    n = tuple(a - b for a, b in zip(head_world(n), o))   # rotation only
                p, n = curl_fingers(p, n, raw, bind)
                tg = (v.tangent.x, v.tangent.y, v.tangent.z) if v.tangent else (1.0, 0.0, 0.0)
                pos, nrm, tan = [0.0] * 3, [0.0] * 3, [0.0] * 3
                for (fox, _), w in top:
                    a = retarget.PES_RENDER_BIND[fox][0]
                    q = retarget.PES_ALIGN.get(fox, (0.0, 0.0, 0.0, 1.0))
                    r = retarget._q_rot(q, tuple(pi - ai for pi, ai in zip(p, a)))
                    t = p12[F.FOX_TO_PES12[fox]]
                    rn, rt = retarget._q_rot(q, n), retarget._q_rot(q, tg)
                    for k in range(3):
                        pos[k] += w * (t[k] + r[k])
                        nrm[k] += w * rn[k]
                        tan[k] += w * rt[k]
                ln = math.sqrt(sum(c * c for c in nrm)) or 1.0
                nrm = [c / ln for c in nrm]
                lt = math.sqrt(sum(c * c for c in tan)) or 1.0
                tan = [c / lt for c in tan]
                bin_ = [nrm[1] * tan[2] - nrm[2] * tan[1], nrm[2] * tan[0] - nrm[0] * tan[2],
                        nrm[0] * tan[1] - nrm[1] * tan[0]]
                slots = [F.key_slot(b, slot_of) for b, _ in top]
                ws = [w for _, w in top][:-1]
                while len(slots) < F.MAX_INFLUENCES:
                    slots.append(slots[-1])
                while len(ws) < F.MAX_INFLUENCES:
                    ws.append(0.0)
                uv = v.uv[0] if v.uv else None
                u, vv = (uv.u, uv.v) if uv is not None else (0.0, 0.0)
                if kit_slot:
                    u, vv = kit_uv(u, vv)
                packed = struct.pack('<3f3f4B3f3f3f2f2f', *pos, *ws[:3], *slots, *nrm, *bin_, *tan, u, vv, u, vv)
                if all(fox == HEAD_BONE for (fox, _) in infl):
                    face_of[len(verts)] = pack_face_vertex(pos, head_p12, raw, fslots, skull_slot, packed[F.FACE_REST_OFF:])
                verts.append(packed)
            face_tris, body_tris = [], []
            for face in mesh.faces:
                if min(v.position.y for v in face.vertices) < HIDDEN_Y:
                    continue
                # PES15 winds counter-clockwise-front (cross(b-a, c-a) follows the
                # vertex normal on 99% of faces); Fox, which drawlogic culls for,
                # winds the other way (Caulifla: 1%). Flip, or every inverted-hull
                # outline shell draws over its body.
                tri = [base + index_of[id(face.vertices[k])] for k in (0, 2, 1)]
                (face_tris if all(t in face_of for t in tri) else body_tris).append(tri)
            flags = sub_flags(states) | (SUB_KIT if kit_slot else 0) | (SUB_OUTLINE if OUTLINE_MATERIAL.search(mesh.material or '') else 0)
            if body_tris:
                subs.append((len(idx), 3 * len(body_tris), tex_index[tex], flags))
                idx.extend(t for tri in body_tris for t in tri)
            if face_tris:
                remap = {}
                for tri in face_tris:
                    for t in tri:
                        if t not in remap:
                            remap[t] = len(verts)
                            verts.append(face_of[t])
                subs.append((len(idx), 3 * len(face_tris), tex_index[tex], flags | SUB_FACE))
                idx.extend(remap[t] for tri in face_tris for t in tri)
    subs.sort(key=lambda s: bool(s[3] & SUB_BLEND))
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.startswith('body'):
            os.remove(os.path.join(out_dir, f))
    # extents in body space: face-part vertices are head-local
    body_idx = {i for s in subs if not s[3] & SUB_FACE for i in idx[s[0]:s[0] + s[1]]}
    face_idx = {i for s in subs if s[3] & SUB_FACE for i in idx[s[0]:s[0] + s[1]]}
    ys = [struct.unpack_from('<3f', verts[i])[1] for i in body_idx]
    ys += [struct.unpack_from('<3f', verts[i])[1] + head_p12[1] for i in face_idx]
    ys = ys or [0]
    whole = geometry_whole if hide_body is None else hide_body
    mode = (MODE_BODY if min(ys) < OWN_FEET_Y else MODE_BOOTS) if whole else MODE_HEAD
    with open(os.path.join(out_dir, 'body.bin'), 'wb') as o:
        o.write(struct.pack('<6I', BODY2_MAGIC, len(verts), len(idx), F.VERTEX_STRIDE, len(subs), mode))
        for s in subs:
            o.write(struct.pack('<4I', *s))
        o.write(b''.join(verts))
        o.write(struct.pack('<%dI' % len(idx), *idx))
    for k, t in enumerate(textures):
        write_tex(t, os.path.join(out_dir, 'body_%d.tex' % k))
    print('%s: %d verts, %d tris, %d submeshes, %d textures, mode %s, y %.2f..%.2f' % (
        os.path.basename(folder), len(verts), len(idx) // 3, len(subs), len(textures),
        MODE_NAMES[mode], min(ys), max(ys)))


if __name__ == '__main__':
    # pes15_to_pes12.py <face folder> <kit.dds> <out dir> [hide|keep]
    hint = sys.argv[4] if len(sys.argv) > 4 else None
    convert(*sys.argv[1:4], hide_body=None if hint is None else hint == 'hide')

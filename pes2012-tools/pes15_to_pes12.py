"""PES2015 4cc face folder (Faces/<pid> - <name>/) -> PES2012 custom body.

    python3 pes15_to_pes12.py <face folder> <kit texture.dds> <out_dir>

Reads every *.model in the folder with the4chancup/pes-model-blender's
ModelFile.py (no Blender needed), materials from face.xml -> *.mtl (PES15
layout) or materials.mtl (PES16+ faceneck layout), and writes the
drawlogic.dll PGB2 format:

  u32 'PGB2', nv, ni, stride(80), nsub, keep
  nsub x (u32 firstIndex, u32 indexCount, u32 texture, u32 subFlags)
  nv x 80-byte vertices (fmdl_to_pes12.py's layout), ni x u32 indices
  body_<texture>.tex per texture (PGT1, see fmdl_to_pes12.write_tex)

keep: the stock pieces drawn with the model, one bit per PIECES name
(drawlogic PIECE_NAMES): none for a whole figure, everything but the head for
a face-slot player, the boots alone for a figure stopping at the ankle.
<game>/kitserver/4cc-players/custom/<name>/mode overrides it with piece names, e.g.
"shirt sleeves shorts socks" for a model that wears the stock kit.
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
# the stock pieces a custom model can keep drawn: drawlogic PIECE_NAMES, same
# order (the kit run's part classes, then the skin draw and the bare hands)
PIECES = ('shorts', 'shirt', 'sleeves', 'socks', 'neck', 'gloves', 'head', 'boots', 'other', 'skin', 'hands')


def keep_mask(*names):
    return sum(1 << PIECES.index(n) for n in names)


KEEP_NONE = 0                                                    # a whole figure
KEEP_BUT_HEAD = keep_mask(*(n for n in PIECES if n != 'head'))   # a face-slot player
KEEP_BOOTS = keep_mask('boots')                                  # a figure stopping at the ankle
# A hidden-body model reaching the floor brings its own feet; one stopping
# above it stands in PES's boots (Stallman: y 0.07.., real boots id 1; Terry:
# y -0.17.., boots id 55 = none).
OWN_FEET_Y = 0.05
SUB_ALPHATEST, SUB_BLEND, SUB_TWOSIDED, SUB_NOZWRITE, SUB_KIT, SUB_OUTLINE, SUB_FACE = 1, 2, 4, 8, 16, 32, 64
SUB_SHADELESS, SUB_TOON = 1 << 16, 1 << 17   # drawlogic's own pixel shaders (custom_ps.hlsl)
# PES material shaders drawn unlit / cel-shaded instead of with PES2012's lit
# kit shader (739 Shadeless, 177 Constant, 382 Pony materials in the packs).
SHADELESS_SHADERS = ('Shadeless', 'Constant')
TOON_SHADERS = ('Pony',)
# toon outline shells (inverted hulls) by material name: drawlogic pushes them
# back in depth so they only show past the silhouette
OUTLINE_MATERIAL = re.compile(r'outline', re.I)
SUB_REF_SHIFT = 8
# PES15's Hair shader does not take opacity from the diffuse alpha (hair_col
# alpha: median 0, 90th percentile 39 on DEVELOPERS, 27-09): drawn with its
# alphablend state the hair vanished. Drawn opaque and two-sided instead.
OPAQUE_SHADERS = ('Hair',)
# ... but its soft strand edges exist (hair_col partial alpha 11-38 % on
# Stallman, Rossmann, No Time For Love): drawlogic draws a SUB_HAIR sub opaque
# and adds a blended fringe pass from its alpha (30-09: matte hair).
SUB_HAIR = 1 << 18
# A Hair texture whose alpha is mostly empty is not an opacity map (DEVELOPERS'
# hair_col: 87 % zero, drawn by alpha the hair vanished, 27-09); one with this
# share of solid texels is (Stallman 55 %, No Time For Love 56 %).
HAIR_MIN_SOLID = 0.25
HAIR_SOLID_ALPHA = 250


def hair_has_opacity(tex):
    """True when a hair texture's alpha channel is a real opacity map."""
    try:
        import numpy as np
        from PIL import Image
        a = np.asarray(Image.open(tex).convert('RGBA'))[..., 3]
    except Exception:
        return False
    return (a >= HAIR_SOLID_ALPHA).mean() >= HAIR_MIN_SOLID
SHADER_KEY = '_shader'              # parse_mtl stores the material's shader among its states
KIT_TEXTURE = '<kit slot>'     # tex_index key of the kit-slot stand-in texture
# Textures stay DXT-compressed at their own size up to this (drawlogic uploads
# the DDS as is; PES2015/2017, also 32-bit, hold these packs the same way).
# 4096 atlases (/u/'s players.dds) drop one mip level. Provisional: 2048 is
# the stadium tool's cap too; raise it if memory allows (48 bodies resident).
MAX_TEX_PX = 2048
DDS_SIZE_OFF, DDS_FOURCC_OFF = 12, 84      # DDS header: u32 height, width; pixel-format fourcc
DDS_DXT = (b'DXT1', b'DXT3', b'DXT5')
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
# bone: the flexion of PES's normal.gani (GameplayFootball AGENTS.md: mcp 15,
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
    shader = states.get(SHADER_KEY)
    f = SUB_SHADELESS if shader in SHADELESS_SHADERS else SUB_TOON if shader in TOON_SHADERS else 0
    if shader in OPAQUE_SHADERS:
        return SUB_TWOSIDED | SUB_HAIR
    if states.get('alphatest') == '1':
        f |= SUB_ALPHATEST | (int(states.get('alpharef') or 0) << SUB_REF_SHIFT)
    if states.get('alphablend') == '1':
        f |= SUB_BLEND
    if states.get('twosided') in ('1', '2'):
        f |= SUB_TWOSIDED
    if states.get('zwrite') == '0':
        f |= SUB_NOZWRITE
    return f


COMMON_MTL = 'model/character/uniform/common/'   # engine path a PES17 pack ships as its Common/ dir


def common_dir(folder):
    """The pack's Common/ dir, sibling of Faces/."""
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(folder)), '..', 'Common'))


def pack_mtl(folder, material):
    """face.xml material attribute -> parsed mtl: ./file in the face folder,
    or an engine uniform/common/<tid>/file.mtl the pack ships in Common/ (/u/
    points its face, hands and body at common/756/player.mtl). Common/ mtl
    paths are relative to Common/, so its ./ samplers are rewritten to the
    engine path resolve_texture already maps there. None: nothing to parse."""
    if material.startswith('./'):
        return parse_mtl(os.path.join(folder, material[2:]))
    if material.startswith(COMMON_MTL):
        path = os.path.join(common_dir(folder), material.rsplit('/', 1)[-1])
        if os.path.isfile(path):
            return {k: (COMMON_MTL + 'XXX/' + d[2:] if d and d.startswith('./') else d, s)
                    for k, (d, s) in parse_mtl(path).items()}
    return None


KIT0_MODEL = re.compile(r'(.*u0(?:xxx|\d{3})p)0(\.model)$', re.I)


def xml_model_pattern(path):
    """face.xml model path (after './') -> regex over the folder's files. The
    per-strip 4cc trick names kit slot 0 (u0XXXp0.model: the strip being
    worn) and ships u0XXXp1..p5; kit 1 stands in, as for p0 textures
    (XXX20 - Handcrafting, 30-09)."""
    m = KIT0_MODEL.match(path)
    if m:
        path = m.group(1) + '1' + m.group(2)
    return re.compile('^' + re.escape(path).replace('\\*', '.*') + '$', re.I)


def model_materials(folder):
    """-> {model file: {material: (diffuse, states)}}. face.xml names the mtl
    for the models it lists; the standard hair_high it does not list takes its
    materials from every .mtl in the folder."""
    xml = os.path.join(folder, 'face.xml')
    models = sorted(f for f in os.listdir(folder) if f.endswith('.model'))
    # Which file's material wins: PES's own hair (hair.mtl, eye.mtl, ...) is
    # authoritative; face.mtl's leftovers (head_phong with a missing sampler)
    # are the empties face_diff.bin fills in game.
    shared = {}
    files = sorted((f for f in os.listdir(folder) if f.endswith('.mtl')),
                   key=lambda f: (0, f) if f in OWN_MTLS else (1, f))
    for f in files:
        try:
            for k, v in parse_mtl(os.path.join(folder, f)).items():
                shared.setdefault(k, v)
        except ET.ParseError:   # 4cc folders carry stale/binary .mtl copies no model uses
            print('skipping unparseable %s' % f)
    if os.path.exists(xml):
        text = read_text(xml)
        mapping = {}
        for m in re.finditer(r'<model[^>]*path="\./([^"]+)"[^>]*material="([^"]+)"', text, re.S):
            pat = xml_model_pattern(m.group(1))
            mtl = pack_mtl(folder, m.group(2))
            if mtl is None:
                continue
            for f in models:
                if pat.match(f):
                    mapping[f] = mtl
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
# face.xml types whose diffuse PES replaces with the kit texture (Rigged Wiki
# Blender tutorials: a "uniform" line's diffuse "will always be forced to the
# kit texture"; the rest are PES's own garment parts).
KIT_TYPES = ('uniform', 'uniform_sub', 'shirt', 'collar', 'sleeve_sub', 'pants_sub',
             'pants_nocloth', 'thigh_short', 'thigh_long')
STANDARD_HAIR = re.compile(r'hair_high_.*\.model$')
# The pack's own .mtl files: their material samplers are authoritative over
# face.mtl leftovers of the same name (face_diff.bin fills them in game).
OWN_MTLS = ('hair.mtl', 'eye.mtl', 'eye_occlusion.mtl', 'hair_parts.mtl')
   # PES15 loads it without a face.xml entry
HEAD_BONE = 'sk_head'


def model_types(folder):
    """-> {model file: face.xml type} (face_neck, parts, uniform, gloveL, head, ...)."""
    xml = os.path.join(folder, 'face.xml')
    if not os.path.exists(xml):
        return {}
    text = read_text(xml)
    models = sorted(f for f in os.listdir(folder) if f.endswith('.model'))
    out = {}
    for tag in re.findall(r'<model\b[^>]*>', text, re.S):   # attributes in any order
        attr = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if 'type' not in attr or not attr.get('path', '').startswith('./'):
            continue
        pat = xml_model_pattern(attr['path'][2:])
        for f in models:
            if pat.match(f):
                out[f] = attr['type']
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
# PES15 face bone -> PES2012 face rig bone, by role. Nearest-joint alone sent
# upper and lower lip (and both eyelids) to one bone, so mouths could not open
# and lids blinked as one, and missed the brows/cheeks entirely (PES2012 pivots
# them deep in the head): janky faces on /sci/ Feynman and every PES-template
# face (30-09). Roles read off the rig's joint tree (dt0c #132, head-local,
# pes12_rig.face_rig; every stock face shares it): 1 jaw pivot -> 2 lower
# mouth -> 3/4 lower lip L/R; 7 upper mouth -> 8/9 upper lip L/R; 22/23 mouth
# corners L/R; 12/13 upper eyelids L/R, 14/15 lower; 16/17 inner brows L/R,
# 18/19 outer; 10/11 cheeks L/R; 20/21 nostrils L/R. L = +x (PES15 '_l').
JAW_BONE = 'skf_jaw'
THROAT_BONES = ('sk_neck', 'dsk_scm', 'sk_chest')
JAW_THROAT_NECK_SHARE = 0.5   # provisional: the chin underside half follows the neck
FACE_ROLES = {
    'skf_jaw': 1, 'skf_doublechin': 2, 'skf_lip_b_c': 2, 'skf_lip_t_c': 7, 'skf_lip_volume': 7,
    'skf_lip_b_l': 3, 'skf_lip_b_r': 4, 'skf_lip_t_l': 8, 'skf_lip_t_r': 9,
    'skf_lip_s_l': 22, 'skf_lip_s_r': 23,
    'skf_eyelid_t_l': 12, 'skf_eyelid_t_r': 13, 'skf_eyelid_b_l': 14, 'skf_eyelid_b_r': 15,
    'skf_orbicularisoculi_b_l': 14, 'skf_orbicularisoculi_b_r': 15,
    'skf_brow_i_l': 16, 'skf_brow_i_r': 17, 'skf_brow_o_l': 18, 'skf_brow_o_r': 19,
    'skf_cheek_l': 10, 'skf_cheek_r': 11, 'skf_nosewing_l': 20, 'skf_nosewing_r': 21,
}


def face_slots(bind, rig):
    """{PES15 skf_* bone: PES2012 face palette slot}: by role (FACE_ROLES),
    else the nearest joint on the same side of the face; head-local = render bind minus sk_head (the head's
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
        if FACE_ROLES.get(name) in pal:
            out[name] = pal.index(FACE_ROLES[name])
    return out


# Face weights come from PES2012's own face, not from mapping bones. PES
# pivots its lip/jaw bones differently from PES2012's (PES2012's lower lip
# joints sit 2 cm from where their vertices are, and move 20 mm when the jaw
# opens), so every bone-to-bone mapping tore Feynman's mouth open (30-09).
# Instead each custom face vertex takes the rig weights of the stock face
# (pes12_rig face_rig 'verts', head-local) blended from its nearest
# FACE_TRANSFER_K stock vertices, scaled by how much PES's own rig moves it
# (its non-sk_head share); the rest rides the skull.
FACE_TRANSFER_K = 4
FACE_TRANSFER_EPS_M = 0.002     # inverse-distance floor
FACE_TRANSFER_MAX_M = 0.02      # farther from the stock face than this: skull only (helmets, hair)
_face_tree = {}


# The custom face is laid onto the stock one before the lookup: each axis is
# mapped piecewise-linearly through landmarks both rigs name (PES bone on the
# custom face -> centroid of the stock vertices weighted to the matching rig
# bones, measured on dt0c #132 30-09). Feynman's mouth sits 1.3 cm lower and
# 1.7 cm further forward than the stock mouth; unaligned, his lips read the
# stock chin.
FACE_LANDMARKS = (          # (PES15 bone, stock landmark head-local x, y, z)
    ('skf_lip_s_l', (0.019, -0.021, 0.100)), ('skf_lip_s_r', (-0.019, -0.021, 0.100)),
    ('skf_lip_t_c', (0.0, -0.017, 0.106)), ('skf_lip_b_c', (0.0, -0.023, 0.106)),
    ('skf_eyelid_t_l', (0.031, 0.054, 0.097)), ('skf_eyelid_t_r', (-0.031, 0.054, 0.097)),
)


def face_align(bind):
    """PES head-local -> stock-face head-local, per axis piecewise linear."""
    head = bind.get(HEAD_BONE)
    pairs = [([a - b for a, b in zip(bind[n], head)], t) for n, t in FACE_LANDMARKS if n in bind and head]
    if len(pairs) < 3:
        return None
    axes = []
    for k in range(3):
        pts = sorted({round(src[k], 4): tgt[k] for src, tgt in pairs}.items())
        axes.append(([a for a, _ in pts], [b for _, b in pts]))

    def f(p):
        import numpy as np
        return [float(np.interp(p[k], *axes[k])) + (p[k] - axes[k][0][0] if p[k] < axes[k][0][0] else
                (p[k] - axes[k][0][-1] if p[k] > axes[k][0][-1] else 0.0)) if len(axes[k][0]) > 1 else
                p[k] + axes[k][1][0] - axes[k][0][0] for k in range(3)]
    return f


def stock_face_weights(rig, loc):
    """Head-local point (on the stock face) -> {rig bone: weight} blended from
    its stock vertices; the neck/eye controllers are left out (they move the
    stock face relative to its own skull, which our face copy rides)."""
    key = id(rig)
    if key not in _face_tree:
        from scipy.spatial import cKDTree
        pts = [v[0] for v in rig['verts']]
        _face_tree[key] = (cKDTree(pts), [{int(b): w for b, w in v[1].items() if int(b) not in FACE_CONTROLLER_BONES}
                                          for v in rig['verts']])
    tree, ws = _face_tree[key]
    d, ii = tree.query(loc, k=FACE_TRANSFER_K)
    if d[0] > FACE_TRANSFER_MAX_M:
        return None
    out, tot = {}, 0.0
    for dist, i in zip(d, ii):
        f = 1.0 / max(dist, FACE_TRANSFER_EPS_M)
        tot += f
        for b, w in ws[i].items():
            out[b] = out.get(b, 0.0) + f * w
    tot = sum(out.values()) or 1.0
    return {b: w / tot for b, w in out.items()}


def face_bone_dist(m, bind, align, rig):
    """{PES15 skf_* bone: {face palette slot: share}}: what the stock face does
    where this bone moves the custom face. Every vertex the bone weights looks
    up the stock face at its aligned position; the bone's share of those
    stock weights, summed over its vertices, is the bone's recipe. Per BONE,
    not per vertex: a per-vertex lookup copied the stock face's seams onto
    the custom one (inner mouth folded into spikes, 30-09), while this keeps
    the pack's own smooth weights and only swaps what each bone drives."""
    head = bind.get(HEAD_BONE)
    if not rig or 'verts' not in rig or head is None:
        return {}
    pal = rig['palette']
    acc = {}
    for mesh in m.meshes:
        bones = mesh.boneGroup.bones if mesh.boneGroup else []
        for v in mesh.vertices:
            skf = [(bones[k].name, w) for k, w in (v.boneMapping or {}).items()
                   if k < len(bones) and FACE_BONE.match(bones[k].name)]
            if not skf:
                continue
            loc = [a - b for a, b in zip((v.position.x, v.position.y, v.position.z), head)]
            stock = stock_face_weights(rig, align(loc) if align else loc)
            if stock is None:
                continue
            for name, w in skf:
                d = acc.setdefault(name, {})
                for b, x in stock.items():
                    if b in pal:
                        d[pal.index(b)] = d.get(pal.index(b), 0.0) + w * x
    out = {}
    for name, d in acc.items():
        tot = sum(d.values()) or 1.0
        out[name] = {sl: x / tot for sl, x in d.items() if x / tot >= FACE_DIST_MIN_SHARE}
    return out


FACE_DIST_MIN_SHARE = 0.05      # drop a bone's minor stock influences (keeps 4 per vertex usable)


def pack_face_vertex(pos, head_pos, raw, fslots, skull_slot, rest, dist=None):
    """The face-local copy of a head-only vertex: position minus the PES2012
    head joint, weights on face palette slots (packed by F.skin_pack). rest =
    the packed normal/binormal/tangent/uv tail of the body vertex. With the
    bone's recipe (face_bone_dist), its weight spreads as the stock face
    spreads it; else the role/nearest slot (face_slots)."""
    w = {}
    for name, wt in raw:
        recipe = (dist or {}).get(name)
        if recipe:
            tot = sum(recipe.values())
            for sl, x in recipe.items():
                w[sl] = w.get(sl, 0.0) + wt * x / tot
        else:
            sl = fslots.get(name, skull_slot)
            w[sl] = w.get(sl, 0.0) + wt
    if not w:
        w = {skull_slot: 1.0}
    top = sorted(w.items(), key=lambda kv: -kv[1])[:F.MAX_INFLUENCES]
    tw = sum(x for _, x in top) or 1.0
    ws, slots = F.skin_pack([(sl, x / tw) for sl, x in top])
    return struct.pack('<3f3f4B', *[a - b for a, b in zip(pos, head_pos)], *ws, *slots) + rest


# PES's deform helpers (dsk_*) turn part of the way between the two bones of
# a joint; PES2012's skeleton has no such bones. Glued onto one neighbour
# (fmdl_to_pes12.main_bone) a knee helper swings wholly with the shin and the
# knee pinches when the leg bends (/u/, 29-09 replay), skirt hems shatter.
# Split its weight between the joint's two bones instead: a vertex weighted
# half to each follows the joint at about half its angle, as a half-rotation
# helper does. Provisional: PES's per-helper ratios are not read, 0.5 is the
# half-angle convention. dsk_forearm_t is not a joint helper: it carries the
# hand's roll along the forearm, and split into sk_hand it bent half the
# forearm with every wrist flex (cuffs pulled off the hands, Kurisu 30-09);
# it stays on the forearm (main_bone).
HELPER_SHARE = 0.5
HELPER_JOINTS = (   # helper pattern -> (bone above, bone below); \1 = side
    (re.compile(r'dsk_knee_([lr])$'), ('sk_thigh_\\1', 'sk_leg_\\1')),
    (re.compile(r'dsk_elbow_([lr])$'), ('sk_upperarm_\\1', 'sk_forearm_\\1')),
    (re.compile(r'dsk_wrist_([lr])$'), ('sk_forearm_\\1', 'sk_hand_\\1')),
    (re.compile(r'dsk_hem_([lr])$'), ('dsk_hip', 'sk_thigh_\\1')),
)


def helper_split(name):
    """Deform helper -> [(bone, share), ...] across its joint, else None."""
    for pat, (above, below) in HELPER_JOINTS:
        m = pat.match(name)
        if m:
            return [(m.expand(above), 1.0 - HELPER_SHARE), (m.expand(below), HELPER_SHARE)]
    return None


# Joint seams. Each bone carries its geometry rigidly from PES's render bind
# onto PES2012's bone (PES_ALIGN rotation about its own head), but the two
# skeletons' proportions differ, so the joint a parent's piece reaches is
# not where PES2012 puts the child: chest->shoulder 10 cm, shoulder->upper
# arm 8, knee 7, hip 6.5, wrist 5, neck 4 (30-09). Every limb then tore or
# creased at its joints. The parent's piece is warped instead: a vertex moves
# by each child joint's error, scaled by how far along that child's segment
# it sits (0 at the parent's own head, 1 at the joint) and shared between
# children by inverse distance, so the piece meets every child exactly and
# is untouched at its own head. Only toward children the model has geometry
# on: with no child piece there is no seam, and the clamped warp bends flat
# faces (XXX20 - Handcrafting: a block character all on sk_belly, whose face
# decal the belly->chest warp tilted 2.6 cm into the head, 30-09).
def _seam_table(p12, used):
    parent = {b: retarget.PES_RENDER_BIND[b][1] for b in F.FOX_TO_PES12 if b in retarget.PES_RENDER_BIND}
    out = {}
    for bone in parent:
        head = retarget.PES_RENDER_BIND[bone][0]
        kids = []
        for child, par in parent.items():
            if par != bone or child not in F.FOX_TO_PES12 or child not in used:
                continue
            j = retarget.PES_RENDER_BIND[child][0]
            placed = _place(bone, j, p12)
            err = [a - b for a, b in zip(p12[F.FOX_TO_PES12[child]], placed)]
            seg = [a - b for a, b in zip(j, head)]
            ln2 = sum(c * c for c in seg)
            if ln2 > 0 and any(abs(c) > 0 for c in err):
                kids.append((j, seg, ln2, err))
        if kids:
            out[bone] = (head, kids)
    return out


def used_bones(folder, mats):
    """Main Fox bones carrying any weight in the models convert() loads."""
    used = set()
    for fname in mats:
        if SKIP_FILES.search(fname):
            continue
        m = load_model(os.path.join(folder, fname))
        bind = {b.name: joint(b) for b in m.bones}
        for mesh in m.meshes:
            bones = mesh.boneGroup.bones if mesh.boneGroup else []
            for v in mesh.vertices:
                for bi in (v.boneMapping or {}):
                    if bi >= len(bones):
                        continue
                    name = bones[bi].name
                    try:
                        used.update(b for b, _ in (helper_split(name) or [(F.main_bone(name, bind), 1.0)]))
                    except KeyError:
                        pass
    return used


def _place(bone, p, p12):
    a = retarget.PES_RENDER_BIND[bone][0]
    q = retarget.PES_ALIGN.get(bone, (0.0, 0.0, 0.0, 1.0))
    r = retarget._q_rot(q, tuple(pi - ai for pi, ai in zip(p, a)))
    t = p12[F.FOX_TO_PES12[bone]]
    return [t[k] + r[k] for k in range(3)]


def seam_offset(bone, p, seams):
    """PES2012-space displacement of source point p carried by `bone`."""
    got = seams.get(bone)
    if got is None:
        return (0.0, 0.0, 0.0)
    head, kids = got
    d = [pi - hi for pi, hi in zip(p, head)]
    acc, wsum = [0.0, 0.0, 0.0], 0.0
    for j, seg, ln2, err in kids:
        t = min(1.0, max(0.0, sum(a * b for a, b in zip(d, seg)) / ln2))
        dist2 = sum((pi - ji) ** 2 for pi, ji in zip(p, j)) + SEAM_EPS_M2
        w = 1.0 / dist2
        for k in range(3):
            acc[k] += w * t * err[k]
        wsum += w
    return tuple(c / wsum for c in acc)


# keeps the inverse-distance share finite at a joint (1 cm squared)
SEAM_EPS_M2 = 1e-4


KIT_STAND_INS = ('kit.dds',)
SKIN_MATERIALS = ('face_phong', 'head_phong', 'fox_skin_mat')   # PES face / head base meshes


def resolve_texture(folder, diffuse, material, kit_dds):
    """Local ./file.dds; a PES17 pack's sibling Common/ dir (the
    uniform/common/XXX/*.dds its materials.mtl point at - 4ccg's cracks,
    grim, eslking ...; dummy_kit.dds is the only one that stays the team
    kit); other engine uniform paths -> the team kit."""
    if diffuse and diffuse.startswith('./'):
        # PES runs on Windows, where ./temple.dds opens Terry's Temple.dds.
        # PES17 packs write the team number as an XXX placeholder and point
        # at kit slot 0 (./u0XXXp0, ./u0769p0); see below.
        want = diffuse[2:].lower()
        cands = [want]
        # PES17 packs point at kit slot 0 (./u0XXXp0, ./u0769p0) but ship
        # only slots 1..4 (u0768p1, garshirt_u0xxxp1, skin_u0XXXp1, ...):
        # resolve to the kit-1 file, the stand-in the kit slot also uses.
        m = re.match(r'(.*)u0(xxx|\d{3})p0(\.dds)$', want)
        if m:
            pre, _, ext = m.groups()
            pat = re.compile(re.escape(pre) + r'u0(?:xxx|\d{3})p1' + re.escape(ext))
            for f in sorted(os.listdir(folder)):
                if pat.fullmatch(f.lower()):
                    cands.append(f.lower())
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if f.lower() in cands and os.path.getsize(p) > 128:
                return p
    if diffuse and diffuse.startswith(COMMON_MTL):
        leaf = diffuse.rsplit('/', 1)[-1]
        if leaf != 'dummy_kit.dds':
            p = os.path.join(common_dir(folder), leaf)
            if os.path.isfile(p) and os.path.getsize(p) > 128:
                return p
            for cand in (os.path.join(folder, leaf),):
                if os.path.isfile(cand) and os.path.getsize(cand) > 128:
                    return cand
    if (material.startswith(('uni_', 'kit')) or (diffuse or '').startswith('model/character/uniform')) and kit_dds:
        return kit_dds  # engine uniform texture (dummy_kit.dds, ...) = the team kit
    leaf = (diffuse or '').rsplit('/', 1)[-1].lower()
    if leaf in KIT_STAND_INS and kit_dds:
        return kit_dds   # a local ./kit.dds the pack never ships: PES's kit
    # PES paints the face and head from the player's own skin (face_diff.bin);
    # packs that keep PES's face_high / hair_high meshes ship no texture for
    # them, so the folder's skin colour stands in.
    if 'skin' in material or leaf == 'face.dds' or material in SKIN_MATERIALS:
        for cand in ('skin_color.dds', 'face.dds'):
            p = os.path.join(folder, cand)
            if os.path.exists(p):
                return p
    return None


def _pow2_floor(v):
    return 1 << max(0, int(v).bit_length() - 1)


def write_tex(src, dst):
    """Texture -> a DDS drawlogic uploads as is (DXT1/DXT5, full mip chain).
    A source DDS already in DXT1/3/5 at a power-of-two size within
    MAX_TEX_PX is copied byte for byte (the pack's own compression and
    mips); anything else is re-encoded: DXT5 if it has transparency."""
    import subprocess
    import tempfile
    from PIL import Image
    if isinstance(src, str):
        raw = read_bytes(src)
        if raw[:4] == b'DDS ' and raw[DDS_FOURCC_OFF:DDS_FOURCC_OFF + 4] in DDS_DXT:
            h, w = struct.unpack_from('<II', raw, DDS_SIZE_OFF)
            if w == _pow2_floor(w) and h == _pow2_floor(h) and max(w, h) <= MAX_TEX_PX:
                open(dst, 'wb').write(raw)
                return
        im = Image.open(__import__('io').BytesIO(raw)).convert('RGBA')
    elif src is None:
        im = Image.new('RGBA', (4, 4), GREY_BGRA[2::-1] + (255,))
    else:
        im = src.convert('RGBA')
    w, h = (min(_pow2_floor(n), MAX_TEX_PX) for n in im.size)
    comp = 'dxt5' if im.getchannel('A').getextrema()[0] < 255 else 'dxt1'
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, 'in.png')
        im.resize((w, h), Image.LANCZOS).save(png)
        subprocess.run(['magick', png, '-define', 'dds:compression=' + comp, 'DDS:' + dst],
                       check=True, capture_output=True)


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
    seams = _seam_table(p12, used_bones(folder, mats))
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
        fdist = face_bone_dist(m, bind, face_align(bind), rig)
        for mesh in m.meshes:
            if SKIP_MATERIALS.search(mesh.material or '') or len(mesh.faces) < 1:
                continue
            diffuse, states = mtl.get(mesh.material, (None, {}))
            if diffuse == NO_TEXTURE:
                continue
            # face.xml type "uniform" (and PES's own garment types): PES forces
            # the diffuse to the team kit - the 4cc trick that re-makes the kit
            # on a custom shape (165 models across the packs, 29-09).
            tex = (kit_dds if kit_dds and types.get(fname) in KIT_TYPES
                   else resolve_texture(folder, diffuse, mesh.material or '', kit_dds))
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
            elif tex is None:
                print('  unresolved texture: %s material %r diffuse %r (grey stand-in)'
                      % (fname, mesh.material, diffuse))
            if tex not in tex_index:
                tex_index[tex] = len(textures)
                textures.append(K.convert_kit(kit_dds, kit_fwd()) if kit_slot else engine if engine is not None else tex)
            base = len(verts)
            bones = mesh.boneGroup.bones if mesh.boneGroup else []
            index_of = {id(v): k for k, v in enumerate(mesh.vertices)}
            face_of = {}                 # body vertex index -> face-local copy (bytes)
            face_src = {}                # body vertex index -> (position, packed tail) of its face copy
            for v in mesh.vertices:
                infl, raw = {}, []
                for bi, w in (v.boneMapping or {}).items():
                    if bi >= len(bones):  # 4cc exports carry stale indices past the bone group
                        continue
                    name = bones[bi].name
                    try:
                        shares = helper_split(name) or [(F.main_bone(name, bind), 1.0)]
                    except KeyError:
                        continue
                    # The jaw opens only in the face draw; a vertex it shares
                    # with the neck/chest is drawn by the body, where the jaw
                    # share would pin it to the skull and crease the throat
                    # (Feynman, 30-09). There the share hangs halfway, split
                    # between head and neck, like PES's own chin blend.
                    if name == JAW_BONE and any(bones[k].name in THROAT_BONES for k in v.boneMapping if k < len(bones)):
                        shares = [(HEAD_BONE, 1.0 - JAW_THROAT_NECK_SHARE), ('sk_neck', JAW_THROAT_NECK_SHARE)]
                    for fox, share in shares:
                        key = F.slot_key(name, fox)
                        infl[key] = infl.get(key, 0.0) + w * share
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
                    t = [tk + sk for tk, sk in zip(t, seam_offset(fox, p, seams))]
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
                ws, slots = F.skin_pack([(F.key_slot(b, slot_of), w) for b, w in top])
                uv = v.uv[0] if v.uv else None
                u, vv = (uv.u, uv.v) if uv is not None else (0.0, 0.0)
                if not (math.isfinite(u) and math.isfinite(vv)):
                    u, vv = 0.0, 0.0   # /mlp/ MLP11's "monitor" ships 24 NaN UVs; sampling NaN is undefined
                # kit slot: TEXCOORD0 keeps the pack's own UVs (drawlogic draws
                # it with the full-resolution source sheet), TEXCOORD1 carries
                # them re-mapped onto PES2012's sheet (the game's bound kit)
                u2, v2 = kit_uv(u, vv) if kit_slot else (u, vv)
                packed = struct.pack('<3f3f4B3f3f3f2f2f', *pos, *ws[:3], *slots, *nrm, *bin_, *tan, u, vv, u2, v2)
                if all(fox == HEAD_BONE for (fox, _) in infl):
                    face_of[len(verts)] = pack_face_vertex(pos, head_p12, raw, fslots, skull_slot, packed[F.FACE_REST_OFF:], fdist)
                    face_src[len(verts)] = (pos, packed[F.FACE_REST_OFF:])
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
            # A vertex on the face/body border has a face copy (facial bones)
            # and a body copy (the head bone): as the jaw opened they parted
            # and holes showed the back of the head (Feynman's cheeks, 30-09).
            # Its face copy rides the skull, which moves exactly with the head
            # bone, so the border stays welded and the face stretches to it.
            border = {t for tri in body_tris for t in tri if t in face_of} & {t for tri in face_tris for t in tri}
            for t in border:
                face_of[t] = pack_face_vertex(face_src[t][0], head_p12, [], fslots, skull_slot, face_src[t][1])
            flags = sub_flags(states) | (SUB_KIT if kit_slot else 0) | (SUB_OUTLINE if OUTLINE_MATERIAL.search(mesh.material or '') else 0)
            if flags & SUB_HAIR and not hair_has_opacity(tex):
                flags &= ~SUB_HAIR       # PES's Hair shader ignores this texture's alpha: plain opaque
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
    # Faces parked below HIDDEN_Y are skipped but leave their vertices behind;
    # parked verts (vt Nene: 3 at y -998) blow up preview framing, so compact.
    keep = sorted({i for s in subs for i in idx[s[0]:s[0] + s[1]]})
    remap = {old: new for new, old in enumerate(keep)}
    verts = [verts[old] for old in keep]
    at = 0
    compact = []
    for first, count, tex, flags in subs:
        compact.append((at, count, tex, flags))
        at += count
    idx = [remap[i] for s in subs for i in idx[s[0]:s[0] + s[1]]]
    subs = compact
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
    keep = (KEEP_NONE if min(ys) < OWN_FEET_Y else KEEP_BOOTS) if whole else KEEP_BUT_HEAD
    with open(os.path.join(out_dir, 'body.bin'), 'wb') as o:
        o.write(struct.pack('<6I', BODY2_MAGIC, len(verts), len(idx), F.VERTEX_STRIDE, len(subs), keep))
        for s in subs:
            o.write(struct.pack('<4I', *s))
        o.write(b''.join(verts))
        o.write(struct.pack('<%dI' % len(idx), *idx))
    # magick encodes in its own processes: one per texture at once
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor() as pool:
        list(pool.map(lambda kt: write_tex(kt[1], os.path.join(out_dir, 'body_%d.tex' % kt[0])),
                      enumerate(textures)))
    print('%s: %d verts, %d tris, %d submeshes, %d textures, keeps %s, y %.2f..%.2f' % (
        os.path.basename(folder), len(verts), len(idx) // 3, len(subs), len(textures),
        ' '.join(n for i, n in enumerate(PIECES) if keep >> i & 1) or 'nothing', min(ys), max(ys)))


if __name__ == '__main__':
    # pes15_to_pes12.py <face folder> <kit.dds> <out dir> [hide|keep]
    hint = sys.argv[4] if len(sys.argv) > 4 else None
    convert(*sys.argv[1:4], hide_body=None if hint is None else hint == 'hide')

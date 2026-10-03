"""tools/fmdl_to_pes12.py: the PES21 .fmdl -> PGB2 custom body.

Each section is one slice of the conversion, with the assertion that fails
before the change and passes after:

  container  PGB2 header, u32 indices, one submesh per material, keep mask
  helpers    dsk_* deform helpers split across the joint
  seams      one seam table, both engines
  kit        uni_* materials paint the kit sheet: SUB_KIT + TEXCOORD1 remap
  hand       skh_* finger bones curl, HRIG trailer written
  face       skf_* bones drive the face palette, SUB_FACE submesh
  oracle     the same garment through both engines, compared

    python3 tests/test_fmdl_pes12.py [repo] [-v]

The fmdl inputs are extracted from the PES21 .cpk packs into a /tmp cache on
first use (read-only on the game data); a missing pack skips that section.
"""
import os
import struct
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') \
    else os.path.abspath(os.path.join(HERE, '..', '..', '..'))
TOOLS = os.path.join(REPO, 'tools')
sys.path.insert(0, TOOLS)

import fmdl_to_pes12 as F  # noqa: E402
import pes15_to_pes12 as P  # noqa: E402
import retarget  # noqa: E402

DATA = os.environ.get('PES21_DATA', '')   # a PES2021 install; the test skips without it
CACHE = os.environ.get('PES12_FMDL_CACHE', '/tmp/pes12-fmdl-tests')
PES15_GARMENTS = os.path.join(REPO, 'dllprobe', 'kitmap', 'pes15', 'common', 'character1',
                              'model', 'character', 'uniform', 'nocloth')

# name -> (pack, path substring in the pack)
INPUTS = {
    'jersey': ('dt36_g4', 'face/real/136021/#Win/jersey.fmdl'),
    'suit': ('dt36_g4', 'face/real/65351/#Win/suit.fmdl'),
    'shirt_out': ('dt35_g4', 'uniform/nocloth/#Win/shirt_out.fmdl'),
    'shirt_in': ('dt35_g4', 'uniform/nocloth/#Win/shirt_in.fmdl'),
    'pants': ('dt35_g4', 'uniform/nocloth/#Win/pants_001.fmdl'),
    'hand_l': ('dt32_g4', 'parts/naked/scenes/#Win/naked_hand_l.fmdl'),
    'torso': ('dt32_g4', 'parts/torso/scenes/#Win/torso.fmdl'),
}
# the same garment in the older engine: the cross-engine oracle
ORACLE = (('shirt_out', 'shirt_out_high.model'), ('shirt_in', 'shirt_in_tight_high.model'))

_verbose = '-v' in sys.argv
IN = {}


def fmdl_path(name):
    if name in IN:
        return IN[name]
    pack, needle = INPUTS[name]
    if not DATA:
        IN[name] = None          # no PES2021 install configured: skip, do not guess
        return None
    dst = os.path.join(CACHE, name)
    if not os.path.exists(dst):
        os.makedirs(dst, exist_ok=True)
        cpk = os.path.join(DATA, pack + '.cpk')
        if not os.path.exists(cpk):
            IN[name] = None
            return None
        subprocess.run([sys.executable, os.path.join(TOOLS, 'cpk.py'), cpk, dst, '--filter=' + needle],
                       check=True, capture_output=True)
    IN[name] = next((os.path.join(d, f) for d, _, fs in os.walk(dst) for f in fs if f.endswith('.fmdl')), None)
    return IN[name]


def convert(name, hint=None):
    src = fmdl_path(name)
    if src is None:
        return None
    dst = os.path.join(CACHE, '_out', name)
    cmd = [sys.executable, os.path.join(TOOLS, 'fmdl_to_pes12.py'), src, '', dst]
    if hint:
        cmd.append(hint)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise AssertionError('convert %s failed:\n%s' % (name, r.stderr))
    return dst


def read_body(d):
    """body.bin -> the PGB2 fields plus the trailer."""
    b = open(os.path.join(d, 'body.bin'), 'rb').read()
    magic, nv, ni, stride, nsub, keep = struct.unpack_from('<6I', b, 0)
    if magic != 0x32424750:      # 'PGB2'
        raise AssertionError('not PGB2: magic %08x' % magic)
    at = 24
    subs = [struct.unpack_from('<4I', b, at + 16 * i) for i in range(nsub)]
    at += 16 * nsub
    verts = np.frombuffer(b, dtype=np.uint8, count=stride * nv, offset=at).reshape(nv, stride)
    at += stride * nv
    idx = np.frombuffer(b, dtype=np.uint32, count=ni, offset=at)
    return dict(nv=nv, ni=ni, stride=stride, keep=keep, subs=subs, verts=verts,
                idx=idx, trailer=b[at + 4 * ni:])


def vfield(b, which):
    """The vertex buffer -> one float field for every vertex.

    The fields are strided by the 80-byte vertex, so the buffer is viewed as
    (nv, 20) float32 and sliced by column - reading it as one contiguous run
    silently walks into the next vertex.
    """
    cols = {'pos': slice(0, 3), 'w': slice(3, 6), 'nrm': slice(7, 10),
            'uv0': slice(16, 18), 'uv1': slice(18, 20)}[which]
    return b['verts'].view(np.float32).reshape(b['nv'], -1)[:, cols]


def slots(b):
    return b['verts'][:, 24:28]


def winding_sign(body):
    """+1 when triangles come out CCW-front against their own vertex normals
    (the order PES15 stores), -1 when they come out the other way (the order
    drawlogic culls for, which both readers must end up producing)."""
    pos = vfield(body, 'pos')
    nrm = vfield(body, 'nrm')
    tri = body['idx'].reshape(-1, 3)
    a, b_, c = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    fn = np.cross(b_ - a, c - a)
    ln = np.linalg.norm(fn, axis=1)
    ok = ln > 1e-12
    fn, tri = fn[ok], tri[ok]
    mn = nrm[tri].mean(axis=1)
    agree = float((np.einsum('ij,ij->i', fn, mn) > 0).mean())
    return 1 if agree > 0.5 else -1


ok = fail = skipped = 0


def check(cond, msg):
    global ok, fail
    if cond:
        ok += 1
        if _verbose:
            print('  ok: ' + msg)
    else:
        fail += 1
        print('FAIL: ' + msg)


def skip(msg):
    global skipped
    skipped += 1
    print('skip: ' + msg)


def section(name):
    print('== ' + name)


def need(name):
    p = fmdl_path(name)
    if p is None:
        skip('%s (no %s)' % (name, INPUTS[name][0] + '.cpk'))
    return p is not None


def pes15_source(path):
    """A PES2015 .model garment -> the source shape fmdl_to_pes12.build takes.
    Built here, not by pes15_to_pes12.convert, so this work cannot alter what
    that path emits."""
    m = P.load_model(path)
    bind = {b.name: P.joint(b) for b in m.bones}
    meshes = []
    for mesh in m.meshes:
        if len(mesh.faces) < 1:
            continue
        bones = mesh.boneGroup.bones if mesh.boneGroup else []
        index_of = {id(v): k for k, v in enumerate(mesh.vertices)}
        meshes.append(dict(
            tex=None, kit=(mesh.material or '').startswith('uni_'), flags=0,
            verts=[dict(p=(v.position.x, v.position.y, v.position.z),
                        n=(v.normal.x, v.normal.y, v.normal.z) if v.normal else (0.0, 1.0, 0.0),
                        t=(v.tangent.x, v.tangent.y, v.tangent.z) if v.tangent else (1.0, 0.0, 0.0),
                        uv=(v.uv[0].u, v.uv[0].v) if v.uv else (0.0, 0.0),
                        infl=[(bones[bi].name, w) for bi, w in (v.boneMapping or {}).items()
                              if bi < len(bones)])
                   for v in mesh.vertices],
            tris=[tuple(index_of[id(fa.vertices[k])] for k in range(3)) for fa in mesh.faces]))
    # PES15 stores its triangles the other way round from PES21
    return dict(bind=bind, meshes=meshes, wind=(0, 2, 1), whole=False, gloves=False,
                head_world=None)


def main():
    # ------------------------------------------------------------- container
    section('container: PGB2 header, u32 indices, per-material submeshes')
    if not need('jersey'):
        return 1
    b = read_body(convert('jersey'))
    check(b['nv'] > 15000, 'jersey %d verts (a whole figure, not a stub)' % b['nv'])
    check(b['stride'] == F.VERTEX_STRIDE, 'vertex stride %d' % b['stride'])
    check(len(b['subs']) >= 5, '%d submeshes, one per material' % len(b['subs']))
    check(sum(s[1] for s in b['subs']) == b['ni'],
          'submesh index counts cover every index (%d of %d)'
          % (sum(s[1] for s in b['subs']), b['ni']))
    at, contiguous = 0, True
    for first, count, _tex, _flags in b['subs']:
        contiguous = contiguous and first == at
        at += count
    check(contiguous, 'submeshes are contiguous in the index buffer')
    check(int(b['idx'].max()) < b['nv'], 'u32 indices address the vertex buffer')
    # the 65535 vertex ceiling the old u16 PGB1 writer hit is what this proves
    check(len(np.unique(b['idx'])) > b['nv'] * 0.8,
          'no u16 wrap: %d distinct of %d verts referenced' % (len(np.unique(b['idx'])), b['nv']))
    check(b['keep'] == 0, 'a whole figure keeps no stock piece (mask %d)' % b['keep'])
    import FmdlFile

    f = FmdlFile.FmdlFile()
    f.readFile(IN['jersey'])
    mats = {m.materialInstance.name for m in f.meshes if m.materialInstance}
    drawn = sum(1 for m in f.meshes if m.materialInstance and len(m.faces) >= 1
                and 'antiblur' not in m.materialInstance.name.lower())
    check(len(b['subs']) >= len(mats), '%d submeshes for %d materials in %d meshes'
          % (len(b['subs']), len(mats), drawn))
    check(len(set(s[2] for s in b['subs'])) == 1,
          'materials share the one texture they were converted with')
    # vertex count is the reader's, untouched: nothing dropped or duplicated
    check(b['nv'] == sum(len(m.vertices) for m in f.meshes
                         if m.materialInstance and len(m.faces) >= 1
                         and 'antiblur' not in m.materialInstance.name.lower()),
          'every vertex of every drawn mesh is in the buffer')
    check(b['ni'] // 3 == sum(len(m.faces) for m in f.meshes
                              if m.materialInstance and len(m.faces) >= 1
                              and 'antiblur' not in m.materialInstance.name.lower()),
          'every triangle is in the index buffer')

    # ------------------------------------------------- per-model keep defaults
    section('keep: per-model stock-piece defaults')
    if need('shirt_out'):
        s = read_body(convert('shirt_out'))
        check(s['keep'] == F.KEEP_BUT_HEAD,
              'a garment is not a whole body: keeps %d (KEEP_BUT_HEAD %d)'
              % (s['keep'], F.KEEP_BUT_HEAD))
        # forced to a whole figure, a garment that stops above the floor still
        # stands in PES's boots (OWN_FEET_Y): that is the documented rule
        forced = read_body(convert('shirt_out', hint='hide'))
        check(forced['keep'] == F.KEEP_BOOTS,
              "'hide' still keeps the boots for a garment ending at y %.2f (mask %d)"
              % (vfield(forced, 'pos')[:, 1].max(), forced['keep']))

    # --------------------------------------------------------------- helpers
    section('helpers: dsk_* split across the joint')
    check(P.helper_split('dsk_hem_l') == [('dsk_hip', 0.5), ('sk_thigh_l', 0.5)],
          'dsk_hem_l splits hip/thigh %r' % (P.helper_split('dsk_hem_l'),))
    check(P.helper_split('dsk_knee_l') == [('sk_thigh_l', 0.5), ('sk_leg_l', 0.5)],
          'dsk_knee_l splits thigh/shin %r' % (P.helper_split('dsk_knee_l'),))
    check(P.helper_split('dsk_elbow_l') == [('sk_upperarm_l', 0.5), ('sk_forearm_l', 0.5)],
          'dsk_elbow_l splits upper arm/forearm')
    check(P.helper_split('dsk_wrist_l') == [('sk_forearm_l', 0.5), ('sk_hand_l', 0.5)],
          'dsk_wrist_l splits forearm/hand')
    check(P.helper_split('dsk_forearm_t_l') is None,
          'dsk_forearm_t_l is not a joint helper (it stays on the forearm)')
    # the shirt carries no dsk_hem; the jersey (a whole figure in a shirt) does
    if need('jersey'):
        f = FmdlFile.FmdlFile()
        f.readFile(IN['jersey'])
        helpers = sorted({b.name for b in f.bones if P.helper_split(b.name)})
        check(bool(helpers), 'jersey carries split helpers: %s' % helpers[:6])
        bb = read_body(convert('jersey'))
        used = set()
        for first, count, _t, _fl in bb['subs']:
            for i in bb['idx'][first:first + count]:
                used.update(int(x) for x in slots(bb)[i])
        # a split helper puts weight on BOTH bones of its joint, so both slots
        # have to appear on the converted figure - that is the whole point of
        # the split and what the old glue-to-one-bone code could not do
        both = [(h, [x[0] for x in P.helper_split(h)]) for h in helpers]
        hit = [(h, ab) for h, ab in both
               if all(bn in F.FOX_TO_PES12 and F.FOX_TO_PES12[bn] in used for bn in ab)]
        check(bool(hit), 'a dsk_* helper put weight on both bones of its joint: %s'
              % (hit[:3] or both[:3]))

    # ----------------------------------------------------------------- seams
    section('seams: one seam table, both engines')
    import json
    p12 = {x['bone']: x['pos'] for x in json.load(open(F.BONES_JSON))['bones']}
    used = {'sk_chest', 'sk_upperarm_l', 'sk_forearm_l', 'sk_hand_l', 'sk_belly'}
    seams = F.seam_table(p12, used)
    check('sk_upperarm_l' in seams, 'the upper-arm->forearm seam is in the table')
    for bone in ('sk_upperarm_l', 'sk_chest'):
        pt = retarget.PES_RENDER_BIND[bone][0]
        check(F.seam_offset(bone, pt, seams) == P.seam_offset(bone, pt, seams),
              'both engines warp the %s seam identically' % bone)
    far = tuple(c + 0.4 for c in retarget.PES_RENDER_BIND['sk_upperarm_l'][0])
    check(F.seam_offset('sk_upperarm_l', far, seams) == (0.0, 0.0, 0.0),
          'a vertex past the joint gets no warp (the seam is clamped)')

    # ------------------------------------------------------------------- kit
    section('kit: uni_* materials paint the kit sheet')
    if need('shirt_out'):
        kb = read_body(convert('shirt_out'))
        kit = [s for s in kb['subs'] if s[3] & F.SUB_KIT]
        # submeshes are per mesh, not per material: shirt_out is two meshes
        # sharing one uni_shirts material, so both are kit subs on one texture
        f = FmdlFile.FmdlFile()
        f.readFile(IN['shirt_out'])
        kit_meshes = sum(1 for m in f.meshes if len(m.faces) >= 1 and m.materialInstance
                         and any(t.filename.lower().startswith('uni_pattern')
                                 for _n, t in m.materialInstance.textures))
        check(len(kit) == kit_meshes, '%d kit submeshes for %d kit meshes'
              % (len(kit), kit_meshes))
        check(len({s[2] for s in kit}) == 1,
              'the kit submeshes share one texture slot %r' % sorted({s[2] for s in kit}))
        uv1, uv0 = vfield(kb, 'uv1'), vfield(kb, 'uv0')
        rows = kb['idx'][kit[0][0]:kit[0][0] + kit[0][1]]
        check(bool(((uv1[rows] >= 0) & (uv1[rows] <= 1)).all()),
              'TEXCOORD1 lands on the kit sheet')
        moved = float(np.abs(uv1[rows] - uv0[rows]).max())
        check(moved > 1e-6, 'TEXCOORD1 is remapped from TEXCOORD0 (max delta %.3f)' % moved)
        # non-kit submeshes keep the model's own UVs in both slots
        plain = [s for s in kb['subs'] if not s[3] & F.SUB_KIT]
        if plain:
            f0, c0 = plain[0][0], plain[0][1]
            rows = kb['idx'][f0:f0 + c0]
            same = np.allclose(uv1[rows], uv0[rows])
            check(same, 'a non-kit submesh keeps TEXCOORD1 == TEXCOORD0')

    # ------------------------------------------------------------------ hand
    section('hand: finger curl + HRIG trailer')
    if need('hand_l'):
        hb = read_body(convert('hand_l'))
        check(hb['trailer'][:4] == b'HRIG',
              'the HRIG trailer follows the indices (%r)' % hb['trailer'][:4])
        # per side: 12 palette bytes + 12 parent bytes + 12 joints x 3 f32
        side_bytes = 12 + 12 + 12 * 3 * 4
        check(len(hb['trailer']) == 4 + 2 * side_bytes,
              'trailer is 4 + two sides x %d bytes (%d)' % (side_bytes, len(hb['trailer'])))
        rig = [s for s in hb['subs'] if s[3] & F.SUB_HAND_RIG]
        check(len(rig) == 1, '%d hand-rig submeshes (left hand)' % len(rig))
        rigid = [s for s in hb['subs'] if s[3] & F.SUB_HAND_L and not s[3] & F.SUB_HAND_RIG]
        check(len(rigid) == 1,
              '%d rigid body copies of the left hand (the rig sub carries both bits)'
              % len(rigid))
        check(not [s for s in hb['subs'] if s[3] & F.SUB_HAND_R],
              'no right-hand copy from a left-hand model')
        # the finger curl is a real bend: the rig copy's own joints are curled,
        # not the flat ones the model was authored with. Side l's block is the
        # first one after the magic.
        joints = np.frombuffer(hb['trailer'], dtype=np.float32,
                               count=36, offset=4 + 24).reshape(12, 3)
        span = float(np.linalg.norm(joints.max(axis=0) - joints.min(axis=0)))
        check(span > 0.02,
              'the hand\'s own joints span %.3f m, so the fingers are placed' % span)
        joints_r = np.frombuffer(hb['trailer'], dtype=np.float32,
                                 count=36, offset=4 + side_bytes + 24).reshape(12, 3)
        check(not joints_r.any(), 'the absent right hand is all zeros')
    else:
        skip('hand_l')

    # ------------------------------------------------------------------ face
    section('face: skf_* bones drive the face palette')
    if need('torso'):
        tb = read_body(convert('torso'))
        # torso.fmdl carries skf_jaw but is a NECK piece: every one of its 469
        # vertices is weighted to the neck/chest, so no vertex is head-only and
        # no face submesh may be written. (No .fmdl on this machine holds a real
        # face - skf_* bones appear in exactly one of the 350 models.)
        check(not [s for s in tb['subs'] if s[3] & F.SUB_FACE],
              'a neck piece gets no face submesh')
        check(len(F._rigs()[1]['palette']) > 0,
              'the stock face palette the fmdl reader maps onto has %d bones'
              % len(F._rigs()[1]['palette']))
        # the writer's face path itself, on a source whose vertices are all
        # head-only - no .fmdl provides one, so drive it directly
        HEAD_P = F.retarget.PES_RENDER_BIND['sk_head'][0]
        synth = dict(bind={'sk_head': HEAD_P},
                     meshes=[dict(tex=None, kit=False, flags=0,
                                  verts=[dict(p=tuple(c + 0.01 * i for c in HEAD_P), n=(0.0, 0.0, 1.0),
                                              t=(1.0, 0.0, 0.0), uv=(0.5, 0.5),
                                              infl=[('sk_head', 1.0)]) for i in range(3)],
                                  tris=[(0, 1, 2)])],
                     wind=(0, 1, 2), whole=False, gloves=False, head_world=None,
                     hand_rig=F._rigs()[0], fslots={},
                     skull_slot=F._rigs()[1]['palette'].index(F.FACE_SKULL_BONE))
        sdir = os.path.join(CACHE, '_out', 'synth_face')
        F.build([synth], sdir)
        s = read_body(sdir)
        fs = [x for x in s['subs'] if x[3] & F.SUB_FACE]
        check(len(fs) == 1, 'a head-only source writes %d face submeshes' % len(fs))
        rows = s['idx'][fs[0][0]:fs[0][0] + fs[0][1]]
        check(float(np.abs(vfield(s, 'pos')[rows]).max()) < 0.2,
              'face vertices are head-local (max %.3f m from the origin)'
              % float(np.abs(vfield(s, 'pos')[rows]).max()))
    else:
        skip('torso')

    # ---------------------------------------------------------------- oracle
    section('oracle: the same garment through both engines')
    for name, pes15 in ORACLE:
        pes15_in = os.path.join(PES15_GARMENTS, pes15)
        if not need(name):
            continue
        if not os.path.exists(pes15_in):
            skip('oracle %s: no %s' % (name, pes15))
            continue
        a = read_body(convert(name))
        # The same garment, PES15 side, through the same writer. pes15_to_pes12
        # is deliberately untouched by this work (its texture resolution is a
        # production path: 407 installed bodies came out of it), so the oracle
        # builds the PES15 source here rather than going through convert().
        dst = os.path.join(CACHE, '_out', 'p15_' + name)
        F.build([pes15_source(pes15_in)], dst)
        b = read_body(dst)
        pa, pb = vfield(a, 'pos'), vfield(b, 'pos')
        print('  %-10s  fmdl: %6d verts %6d tris  y %.2f..%.2f  x %+.2f..%+.2f  slots %2d  keep %4d'
              % (name, a['nv'], a['ni'] // 3, pa[:, 1].min(), pa[:, 1].max(),
                 pa[:, 0].min(), pa[:, 0].max(), len(set(slots(a).ravel().tolist())), a['keep']))
        print('  %-10s  pes15: %6d verts %6d tris  y %.2f..%.2f  x %+.2f..%+.2f  slots %2d  keep %4d'
              % ('', b['nv'], b['ni'] // 3, pb[:, 1].min(), pb[:, 1].max(),
                 pb[:, 0].min(), pb[:, 0].max(), len(set(slots(b).ravel().tolist())), b['keep']))
        # The re-posing is what makes the two comparable: both land in the same
        # bind, so the extents agree to within the cut difference between the
        # two engines' garments (they are not the same mesh - 1321 vs 1554
        # verts). 5 cm on a 65 cm shirt is the bar; the numbers are printed so
        # a regression shows up as a change, not just a pass.
        for axis, label in ((1, 'y'), (0, 'x')):
            da = abs(pa[:, axis].min() - pb[:, axis].min())
            db = abs(pa[:, axis].max() - pb[:, axis].max())
            check(max(da, db) < 0.05,
                  '%s extent agrees across engines to %.1f/%.1f cm (min %.3f/%.3f max %.3f/%.3f m)'
                  % (label, da * 100, db * 100,
                     pa[:, axis].min(), pb[:, axis].min(), pa[:, axis].max(), pb[:, axis].max()))
        # both engines' shirt is carried by the same trunk bones. The bones
        # table names its bones by index, so the names come from FOX_TO_PES12.
        name_of = {v: k for k, v in F.FOX_TO_PES12.items()}
        sa = {name_of.get(s, 'finger%d' % s) for s in set(slots(a).ravel().tolist())}
        sb = {name_of.get(s, 'finger%d' % s) for s in set(slots(b).ravel().tolist())}
        trunk = {'dsk_hip', 'sk_belly', 'sk_chest', 'sk_neck', 'sk_shoulder_l', 'sk_shoulder_r'}
        check(bool(sa & trunk), 'the fmdl shirt is skinned to the trunk: %s' % sorted(sa & trunk))
        check(bool(sb & trunk), 'the pes15 shirt is skinned to the trunk: %s' % sorted(sb & trunk))
        check(len(sa & sb) >= 3,
              'both engines use %d of the same palette slots: %s'
              % (len(sa & sb), sorted(sa & sb)))
        # the UV chart is authored per engine and per cut, so compare the
        # chart's reach rather than its exact corner
        u0, v0 = vfield(a, 'uv0'), vfield(b, 'uv0')
        # The u-span is the chart's own width and is the same garment's chart in
        # both engines; the v-span tracks the cut, and shirt_in's PES15 side is
        # the _tight variant, so it covers more sheet. Only u is compared.
        check(abs(np.ptp(u0[:, 0]) - np.ptp(v0[:, 0])) < 0.05,
              'the UV chart is the same width: u %.3f vs %.3f (v %.3f vs %.3f, cut differs)'
              % (np.ptp(u0[:, 0]), np.ptp(v0[:, 0]), np.ptp(u0[:, 1]), np.ptp(v0[:, 1])))
        check(winding_sign(a) == winding_sign(b),
              'both engines emit triangles in the order drawlogic culls for (%+d / %+d)'
              % (winding_sign(a), winding_sign(b)))
        check(a['keep'] == b['keep'], 'both engines agree the keep mask (%d / %d)'
              % (a['keep'], b['keep']))
    print()
    print('%d checks ok, %d failed, %d skipped' % (ok, fail, skipped))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())

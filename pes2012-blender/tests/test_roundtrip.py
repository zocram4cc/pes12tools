"""PGB2 custom bodies, pure python (KTMDL models: test_pure.py).

- PGB2: a built custom body.bin parse-compare (PES12_BODY_DIR, default
  <repo>/dllprobe/custom/p272101).
- pack_body: export path on it, influence+position parse-compare.
- skin order: the converter (pes2012-tools fmdl_to_pes12) and pgb2 agree
  with PES2012's skin VS.
Run: python3 tests/test_roundtrip.py [repo]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, '..', 'pes2012_starter_pack')
# repo root: argv[1], else PES12_REPO env, else the checkout (tests/ -> dist/ -> repo = 3 up)
REPO = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('PES12_REPO')
REPO = os.path.abspath(REPO or os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, PACK)
# fmdl_to_pes12: the sibling pes2012-tools package, else the repo's tools/
for _t in (os.path.join(HERE, '..', '..', 'pes2012-tools'), os.path.join(REPO, 'tools')):
    if os.path.exists(os.path.join(_t, 'fmdl_to_pes12.py')):
        sys.path.insert(0, _t)
        break

import pgb2  # noqa: E402


P272101 = os.environ.get('PES12_BODY_DIR', os.path.join(REPO, 'dllprobe', 'custom', 'p272101'))


def test_pgb2():
    raw = open(os.path.join(P272101, 'body.bin'), 'rb').read()
    parsed, rebuilt = pgb2.round_trip(raw)
    assert rebuilt == raw, 'PGB2 parse-compare failed'
    # bodies now ship DDS textures (tools/pes15_to_pes12.py write_tex copies
    # the packs' own DXT); PGT1 stays a format drawlogic loads, so its codec
    # round-trips on a synthetic 4x2 sheet (full mip chain down to 1x1)
    mips = [bytes(range(4 * 2 * 4)), bytes(2 * 1 * 4), bytes(1 * 1 * 4)]
    t = pgb2.build_tex(4, 2, mips)
    q = pgb2.parse_tex(t)
    assert pgb2.build_tex(q['w'], q['h'], q['mips']) == t
    print('pgb2 p272101: %d verts %d tris %d subs keeps %s OK'
          % (len(parsed['verts']), len(parsed['idx']) // 3, len(parsed['subs']),
             ' '.join(n for i, n in enumerate(pgb2.PIECES) if parsed['keep'] >> i & 1) or 'nothing'))


def _influence_map(parsed):
    face_verts = {i for s in parsed['subs'] if s['flags'] & pgb2.SUB_FACE
                  for i in parsed['idx'][s['first']:s['first'] + s['count']]}
    return face_verts


def test_pack_body():
    """pack_body (the export path minus Blender) vs p272101, compared as
    influence maps + positions: slot order may flip on exact weight ties."""
    raw = open(os.path.join(P272101, 'body.bin'), 'rb').read()
    parsed = pgb2.parse(raw)
    face_verts = _influence_map(parsed)
    vert_data = {}
    for vi, v in enumerate(parsed['verts']):
        infl = pgb2.slots_to_influences(v['slots'], v['weights'])
        p = v['pos']
        if vi in face_verts:
            p = (p[0] + pgb2.HEAD_POS[0], p[1] + pgb2.HEAD_POS[1],
                 p[2] + pgb2.HEAD_POS[2])
        # shared verts (face and body submeshes cite the same vertex) keep
        # both maps; pack_body picks by submesh
        vert_data[vi] = dict(pos=p, nrm=v['nrm'], tan=v['tan'], bin=v['bin'],
                             uv0=v['uv0'], uv1=v['uv1'],
                             infl=dict(infl),
                             face=dict(infl) if vi in face_verts else {})
    tris_by_mat, mat_flags, mat_tex, mat_face = {}, {}, {}, {}
    for mi, s in enumerate(parsed['subs']):
        tris_by_mat[mi] = [tuple(parsed['idx'][k:k + 3])
                           for k in range(s['first'], s['first'] + s['count'], 3)]
        mat_flags[mi], mat_tex[mi] = s['flags'], s['tex']
        mat_face[mi] = bool(s['flags'] & pgb2.SUB_FACE)
    out = pgb2.pack_body(tris_by_mat, vert_data, mat_flags, mat_tex,
                         mat_face, parsed['keep'])
    check = pgb2.parse(out)
    assert len(check['subs']) == len(parsed['subs']) and len(check['subs']) > 0
    assert [s['flags'] for s in check['subs']] == [s['flags'] for s in parsed['subs']]
    assert [s['tex'] for s in check['subs']] == [s['tex'] for s in parsed['subs']]
    # pack_body emits Blender-style split verts (one per triangle corner)
    # in blended-last submesh order, so compare per output submesh range
    # against the source vert each output index cites.
    assert (len(check['verts']), len(check['idx'])) == (
        len(parsed['idx']), len(parsed['idx']))
    # output submesh mi corresponds to input submesh order[mi]
    order = sorted(range(len(parsed['subs'])),
                   key=lambda mi: bool(parsed['subs'][mi]['flags'] & pgb2.SUB_BLEND))
    worst = 0.0
    for mi, s in enumerate(check['subs']):
        src = parsed['subs'][order[mi]]
        assert (s['flags'], s['tex'], s['count']) == (src['flags'], src['tex'], src['count'])
        for ni in range(s['first'], s['first'] + s['count']):
            vi = parsed['idx'][src['first'] + (ni - s['first'])]
            a, b = parsed['verts'][vi], check['verts'][ni]
            assert check['idx'][ni] == ni
            ia = pgb2.slots_to_influences(a['slots'], a['weights'])
            ib = pgb2.slots_to_influences(b['slots'], b['weights'])
            # influences_to_slots drops sub-MIN_WEIGHT authoring noise
            # (tools/fmdl_to_pes12.py), so compare above that floor.
            fa = {t: w for t, w in ia.items() if w >= pgb2.MIN_WEIGHT}
            fb = {t: w for t, w in ib.items() if w >= pgb2.MIN_WEIGHT}
            for t in set(fa) | set(fb):
                worst = max(worst, abs(fa.get(t, 0.0) - fb.get(t, 0.0)))
            worst = max(worst, max(abs(x - y) for x, y in zip(a['pos'], b['pos'])))
    assert worst < 2e-6, worst
    print('pack_body p272101: %d split verts influence+pos match (worst %.2g) OK'
          % (len(check['verts']), worst))


def _stad_attrs(v):
    return dict(pos=tuple(v['POSITION']), nrm=tuple(v['NORMAL']),
                uv=[tuple(v['TEXCOORD0']), tuple(v['TEXCOORD1'])])

def _tri_normal_dot(packet, tri):
    """Face-normal / vertex-normal agreement (+/-/0) for one triangle."""
    a, b, c = [packet['vertices'][v]['POSITION'] for v in tri]
    ab = [b[k] - a[k] for k in range(3)]
    ac = [c[k] - a[k] for k in range(3)]
    fn = [ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
          ab[0] * ac[1] - ab[1] * ac[0]]
    vn = [0.0] * 3
    for v in tri:
        for k in range(3):
            vn[k] += packet['vertices'][v]['NORMAL'][k]
    return sum(f * w for f, w in zip(fn, vn))


def test_skin_order():
    """PES2012's skin VS: blend index 0 takes 1 - (w0 + w1 + w2), index k + 1
    takes w[k]. The converter (fmdl_to_pes12.skin_pack) and the add-on
    (pgb2) must both produce weights the shader reads back as authored."""
    import fmdl_to_pes12 as F
    for infl in ([(8, 1.0)], [(8, 0.76), (7, 0.24)], [(3, 0.5), (1, 0.3), (0, 0.2)],
                 [(9, 0.4), (8, 0.3), (7, 0.2), (6, 0.1)]):
        ws, slots = F.skin_pack(infl)
        gpu = {}
        for s, w in zip(slots, [1.0 - sum(ws)] + list(ws)):
            gpu[s] = gpu.get(s, 0.0) + w
        want = dict(infl)
        assert all(abs(gpu.get(s, 0.0) - w) < 1e-6 for s, w in want.items()), (infl, slots, ws)
        assert all(s in want for s, w in gpu.items() if w > 1e-6), (infl, slots, ws)
        a_slots, a_ws = pgb2.influences_to_slots(want)
        assert (list(a_slots), list(a_ws)) == (slots, ws), 'pgb2 and skin_pack disagree'
        back = pgb2.slots_to_influences(slots, ws)
        assert all(abs(back[s] - w) < 1e-6 for s, w in want.items())
    print('skin order: converter and add-on match the skin VS OK')


if __name__ == '__main__':
    test_pgb2()
    test_skin_order()
    test_pack_body()
    print('PGB2 TESTS PASS')

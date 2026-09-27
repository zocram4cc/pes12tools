"""Acceptance: pure-python round-trips (no Blender needed).

- ball: stock dt0b #11 BIN -> split -> packer rebuild of packet 0 from
  parsed attrs -> build -> join -> re-parse, vertex count matches.
- generic: dt07 #1 rows split/join round-trip (compact header preserved).
- PGB2: dllprobe/custom/p272101 body.bin + body_0.tex parse-compare.
- pack_body: export path on p272101, influence+position parse-compare.
Run: python3 tests/test_roundtrip.py (from dist/pes2012-blender/).
"""
import importlib.util
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, '..', 'pes2012_starter_pack')
# repo root: argv[1], else PES12_REPO env, else the checkout (tests/ -> dist/ -> repo = 3 up)
REPO = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('PES12_REPO')
REPO = os.path.abspath(REPO or os.path.join(HERE, '..', '..', '..'))
BLENDER_TEST = os.environ.get('PES12_BLENDER_TEST',
                              os.path.join(REPO, 'dllprobe', 'blender_test'))
sys.path.insert(0, PACK)
sys.path.insert(0, os.path.join(REPO, 'tools'))

import afs  # noqa: E402
import binwrap  # noqa: E402
import ktpack  # noqa: E402
import ktmdl_write as W  # noqa: E402
import pgb2  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'vktmdl', os.path.join(PACK, 'pes_ktmdl_importer', 'ktmdl.py'))
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)

DT0B = os.path.join(REPO, 'Pro Evolution Soccer 2012', 'img', 'dt0b.img')
DT07 = os.path.join(REPO, 'Pro Evolution Soccer 2012', 'img', 'dt07.img')
P272101 = os.path.join(REPO, 'dllprobe', 'custom', 'p272101')


def stock_body(img, index):
    raw = afs.read(img, index)
    assert raw[3:8] == b'WESYS', 'entry %d not WESYS' % index
    return raw[:8], zlib.decompress(raw[16:])


def test_ball():
    tag, body = stock_body(DT0B, 11)
    kt, tex = binwrap.split_ball(body)
    m = V.parse_bytes(kt, 'ball11')
    assert len(m['packets']) == 1 and m['packets'][0]['primType'] == 0
    p = m['packets'][0]
    nv = len(p['vertices'])
    attrs = [dict(pos=tuple(v['POSITION']), nrm=tuple(v['NORMAL']),
                  tan=tuple(v['TANGENT']), bin=tuple(v['BINORMAL']),
                  uv=[tuple(v['TEXCOORD0']), tuple(v['TEXCOORD1'])])
             for v in p['vertices']]
    row = ktpack.mesh_row(kt, 0, attrs)  # template topology
    new_kt = W.build(kt, [row])
    out_body = binwrap.join_ball(new_kt, tex)
    m2 = V.parse_bytes(new_kt, 'ball11-re')
    assert len(m2['packets'][0]['vertices']) == nv, 'vertex count changed'
    os.makedirs(BLENDER_TEST, exist_ok=True)
    open(os.path.join(BLENDER_TEST, 'ball11_stock.bin'),
         'wb').write(afs.read(DT0B, 11))
    open(os.path.join(BLENDER_TEST, 'ball11_rebuilt.bin'),
         'wb').write(binwrap.wesys_wrap(out_body))
    print('ball: %d verts round-trip OK' % nv)


def test_ball_edited_topology():
    """New topology: first 100 triangles of the ball rebuild cleanly."""
    _tag, body = stock_body(DT0B, 11)
    kt, tex = binwrap.split_ball(body)
    m = V.parse_bytes(kt, 'ball11')
    p = m['packets'][0]
    tris = [tuple(f) for f in p['triangles'][:100]]
    keep = sorted({v for tri in tris for v in tri})
    remap = {v: k for k, v in enumerate(keep)}
    attrs = [dict(pos=tuple(p['vertices'][v]['POSITION']),
                  nrm=tuple(p['vertices'][v]['NORMAL']),
                  tan=tuple(p['vertices'][v]['TANGENT']),
                  bin=tuple(p['vertices'][v]['BINORMAL']),
                  uv=[tuple(p['vertices'][v]['TEXCOORD0']),
                      tuple(p['vertices'][v]['TEXCOORD1'])]) for v in keep]
    new_kt = W.build(kt, [ktpack.mesh_row(
        kt, 0, attrs, [[remap[v] for v in tri] for tri in tris])])
    m2 = V.parse_bytes(new_kt, 'ball11-edit')
    assert len(m2['packets'][0]['vertices']) == len(keep)
    print('ball edited-topology: %d verts / %d tris OK' % (len(keep), len(tris)))


def test_generic():
    _tag, body = stock_body(DT07, 1)
    kind, blocks = binwrap.split_body(body)
    assert kind == 'generic' and binwrap.find_ktmdl(blocks)
    n, _, hs = struct.unpack_from('<III', body)
    assert binwrap.join_generic(blocks, compact=(hs == 12 + 12 * n)) == body
    print('generic dt07#1: %d blocks round-trip OK' % len(blocks))


def test_pgb2():
    raw = open(os.path.join(P272101, 'body.bin'), 'rb').read()
    parsed, rebuilt = pgb2.round_trip(raw)
    assert rebuilt == raw, 'PGB2 parse-compare failed'
    tpath = os.path.join(P272101, 'body_0.tex')
    t = open(tpath, 'rb').read()
    q = pgb2.parse_tex(t)
    assert pgb2.build_tex(q['w'], q['h'], q['mips']) == t
    print('pgb2 p272101: %d verts %d tris %d subs mode %s OK'
          % (len(parsed['verts']), len(parsed['idx']) // 3,
             len(parsed['subs']), pgb2.MODES[parsed['mode']]))


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
        vert_data[vi] = dict(pos=p, nrm=v['nrm'], tan=v['tan'], bin=v['bin'],
                             uv0=v['uv0'], uv1=v['uv1'],
                             infl={} if vi in face_verts else dict(infl),
                             face=dict(infl) if vi in face_verts else {})
    tris_by_mat, mat_flags, mat_tex, mat_face = {}, {}, {}, {}
    for mi, s in enumerate(parsed['subs']):
        tris_by_mat[mi] = [tuple(parsed['idx'][k:k + 3])
                           for k in range(s['first'], s['first'] + s['count'], 3)]
        mat_flags[mi], mat_tex[mi] = s['flags'], s['tex']
        mat_face[mi] = bool(s['flags'] & pgb2.SUB_FACE)
    out = pgb2.pack_body(tris_by_mat, vert_data, mat_flags, mat_tex,
                         mat_face, parsed['mode'])
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
        for s in set(ia) | set(ib):
            worst = max(worst, abs(ia.get(s, 0.0) - ib.get(s, 0.0)))
        worst = max(worst, max(abs(x - y) for x, y in zip(a['pos'], b['pos'])))
    assert worst < 2e-6, worst
    print('pack_body p272101: %d split verts influence+pos match (worst %.2g) OK'
          % (len(check['verts']), worst))


if __name__ == '__main__':
    test_ball()
    test_ball_edited_topology()
    test_generic()
    test_pgb2()
    test_pack_body()
    print('ALL ROUND-TRIPS PASS')

"""PES2012 rig tables, extracted from the user's own game files.

    python3 pes12_rig.py <game dir>   # -> rig/face_rig.json + rig/body349b2_bones.json

Face rig = the stock face model's 29-bone table (dt0c.img BIN FACE_ENTRY,
block 0): joints in head-local space (origin = the head joint; bone 0 is the
skull and moves exactly with the body's head bone, 27-09 grab) and the face
packet's 27-slot bone palette. drawlogic draws a custom model's face part
with the palette the game uploads for that packet, so the converter needs
slot numbers, not bones.

Officials map (runtime/officialmap.h, compiled into drawlogic): the
referee/linesman full-detail model (dt09 #349 block 1) against the custom
rig's slot order, plus the size of the vertex buffer the game builds for it
(drawlogic recognises an official's draws by that buffer).

Parsed with pes2008-2013-tools' KTMDL reader (moth1995, GPL-3.0).
"""
import importlib.util
import json
import os
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import afs  # noqa: E402

GAME = os.environ.get('PES12_GAME', os.path.join(HERE, 'game'))
OUT_DIR = os.environ.get('PES12_RIG', os.path.join(HERE, 'rig'))
FACE_JSON = os.path.join(OUT_DIR, 'face_rig.json')
FACE_IMG, FACE_ENTRY = 'dt0c.img', 132     # a stock face BIN; every face shares the rig (27-09: #132..#1830)
FACE_PACKET = 3                             # the 669-vertex face mesh (drawn 669/1605/88 in game)
KTMDL_READER = os.environ.get('PES12_KTMDL_READER',
                                 os.path.join(HERE, 'vendor', 'ktmdl_moth.py'))


def ktmdl_reader():
    spec = importlib.util.spec_from_file_location('ktmdl_moth', KTMDL_READER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def face_rig(game=GAME):
    K = ktmdl_reader()
    raw = afs.read(os.path.join(game, 'img', FACE_IMG), FACE_ENTRY)
    data = zlib.decompress(raw[16:]) if raw[3:8] == b'WESYS' else raw
    model = K.parse_bytes(data[data.find(b'KTMDL'):])
    joints = [np.array(b['matrix'], float).reshape(4, 4)[3, :3].round(5).tolist() for b in model['bones']]
    packet = model['packets'][FACE_PACKET]
    pal = packet['bonePalette']
    # The stock face's own skinned vertices (head-local, per-vertex weights
    # on rig bones): pes15_to_pes12 transfers them onto custom faces.
    verts = []
    for v in packet['vertices']:
        ex = list(v.get('BLENDWEIGHT', []))
        ws = [1.0 - sum(ex)] + ex        # skin VS: index 0 takes the remainder
        w = {}
        for k, x in zip(v['BLENDINDICES'], ws):
            if x > 0:
                w[pal[k]] = round(w.get(pal[k], 0.0) + x, 4)
        verts.append([[round(c, 5) for c in v['POSITION'][:3]], w])
    return {'joints': joints,
            'parents': [b['parentIndex'] for b in model['bones']],
            'palette': pal,
            'verts': verts}


def load_face_rig(path=None):
    path = path or FACE_JSON
    if not os.path.exists(path):
        raise SystemExit('no %s: run pes12_rig.py <game dir> first' % path)
    return json.load(open(path))


BODY_IMG, BODY_ENTRY, BODY_BLOCK = 'dt09.img', 349, 2    # the 19-bone in-match body (24-09)
BODY_JSON = os.path.join(OUT_DIR, 'body349b2_bones.json')


def body_bones(game=GAME):
    """The 19 bones + their palette slots in dt09 #349 block 2: bone index,
    palette slot, parent, StrCode-like id, bind position (metres)."""
    K = ktmdl_reader()
    raw = afs.read(os.path.join(game, 'img', BODY_IMG), BODY_ENTRY)
    data = zlib.decompress(raw[16:]) if raw[3:8] == b'WESYS' else raw
    starts, at = [], 0
    while True:
        b = data.find(b'KTMDL', at)
        if b < 0:
            break
        starts.append(b)
        at = b + 1
    model = K.parse_bytes(data[starts[BODY_BLOCK]:])
    palette = model['packets'][0]['bonePalette']
    return {'palette': palette,
            'bones': [{'bone': i, 'slot': palette.index(i),
                       'parent': b['parentIndex'], 'id': b['nameIdHex'],
                       'pos': np.array(b['matrix'], float).reshape(4, 4)[3, :3].round(3).tolist()}
                      for i, b in enumerate(model['bones'])]}


def load_body_bones(path=None):
    """The 19 body bones this script extracted (BODY_JSON unless given)."""
    path = path or BODY_JSON
    if not os.path.exists(path):
        raise SystemExit('no %s: run pes12_rig.py <game dir> first' % path)
    return json.load(open(path))


OFFICIAL_BLOCK = 1                  # dt09 #349 block 1: the referee/linesman LOD0 model (01-10)
OFFICIAL_H = os.path.join(HERE, 'runtime', 'officialmap.h')   # rebuild runtime/ after regenerating
VB_ALIGN = 16                       # the game pads its model VB per draw to 16 bytes (01-10 probes)


def _body_block(game, block):
    K = ktmdl_reader()
    raw = afs.read(os.path.join(game, 'img', BODY_IMG), BODY_ENTRY)
    data = zlib.decompress(raw[16:]) if raw[3:8] == b'WESYS' else raw
    starts, at = [], 0
    while (b := data.find(b'KTMDL', at)) >= 0:
        starts.append(b)
        at = b + 1
    return K.parse_bytes(data[starts[block]:])


def official_map(game=GAME):
    """The officials' model (block 1) against the custom rig's slot order
    (block 2's palette): for each of our 19 slots, the official draw's
    palette slot holding the same bone; and the size of the vertex buffer
    the game builds for the model (its packets' vertices, 16-aligned each)."""
    off, body = _body_block(game, OFFICIAL_BLOCK), _body_block(game, BODY_BLOCK)
    if len(off['bones']) != len(body['bones']):
        raise SystemExit('official model has %d bones, body %d' % (len(off['bones']), len(body['bones'])))
    pals = {tuple(p['bonePalette']) for p in off['packets']}
    if len(pals) != 1:
        raise SystemExit('official packets disagree on the palette: %d palettes' % len(pals))
    pal_off, pal_body = list(pals.pop()), body['packets'][0]['bonePalette']
    vb = vb_band(off)
    # Officials have no face draw: their custom face parts are drawn rigidly on
    # the head joint. The head = the highest leaf bone; its bind must be a pure
    # translation for "skin matrix x bind translation" to be the joint frame.
    parents = [b['parentIndex'] for b in body['bones']]
    binds = [np.array(b['matrix'], float).reshape(4, 4) for b in body['bones']]
    head = max((i for i in range(len(binds)) if i not in parents), key=lambda i: binds[i][3, 1])
    if not np.allclose(binds[head][:3, :3], np.eye(3), atol=1e-4):
        raise SystemExit('head bone %d bind has a rotation' % head)
    return {'slots': [pal_off.index(b) for b in pal_body], 'vb_bytes': vb,
            'head_slot': pal_body.index(head), 'head_bind': binds[head][3, :3].round(5).tolist(),
            'face_slots': len(face_rig(game)['palette'])}


CLOSE_BLOCK = 0    # dt09 #349 block 0: the officials' close-up model (57 bones, 01-10 grab)


def vb_band(model):
    """The size range of the vertex buffer the game builds for a model: its
    packets' vertices, plus up to VB_ALIGN-1 bytes of padding per packet (it
    merges and 16-aligns draws; measured 291392 for block 1, 593264 for
    block 0, both inside). -> (lo, hi), lo <= size < hi."""
    raw = sum(p['vertexDescriptor']['count'] * p['vertexDescriptor']['stride'] for p in model['packets'])
    return raw, raw + VB_ALIGN * len(model['packets'])


def close_map(game=GAME):
    """The close-up model against the custom rig: its packets use two
    palettes, neither holding all 19 body bones. For each of our slots: which
    palette group carries the bone, and its slot there; plus each group's
    first byte in the model's vertex buffer (16-aligned packets in order) and
    the buffer's size. Bones are matched by id (nameIdHex)."""
    close, body = _body_block(game, CLOSE_BLOCK), _body_block(game, BODY_BLOCK)
    ids = [b['nameIdHex'] for b in close['bones']]
    groups, starts, off = [], [], 0
    for p in close['packets']:
        pal = p['bonePalette']
        if not groups or pal != groups[-1]:
            if pal in groups:
                raise SystemExit('close-up palettes interleave: packet %d' % p['index'])
            groups.append(pal)
            starts.append(off)   # unpadded: a draw at or past it is in this group
        off += p['vertexDescriptor']['count'] * p['vertexDescriptor']['stride']
    src = []
    for b in body['packets'][0]['bonePalette']:
        want = body['bones'][b]['nameIdHex']
        for g, pal in enumerate(groups):
            hit = [k for k, j in enumerate(pal) if ids[j] == want]
            if hit:
                src.append((g, hit[0]))
                break
        else:
            raise SystemExit('body bone %s not in any close-up palette' % want)
    return {'src': src, 'starts': starts, 'vb_bytes': vb_band(close)}


def write_official_h(game=GAME):
    m = official_map(game)
    with open(OFFICIAL_H, 'w') as f:
        f.write('// generated by tools/pes12_rig.py from dt09 #349 blocks %d (officials) and %d (body)\n'
                % (OFFICIAL_BLOCK, BODY_BLOCK))
        f.write('// our slot s (block %d palette order) -> the official draw\'s palette slot\n' % BODY_BLOCK)
        f.write('static const int OFFICIAL_SLOT[%d] = {%s};\n' % (len(m['slots']), ', '.join(map(str, m['slots']))))
        f.write('// the officials\' vertex buffer size band: lo <= size < hi\n')
        f.write('static const UINT OFFICIAL_VB_LO = %d, OFFICIAL_VB_HI = %d;\n' % m['vb_bytes'])
        f.write('// rigid face: the head bone (our slot), its bind position (m), the face palette size\n')
        f.write('static const UINT HEAD_SLOT = %d;\n' % m['head_slot'])
        f.write('static const float HEAD_BIND[3] = {%s};\n' % ', '.join('%.5ff' % v for v in m['head_bind']))
        f.write('static const UINT FACE_SLOTS = %d;\n' % m['face_slots'])
        c = close_map(game)
        f.write('// close-up model (block %d): our slot -> (palette group, slot); groups start at\n'
                '// these VB bytes (the game draws a group\'s packets in VB order)\n' % CLOSE_BLOCK)
        f.write('static const UINT CLOSE_VB_LO = %d, CLOSE_VB_HI = %d;\n' % c['vb_bytes'])
        f.write('static const UINT CLOSE_GROUPS = %d;\n' % len(c['starts']))
        f.write('static const UINT CLOSE_GROUP_START[%d] = {%s};\n' % (len(c['starts']), ', '.join(map(str, c['starts']))))
        f.write('static const int CLOSE_SRC_GROUP[%d] = {%s};\n' % (len(c['src']), ', '.join(str(g) for g, _ in c['src'])))
        f.write('static const int CLOSE_SRC_SLOT[%d] = {%s};\n' % (len(c['src']), ', '.join(str(s) for _, s in c['src'])))
    return OFFICIAL_H


def write(game=GAME):
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(face_rig(game), open(FACE_JSON, 'w'), indent=1)
    print('wrote', FACE_JSON)
    json.dump(body_bones(game), open(BODY_JSON, 'w'), indent=1)
    print('wrote', BODY_JSON)
    print('wrote', write_official_h(game))


if __name__ == '__main__':
    write(sys.argv[1] if len(sys.argv) > 1 else GAME)

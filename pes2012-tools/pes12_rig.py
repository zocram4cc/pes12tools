"""PES2012 rig tables, extracted from the user's own game files.

    python3 pes12_rig.py <game dir>   # -> rig/face_rig.json + rig/body349b2_bones.json

Face rig = the stock face model's 29-bone table (dt0c.img BIN FACE_ENTRY,
block 0): joints in head-local space (origin = the head joint; bone 0 is the
skull and moves exactly with the body's head bone, 27-09 grab) and the face
packet's 27-slot bone palette. drawlogic draws a custom model's face part
with the palette the game uploads for that packet, so the converter needs
slot numbers, not bones.

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
    return {'joints': joints,
            'parents': [b['parentIndex'] for b in model['bones']],
            'palette': model['packets'][FACE_PACKET]['bonePalette']}


def load_face_rig(path=None):
    path = path or FACE_JSON
    if not os.path.exists(path):
        raise SystemExit('no %s: run pes12_rig.py <game dir> first' % path)
    return json.load(open(path))


BODY_IMG, BODY_ENTRY, BODY_BLOCK = 'dt09.img', 349, 2
BODY_JSON = os.path.join(OUT_DIR, 'body349b2_bones.json')


def body_bones(game=GAME):
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
                       'pos': [np.array(b['matrix'], float).reshape(4, 4)[3, :3].round(3).tolist()][0]}
                      for i, b in enumerate(model['bones'])]}


def load_body_bones(path=None):
    path = path or BODY_JSON
    if not os.path.exists(path):
        raise SystemExit('no %s: run pes12_rig.py <game dir> first' % path)
    return json.load(open(path))


def write(game=GAME):
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(face_rig(game), open(FACE_JSON, 'w'), indent=1)
    print('wrote', FACE_JSON)
    json.dump(body_bones(game), open(BODY_JSON, 'w'), indent=1)
    print('wrote', BODY_JSON)


if __name__ == '__main__':
    write(sys.argv[1] if len(sys.argv) > 1 else GAME)

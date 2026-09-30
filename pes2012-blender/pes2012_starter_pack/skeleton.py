"""Model skeletons, per block (pure python, no Blender).

Bone identity is the pair (block, node index): matching nameIds across
blocks are different deform bones that happen to share a hash half
(dt09 #349 node 0 face/head blocks vs pattern/texture blocks), and even
equal nameIds carry different binds (0.8 mm apart on face #164 node 24 -
more than float tolerance, less than any artist would notice). Merging by
bone id welded faces; merging by node index glued boots (dt07 #2960: block
1's thigh is not block 0's shinguard, 30-09). The skin binds each packet by
node index through its packet palette, inside one block: identity follows
the bind, not the hash.

A Skeleton is one block's bones. Names: body bones from
rig/body349b2_bones.json by node index; face rig bones face_NN in palette
order; anything else bone_NNN.
"""
import json
import os


class SBone:
    def __init__(self, bid, name, parent_id, bind):
        self.id = bid             # node index within the block
        self.name = name
        self.parent_id = parent_id
        self.bind = bind          # 16 floats, row-major 4x4


class Skeleton:
    def __init__(self, bones, block=''):
        self.bones = list(bones)
        self.by_id = {b.id: b for b in self.bones}
        self.block = block

    def __len__(self):
        return len(self.bones)


def _rig_names(rig_dir):
    """node index -> Fox name from the rig table, when present."""
    if not rig_dir:
        return {}
    path = os.path.join(rig_dir, 'body349b2_bones.json')
    if not os.path.exists(path):
        return {}
    table = json.load(open(path))
    bones = table['bones'] if isinstance(table, dict) else table
    out = {}
    for row in bones:
        bid = row.get('id', row.get('bone'))
        name = row.get('bone', row.get('name'))
        if isinstance(bid, int) and isinstance(name, str):
            out[bid] = name
    return out


def build_one(block_name, parsed, rig_dir=None, face_bones=()):
    """One block's parsed model dict -> Skeleton."""
    names = _rig_names(rig_dir)
    face = {b: 'face_%02d' % i for i, b in enumerate(face_bones)}
    out = []
    for bone in parsed['bones']:
        bid = int(bone['index'])
        bind = [float(x) for row in bone['matrix'] for x in row]
        parent = bone['parentIndex'] if bone['parentIndex'] >= 0 else -1
        out.append(SBone(bid, names.get(bid, face.get(bid, 'bone_%03d' % bid)), parent, bind))
    return Skeleton(out, block_name)


def build(models, rig_dir=None, face_bones=()):
    """[(block_name, parsed_model_dict)] -> [Skeleton], one per block."""
    return [build_one(name, parsed, rig_dir, face_bones) for name, parsed in models]

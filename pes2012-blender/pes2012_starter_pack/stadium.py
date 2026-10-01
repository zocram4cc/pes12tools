# SPDX-License-Identifier: GPL-3.0-or-later
"""Licensed stadium slots of the user's own PES2012, pure python.

A slot is the set of dt07 entries pes2012.exe loads for one licensed
stadium (pes2012-tools pes12_stadium.py, 10-stadiums.md): one record per
stadium, u32 fields (img << 24 | entry, img 7 = dt07):
  header  (standTexFirst, standTexLast, geometry, g+32, g+64, extra)
  6 x     (pitchTex, lightmapFirst, lightmapLast, props, x, sky, sky)
Geometry and props are KTMDL entries; stand textures, lightmaps, skies and
pitch art are WE00 texture entries. The models' texture rows resolve
against the WE00 blocks of every entry loaded with the stadium; a few ids
live in shared entries (pitch-side, adboards) and some in no entry at all.

slots(game) -> [Slot]; slot_entries(slot) -> {role: [entry]};
texture_index(game, slot) -> {texture id: (entry, block)}.
Overrides are read from and written to an afs2fs root (kitserver
4cc-dlc), never the stock .img.
"""
import os
import struct
from collections import namedtuple

import container  # noqa: E402  (flat: the package runs outside Blender in tests)
import textures  # noqa: E402

AFS_MAGIC = b'AFS\0'
DT07_TAG = 0x0700
HEADER_FIELDS, VARIANT_FIELDS, VARIANTS = 6, 7, 6
SLOT_STRIDE = 32           # header: geometry, geometry+32, geometry+64
PITCH_FIELD, LM_FIRST, LM_LAST, PROPS_FIELD, SKY_FIELD = 0, 1, 2, 3, 5
# Entries every licensed stadium loads besides its own (01-10: slot 30's
# rows 10671 / 11205 resolve in dt07 46-51, 14500 in the adboard strips).
SHARED_TEXTURE_ENTRIES = tuple(range(46, 52)) + tuple(range(2920, 2964))

Slot = namedtuple('Slot', 'number geometry stand props lightmaps skies pitches')


def _img(game, name='dt07.img'):
    return os.path.join(game, 'img', name)


def afs_entries(path):
    with open(path, 'rb') as f:
        head = f.read(8)
        if head[:4] != AFS_MAGIC:
            raise ValueError('%s: not AFS' % path)
        n = struct.unpack_from('<I', head, 4)[0]
        table = f.read(8 * n)
    return [struct.unpack_from('<II', table, 8 * i) for i in range(n)]


def override_path(root, entry, img='dt07.img'):
    return os.path.join(root, 'img', img, '%s_%d.bin' % (img.split('.')[0], entry))


def read_raw(game, entry, root=None, img='dt07.img'):
    """Entry bytes as the game will load them: the afs2fs override in root if
    there is one, else the stock entry."""
    p = override_path(root, entry, img) if root else None
    if p and os.path.exists(p):
        return open(p, 'rb').read()
    path = _img(game, img)
    off, size = afs_entries(path)[entry]
    with open(path, 'rb') as f:
        f.seek(off)
        return f.read(size)


def write_raw(game, root, entry, raw, img='dt07.img'):
    """Write an override; one equal to stock is removed instead."""
    dest = override_path(root, entry, img)
    if raw == read_raw(game, entry, None, img):
        if os.path.exists(dest):
            os.remove(dest)
        return None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, 'wb').write(raw)
    return dest


def _is_ktmdl(game, entry):
    try:
        c = container.read(read_raw(game, entry))
    except Exception:
        return False
    return bool(c.ktmdl_blocks())


def slots(game):
    """pes2012.exe -> [Slot] sorted by geometry entry; a record counts if
    its geometry entry holds KTMDL, placeholders (all lightmap ranges empty)
    are left out. Same rules as pes12_stadium.read_slots."""
    data = open(os.path.join(game, 'pes2012.exe'), 'rb').read()
    heads = {}
    n = 4 * (HEADER_FIELDS + VARIANTS * VARIANT_FIELDS)
    for o in range(0, len(data) - n, 4):
        w = struct.unpack_from('<%dI' % HEADER_FIELDS, data, o)
        if any(x >> 16 != DT07_TAG for x in w):
            continue
        e = [x & 0xFFFF for x in w]
        if e[3] != e[2] + SLOT_STRIDE or e[4] != e[2] + 2 * SLOT_STRIDE:
            continue
        vs = []
        for k in range(VARIANTS):
            v = struct.unpack_from('<%dI' % VARIANT_FIELDS, data, o + 4 * HEADER_FIELDS + 4 * VARIANT_FIELDS * k)
            if any(x >> 16 != DT07_TAG for x in v):
                break
            vs.append([x & 0xFFFF for x in v])
        if len(vs) == VARIANTS:
            heads.setdefault(e[2], (e, vs))
    out = []
    for num, g in enumerate(sorted(heads)):
        e, vs = heads[g]
        lms = [(v[LM_FIRST], v[LM_LAST]) for v in vs]
        if all(a > b for a, b in lms) or not _is_ktmdl(game, g):
            continue
        out.append(Slot(num, g, (e[0], e[1]), sorted({v[PROPS_FIELD] for v in vs}), lms,
                        sorted({v[SKY_FIELD] for v in vs}), sorted({v[PITCH_FIELD] for v in vs})))
    return out


def slot_entries(slot):
    """{role: [dt07 entries]}: models first (geometry, props), then textures."""
    lm = sorted({e for a, b in slot.lightmaps for e in range(a, b + 1)})
    return {'geometry': [slot.geometry], 'props': list(slot.props),
            'stand': list(range(slot.stand[0], slot.stand[1] + 1)),
            'lightmap': lm, 'sky': list(slot.skies), 'pitch': list(slot.pitches)}


def texture_index(game, slot, root=None):
    """{texture id: (entry, block index)} over the slot's texture entries,
    then the shared ones; the first holder of an id wins, as in load order."""
    roles = slot_entries(slot)
    order = roles['stand'] + roles['lightmap'] + roles['sky'] + roles['pitch'] + list(SHARED_TEXTURE_ENTRIES)
    out = {}
    for e in order:
        try:
            c = container.read(read_raw(game, e, root))
        except Exception:
            continue
        for k, b in enumerate(c.blocks):
            if textures.is_texture(b.data):
                out.setdefault(textures.texture_id(b.data), (e, k))
    return out

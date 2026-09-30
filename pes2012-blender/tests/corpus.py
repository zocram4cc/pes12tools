"""The user's own PES2012 as the test corpus (nothing of it ships).

PES12_GAME = the game folder (default: the repo checkout's
'Pro Evolution Soccer 2012'). Tests skip with a message when it is absent.
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pes2012_starter_pack'))
import container  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.environ.get('PES12_GAME', os.path.join(HERE, '..', '..', '..', 'Pro Evolution Soccer 2012'))
IMG_DIR = os.path.join(GAME, 'img')
AFS_MAGIC = b'AFS\0'
KTMDL_MAGIC = b'KTMDL\x00\x00\x00'

# one stock example per class (11-blender-addon.md census, 30-09)
CLASSES = {
    'ball': ('dt0b.img', 2),
    'stadium_side': ('dt08.img', 78),
    'stadium_empty_first': ('dt08.img', 260),
    'boots_static': ('dt07.img', 1840),
    'boots_skinned': ('dt07.img', 2960),
    'hair_3bone': ('dt0c.img', 3936),
    'small_5bone': ('dt0c.img', 5617),
    'face_dt0c': ('dt0c.img', 164),
    'face_dt0d': ('dt0d.img', 2660),
    'body_21': ('dt09.img', 534),
    'body_40': ('dt09.img', 330),
    'body_multi': ('dt09.img', 342),
    'body_349': ('dt09.img', 349),
    'kit': ('dt0c.img', 3),
}


def available():
    return os.path.isdir(IMG_DIR)


def afs_entries(path):
    with open(path, 'rb') as f:
        head = f.read(8)
        if head[:4] != AFS_MAGIC:
            raise ValueError('%s: not AFS' % path)
        n = struct.unpack_from('<I', head, 4)[0]
        table = f.read(8 * n)
    return [struct.unpack_from('<II', table, 8 * i) for i in range(n)]


def read_entry(img, index):
    path = os.path.join(IMG_DIR, img)
    off, size = afs_entries(path)[index]
    with open(path, 'rb') as f:
        f.seek(off)
        return f.read(size)


def _body(raw):
    if raw[3:8] == b'WESYS':
        try:
            return zlib.decompress(raw[16:])
        except zlib.error:
            return b''
    return raw


def entries(imgs=None):
    """-> [(img, index, raw)] for every entry holding a KTMDL."""
    out = []
    for img in sorted(imgs or os.listdir(IMG_DIR)):
        if not img.endswith('.img'):
            continue
        path = os.path.join(IMG_DIR, img)
        try:
            table = afs_entries(path)
        except ValueError:
            continue
        with open(path, 'rb') as f:
            for i, (off, size) in enumerate(table):
                if not size:
                    continue
                f.seek(off)
                raw = f.read(size)
                if container.read(raw).ktmdl_blocks():
                    out.append((img, i, raw))
    return out

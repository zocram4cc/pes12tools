"""Stamp a custom-body marker into a PES2012 face.bin (kitserver GDB face).

    python3 mark_face.py <template face.bin> <player id> <out face.bin> [model dir]

drawlogic.dll identifies a player's draws by the face texture drawn just
before his kit (all faces share one head mesh, 24-09). fserv serves this file
for the player id in GDB/faces/map.txt; the marker IS that player id, and
drawlogic loads the body from custom/p<id>/ - no table, no count limit.

The marker fills every block of the MARKED_MIPS smallest mips of every DXT
texture in the file - invisible at play distances, and the face is hidden when
the body is swapped. Each 8-byte unit (DXT1 colour block / DXT5 alpha and
colour halves) becomes  'P' 'G' 'D' id0 id1 id2 sum ~sum  (id = u24 LE,
sum = id0 + id1 + id2 mod 256; drawlogic.cpp readMarker).  The game rebuilds
faces into a 512x512 DXT5 atlas at load, so the tag is searched for, not
addressed.

With a model dir, the face's colour texture is first tinted to the model's
skin colour: the game's stock skin draw (arms, legs, neck, bare hands) samples
this atlas - the template's DXT1 holds the body-skin strip beside the face -
so a kept stock piece then matches the model's own skin (owner 09-10). The
colour is the model's skin swatch (skin.*, the 4cc export's Faces/XXXnn/skin
colour), else the mean of its limb skin texture (a SUB_SKIN sub: PES's
Skin_New); neither -> no tint, and the stock skin keeps his EDIT skin tone.
"""
import io
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fmdl_to_pes12 as F  # noqa: E402

MARKER_TAG = b'PGD'
MARKER_ID_MAX = 0xFFFFFF       # u24 player id
MARKED_MIPS = 4               # 1x1 .. 8x8
DDS_HEADER_BYTES = 128
WESYS_HEADER_BYTES = 16
# the model folder's skin swatch, in search order (pes12_import_team.copy_swatch)
SKIN_SWATCH_FILES = ('skin.dds', 'skin.png', 'skin.bmp', 'skin.tga', 'skin.jpg')
PGT1_MAGIC, PGT1_HEADER_BYTES = b'PGT1', 16   # drawlogic's RGBA .tex (pgb2.build_tex)
DXT1_BLOCK_BYTES = 8
RGB565_MAX = (31, 63, 31)


def unwesys(raw):
    return zlib.decompress(raw[WESYS_HEADER_BYTES:])


def rewesys(template, body):
    comp = zlib.compress(body, 9)
    hdr = bytearray(template[:WESYS_HEADER_BYTES])
    struct.pack_into('<II', hdr, 8, len(comp), len(body))
    return bytes(hdr) + comp


def marker_block(n):
    if not 0 <= n <= MARKER_ID_MAX:
        raise ValueError('player id %d does not fit the u24 marker' % n)
    idb = n.to_bytes(3, 'little')
    s = sum(idb) & 0xFF
    return MARKER_TAG + idb + bytes([s, 0xFF - s])


def mip_ranges(body, dds):
    h, w = struct.unpack_from('<II', body, dds + 12)
    mips = max(struct.unpack_from('<I', body, dds + 28)[0], 1)
    four = body[dds + 84:dds + 88]
    block = 8 if four == b'DXT1' else 16
    off, out = dds + DDS_HEADER_BYTES, []
    for m in range(mips):
        mw, mh = max(1, w >> m), max(1, h >> m)
        size = ((mw + 3) // 4) * ((mh + 3) // 4) * block
        out.append((off, size))
        off += size
    return out, four


def mean_rgb(data):
    """Mean colour (0..255) of an image file's texels that are not fully clear."""
    import numpy as np
    if data[:4] == PGT1_MAGIC:
        _, w, h, _ = struct.unpack_from('<4I', data)
        px = np.frombuffer(data, np.uint8, w * h * 4, PGT1_HEADER_BYTES).reshape(-1, 4)[:, [2, 1, 0, 3]]
    else:
        from PIL import Image
        px = np.asarray(Image.open(io.BytesIO(data)).convert('RGBA')).reshape(-1, 4)
    seen = px[px[:, 3] > 0]
    return (seen if len(seen) else px)[:, :3].astype(float).mean(0)


def skin_colour(model_dir):
    """The model's skin colour (0..255 RGB): its swatch, else its limb skin texture's mean; None if neither."""
    have = {f.lower(): f for f in os.listdir(model_dir)}
    for name in SKIN_SWATCH_FILES:
        if name in have:
            return mean_rgb(open(os.path.join(model_dir, have[name]), 'rb').read())
    b = open(os.path.join(model_dir, 'body.bin'), 'rb').read()
    if struct.unpack_from('<I', b)[0] != F.BODY2_MAGIC:
        return None
    nsub = struct.unpack_from('<I', b, 16)[0]
    subs = [struct.unpack_from('<4I', b, 24 + 16 * k) for k in range(nsub)]
    skin = [t for _, _, t, fl in subs if fl & F.SUB_SKIN]
    if not skin:
        return None
    return mean_rgb(open(os.path.join(model_dir, 'body_%d.tex' % max(set(skin), key=skin.count)), 'rb').read())


def tint_565(c, ratio):
    ch = ((c >> 11) & 31, (c >> 5) & 63, c & 31)
    r, g, b = (min(mx, int(v * k + 0.5)) for v, k, mx in zip(ch, ratio, RGB565_MAX))
    return r << 11 | g << 5 | b


def tint_dxt1_block(body, o, ratio):
    """One DXT1 block's colour times ratio: its colours are linear in the two
    endpoints, so they scale alone. The block's mode is the endpoint order
    (c0 > c1: four colours, else three + clear): a tint that flips the order
    swaps the endpoints back and remaps the indices."""
    c0, c1, idx = struct.unpack_from('<HHI', body, o)
    four = c0 > c1
    n0, n1 = tint_565(c0, ratio), tint_565(c1, ratio)
    if four and n0 <= n1:
        if n0 == n1:
            idx = 0                                   # one colour left: index 0 in either mode
        else:
            n0, n1, idx = n1, n0, idx ^ 0x55555555    # 0<->1, 2<->3
    elif not four and n0 > n1:
        n0, n1 = n1, n0
        for k in range(16):                           # 0<->1; 2 (mid) and 3 (clear) stay
            if (idx >> 2 * k) & 3 < 2:
                idx ^= 1 << 2 * k
    struct.pack_into('<HHI', body, o, n0, n1, idx)


def tint_colour_textures(body, target):
    """Every DXT1 texture (the template's face + body-skin colour map; its
    DXT5 is a neutral grey map) scaled so its mean becomes `target`."""
    at = 0
    while (dds := body.find(b'DDS |', at)) >= 0:
        mips, four = mip_ranges(body, dds)
        if four == b'DXT1':
            end = mips[-1][0] + mips[-1][1]
            ratio = [t / max(m, 1.0) for t, m in zip(target, mean_rgb(bytes(body[dds:end])))]
            for off, size in mips:
                for o in range(off, off + size, DXT1_BLOCK_BYTES):
                    tint_dxt1_block(body, o, ratio)
        at = dds + 4


def mark(template_path, n, out_path, model_dir=None):
    raw = open(template_path, 'rb').read()
    body = bytearray(unwesys(raw))
    target = skin_colour(model_dir) if model_dir else None
    if target is not None:
        tint_colour_textures(body, target)
    at, stamped = 0, 0
    while True:
        dds = body.find(b'DDS |', at)
        if dds < 0:
            break
        mips, four = mip_ranges(body, dds)
        if four in (b'DXT1', b'DXT5', b'DXT3'):
            for off, size in mips[-MARKED_MIPS:]:
                for o in range(off, off + size, 8):
                    body[o:o + 8] = marker_block(n)
            stamped += 1
        at = dds + 4
    if not stamped:
        raise ValueError('no DXT texture in %s' % template_path)
    open(out_path, 'wb').write(rewesys(raw, bytes(body)))
    tint = '' if target is None else ', skin tinted to %d,%d,%d' % tuple(round(v) for v in target)
    print('stamped player id %d into %d texture(s)%s -> %s' % (n, stamped, tint, out_path))


if __name__ == '__main__':
    mark(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)

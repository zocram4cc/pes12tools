"""Stamp a custom-body marker into a PES2012 face.bin (kitserver GDB face).

    python3 mark_face.py <template face.bin> <player id> <out face.bin>

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
"""
import struct
import sys
import zlib

MARKER_TAG = b'PGD'
MARKER_ID_MAX = 0xFFFFFF       # u24 player id
MARKED_MIPS = 4               # 1x1 .. 8x8
DDS_HEADER_BYTES = 128
WESYS_HEADER_BYTES = 16


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


def mark(template_path, n, out_path):
    raw = open(template_path, 'rb').read()
    body = bytearray(unwesys(raw))
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
    print('stamped player id %d into %d texture(s) -> %s' % (n, stamped, out_path))


if __name__ == '__main__':
    mark(sys.argv[1], int(sys.argv[2]), sys.argv[3])

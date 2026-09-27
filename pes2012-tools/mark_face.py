"""Stamp a custom-body marker into a PES2012 face.bin (kitserver GDB face).

    python3 mark_face.py <template face.bin> <marker 0-255> <out face.bin>

drawlogic.dll identifies a player's draws by the face texture drawn just
before his kit (all faces share one head mesh, 24-09). fserv serves this file
for the player id in GDB/faces/map.txt, so the marker is keyed by player id.

The marker fills every block of the MARKED_MIPS smallest mips of every DXT
texture in the file - invisible at play distances, and the face is hidden when
the body is swapped. Each 8-byte unit (DXT1 colour block / DXT5 alpha and
colour halves) becomes  'P' 'G' 'B' marker ~marker 0 0 0.  The game rebuilds
faces into a 512x512 DXT5 atlas at load, so the tag is searched for, not
addressed.
"""
import struct
import sys
import zlib

MARKER_TAG = b'PGB'
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
    return MARKER_TAG + bytes([n, 255 - n, 0, 0, 0])


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
    print('stamped marker %d into %d texture(s) -> %s' % (n, stamped, out_path))


if __name__ == '__main__':
    mark(sys.argv[1], int(sys.argv[2]), sys.argv[3])

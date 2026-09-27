"""PES2012 menu string tables (dt06.img entries 4/17, dt05_e.img 1/8/9).

Decompressed layout (25-09): u32 nBlocks, u32 8, then nBlocks x
(u32 start, u32 len, u32 nameOff). Each block: u32 count, u32 ?, then
count x (u32 id, u16 bytes+NUL, u16 characters, u32 strOff), then NUL-terminated
strings; strOff is relative to the block start. Block names ("Sys1C") are
8-byte tags after the last block. The league names the menus show live in
dt06 (dt05_e holds copies the screens never read).

  python3 pes12strings.py rename <game dir>   # 4cc league names, in place
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import afs  # noqa: E402
from pes12db import wesys  # noqa: E402

# 4cc league names on the four league slots the 4cc base uses (owner, 25-09)
LEAGUE_RENAMES = {
    'English League': '4chan Cup',
    'Portugal League': 'Backup Teams',
    'PES League': '/vg/ League',
    'D2 League': 'Invitational Teams',
}
STRING_TABLES = {'img/dt06.img': (4, 17)}
BLOCK_ALIGN = 4          # block starts are 4-aligned (stock 0x304)
NAMES_ALIGN = 16         # block-name tags start 16-aligned (stock 0x2F90)
BLOCK_NAME_LEN = 8
HEADER_WORDS, BLOCK_HEAD = 2, 8
RECORD = 12


def _pad(b, n):
    return b + b'\0' * (-len(b) % n)


def parse(blob):
    n = struct.unpack_from('<I', blob)[0]
    blocks = []
    for k in range(n):
        start, ln, name_off = struct.unpack_from('<III', blob, 4 * HEADER_WORDS + 12 * k)
        count, x = struct.unpack_from('<II', blob, start)
        recs = []
        for r in range(count):
            sid, size, sl, so = struct.unpack_from('<IHHI', blob, start + BLOCK_HEAD + RECORD * r)
            recs.append((sid, blob[start + so:start + so + size - 1].decode('utf-8')))
        blocks.append({'x': x, 'recs': recs,
                       'name': blob[name_off:name_off + BLOCK_NAME_LEN]})
    return blocks


def build(blocks):
    bodies = []
    for b in blocks:
        head = struct.pack('<II', len(b['recs']), b['x'])
        so = BLOCK_HEAD + RECORD * len(b['recs'])
        recs, strs = b'', b''
        for sid, s in b['recs']:
            e = s.encode('utf-8') + b'\0'
            recs += struct.pack('<IHHI', sid, len(e), len(s), so + len(strs))  # len = characters
            strs += e
        bodies.append(head + recs + strs)
    hs = 4 * HEADER_WORDS + 12 * len(blocks)
    starts, at = [], hs
    for body in bodies:
        starts.append(at)
        at = -(-(at + len(body)) // BLOCK_ALIGN) * BLOCK_ALIGN
    names_at = -(-(starts[-1] + len(bodies[-1])) // NAMES_ALIGN) * NAMES_ALIGN
    out = struct.pack('<II', len(blocks), 8)
    for k, body in enumerate(bodies):
        out += struct.pack('<III', starts[k], len(body), names_at + BLOCK_NAME_LEN * k)
    for k, body in enumerate(bodies):
        out = out.ljust(starts[k], b'\0') + body
    out = out.ljust(names_at, b'\0')
    for b in blocks:
        out += b['name']
    return out


def rename(blob, mapping):
    blocks = parse(blob)
    hits = 0
    for b in blocks:
        for k, (sid, s) in enumerate(b['recs']):
            if s in mapping:
                b['recs'][k] = (sid, mapping[s])
                hits += 1
    return build(blocks), hits


def rename_blobs(game):
    """-> {img: {entry: WESYS bytes}} with the 4cc league names, built from
    the install's (stock) string tables."""
    out = {}
    for img, idxs in STRING_TABLES.items():
        path = os.path.join(game, img)
        out[img] = {}
        for i in idxs:
            blob = zlib.decompress(afs.read(path, i)[16:])
            assert build(parse(blob)) == blob, 'string table round-trip failed: %s #%d' % (img, i)
            new, hits = rename(blob, LEAGUE_RENAMES)
            if hits:
                out[img][i] = wesys(new)
            print('%s #%d: %d names renamed' % (img, i, hits))
    return out


def cmd_rename(game):
    """Patch the img files in place (the new tables fit their stock slots)."""
    for img, blobs in rename_blobs(game).items():
        afs.replace(os.path.join(game, img), blobs)


if __name__ == '__main__':
    {'rename': cmd_rename}[sys.argv[1]](sys.argv[2])

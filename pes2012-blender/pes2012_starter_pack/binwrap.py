"""WESYS + inner-TOC container codec (pure python, no Blender).

Provenance: tools/ktmdl.py unwesys, tools/pes12_ball.py split_bin/join_bin
(ball dialect), tools/pes12db.py leagues/names_blocks (generic dialect).

WESYS: 8-byte tag + u32 csize (zlib stream) + u32 usize, then zlib + zero
pad to 16 (tools/pes12_ball.py convert).
"""
import struct
import zlib

WESYS_TAG = b'\x00\x01\x01WESYS'
WESYS_MAGIC = b'WESYS'
KTMDL_MAGIC = b'KTMDL\x00\x00\x00'


def unwesys(data):
    """WESYS container -> decompressed body (passthrough if raw)."""
    if data[3:8] == WESYS_MAGIC:
        return zlib.decompress(data[16:])
    return data


def wesys_wrap(body):
    """Decompressed body -> WESYS BIN bytes (16-padded, stock dt0b#11 style)."""
    comp = zlib.compress(bytes(body), 9)
    return (WESYS_TAG + struct.pack('<II', len(comp), len(body)) + comp +
            b'\x00' * ((-len(comp)) % 16))


def wesys_tag(raw):
    """First 8 bytes of a stock BIN (the tag preserved across re-wrap)."""
    return bytes(raw[:8])


# --- ball dialect (tools/pes12_ball.py split_bin/join_bin) ---

def split_ball(body):
    """Decompressed ball BIN -> (ktmdl, [texture contents])."""
    n, _, hs = struct.unpack_from('<III', body)
    if (n, hs) != (4, 64):
        raise ValueError('not a 4-entry ball BIN')
    sizes = [struct.unpack_from('<I', body, 12 + 12 * k)[0] for k in range(n)]
    ends = [struct.unpack_from('<I', body, 12 + 12 * k + 8)[0] for k in range(3)]
    bounds = [hs, *ends, len(body)]
    blocks = []
    for k in range(4):
        content = body[bounds[k]:bounds[k] + sizes[k]]
        pad = body[bounds[k] + sizes[k]:bounds[k + 1]]
        if any(pad) or len(pad) != (-sizes[k]) % 16:
            raise ValueError('block %d: unexpected pad' % k)
        blocks.append(bytes(content))
    return blocks[0], blocks[1:]


def join_ball(ktmdl, tex_blocks):
    """(ktmdl, textures) -> decompressed ball BIN (tools/pes12_ball join_bin)."""
    n, hs = 4, 64
    ktmdl = bytearray(ktmdl)
    while len(ktmdl) % 16:
        ktmdl += b'\x00'
    struct.pack_into('<I', ktmdl, 0x90, len(ktmdl))  # header.size, ref parser
    sizes = [len(ktmdl)] + [len(b) for b in tex_blocks]
    out = bytearray(struct.pack('<III', n, 8, hs))
    pos = hs + len(ktmdl)
    out += struct.pack('<IiI', sizes[0], -16, pos)
    for k in range(2):
        pos += sizes[k + 1] + (-sizes[k + 1]) % 16
        out += struct.pack('<IiI', sizes[k + 1], -16, pos)
    out += struct.pack('<IiI', sizes[3], -16, 0) + b'\x00' * 4
    out += ktmdl
    for b in tex_blocks:
        out += b + b'\x00' * ((-len(b)) % 16)
    return bytes(out)


# --- generic dialect (tools/pes12db.py names_blocks/write_names) ---
def split_generic(body):
    """Decompressed generic BIN -> list of blocks.

    Header: n, 8, hs, then n x (byteLen, 0xFFFFFFF0, end): blocks 0..n-2
    run start->end chained, the last runs size bytes (tools/pes12db.py
    names_blocks). hs is 12+12n (compact, dt07) or +4 pad dwords (names
    dialect). Aliased rows (same block twice, e.g. anyukov rows 0/1) are
    rejected.
    """
    n, _, hs = struct.unpack_from('<III', body)
    ent = [struct.unpack_from('<III', body, 12 + 12 * k) for k in range(n)]
    if hs not in (12 + 12 * n, 12 + 12 * n + 4):
        raise ValueError('not a generic BIN (hs=%d)' % hs)
    for _size, flag, _end in ent:
        if flag != 0xFFFFFFF0:
            raise ValueError('not a generic BIN (flag 0x%X)' % flag)
    blocks, seen, s = [], set(), hs
    for k, (size, _, end) in enumerate(ent):
        blk = bytes(body[s:end]) if k < n - 1 else bytes(body[s:s + size])
        if (s, end if k < n - 1 else s + size) in seen:
            raise ValueError('block %d: aliased TOC row' % k)
        seen.add((s, end if k < n - 1 else s + size))
        blocks.append(blk)
        s = end
    return blocks


def join_generic(blocks, compact=None):
    """Block list -> decompressed generic BIN (tools/pes12db write_names).
    compact=True writes hs=12+12n (dt07/dt0b style), False the +4-pad
    names dialect; None keeps the names dialect like write_names.
    """
    hs = 12 + 12 * len(blocks) + (0 if compact else 4)
    hdr = bytearray(struct.pack('<III', len(blocks), 8, hs))
    if not compact:
        hdr += b'\0\0\0\0'
    o = hs
    for k, blk in enumerate(blocks):
        o += len(blk)
        hdr += struct.pack('<III', len(blk), 0xFFFFFFF0, o if k < len(blocks) - 1 else 0)
    return bytes(hdr) + b''.join(blocks)


def split_body(body):
    """Decompressed BIN -> ('ball', [ktmdl, *tex]) or ('generic', blocks)."""
    try:
        kt, tex = split_ball(body)
        return 'ball', [kt, *tex]
    except ValueError:
        return 'generic', split_generic(body)


def find_ktmdl(blocks):
    """Indices of blocks starting with the KTMDL magic."""
    return [k for k, b in enumerate(blocks) if bytes(b[:8]) == KTMDL_MAGIC]

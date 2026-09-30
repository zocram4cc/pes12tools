"""PES2012 BIN containers (pure python, no Blender).

Every KTMDL-bearing entry of PES2012 v1.06 (census 30-09: dt04, dt06-dt09,
dt0b-dt0d) is one of:

  raw      the payload itself (a bare KTMDL or unknown bytes)
  rows     u32 count, u32 ROW_TAG, then count rows of 12 bytes
           (u32 offset, u32 size, u32 field) and the blocks at those
           offsets. field is -16 on most entries and something else on
           others (dt09 #534: 0x000F1E30 upward), so it is kept, never
           checked. Balls, stadium sides, boots, faces, bodies and kits
           all use it; the old add-on read it with three different
           dialects (ball, generic, stadium) that were views of this one.

either optionally wrapped in WESYS: 8-byte tag (b'\\0\\1\\1WESYS'),
u32 compressed size, u32 size, zlib stream, zero pad.

read() keeps the source bytes; write() returns them unchanged when no block
changed, so an unedited round trip is exact whatever the padding or zlib
settings the game's files were written with.
"""
import struct
import zlib

WESYS_MAGIC = b'WESYS'
WESYS_HEAD = 16                 # tag 8 + csize 4 + usize 4
WESYS_ALIGN = 16                # stock BINs pad the zlib stream to 16
KTMDL_MAGIC = b'KTMDL\x00\x00\x00'
ROW_TAG = 8                     # second u32 of every row container
ROW_SIZE = 12
ROW_PREFIX = 8                  # count + tag before the first row
BLOCK_ALIGN = 16                # blocks start 16-aligned in every stock row container
MAX_ROWS = 4096                 # sanity bound when sniffing (largest stock count: 40, dt09)
TEXTURE_MAGICS = (b'WE00', b'DDS ')


class Block:
    """One payload of a container. row = (offset, size, field) as read."""

    def __init__(self, data, row=None):
        self.data = bytes(data)
        self.row = row
        self._orig = self.data

    @property
    def kind(self):
        if self.data[:8] == KTMDL_MAGIC:
            return 'ktmdl'
        if self.data[:4] in TEXTURE_MAGICS:
            return 'texture'
        return 'other'

    @property
    def changed(self):
        return self.data != self._orig


class Container:
    def __init__(self, raw, wesys_head, layout, header, blocks):
        self.raw = raw                  # the entry's bytes as read
        self.wesys_head = wesys_head    # 16 bytes or None
        self.layout = layout            # 'rows' | 'raw'
        self.header = header            # rows: bytes before the first block
        self.blocks = blocks

    def ktmdl_blocks(self):
        return [b for b in self.blocks if b.kind == 'ktmdl']


def _try_zlib(stream):
    try:
        return zlib.decompress(stream)
    except zlib.error:
        return None


def _unwrap(raw):
    # WESYS with a real zlib stream: decompressed body. The dt04/dt07/dt08
    # boots/statue range stores the body UNCOMPRESSED (csize == entry length,
    # usize 0); decompress probes the actual stream, not the header sizes.
    if len(raw) >= WESYS_HEAD and raw[3:8] == WESYS_MAGIC:
        body = _try_zlib(raw[WESYS_HEAD:])
        if body is not None:
            return bytes(raw[:WESYS_HEAD]), body
        csize = struct.unpack_from('<I', raw, 8)[0]
        return bytes(raw[:WESYS_HEAD]), bytes(raw[WESYS_HEAD:WESYS_HEAD + csize])
    return None, bytes(raw)


def _rows(body):
    """-> [(offset, size, field)] if body is a row container, else None."""
    if len(body) < ROW_PREFIX:
        return None
    n, tag = struct.unpack_from('<II', body)
    if tag != ROW_TAG or not 0 < n <= MAX_ROWS or ROW_PREFIX + ROW_SIZE * n > len(body):
        return None
    rows = [struct.unpack_from('<III', body, ROW_PREFIX + ROW_SIZE * k) for k in range(n)]
    table_end = ROW_PREFIX + ROW_SIZE * n
    for off, size, _ in rows:
        if off < table_end or off + size > len(body):
            return None
    return rows


def read(raw):
    raw = bytes(raw)
    head, body = _unwrap(raw)
    rows = _rows(body)
    if rows is None:
        return Container(raw, head, 'raw', b'', [Block(body)])
    first = min(off for off, _, _ in rows)
    return Container(raw, head, 'rows', body[:first],
                     [Block(body[off:off + size], (off, size, field)) for off, size, field in rows])


def _body(c):
    if c.layout == 'raw':
        return c.blocks[0].data
    out = bytearray(c.header)
    pos = len(c.header)
    for k, b in enumerate(c.blocks):
        pos += (-pos) % BLOCK_ALIGN
        field = b.row[2] if b.row else (1 << 32) - BLOCK_ALIGN   # -16 as u32
        struct.pack_into('<III', out, ROW_PREFIX + ROW_SIZE * k, pos, len(b.data), field)
        out += b'\x00' * (pos - len(out)) + b.data
        pos += len(b.data)
    out += b'\x00' * ((-len(out)) % BLOCK_ALIGN)
    return bytes(out)


def write(c):
    if not any(b.changed for b in c.blocks):
        return c.raw
    body = _body(c)
    if c.wesys_head is None:
        return body
    if _try_zlib(c.raw[WESYS_HEAD:]) is None:
        # non-zlib (uncompressed-body) entry: body stored as-is
        head = c.wesys_head[:8] + struct.pack('<II', len(body), 0)
        return head + body + b'\x00' * ((-len(body)) % WESYS_ALIGN)
    comp = zlib.compress(body, 9)
    head = c.wesys_head[:8] + struct.pack('<II', len(comp), len(body))
    return head + comp + b'\x00' * ((-len(comp)) % WESYS_ALIGN)

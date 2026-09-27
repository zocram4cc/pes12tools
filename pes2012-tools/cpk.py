#!/usr/bin/env python3
"""Minimal CRI CPK extractor: @UTF table parsing + CRILAYLA decompression.

Covers what PES dt*.cpk files use: a TOC of (DirName, FileName, FileOffset,
FileSize, ExtractSize) with per-file optional CRILAYLA compression.
"""
import io
import os
import struct
import sys


def read_utf(data: bytes):
    """Parses one @UTF table into a list of row dicts."""
    if data[:4] != b"@UTF":
        raise ValueError("not a @UTF table: %r" % data[:4])
    (table_size,) = struct.unpack(">I", data[4:8])
    body = data[8:8 + table_size]
    (rows_offset, strings_offset, data_offset, name_off, columns, row_width,
     rows) = struct.unpack(">IIIIHHI", body[:24])

    strings = body[strings_offset:data_offset]

    def get_string(off):
        end = strings.find(b"\0", off)
        return strings[off:end].decode("utf-8", "replace")

    # column schema
    cols = []
    pos = 24
    for _ in range(columns):
        flags = body[pos]
        pos += 1
        if flags == 0:  # rare: padded schema
            pos += 4
            continue
        storage = flags & 0xF0
        vtype = flags & 0x0F
        (noff,) = struct.unpack(">I", body[pos:pos + 4])
        pos += 4
        name = get_string(noff)
        const = None
        if storage == 0x30:  # constant stored inline in schema
            const, pos = read_value(body, pos, vtype, strings, data_offset)
        cols.append((name, storage, vtype, const))

    out = []
    for r in range(rows):
        rowpos = rows_offset + r * row_width
        row = {}
        for name, storage, vtype, const in cols:
            if storage == 0x30:
                row[name] = const
            elif storage == 0x10:  # zero
                row[name] = 0
            elif storage == 0x50:  # per-row
                val, rowpos = read_value(body, rowpos, vtype, strings, data_offset)
                row[name] = val
            else:
                raise ValueError("unknown storage %02x" % storage)
        out.append(row)
    return out


def read_value(body, pos, vtype, strings, data_offset):
    if vtype in (0, 1):  # u8/s8
        return body[pos], pos + 1
    if vtype in (2, 3):  # u16/s16
        return struct.unpack(">H", body[pos:pos + 2])[0], pos + 2
    if vtype in (4, 5):  # u32/s32
        return struct.unpack(">I", body[pos:pos + 4])[0], pos + 4
    if vtype in (6, 7):  # u64/s64
        return struct.unpack(">Q", body[pos:pos + 8])[0], pos + 8
    if vtype == 8:  # float
        return struct.unpack(">f", body[pos:pos + 4])[0], pos + 4
    if vtype == 0xA:  # string
        (off,) = struct.unpack(">I", body[pos:pos + 4])
        end = strings.find(b"\0", off)
        return strings[off:end].decode("utf-8", "replace"), pos + 4
    if vtype == 0xB:  # data blob (offset, size) relative to data_offset
        off, size = struct.unpack(">II", body[pos:pos + 8])
        return (data_offset + off, size), pos + 8
    raise ValueError("unknown type %x" % vtype)


def crilayla_decompress(data: bytes) -> bytes:
    """CRILAYLA: backwards LZ with variable-length fields + 0x100 raw prefix."""
    if data[:8] != b"CRILAYLA":
        return data
    uncompressed_size, _header_offset = struct.unpack("<II", data[8:16])
    # the 0x100 raw prefix and the bitstream are anchored to the END of the
    # blob (some cpks pad between the compressed data and the prefix, so
    # 16+header_offset is NOT a reliable anchor)
    raw = data[len(data) - 0x100:]
    bit_top = len(data) - 0x100 - 1  # bits are read backwards from here

    out = bytearray(uncompressed_size)
    out_pos = uncompressed_size  # filled backwards

    bitpos = 0

    def get_bits(n):
        nonlocal bitpos
        val = 0
        for _ in range(n):
            byte = data[bit_top - (bitpos >> 3)]
            bit = (byte >> (7 - (bitpos & 7))) & 1  # MSB-first within each byte
            val = (val << 1) | bit
            bitpos += 1
        return val

    while out_pos > 0:
        if get_bits(1):
            offset = get_bits(13) + 3
            length = 3
            # variable-length length: 2,3,5,8-bit chunks then 8s
            for bits in (2, 3, 5):
                chunk = get_bits(bits)
                length += chunk
                if chunk != (1 << bits) - 1:
                    break
            else:
                while True:
                    chunk = get_bits(8)
                    length += chunk
                    if chunk != 255:
                        break
            for _ in range(length):
                out[out_pos - 1] = out[out_pos - 1 + offset]
                out_pos -= 1
        else:
            out[out_pos - 1] = get_bits(8)
            out_pos -= 1

    return bytes(raw) + bytes(out)


def extract(cpk_path, dest, list_only=False, pattern=None):
    f = open(cpk_path, "rb")
    magic = f.read(4)
    assert magic == b"CPK ", magic
    f.seek(16)
    header = read_utf(strip_crypt(f.read(struct.unpack("<Q", open(cpk_path,'rb').read(24)[16:24])[0] if False else 2048)))
    row = header[0]
    toc_offset = row["TocOffset"]
    content_offset = row.get("ContentOffset", 0)

    f.seek(toc_offset)
    tmagic = f.read(4)
    assert tmagic == b"TOC ", tmagic
    f.seek(toc_offset + 16)
    (tsize,) = struct.unpack("<I", f.read(8)[4:8]) if False else (0,)
    # the @UTF block declares its own size at +4 (big-endian, excludes the
    # 8-byte magic+size header); read exactly that instead of slurping the
    # rest of a possibly multi-GB file
    f.seek(toc_offset + 16)
    probe = strip_crypt(f.read(16))
    (utf_size,) = struct.unpack(">I", probe[4:8])
    f.seek(toc_offset + 16)
    toc = read_utf(strip_crypt(f.read(8 + utf_size)))

    base = min(toc_offset, content_offset) if content_offset else toc_offset

    count = 0
    for row in toc:
        name = row["FileName"]
        dirname = row.get("DirName", "") or ""
        offset = row["FileOffset"] + base
        size = row["FileSize"]
        extract_size = row.get("ExtractSize", size)
        rel = os.path.join(dirname, name)
        if pattern is not None and pattern not in rel:
            continue
        if list_only:
            print("%10d %10d  %s" % (size, extract_size, rel))
            count += 1
            continue
        f.seek(offset)
        blob = f.read(size)
        if extract_size != size or blob[:8] == b"CRILAYLA":
            blob = crilayla_decompress(blob)
        outpath = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        with open(outpath, "wb") as o:
            o.write(blob)
        count += 1
    print("files:", count)


def strip_crypt(blob: bytes) -> bytes:
    """CPK @UTF blocks are sometimes XOR-obfuscated; detect and undo."""
    if blob[:4] == b"@UTF":
        return blob
    # standard CRI obfuscation: x = 0x655f; m = 0x4115
    out = bytearray(blob)
    x, m = 0x5F, 0x15
    for i in range(len(out)):
        out[i] ^= x & 0xFF
        x = (x * m) & 0xFF
    if out[:4] == b"@UTF":
        return bytes(out)
    raise ValueError("cannot decode @UTF block")


def parse_args(argv):
    """-> (cpk, dest, list_only, pattern) from the arguments after the script.

    The destination is positional, so a flag in its place has to be recognised as
    one: "cpk.py dt15.cpk --list" once extracted the whole archive into a
    directory named "--list". A listing writes nothing, so it needs no
    destination at all.
    """
    cpk_path = argv[0]
    rest = argv[1:]
    dest = None
    list_only = False
    pattern = None
    for arg in rest:
        if arg == "--list":
            list_only = True
        elif arg.startswith("--filter="):
            pattern = arg.split("=", 1)[1]
        elif not arg.startswith("--") and dest is None:
            dest = arg
    if dest is None and not list_only:
        dest = "extracted"
    return (cpk_path, dest, list_only, pattern)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: cpk.py <archive.cpk> [<dest>] [--list] [--filter=<text>]")
        sys.exit(2)
    extract(*parse_args(sys.argv[1:]))

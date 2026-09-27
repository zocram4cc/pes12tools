"""Fox Engine .ftex texture -> DDS/PNG (PES 2019-2021 single-file variant).

Format knowledge follows the 4cc community's pes-fmdl Ftex module.

Header (little-endian, 64 bytes):
  4s   "FTEX"
  f32  version (2.03 for PES21)
  u16  pixelFormat, u16 height, u16 width, u16 depth
  u8   mipCount, u8 nrtFlag, u16 flags, u32, u32, u32 textureType
  u8   ftexsCount (0 = data inline), u8, 14x pad, 16s hash

Then mipCount x 16-byte mip infos:
  u32 offset, u32 uncompressedSize, u32 compressedSize,
  u8 index, u8 ftexsNumber, u16 chunkCount

Mip data at `offset`: chunkCount == 0 -> one blob (zlib if compressedSize
else raw); otherwise chunkCount x { u16 compSize, u16 uncompSize,
u32 offset (bit31 set = stored raw) } indexes relative to the mip offset.
"""

import io
import struct
import sys
import zlib

# ftex pixelFormat -> (DDS FourCC, DXGI format for DX10 extension)
FORMATS = {
    0: (None, None),        # RGBA8, plain
    1: (b"DX10", 61),       # R8_UNORM
    2: (b"DXT1", None),
    3: (b"DXT3", None),
    4: (b"DXT5", None),
    8: (b"DX10", 80),       # BC4
    9: (b"DX10", 83),       # BC5
    10: (b"DX10", 95),      # BC6H
    11: (b"DX10", 98),      # BC7
    12: (b"DX10", 10),      # R16G16B16A16_FLOAT
    13: (b"DX10", 2),       # R32G32B32A32_FLOAT
    14: (b"DX10", 24),      # R10G10B10A2_UNORM
    15: (b"DX10", 26),      # R11G11B10_FLOAT
}


def _read_mip(blob, offset, chunk_count, uncomp_size, comp_size):
    if chunk_count == 0:
        if comp_size == 0:
            return blob[offset:offset + uncomp_size]
        return zlib.decompress(blob[offset:offset + comp_size])
    out = []
    at = offset
    for _ in range(chunk_count):
        comp, uncomp, rel = struct.unpack_from("<HHI", blob, at)
        at += 8
        # bit 31 marks a stored chunk, but the smallest mips (a single 16-byte
        # DXT block) are sometimes stored plain without that flag set, so an
        # equal compressed size, or a chunk zlib cannot read, means stored too
        raw = bool(rel & 0x80000000) or comp == uncomp
        rel &= 0x7FFFFFFF
        data = blob[offset + rel:offset + rel + comp]
        if not raw:
            try:
                data = zlib.decompress(data)
            except zlib.error:
                pass
        out.append(data)
    return b"".join(out)


def parse(blob: bytes):
    # Width first, then height. Read the other way round it is invisible on a
    # square texture - most of PES's are - and shears every other one: st002's
    # pitch crest came out as two hatched half-crests, and the ad boards the same.
    magic, version, pf, width, height, depth, mips, _nrt, _flags = \
        struct.unpack_from("<4sfHHHHBBH", blob, 0)
    if magic != b"FTEX":
        raise ValueError("not an ftex")
    ftexs_count = blob[0x20]
    if ftexs_count > 0:
        raise ValueError("ftexs sidecar files not supported (TPP-style ftex)")
    texture_type = struct.unpack_from("<I", blob, 0x18)[0]
    image_count = 6 if texture_type & 4 else 1

    frames = []
    at = 64
    for _img in range(image_count):
        for j in range(mips):
            offset, uncomp, comp, index, _ftexs, chunks = \
                struct.unpack_from("<IIIBBH", blob, at)
            at += 16
            frames.append(_read_mip(blob, offset, chunks, uncomp, comp))
    return pf, width, height, depth, texture_type, frames


def to_dds(blob: bytes) -> bytes:
    pf, width, height, depth, texture_type, frames = parse(blob)
    if pf not in FORMATS:
        raise ValueError("unhandled ftex pixel format %d" % pf)
    fourcc, dxgi = FORMATS[pf]
    mips = len(frames) if not (texture_type & 4) else len(frames) // 6

    flags = 0x1 | 0x2 | 0x4 | 0x1000
    caps1 = 0x1000
    caps2 = 0
    if texture_type & 4:
        caps1 |= 0x8
        caps2 |= 0xFE00
    if mips > 1:
        flags |= 0x20000
        caps1 |= 0x8 | 0x400000

    if pf == 0:
        flags |= 0x8  # pitch
        pitch_or_linear = 4 * width
        pixfmt = struct.pack("<II4sIIIII", 32, 0x41, b"\0\0\0\0", 32,
                             0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    else:
        flags |= 0x80000  # linear size
        pitch_or_linear = len(frames[0])
        pixfmt = struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)

    header = struct.pack("<4sIIIIII44x", b"DDS ", 124, flags, height, width,
                         pitch_or_linear, depth if depth > 1 else 0)
    header += struct.pack("<I", mips if mips > 1 else 0)[0:0]  # (mip count folded below)
    # rebuild with mip count in the right slot: DDS header layout is
    # size, flags, height, width, pitch, depth, mipCount, 11 reserved dwords
    header = struct.pack("<4sIIIIIII44x", b"DDS ", 124, flags, height, width,
                         pitch_or_linear, depth if depth > 1 else 0,
                         mips if mips > 1 else 0)
    header += pixfmt
    header += struct.pack("<IIIII", caps1, caps2, 0, 0, 0)
    if fourcc == b"DX10":
        dimension = 4 if depth > 1 else 3
        misc = 0x4 if texture_type & 4 else 0
        header += struct.pack("<IIIII", dxgi, dimension, misc, 1, 0)
    return header + b"".join(frames)


def convert(ftex_path, out_path):
    dds = to_dds(open(ftex_path, "rb").read())
    if out_path.lower().endswith(".dds"):
        open(out_path, "wb").write(dds)
    else:
        from PIL import Image
        Image.open(io.BytesIO(dds)).save(out_path)


if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])
    print("wrote", sys.argv[2])

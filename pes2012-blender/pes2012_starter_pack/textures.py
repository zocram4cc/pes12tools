# SPDX-License-Identifier: GPL-3.0-or-later
"""WE00 texture blocks <-> RGBA pixels, numpy only (Blender's Python has no PIL).

A WE00 block is a 16-byte header ('WE00', u16 texture id, u8, u8 kind,
then kind-specific u32/u16 fields) and the texture. In the stock game
(corpus census 01-10): 5128 DDS DXT1/DXT5, 6 DXT3, 1 uncompressed DDS
(dt04 #35), and 2 raw blocks (dt0b #1 32-bit, dt0b #35 8-bit palette).

decode(block) -> (w, h, rgba): top level, rows top-down, uint8 HxWx4.
encode(block, rgba) -> a block of the same format, size and mip count with
the pixels replaced and every mip rebuilt from them; the header bytes are
kept. Unchanged pixels should not be encoded at all: keep the block (DXT is
lossy; encode(decode(b)) != b).
"""
import struct

import numpy as np

MAGIC = b'WE00'
HEADER = 16
ID_OFF = 4                    # u16 texture id (textureNameIds rows hold it as u32)
KIND_OFF = 7                  # 0xFF = a DDS follows the header, else bits per pixel (raw)
KIND_DDS = 0xFF
RAW_FIELDS_OFF = 8            # raw: u16 w, h, palette offset, pixel offset (from block start)
PALETTE_ENTRIES = 256         # 8-bit raw: RGBA palette

DDS_MAGIC = b'DDS '
DDS_HEADER = 128
DDS_H_OFF, DDS_W_OFF, DDS_MIPS_OFF = 12, 16, 28
DDS_PF_FLAGS_OFF, DDS_FOURCC_OFF, DDS_BITS_OFF, DDS_MASKS_OFF = 80, 84, 88, 92
DDPF_ALPHAPIXELS = 0x1
BLOCK_PX = 4                  # DXT: 4x4 texel blocks
DXT_BLOCK_BYTES = {b'DXT1': 8, b'DXT3': 16, b'DXT5': 16}
DXT1_PUNCH_ALPHA = 128        # DXT1 texels below this alpha use the transparent index
POWER_ITERATIONS = 6          # principal axis per block for the colour endpoints


def texture_id(block):
    return struct.unpack_from('<H', block, ID_OFF)[0]


def is_texture(block):
    return block[:4] == MAGIC and len(block) > HEADER


def _dds(block):
    if block[KIND_OFF] != KIND_DDS or block[HEADER:HEADER + 4] != DDS_MAGIC:
        return None
    h, w = struct.unpack_from('<II', block, HEADER + DDS_H_OFF)
    mips = max(1, struct.unpack_from('<I', block, HEADER + DDS_MIPS_OFF)[0])
    return w, h, mips, block[HEADER + DDS_FOURCC_OFF:HEADER + DDS_FOURCC_OFF + 4]


def _mip_sizes(w, h, mips):
    out = []
    for _ in range(mips):
        out.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return out


def _dxt_level_bytes(w, h, four):
    return ((w + 3) // 4) * ((h + 3) // 4) * DXT_BLOCK_BYTES[four]


# --- DXT decode -------------------------------------------------------------

def _565(c):
    c = c.astype(np.uint32)
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], -1).astype(np.int32)


def _decode_colour(blk, dxt1):
    """blk: N x 8 uint8 (c0, c1, 32 index bits) -> N x 16 x 4 RGBA."""
    c0 = blk[:, 0].astype(np.uint16) | (blk[:, 1].astype(np.uint16) << 8)
    c1 = blk[:, 2].astype(np.uint16) | (blk[:, 3].astype(np.uint16) << 8)
    p0, p1 = _565(c0), _565(c1)
    four = (c0 > c1) | (not dxt1)
    pal = np.empty((len(blk), 4, 4), np.int32)
    pal[:, 0, :3], pal[:, 1, :3] = p0, p1
    pal[:, 2, :3] = np.where(four[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)
    pal[:, 3, :3] = np.where(four[:, None], (p0 + 2 * p1) // 3, 0)
    pal[:, :, 3] = 255
    if dxt1:
        pal[:, 3, 3] = np.where(four, 255, 0)
    bits = blk[:, 4:8].astype(np.uint32)
    word = bits[:, 0] | (bits[:, 1] << 8) | (bits[:, 2] << 16) | (bits[:, 3] << 24)
    idx = (word[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3
    return np.take_along_axis(pal, idx[:, :, None].astype(np.int64), 1)


def _decode_alpha5(blk):
    a0, a1 = blk[:, 0].astype(np.int32), blk[:, 1].astype(np.int32)
    pal = np.empty((len(blk), 8), np.int32)
    pal[:, 0], pal[:, 1] = a0, a1
    eight = a0 > a1
    for k in range(1, 7):   # 8-level: (7-k)a0 + k a1 over 7; 6-level: (5-k)a0 + k a1 over 5, then 0/255
        pal[:, k + 1] = np.where(eight, ((7 - k) * a0 + k * a1) // 7,
                                 ((5 - k) * a0 + k * a1) // 5 if k < 5 else (0 if k == 5 else 255))
    bits = np.zeros(len(blk), np.uint64)
    for i in range(6):
        bits |= blk[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
    idx = (bits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)
    return np.take_along_axis(pal, idx.astype(np.int64), 1)


def _decode_dxt(data, w, h, four):
    nbx, nby = (w + 3) // 4, (h + 3) // 4
    bb = DXT_BLOCK_BYTES[four]
    blk = np.frombuffer(data, np.uint8, nbx * nby * bb).reshape(-1, bb)
    if four == b'DXT1':
        px = _decode_colour(blk, True)
    else:
        px = _decode_colour(blk[:, 8:], False)
        if four == b'DXT5':
            px[:, :, 3] = _decode_alpha5(blk[:, :8])
        else:
            nib = blk[:, :8].astype(np.int32)
            a = np.stack([nib & 15, nib >> 4], -1).reshape(len(blk), 16)
            px[:, :, 3] = a * 17
    img = px.reshape(nby, nbx, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(nby * 4, nbx * 4, 4)
    return img[:h, :w].astype(np.uint8)


# --- DXT encode -------------------------------------------------------------

def _to565(c):
    c = np.clip(np.rint(c), 0, 255).astype(np.int32)
    return ((c[..., 0] >> 3) << 11) | ((c[..., 1] >> 2) << 5) | (c[..., 2] >> 3)


def _blocks(img):
    """HxWx4 -> N x 16 x 4 float, edge-padded to whole blocks."""
    h, w = img.shape[:2]
    ph, pw = -h % BLOCK_PX, -w % BLOCK_PX
    if ph or pw:
        img = np.pad(img, ((0, ph), (0, pw), (0, 0)), mode='edge')
    nby, nbx = img.shape[0] // 4, img.shape[1] // 4
    return img.reshape(nby, 4, nbx, 4, 4).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 4).astype(np.float32)


def _encode_colour(px, punch):
    """px: N x 16 x 4 -> N x 8. punch (N bools): DXT1 3-colour + transparent."""
    rgb = px[:, :, :3]
    mean = rgb.mean(1, keepdims=True)
    d = rgb - mean
    cov = np.einsum('nki,nkj->nij', d, d)
    axis = np.ones((len(px), 3), np.float32)
    for _ in range(POWER_ITERATIONS):
        axis = np.einsum('nij,nj->ni', cov, axis)
        axis /= np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-6)
    t = np.einsum('nki,ni->nk', d, axis)
    lo = mean[:, 0] + axis * t.min(1, keepdims=True)
    hi = mean[:, 0] + axis * t.max(1, keepdims=True)
    c0, c1 = _to565(hi), _to565(lo)
    # 4-colour mode needs c0 > c1, punch-through needs c0 <= c1
    swap = np.where(punch, c0 > c1, c0 < c1)
    c0, c1 = np.where(swap, c1, c0), np.where(swap, c0, c1)
    p0, p1 = _565(c0.astype(np.uint16)).astype(np.float32), _565(c1.astype(np.uint16)).astype(np.float32)
    four = ~punch
    pal = np.stack([p0, p1,
                    np.where(four[:, None], (2 * p0 + p1) / 3, (p0 + p1) / 2),
                    np.where(four[:, None], (p0 + 2 * p1) / 3, np.inf)], 1)
    dist = ((rgb[:, :, None, :] - pal[:, None, :, :]) ** 2).sum(-1)
    dist = np.nan_to_num(dist, nan=np.inf, posinf=np.inf)
    idx = dist.argmin(2)
    if punch.any():
        transparent = punch[:, None] & (px[:, :, 3] < DXT1_PUNCH_ALPHA)
        idx = np.where(transparent, 3, np.where(punch[:, None] & (idx == 3), 2, idx))
    idx = np.where((c0 == c1)[:, None] & four[:, None], 0, idx)
    word = (idx.astype(np.uint32) << (2 * np.arange(16, dtype=np.uint32))).sum(1).astype(np.uint32)
    out = np.empty((len(px), 8), np.uint8)
    out[:, 0], out[:, 1] = c0 & 255, c0 >> 8
    out[:, 2], out[:, 3] = c1 & 255, c1 >> 8
    for i in range(4):
        out[:, 4 + i] = (word >> (8 * i)) & 255
    return out


def _encode_alpha5(a):
    a0 = a.max(1).astype(np.int32)
    a1 = a.min(1).astype(np.int32)
    flat = a0 == a1
    a1 = np.where(flat, a1, a1)
    k = np.arange(1, 7)
    pal = np.concatenate([a0[:, None], a1[:, None], ((7 - k) * a0[:, None] + k * a1[:, None]) // 7], 1)
    idx = np.abs(a[:, :, None] - pal[:, None, :]).argmin(2)
    idx = np.where(flat[:, None], 0, idx)
    bits = (idx.astype(np.uint64) << (np.uint64(3) * np.arange(16, dtype=np.uint64))).sum(1)
    out = np.empty((len(a), 8), np.uint8)
    out[:, 0], out[:, 1] = a0, a1
    for i in range(6):
        out[:, 2 + i] = (bits >> np.uint64(8 * i)) & np.uint64(255)
    return out


def _encode_dxt(img, four):
    px = _blocks(img)
    if four == b'DXT1':
        punch = (px[:, :, 3] < DXT1_PUNCH_ALPHA).any(1)
        return _encode_colour(px, punch).tobytes()
    colour = _encode_colour(px, np.zeros(len(px), bool))
    if four == b'DXT5':
        alpha = _encode_alpha5(px[:, :, 3])
    else:
        nib = np.clip(np.rint(px[:, :, 3] / 17), 0, 15).astype(np.uint8)
        alpha = (nib[:, 0::2] | (nib[:, 1::2] << 4)).astype(np.uint8)
    return np.concatenate([alpha, colour], 1).tobytes()


# --- mips, masks, raw ---------------------------------------------------------

def _downsample(img):
    h, w = img.shape[:2]
    nh, nw = max(1, h // 2), max(1, w // 2)
    f = img[:nh * (2 if h > 1 else 1), :nw * (2 if w > 1 else 1)].astype(np.float32)
    f = f.reshape(nh, f.shape[0] // nh, nw, f.shape[1] // nw, 4).mean((1, 3))
    return np.clip(np.rint(f), 0, 255).astype(np.uint8)


def _masks(block):
    flags = struct.unpack_from('<I', block, HEADER + DDS_PF_FLAGS_OFF)[0]
    bits = struct.unpack_from('<I', block, HEADER + DDS_BITS_OFF)[0]
    masks = list(struct.unpack_from('<4I', block, HEADER + DDS_MASKS_OFF))
    if not flags & DDPF_ALPHAPIXELS:
        masks[3] = 0
    return bits, masks


def _shift(m):
    return (m & -m).bit_length() - 1 if m else 0


def _decode_masked(data, w, h, bits, masks):
    bpp = bits // 8
    raw = np.frombuffer(data, np.uint8, w * h * bpp).reshape(-1, bpp).astype(np.uint32)
    v = sum(raw[:, i] << (8 * i) for i in range(bpp))
    out = np.empty((w * h, 4), np.uint8)
    for c, m in enumerate(masks):
        if m:
            top = (m >> _shift(m))
            out[:, c] = ((v & m) >> _shift(m)) * 255 // top
        else:
            out[:, c] = 255
    return out.reshape(h, w, 4)


def _encode_masked(img, bits, masks):
    px = img.reshape(-1, 4).astype(np.uint32)
    v = np.zeros(len(px), np.uint32)
    for c, m in enumerate(masks):
        if m:
            top = m >> _shift(m)
            v |= ((px[:, c] * top + 127) // 255) << _shift(m)
    bpp = bits // 8
    return np.stack([(v >> (8 * i)) & 255 for i in range(bpp)], 1).astype(np.uint8).tobytes()


def _raw_fields(block):
    return struct.unpack_from('<4H', block, RAW_FIELDS_OFF)


# --- API ----------------------------------------------------------------------

def decode(block):
    """WE00 block -> (w, h, rgba uint8 HxWx4, top level, rows top-down)."""
    dds = _dds(block)
    if dds:
        w, h, _, four = dds
        data = block[HEADER + DDS_HEADER:]
        if four in DXT_BLOCK_BYTES:
            return w, h, _decode_dxt(data, w, h, four)
        bits, masks = _masks(block)
        return w, h, _decode_masked(data, w, h, bits, masks)
    bpp = block[KIND_OFF]
    w, h, pal, pix = _raw_fields(block)
    if bpp == 8:
        p = np.frombuffer(block, np.uint8, PALETTE_ENTRIES * 4, pal).reshape(-1, 4)
        idx = np.frombuffer(block, np.uint8, w * h, pix)
        return w, h, p[idx].reshape(h, w, 4).copy()
    if bpp == 32:
        return w, h, np.frombuffer(block, np.uint8, w * h * 4, pix).reshape(h, w, 4).copy()
    raise ValueError('WE00 texture %d: kind %d not supported' % (texture_id(block), bpp))


def encode(block, rgba):
    """Replace a WE00 block's pixels (HxWx4 uint8, the block's own size)."""
    w, h, _ = decode(block)
    rgba = np.ascontiguousarray(rgba, np.uint8)
    if rgba.shape != (h, w, 4):
        raise ValueError('texture %d is %dx%d, image is %dx%d' % (texture_id(block), w, h, rgba.shape[1], rgba.shape[0]))
    dds = _dds(block)
    if dds:
        _, _, mips, four = dds
        levels, img = [], rgba
        for lw, lh in _mip_sizes(w, h, mips):
            levels.append(_encode_dxt(img, four) if four in DXT_BLOCK_BYTES else _encode_masked(img, *_masks(block)))
            img = _downsample(img)
        payload = b''.join(levels)
        old = len(block) - HEADER - DDS_HEADER
        if len(payload) > old:
            raise ValueError('texture %d: %d payload bytes, block holds %d' % (texture_id(block), len(payload), old))
        return block[:HEADER + DDS_HEADER] + payload + block[HEADER + DDS_HEADER + len(payload):]
    bpp = block[KIND_OFF]
    _, _, pal, pix = _raw_fields(block)
    out = bytearray(block)
    if bpp == 32:
        out[pix:pix + w * h * 4] = rgba.tobytes()
    else:
        # ponytail: nearest colour of the block's own palette; a re-quantised
        # palette if an 8-bit texture (two balls) ever needs new colours
        p = np.frombuffer(block, np.uint8, PALETTE_ENTRIES * 4, pal).reshape(-1, 4).astype(np.int32)
        flat = rgba.reshape(-1, 4).astype(np.int32)
        idx = np.empty(len(flat), np.uint8)
        for s in range(0, len(flat), 4096):
            idx[s:s + 4096] = ((flat[s:s + 4096, None, :] - p[None]) ** 2).sum(-1).argmin(1)
        out[pix:pix + w * h] = idx.tobytes()
    return bytes(out)


# --- which texture block a model's texture row means ---------------------------

def bind(container_blocks, row_ids):
    """{texture row id: index of the WE00 block in container_blocks} for the
    model rows that this BIN itself carries a texture for.

    Two rules, both checked on every stock entry (01-10): a block whose id
    equals a row id is that row (faces, some bodies); a BIN with exactly as
    many texture blocks as distinct row ids and no id overlap maps them in
    order, rows sorted by id against blocks in file order (balls: c/n/s rows
    2/3/4 = DXT1/DXT5/DXT1 blocks on all 32; boots ek*: c/nml/spe rows
    901/980/981 = colour/normal/specular blocks). Rows with no block here
    sample shared textures elsewhere in the game (stadiums, 3400 dt0c kits)
    and are left out. Faces: a face BIN carries ONE texture, id 0, for
    its skin row FACE_SKIN_ROW (f_argNNNN / head_base: all 3184 dt0c/dt0d
    single-texture face BINs); the eyes, mouth and maps are shared."""
    tex = [k for k, b in enumerate(container_blocks) if is_texture(b)]
    ids = {texture_id(container_blocks[k]): k for k in tex}
    rows = sorted(set(row_ids))
    direct = {r: ids[r] for r in rows if r in ids}
    if direct:
        return direct
    if len(rows) == len(tex):
        return dict(zip(rows, tex))
    if len(tex) == 1 and FACE_SKIN_ROW in rows:
        return {FACE_SKIN_ROW: tex[0]}
    return {}


FACE_SKIN_ROW = 200

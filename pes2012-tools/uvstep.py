"""UVStep animated materials for PES2012 custom bodies (PGB2).

PES2019+ plays animation frames stored as a grid on one texture
(left-to-right, top-to-bottom), either on a uniform cycle or driven by a
timing texture (its R picks the frame, a transparent texel draws nothing).
This module is the offline half: the per-sub trailer codec (body.bin),
a pure-python mirror of the pixel-shader frame math, and an authoring CLI
that builds frame-grid and timing textures from plain images.

    python3 tools/uvstep.py grid <out.png> <frame...> [--tiles-u N] [--tiles-v N]
    python3 tools/uvstep.py timing <out.png> <spec> [--width N] [--height N]
    python3 tools/uvstep.py tex <body_K.tex> <image.png>

Grid inputs are image files; one may be an animated GIF (every frame is
taken, in order). All frames must share one size - resampling authored art
here would blur it silently, so mismatches are an error.
Timing <spec> is a comma list of <seconds> (frames 0, 1, ... in order) or
<frame>:<seconds>[:<alpha>]; the strip is time left-to-right over one
u-cycle, so durations become band widths. Taller strips repeat the row;
paint per-part visibility schedules (transparent texels) by hand.

Provenance: PES2019+ fox3dfw_constant_srgb_ndr_uvstep / fox3ddf_blin_uvstep
semantics (owner wiki); the shader itself is dllprobe/custom_ps.hlsl
(entries uvstep / uvstep_lit), the runtime dllprobe/drawlogic.cpp.
Units: seconds, tile counts in texels-of-tiles (ints).
"""
import os
import struct
import sys

# body.bin trailer after the indices (and after any HRIG section, order free;
# drawlogic scans sections by magic, old files/DLLs are unaffected).
USTEP_MAGIC = b'USTP'
# one entry: tile counts (positive ints), frames used (<= U*V), uniform vs
# timing mode, cycle seconds, timing scroll seconds per axis (0 = no motion),
# timing texture slot (0xFF = none), scale flag. Reserved bytes are zero.
USTEP_ENTRY_FMT = '<BBBB3fBBBB'
USTEP_ENTRY_BYTES = struct.calcsize(USTEP_ENTRY_FMT)
USTEP_HEADER_FMT = '<4sI'
NO_TIMING_TEX = 0xFF           # entry.timing_tex: no timing texture bound
MAX_TILES = 255                # counts ride one byte each

# submesh flag bits (drawlogic.cpp SUB_*; 22/23 were free: ref byte is 8-15,
# shaders/hands are 16-21).
SUB_UVSTEP = 1 << 22           # frame-grid animation on this submesh
SUB_UVSTEP_LIT = 1 << 23       # lit variant (toon lighting); without it, constant


def make_params(tile_u=1, tile_v=1, tiles_used=1, use_timing=False,
                sec_cycle=0.0, sec_u=0.0, sec_v=0.0, timing_tex=NO_TIMING_TEX,
                scale=False):
    """Authoring-side params -> plain dict (also the Blender material props)."""
    if not (1 <= tile_u <= MAX_TILES and 1 <= tile_v <= MAX_TILES):
        raise ValueError('tile counts must be 1..%d' % MAX_TILES)
    if not 1 <= tiles_used <= tile_u * tile_v:
        raise ValueError('tiles_used must be 1..U*V')
    return dict(tile_u=int(tile_u), tile_v=int(tile_v),
                tiles_used=int(tiles_used), use_timing=bool(use_timing),
                sec_cycle=float(sec_cycle), sec_u=float(sec_u),
                sec_v=float(sec_v), timing_tex=int(timing_tex),
                scale=bool(scale))


def pack_entry(p):
    """params dict -> 20 trailer bytes."""
    return struct.pack(USTEP_ENTRY_FMT, p['tile_u'], p['tile_v'],
                       p['tiles_used'], 1 if p['use_timing'] else 0,
                       p['sec_cycle'], p['sec_u'], p['sec_v'],
                       p['timing_tex'], 1 if p['scale'] else 0, 0, 0)


def unpack_entry(b):
    """20 trailer bytes -> params dict."""
    tu, tv, used, mode, cyc, su, sv, ttex, sc, _r1, _r2 = struct.unpack(
        USTEP_ENTRY_FMT, b)
    return dict(tile_u=tu, tile_v=tv, tiles_used=used, use_timing=bool(mode),
                sec_cycle=cyc, sec_u=su, sec_v=sv, timing_tex=ttex,
                scale=bool(sc))


def build_trailer(entries):
    """{sub_index: params} -> USTP trailer bytes (empty dict -> b'')."""
    if not entries:
        return b''
    out = bytearray(struct.pack(USTEP_HEADER_FMT, USTEP_MAGIC, len(entries)))
    for sub in sorted(entries):
        out += struct.pack('<I', sub) + pack_entry(entries[sub])
    return bytes(out)


def find_trailer(trailer):
    """Raw trailer bytes -> USTP section bytes or None (HRIG-agnostic scan)."""
    off = 0
    while off + 4 <= len(trailer):
        magic = trailer[off:off + 4]
        if magic == USTEP_MAGIC:
            return trailer[off:]
        off += 4
    return None


def strip_trailer(trailer):
    """Raw trailer bytes with any USTP section removed (HRIG kept)."""
    sec = find_trailer(trailer or b'')
    if sec is None:
        return trailer or b''
    off = (trailer or b'').find(sec)
    head = (trailer or b'')[:off]
    (count,) = struct.unpack_from('<I', sec, 4)
    tail = sec[8 + count * (4 + USTEP_ENTRY_BYTES):]
    return head + tail


def parse_trailer(trailer):
    """Raw trailer bytes -> {sub_index: params} ({} when no USTP section)."""
    sec = find_trailer(trailer or b'')
    if sec is None:
        return {}
    (count,) = struct.unpack_from('<I', sec, 4)
    out = {}
    for k in range(count):
        base = 8 + k * (4 + USTEP_ENTRY_BYTES)
        (sub,) = struct.unpack_from('<I', sec, base)
        out[sub] = unpack_entry(sec[base + 4:base + 4 + USTEP_ENTRY_BYTES])
    return out


# ---- the pixel-shader math, mirrored (custom_ps.hlsl uvstep / uvstep_lit) ----

def r_to_frame(r, tiles_used):
    """Timing texel R (0..255) -> frame: R*Used/256 bands, 0 first, 255 last."""
    return min((int(r) * tiles_used) // 256, tiles_used - 1)


def frame_to_r(frame, tiles_used):
    """Frame -> band-centre R (for authoring timing strips)."""
    return min(255, (int(frame) * 256 + 128) // tiles_used)


def frame_at(p, t, timing_r=None, timing_a=255):
    """(frame, visible) at t seconds. timing_r/a: the sampled timing texel
    (uniform mode ignores both; a transparent texel draws nothing)."""
    if p['use_timing']:
        return r_to_frame(timing_r if timing_r is not None else 0,
                          p['tiles_used']), timing_a != 0
    if p['sec_cycle'] <= 0.0:
        return 0, True            # a zero cycle stays on frame 0
    return int((t % p['sec_cycle']) / p['sec_cycle']
               * p['tiles_used']) % p['tiles_used'], True


def tile_uv(p, uv, frame):
    """Main UVs + frame -> grid UVs. scale: authored UVs span the whole
    texture and shrink into one tile; else they already sit in the top-left
    tile. Grid plays left-to-right, top-to-bottom (v = 0 at the top)."""
    tu, tv = p['tile_u'], p['tile_v']
    lu, lv = (uv[0] / tu, uv[1] / tv) if p['scale'] else (uv[0], uv[1])
    col, row = frame % tu, frame // tu
    return (col + lu) / tu, (row + lv) / tv


def timing_lookup(timing, uv, t, sec_u, sec_v):
    """RGBA timing image (H x W x 4) sampled at timing UVs + time scroll
    (0 cycle = no motion on that axis; UVs wrap) -> (R, A)."""
    from PIL import Image
    import numpy as np
    px = np.asarray(timing.convert('RGBA')).astype(int)
    h, w = px.shape[:2]
    ou = (t / sec_u) if sec_u > 0.0 else 0.0
    ov = (t / sec_v) if sec_v > 0.0 else 0.0
    x = int(((uv[0] + ou) % 1.0) * w) % w
    y = int(((uv[1] + ov) % 1.0) * h) % h
    r, _g, _b, a = (int(v) for v in px[y, x])
    _ = _g, _b
    return r, a


# ---- authoring builders (PIL on the CLI side; Blender has no PIL) ----

def load_frames(paths):
    """Image paths (one may be an animated GIF) -> [RGBA images], one size."""
    from PIL import Image
    out = []
    for path in paths:
        im = Image.open(path)
        for k in range(getattr(im, 'n_frames', 1)):
            im.seek(k)
            out.append(im.convert('RGBA'))
    sizes = {im.size for im in out}
    if len(sizes) != 1:
        raise ValueError('all frames must share one size, got %s' % sorted(sizes))
    if not out:
        raise ValueError('no frames')
    return out


def build_grid(frames, tile_u=0, tile_v=0):
    """[images] -> (grid image, tile_u, tile_v). Counts: explicit, else the
    smallest grid holding every frame (tiles_used = len(frames))."""
    n = len(frames)
    if tile_u or tile_v:
        tile_u, tile_v = int(tile_u or 1), int(tile_v or 1)
    else:
        import math
        tile_u = int(math.ceil(n ** 0.5))
        tile_v = int(math.ceil(n / tile_u))
    if tile_u * tile_v < n:
        raise ValueError('%d frames do not fit %dx%d' % (n, tile_u, tile_v))
    from PIL import Image
    fw, fh = frames[0].size
    grid = Image.new('RGBA', (fw * tile_u, fh * tile_v), (0, 0, 0, 0))
    for k, im in enumerate(frames):
        grid.paste(im, ((k % tile_u) * fw, (k // tile_u) * fh))
    return grid, tile_u, tile_v


def parse_timing_spec(spec):
    """'1.0,2.0' | '0:1.0,2:0.5' | '1:2.0:0' -> [(frame, seconds, alpha)]."""
    out = []
    for item in spec.split(','):
        parts = item.split(':')
        if len(parts) == 1:
            out.append((len(out), float(parts[0]), 255))
        else:
            frame, secs = int(parts[0]), float(parts[1])
            out.append((frame, secs, int(parts[2]) if len(parts) > 2 else 255))
    if any(s <= 0.0 for _, s, _ in out):
        raise ValueError('durations must be positive')
    return out


def build_timing(spec, tiles_used, width=256, height=1):
    """Timing spec -> width x height strip: band widths are durations, R is
    the band-centre frame, alpha rides along (0 = draw nothing there)."""
    from PIL import Image
    total = sum(s for _, s, _ in spec)
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    px = img.load()
    edges, acc = [], 0.0
    for _f, s, _a in spec:
        acc += s
        edges.append(acc / total)
    k = 0
    for x in range(width):
        frac = (x + 0.5) / width
        while k + 1 < len(spec) and frac >= edges[k]:
            k += 1
        frame, _s, alpha = spec[k]
        for y in range(height):
            px[x, y] = (frame_to_r(frame, tiles_used), 0, 0, alpha)
    return img


def main(argv):
    from PIL import Image
    if len(argv) < 4 or argv[1] not in ('grid', 'timing', 'tex'):
        raise SystemExit('usage: uvstep.py grid <out.png> <frame...> [--tiles-u N] [--tiles-v N]\n'
                         '       uvstep.py timing <out.png> <spec> [--width N] [--height N]\n'
                         '       uvstep.py tex <body_K.tex> <image.png>')
    cmd, out = argv[1], argv[2]
    rest, opts = [], {}
    for a in argv[3:]:
        if a.startswith('--'):
            k, _, v = a[2:].partition('=')
            opts[k.replace('-', '_')] = v if _ else True
        else:
            rest.append(a)
    if cmd == 'tex':   # an authored image -> the PGT1 texture drawlogic loads as body_<k>.tex
        from fmdl_to_pes12 import write_tex
        write_tex(rest[0], out)
        print('%s: %dx%d PGT1' % ((out,) + Image.open(rest[0]).size))
    elif cmd == 'grid':
        grid_frames = load_frames(rest)
        grid, tu, tv = build_grid(
            grid_frames, int(opts.get('tiles_u', 0)),
            int(opts.get('tiles_v', 0)))
        grid.save(out)
        print('%s: %d frames in %dx%d' % (out, len(grid_frames), tu, tv))
    else:
        if not rest:
            raise SystemExit('timing needs a <frame>:<seconds> spec')
        spec = parse_timing_spec(rest[0])
        used = max(f for f, _s, _a in spec) + 1
        img = build_timing(spec, used, int(opts.get('width', 256)),
                           int(opts.get('height', 1)))
        img.save(out)
        print('%s: %d bands, %d frames, %.2fs cycle' % (
            out, len(spec), used, sum(s for _f, s, _a in spec)))


if __name__ == '__main__':
    main(sys.argv)

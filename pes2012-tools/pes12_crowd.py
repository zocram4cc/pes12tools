"""PES2012 stadium crowd blocks: decode, build and validate, pure python.

g+32 block 2 (see 10-stadiums.md, pes12_stadium.CROWD_BLOCK): u32 table
size T, (T-4)/4 section offsets, one more section at offset T. A section
is u32 rows, f3 centre, f3 extent (28 B), then 100 B per row:

    u8 side, seats, fill, variant, crowd2, flags2, 0, 0 (8 B)
    f3 facing: the 2D direction the row is watched from (z = 0, unit)
    f3 P0, f3 P1, f3 P2, f3 P3 (48 B): the seat quad, absolute metres
    f2 uv0, f2 uv1, f2 uv2, f2 uv3 (16 B): crowd texture rect

Field semantics (measured over all 31 stock slots, 9950 rows):

- centre/extent = the exact bounding box of the section's quads (max
  deviation 5 cm, only where a section mixes row types).
- facing = horizontal, always: it is the direction the crowd faces, i.e.
  from the stand towards the pitch (verified per section against the
  section centre on slots 1 and 30).
- side (0/1): which length the row is watched from. Quads are
  parallelograms (P1-P0 parallel P3-P2, checked); P0-P1 is the long edge
  for side 1 (median aspect 1.9, long edge 10.5 m) and the short edge for
  side 0 (median aspect 0.7, 8.9 m).
- seats (0-42): crowd pattern selector, kept as is. fill (0/100) and
  variant/flags2 (shared 0..100 value set) likewise: no correlation with
  quad size, density, UV span or section was found, so authored rows copy
  them from a template row.
- uv: a sub-rect of the crowd texture both kinds share (u/v full range
  0..1 both).

PES15/17 `audi/area/stNNN/data/audiarea.bin` (the OPTIONAL import source)
is quads too: patch = u16 index, u16 kind (1 = crowd), u16 data offset;
u32 box (min xyz, max xyz, 0, 0, row spacing, seat width twice) then
96 B quads (P0..P3, uv rect, 0, density, seat width twice) and one
80 B last quad (corners + uv, patch defaults). st020 nests one more
level (sub-patches with their own boxes); the reader flattens them.
"""

import os
import struct
import zlib

# --- PES2012 row layout -----------------------------------------------------

# Meaningful literals: struct sizes and provenance, see module docstring.
ROW_BYTES = 100
SECTION_HEADER_BYTES = 28
UV_FLOATS = 8
# A section box must contain its rows (10-stadiums.md): cameras cull on it.
# Stock boxes are usually the exact bounds (to 5 cm); some span neighbouring
# sections instead, so authored boxes are recomputed exactly (bbox()).
BOX_TOLERANCE_M = 0.05
FLAT_ROW_DY_M = 0.5        # top/bottom edges closer than this: no slope to face down (340 stock rows)

Row = tuple  # (header8 bytes, facing3, corners4 x xyz, uv8)


def split(block):
    """Crowd block -> [(rows, centre, extent, offset)]: rows = [(header8,
    facing, (P0, P1, P2, P3), uv8)]; offset = the section's byte offset,
    kept so join() rebuilds the identical block. The last section lives at
    offset T (first in the body); the rest follow in ascending order
    (measured on every stock slot: each starts where the previous ends)."""
    size = struct.unpack_from("<I", block)[0]
    count = (size - 4) // 4 + 1
    heads = list(struct.unpack_from("<%dI" % (count - 1), block, 4)) + [size] if count > 1 else [size]
    out = []
    for off in heads:
        rows, centre, extent = struct.unpack_from("<I", block, off)[0], struct.unpack_from("<3f", block, off + 4), struct.unpack_from("<3f", block, off + 16)
        recs = []
        for k in range(rows):
            base = off + SECTION_HEADER_BYTES + k * ROW_BYTES
            header = bytes(block[base:base + 8])
            facing = struct.unpack_from("<3f", block, base + 8)
            corners = tuple(struct.unpack_from("<3f", block, base + 20 + 12 * j) for j in range(4))
            uv = struct.unpack_from("<8f", block, base + 68)
            recs.append((header, facing, corners, uv))
        out.append((recs, centre, extent, off))
    return out


def _blob(recs, centre, extent):
    blob = bytearray(struct.pack("<I3f3f", len(recs), *centre, *extent))
    for header, facing, corners, uv in recs:
        blob += bytes(header)
        blob += struct.pack("<3f", *facing)
        for corner in corners:
            blob += struct.pack("<3f", *corner)
        blob += struct.pack("<8f", *uv)
    return bytes(blob)


def join(sections):
    """Inverse of split, byte-exact on stock blocks. The table holds the
    offsets of all sections but the last, which lives at offset T (the
    table size). Sections go back at their own offsets when they still
    tile the body without gaps or overlaps; otherwise all are packed in
    table order after the table, last section first (the stub layout
    hide() writes)."""
    table = 4 + 4 * (len(sections) - 1)
    blobs = [_blob(recs, centre, extent) for recs, centre, extent, _off in sections]
    want = [off for _r, _c, _e, off in sections]
    spans = sorted((off, off + len(blobs[i])) for i, off in enumerate(want))
    if not (all(off >= table for off in want) and spans[0][0] == table
            and all(a == b for (_s, a), (b, _e) in zip(spans, spans[1:]))):
        # Pack in table order after the table, last section first at T.
        want = [0] * len(sections)
        want[-1] = table
        at = table + len(blobs[-1])
        for i, blob in enumerate(blobs[:-1]):
            want[i] = at
            at += len(blob)
    out = bytearray(table + sum(len(b) for b in blobs))
    out[0:table] = struct.pack("<%dI" % len(sections), table, *want[:-1])
    for off, blob in zip(want, blobs):
        out[off:off + len(blob)] = blob
    return bytes(out)


def hide(block):
    """The stock no-crowd layout: every section an empty 28-byte stub
    right after the table (byte-identical to slot 0)."""
    size = struct.unpack_from("<I", block)[0]
    count = (size - 4) // 4 + 1
    stubs = [size + SECTION_HEADER_BYTES * (k + 1) for k in range(count - 1)]
    return struct.pack("<%dI" % count, size, *stubs) + bytes(SECTION_HEADER_BYTES * count)


def bbox(recs):
    """(centre, extent) exactly covering the row quads."""
    xs = [c[k] for _h, _f, corners, _u in recs for c in corners for k in (0,)]
    ys = [c[1] for _h, _f, corners, _u in recs for c in corners]
    zs = [c[2] for _h, _f, corners, _u in recs for c in corners]
    lo, hi = (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
    return tuple((a + b) / 2 for a, b in zip(lo, hi)), tuple((b - a) / 2 for a, b in zip(lo, hi))


def check(block, sections=None):
    """Structurally valid (table, offsets, counts, sizes, boxes)? -> [problems].
    sections: the slot's own section count when known - the game reads
    exactly that many, and fewer crashed the match load (09-10, CRT abort
    at pes2012.exe+c545b on a 3-section block in a 20-section slot)."""
    problems = []
    if len(block) < 4:
        return ["block shorter than the table size"]
    size = struct.unpack_from("<I", block)[0]
    if (size - 4) % 4:
        problems.append("table size %d not 4 mod 4" % size)
        return problems
    count = (size - 4) // 4 + 1
    if sections is not None and count != sections:
        problems.append("%d sections, the slot has %d" % (count, sections))
    if len(block) < size:
        problems.append("block %d bytes shorter than table %d" % (len(block), size))
        return problems
    heads = list(struct.unpack_from("<%dI" % (count - 1), block, 4)) + [size] if count > 1 else [size]
    if any(b < size or b + SECTION_HEADER_BYTES > len(block) for b in heads):
        problems.append("section offset inside the table or past end: %s" % (heads,))
        return problems
    spans = []
    for i, off in enumerate(heads):
        rows = struct.unpack_from("<I", block, off)[0]
        want = off + SECTION_HEADER_BYTES + rows * ROW_BYTES
        if want > len(block):
            problems.append("section %d: %d rows need %d bytes, have %d" % (i, rows, want, len(block)))
            return problems
        spans.append((off, want))
    for (a0, a1), (b0, _b1) in zip(sorted(spans), sorted(spans)[1:]):
        if b0 < a1:
            problems.append("sections overlap at %d" % b0)
    for i, off in enumerate(heads):
        rows = struct.unpack_from("<I", block, off)[0]
        if rows:
            centre = struct.unpack_from("<3f", block, off + 4)
            extent = struct.unpack_from("<3f", block, off + 16)
            xs, ys, zs = [], [], []
            for k in range(rows):
                base = off + SECTION_HEADER_BYTES + k * ROW_BYTES
                for j in range(4):
                    x, y, z = struct.unpack_from("<3f", block, base + 20 + 12 * j)
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
            lo, hi = (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
            # Stock boxes are usually the exact bounds (to 5 cm); some span
            # neighbouring sections instead, so this warns only when rows
            # stick out of their box (what the cameras cull on).
            dev = max([abs(centre[k] - extent[k] - lo[k]) for k in range(3)] + [abs(centre[k] + extent[k] - hi[k]) for k in range(3)])
            inside = all(lo[k] >= centre[k] - extent[k] - BOX_TOLERANCE_M and hi[k] <= centre[k] + extent[k] + BOX_TOLERANCE_M for k in range(3))
            if not inside:
                problems.append("section %d rows stick out of their box by %.2f m" % (i, dev))
    end = max(spans)[1] if spans else size
    if end != len(block):
        problems.append("trailing %d bytes past the last row" % (len(block) - end))
    return problems


def facing_for(corners, axis=None):
    """The facing a row of `corners` (P0/P1 = top edge, P2/P3 = bottom) is
    watched from: the high-to-low slope direction (x, z), z-component 0.
    Measured on all 9950 stock rows: median 0.4 deg off (the rest is
    artist nudge, p99 33 deg). Nearly flat quads have no slope: they take
    `axis` (default: the midpoint's dominant axis towards the pitch)."""
    top = ((corners[0][0] + corners[1][0]) / 2, (corners[0][1] + corners[1][1]) / 2,
           (corners[0][2] + corners[1][2]) / 2)
    bot = ((corners[2][0] + corners[3][0]) / 2, (corners[2][1] + corners[3][1]) / 2,
           (corners[2][2] + corners[3][2]) / 2)
    if top[1] < bot[1]:
        top, bot = bot, top
    if abs(top[1] - bot[1]) >= FLAT_ROW_DY_M:
        length = ((bot[0] - top[0]) ** 2 + (bot[2] - top[2]) ** 2) ** 0.5 or 1.0
        return ((bot[0] - top[0]) / length, (bot[2] - top[2]) / length, 0.0)
    mid = ((top[0] + bot[0]) / 2, (top[2] + bot[2]) / 2)
    if axis is None:
        axis = section_facing((mid[0], 0.0, mid[1]))
    return (axis[0], axis[2], 0.0)

def section_facing(centre):
    """Flat-quad fallback: the dominant horizontal axis towards the pitch."""
    if abs(centre[0]) >= abs(centre[2]):
        return (1.0, 0.0, 0.0) if centre[0] < 0 else (-1.0, 0.0, 0.0)
    return (0.0, 0.0, 1.0) if centre[2] < 0 else (0.0, 0.0, -1.0)


def row_from_template(template, corners, uv=None):
    """A row for `corners` (P0..P3) carrying the template's header and uv;
    facing recomputed from the new quad by facing_for (median 0.4 deg off
    stock across all 9950 rows)."""
    header, _facing, _corners, template_uv = template
    return (header, facing_for(corners), tuple(tuple(c) for c in corners), tuple(uv) if uv is not None else template_uv)


# --- authoring ---------------------------------------------------------------

# Defaults for generated rows (units in the name; provenance: stock medians).
DEFAULT_ROW_SPACING_M = 1.6   # audiarea box row spacing of small stands (st030: 1.6)
DEFAULT_SEAT_WIDTH_M = 0.86   # audiarea box seat width (st030/st008: 0.85-0.86)
DEFAULT_STAND_HEIGHT_M = 6.0  # generated-stand wall height, first value tried
DEFAULT_TILT_DEG = 20.0       # generated-stand slope, first value tried


def rows_from_strip(points, template, spacing=DEFAULT_ROW_SPACING_M, tilt_deg=DEFAULT_TILT_DEG, height=DEFAULT_STAND_HEIGHT_M):
    """A polyline `points` (x/z metres, y ignored) describing a stand's
    front-bottom edge -> one row per `spacing` of height: each row a quad
    from the lifted strip edge back along the slope. -> [row]."""
    import math
    rows = []
    y, tilt = 0.0, math.radians(tilt_deg)
    step_y, step_back = spacing * math.sin(tilt), spacing * math.cos(tilt)
    for (ax, _ay, az), (bx, _by, bz) in zip(points, points[1:]):
        dx, dz = bx - ax, bz - az
        length = (dx * dx + dz * dz) ** 0.5
        if length <= 0:
            continue
        # Outward = away from the pitch (origin); rows climb outwards.
        ox, oz = ax / ((ax * ax + az * az) ** 0.5 or 1.0), az / ((ax * ax + az * az) ** 0.5 or 1.0)
        rows.append(row_from_template(template, ((ax, y, az), (bx, y, bz),
                                                 (bx + ox * step_back, y + step_y, bz + oz * step_back),
                                                 (ax + ox * step_back, y + step_y, az + oz * step_back))))
        y += step_y
    return rows


# --- PES15/17 audiarea.bin (OPTIONAL import source) ---------------------------

# Patches (u16 index, u16 kind 1, u16 data offset, 8-byte header) hold one
# 44-byte box (min xyz, max xyz, ~0, ~0, row spacing, seat width twice;
# pads are denormals, seat pairs may differ) then 96-byte quads (P0..P3,
# uv rect, ~0, density, seat width twice) and one 80-byte last quad
# (corners + uv under the patch defaults). st020's patches nest
# sub-patches (their own boxes, no header); the reader finds boxes by
# signature, so both shapes parse.
AUDI_PATCH_KIND = 1       # patch header kind of crowd data
AUDI_QUAD_BYTES = 96      # P0..P3, uv rect, 0, density, seat width twice
AUDI_LAST_BYTES = 80      # corners + uv only
AUDI_BOX_PAD = 1e-3       # box pads read as denormals, not exact 0
AUDI_QUAD_PAD = 1e-3      # per-quad first float likewise


def _audi_boxes(data):
    """All box offsets by signature: min < max, pads ~0, sane spacing."""
    out = []
    for off in range(0, len(data) - 44, 4):
        try:
            box = struct.unpack_from("<11f", data, off)
        except struct.error:
            break
        if abs(box[6]) > AUDI_BOX_PAD or abs(box[7]) > AUDI_BOX_PAD:
            continue
        # Row spacing to 8 (measured 7.5, st039), seats to 3 (measured 3.0, st025).
        if not (0.05 <= box[8] <= 8 and 0.05 <= box[9] <= 3 and 0.05 <= box[10] <= 3):
            continue
        if not (box[0] < box[3] and box[1] < box[4] and box[2] < box[5]):
            continue
        if not all(abs(x) < 600 for x in box[0:6]):
            continue
        out.append((off, box))
    return out


def read_audiarea(path):
    """audiarea.bin path -> [(box, [quad])]: quad = (P0..P3, uv8, density,
    seat). WESYS-wrapped files (st045) are decompressed. Boxes are found
    by signature; each runs quads (96 B) + one last quad (80 B) up to the
    next patch header (8 B) or box, or the file end."""
    with open(path, "rb") as f:
        raw = f.read()
    data = zlib.decompress(raw[16:]) if raw[3:8] == b"WESYS" else raw
    found = _audi_boxes(data)
    out = []
    bounds = [o for o, _b in found[1:]] + [len(data)]
    for (off, box), nxt in zip(found, bounds):
        # A patch header (index, kind AUDI_PATCH_KIND, data offset) sits in
        # the 8 bytes before the next box; sub-patches (st020) have none.
        end = nxt
        if nxt < len(data):
            _index, kind, doff, _zero = struct.unpack_from("<4H", data, nxt - 8)
            if kind == AUDI_PATCH_KIND and doff == nxt:
                end = nxt - 8
        size = end - (off + 44)
        if size >= AUDI_LAST_BYTES and (size - AUDI_LAST_BYTES) % AUDI_QUAD_BYTES == 0:
            count = (size - AUDI_LAST_BYTES) // AUDI_QUAD_BYTES
        elif size >= 0 and size % AUDI_QUAD_BYTES == 0:
            count, end = size // AUDI_QUAD_BYTES, end
            last = None
            base = off + 44
            quads = []
            for k in range(count):
                q = struct.unpack_from("<24f", data, base + k * AUDI_QUAD_BYTES)
                quads.append((q[0:12], q[12:20], q[21], (q[22] + q[23]) / 2))
            out.append((box, quads))
            continue
        else:
            continue
        base = off + 44
        quads = []
        for k in range(count):
            q = struct.unpack_from("<24f", data, base + k * AUDI_QUAD_BYTES)
            quads.append((q[0:12], q[12:20], q[21], (q[22] + q[23]) / 2))
        last = struct.unpack_from("<20f", data, base + count * AUDI_QUAD_BYTES)
        quads.append((last[0:12], last[12:20], box[8], box[9]))
        out.append((box, quads))
    return out


# PES15/17 audiarea quads: P0/P3 the front (low) edge, P1/P2 the back (high)
# edge (measured 09-10: P0->P1 is the slope in every quad of st030, st008,
# st022). PES2012 rows put the top edge first.
AUDI_TO_ROW_CORNERS = (1, 2, 3, 0)


def audi_to_rows(audi, template):
    """read_audiarea() output -> PES2012 [row], via row_from_template (the
    template carries header + uv; corners come from the pack). Pack corners
    are Fox space: they take the stadium geometry's own basis change,
    (x, y, z) -> (x, y, -z) (pes12_stadium.fox_to_ktmdl); without it every
    pocket landed mirrored across the pitch, away from its platform (09-10,
    Final PEStination: no crowd on any platform)."""
    rows = []
    for _box, quads in audi:
        for corners12, _uv, _density, _seat in quads:
            fox = [(corners12[3 * k], corners12[3 * k + 1], corners12[3 * k + 2]) for k in range(4)]
            corners = [(fox[k][0], fox[k][1], -fox[k][2]) for k in AUDI_TO_ROW_CORNERS]
            rows.append(row_from_template(template, corners))
    return rows


def sections_from_quads(quads, per_section=0):
    """[row] + section split -> [(rows, centre, extent)] with recomputed
    boxes. `per_section`: rows per section (0 = one section per quad, as
    the audiarea importer uses: pack pockets stay separate)."""
    if not quads:
        return []
    if per_section <= 0:
        return [([q],) + bbox([q]) for q in quads]
    return [tuple([quads[k:k + per_section]]) + bbox(quads[k:k + per_section])
            for k in range(0, len(quads), per_section)]

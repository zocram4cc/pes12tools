# SPDX-License-Identifier: GPL-3.0-or-later
"""Stadium crowd blocks (g+32 block 2), pure python: no bpy import, so the
tests run it headless. Mirrors tools/pes12_crowd.py (the dist copy lives
here because the add-on ships no tools/); keep the two in sync.

Row layout (10-stadiums.md): u8 side, seats, fill, variant, crowd2,
flags2, 0, 0; f3 facing (2D: watched from here, z = 0, unit); f3 P0..P3
(absolute seat quad, metres); f2 uv0..uv3 (crowd texture rect).
"""
import os
import struct
import zlib

ROW_BYTES = 100
SECTION_HEADER_BYTES = 28
UV_FLOATS = 8
# A section box must contain its rows: cameras cull on it. Stock boxes are
# usually the exact bounds (to 5 cm); some span neighbouring sections, so
# authored boxes are recomputed exactly (bbox()).
BOX_TOLERANCE_M = 0.05
FLAT_ROW_DY_M = 0.5        # top/bottom edges closer than this: no slope to face down (340 stock rows)

AUDI_PATCH_KIND = 1       # patch header kind of crowd data
AUDI_QUAD_BYTES = 96      # P0..P3, uv rect, 0, density, seat width twice
AUDI_LAST_BYTES = 80      # corners + uv only
AUDI_BOX_PAD = 1e-3       # box pads read as denormals, not exact 0

# Defaults for generated rows (units in the name; stock medians).
DEFAULT_ROW_SPACING_M = 1.6   # audiarea box row spacing of small stands (st030: 1.6)
DEFAULT_SEAT_WIDTH_M = 0.86   # audiarea box seat width (st030/st008: 0.85-0.86)
DEFAULT_TILT_DEG = 20.0       # generated-stand slope, first value tried
CROWD_BLOCK = 2               # g+32 block holding the crowd sections

def split(block):
    """Crowd block -> [(rows, centre, extent, offset)]: rows = [(header8,
    facing, (P0, P1, P2, P3), uv8)]."""
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
    offsets of all sections but the last, which lives at offset T."""
    table = 4 + 4 * (len(sections) - 1)
    blobs = [_blob(recs, centre, extent) for recs, centre, extent, _off in sections]
    offs = [off for _r, _c, _e, off in sections]
    spans = sorted((off, off + len(blobs[i])) for i, off in enumerate(offs))
    if not (all(off >= table for off in offs) and spans[0][0] == table
            and all(a == b for (_s, a), (b, _e) in zip(spans, spans[1:]))):
        # Pack in table order after the table, last section first at T.
        offs = [0] * len(sections)
        offs[-1] = table
        at = table + len(blobs[-1])
        for i, blob in enumerate(blobs[:-1]):
            offs[i] = at
            at += len(blob)
    out = bytearray(table + sum(len(b) for b in blobs))
    out[0:table] = struct.pack("<%dI" % len(sections), table, *offs[:-1])
    for off, blob in zip(offs, blobs):
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
    xs = [c[0] for _h, _f, corners, _u in recs for c in corners]
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


def rows_from_strip(points, template, spacing=DEFAULT_ROW_SPACING_M, tilt_deg=DEFAULT_TILT_DEG):
    """A polyline `points` (x/z metres, y ignored) describing a stand's
    front-bottom edge -> one row per segment, each a quad climbing
    outwards (away from the pitch) by `spacing` at `tilt_deg`. -> [row]."""
    import math
    rows, y, tilt = [], 0.0, math.radians(tilt_deg)
    step_y, step_back = spacing * math.sin(tilt), spacing * math.cos(tilt)
    for (ax, _ay, az), (bx, _by, bz) in zip(points, points[1:]):
        if (ax, az) == (bx, bz):
            continue
        # Outward = away from the pitch (origin); rows climb outwards.
        outward = (ax * ax + az * az) ** 0.5 or 1.0
        ox, oz = ax / outward, az / outward
        rows.append(row_from_template(template, ((ax, y, az), (bx, y, bz),
                                                 (bx + ox * step_back, y + step_y, bz + oz * step_back),
                                                 (ax + ox * step_back, y + step_y, az + oz * step_back))))
        y += step_y
    return rows


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
    seat). WESYS-wrapped files (st045) are decompressed."""
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
            base = off + 44
            quads = []
            for k in range(count):
                q = struct.unpack_from("<24f", data, base + k * AUDI_QUAD_BYTES)
                quads.append((q[0:12], q[12:20], q[21], (q[22] + q[23]) / 2))
            last = struct.unpack_from("<20f", data, base + count * AUDI_QUAD_BYTES)
            quads.append((last[0:12], last[12:20], box[8], box[9]))
            out.append((box, quads))
        elif size >= 0 and size % AUDI_QUAD_BYTES == 0:
            base = off + 44
            quads = []
            for k in range(size // AUDI_QUAD_BYTES):
                q = struct.unpack_from("<24f", data, base + k * AUDI_QUAD_BYTES)
                quads.append((q[0:12], q[12:20], q[21], (q[22] + q[23]) / 2))
            out.append((box, quads))
    return out


# audiarea quads: P0/P3 the front (low) edge, P1/P2 the back (high) edge
# (P0->P1 is the slope in every quad of st030, st008, st022). PES2012 rows
# put the top edge first.
AUDI_TO_ROW_CORNERS = (1, 2, 3, 0)


def audi_to_rows(audi, template):
    """read_audiarea() output -> PES2012 [row], via row_from_template (the
    template carries header + uv; corners come from the pack). Pack corners
    are Fox space and take the stadium geometry's basis change,
    (x, y, z) -> (x, y, -z), so pockets stay on their platforms."""
    rows = []
    for _box, quads in audi:
        for corners12, _uv, _density, _seat in quads:
            fox = [(corners12[3 * k], corners12[3 * k + 1], corners12[3 * k + 2]) for k in range(4)]
            corners = [(fox[k][0], fox[k][1], -fox[k][2]) for k in AUDI_TO_ROW_CORNERS]
            rows.append(row_from_template(template, corners))
    return rows


# --- Blender side (bpy; thin wrappers over the pure core above) ---

CROWD_COLLECTION = 'CROWD'       # sub-collection of a slot import holding one object per section
CROWD_SECTION_PROP = 'pes12_crowd_section'   # mesh object: section index in the slot's crowd block
CROWD_MODE_PROP = 'pes12_crowd_mode'         # CROWD collection: off | stock | custom
CROWD_SOURCE_PROP = 'pes12_crowd_source'     # CROWD collection: spec/audiarea path it was built from


def _section_mesh(name, recs):
    """One section's rows -> a mesh object (bpy): one quad per row, P0..P3
    in PES metres mapped through blender_io.to_blender. The row header
    (8 B), facing and uv rect ride on the polygon as int/float attributes
    so an untouched export rebuilds the identical block."""
    import bpy
    import blender_io
    mesh = bpy.data.meshes.new(name)
    verts, faces = [], []
    for header, _facing, corners, uv in recs:
        base = len(verts)
        verts += [blender_io.to_blender(c) for c in corners]
        faces.append(tuple(range(base, base + 4)))
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    for k in range(8):
        attr = mesh.attributes.new('pes12_crowd_header%d' % k, 'INT', 'FACE')
        attr.data.foreach_set('value', [header[k] for header, _f, _c, _u in recs])
    for k in range(3):
        layer = mesh.attributes.new('pes12_crowd_facing%d' % k, 'FLOAT', 'FACE')
        layer.data.foreach_set('value', [facing[k] for _h, facing, _c, _u in recs])
    for k in range(UV_FLOATS):
        layer = mesh.attributes.new('pes12_crowd_uv%d' % k, 'FLOAT', 'FACE')
        layer.data.foreach_set('value', [uv[k] for _h, _f, _c, uv in recs])
    return bpy.data.objects.new(name, mesh)


def import_slot(context, col, entry_raw):
    """The slot's g+32 entry bytes -> a CROWD sub-collection (one object per
    non-empty section). Empty sections are skipped: turning them back on is
    `crowd <slot> <root> stock`, authoring new ones is `crowd spec`."""
    import bpy
    import container as _container
    blocks = _container.read(entry_raw).blocks
    data = blocks[CROWD_BLOCK].data if len(blocks) > CROWD_BLOCK else b""
    sub = bpy.data.collections.new(CROWD_COLLECTION)
    col.children.link(sub)
    layer = context.view_layer.layer_collection.children[col.name].children[sub.name]
    context.view_layer.active_layer_collection = layer
    n = 0
    for i, (recs, _centre, _extent, _off) in enumerate(split(data)):
        if not recs:
            continue
        obj = _section_mesh('%s_%02d' % (CROWD_COLLECTION, i), recs)
        obj[CROWD_SECTION_PROP] = i
        sub.objects.link(obj)
        n += 1
    sub[CROWD_MODE_PROP] = 'stock' if n else 'off'
    return sub, n


def _object_rows(obj):
    """Mesh object from _section_mesh (edited or not) -> [row]: quad corners
    back in PES metres, header + uv + facing from the face attributes (an
    edited quad keeps its row's facing: facing is section-level style)."""
    import blender_io
    mesh = obj.data
    verts = [blender_io.to_pes(tuple(v.co)) for v in mesh.vertices]
    header_cols = []
    for k in range(8):
        attr = mesh.attributes.get('pes12_crowd_header%d' % k)
        header_cols.append([attr.data[p.index].value for p in mesh.polygons] if attr else [0] * len(mesh.polygons))
    facings = []
    for k in range(3):
        attr = mesh.attributes.get('pes12_crowd_facing%d' % k)
        facings.append([attr.data[p.index].value for p in mesh.polygons] if attr else [0.0] * len(mesh.polygons))
    uvs = []
    for k in range(UV_FLOATS):
        attr = mesh.attributes.get('pes12_crowd_uv%d' % k)
        uvs.append([attr.data[p.index].value for p in mesh.polygons] if attr else [0.0] * len(mesh.polygons))
    rows = []
    for p in mesh.polygons:
        corners = tuple(verts[v] for v in p.vertices[:4])
        rows.append((bytes(header_cols[k][p.index] for k in range(8)),
                     tuple(facings[k][p.index] for k in range(3)), corners,
                     tuple(uvs[k][p.index] for k in range(UV_FLOATS))))
    return rows


def _template_row(col):
    """First row of the fullest CROWD section object (header/uv template)."""
    objs = sorted([o for o in col.all_objects if o.type == 'MESH' and CROWD_SECTION_PROP in o],
                  key=lambda o: len(o.data.polygons), reverse=True)
    if not objs:
        raise ValueError('CROWD collection %s holds no section objects' % col.name)
    return _object_rows(objs[0])[0]


def _crowd_collection(col):
    for sub in col.children:
        if sub.name == CROWD_COLLECTION or CROWD_MODE_PROP in sub:
            return sub
    return None


def export_slot(col, game, root, src_root=None):
    """Slot collection -> the g+32 crowd override (or its removal when the
    crowd is back to stock). Custom sections are rebuilt from their objects
    (edited quads, new faces); stock sections keep their bytes unless their
    object was edited. Returns the written path or None. -> path | None."""
    import os
    import container as _container
    import stadium as _stadium
    sub = _crowd_collection(col)
    if sub is None:
        return None
    slot = {s.number: s for s in _stadium.slots(game)}[int(col['pes12_slot'])]
    entry = slot.geometry + _stadium.SLOT_STRIDE
    c = _container.read(_stadium.read_raw(game, entry, src_root))
    stock = _container.read(_stadium.read_raw(game, entry)).blocks[CROWD_BLOCK].data
    mode = sub.get(CROWD_MODE_PROP, 'stock')
    if mode == 'off':
        c.blocks[CROWD_BLOCK].data = hide(stock)
    elif mode == 'custom' or any(o.type == 'MESH' and CROWD_SECTION_PROP in o for o in sub.all_objects):
        # Every section the slot has stays in the block: a missing section
        # (no rows, so never imported as an object; or its object deleted)
        # becomes an empty stub. The game reads the slot's own count (20 on
        # every stock slot); a 3-section block crashed the match load with
        # the CRT abort at pes2012.exe+c545b (09-10, Final PEStination).
        current = split(c.blocks[CROWD_BLOCK].data)
        by_index = {}
        for o in sub.all_objects:
            if o.type == 'MESH' and CROWD_SECTION_PROP in o:
                k = int(o[CROWD_SECTION_PROP])
                if not 0 <= k < len(current):
                    raise ValueError('%s: crowd section %d, the slot has %d (0..%d)'
                                     % (o.name, k, len(current), len(current) - 1))
                by_index.setdefault(k, []).extend(_object_rows(o))
        empty_box = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        sections = []
        for k, (old_rows, centre, extent, off) in enumerate(current):
            rows = by_index.get(k, [])
            # untouched (same bytes once packed at the file's float32): the
            # stock rows and box go back bit for bit
            if _blob(rows, centre, extent) == _blob(old_rows, centre, extent):
                rows, box = old_rows, (centre, extent)
            else:
                box = bbox(rows) if rows else empty_box
            sections.append((rows,) + box + (off,))
        data = join(sections)
        problems = check(data, sections=len(split(stock)))
        if problems:
            raise ValueError('refusing structurally invalid crowd block: %s' % problems)
        c.blocks[CROWD_BLOCK].data = data
    return _stadium.write_raw(game, root, entry, _container.write(c))


def generate_stand(col, points, template=None, spacing=DEFAULT_ROW_SPACING_M, tilt_deg=DEFAULT_TILT_DEG,
                   section=None):
    """Author rows from a stand polyline (see rows_from_strip) into a new
    CROWD section object. `points`: [(x, y, z)] in PES metres (y ignored);
    `template`: header/uv source (default: the fullest section's first
    row); `section`: section index (default: the first one with no rows;
    the block keeps the slot's own section count, see export_slot)."""
    import bpy
    sub = _crowd_collection(col)
    if sub is None:
        raise ValueError('collection %s has no CROWD sub-collection' % col.name)
    if template is None:
        template = _template_row(sub)
    rows = rows_from_strip(points, template, spacing, tilt_deg)
    if section is None:
        used = {int(o[CROWD_SECTION_PROP]) for o in sub.all_objects if CROWD_SECTION_PROP in o}
        section = next(k for k in range(len(used) + 1) if k not in used)
    obj = _section_mesh('%s_%02d' % (CROWD_COLLECTION, section), rows)
    obj[CROWD_SECTION_PROP] = section
    sub.objects.link(obj)
    sub[CROWD_MODE_PROP] = 'custom'
    return obj, len(rows)


def write_spec(path, sections):
    """[row] sections -> the crowd JSON spec pes12_stadium.py `crowd spec`
    reads (Blender has no json version pitfalls: plain floats/ints)."""
    import json
    spec = {"sections": [[{"corners": [list(c) for c in corners], "header": list(header),
                            "facing": list(facing), "uv": list(uv)}
                           for header, facing, corners, uv in recs] for recs in sections]}
    with open(path, "w") as f:
        json.dump(spec, f)
    return path


def read_spec(path, template):
    """Spec JSON -> [row] (missing header/facing/uv come from `template`)."""
    import json
    with open(path) as f:
        spec = json.load(f)
    rows = []
    for recs in spec["sections"]:
        out = []
        for r in recs:
            corners = tuple(tuple(c) for c in r["corners"])
            if "header" in r or "uv" in r or "facing" in r:
                out.append((bytes(r["header"]) if "header" in r else template[0],
                            tuple(r["facing"]) if "facing" in r else facing_for(corners), corners,
                            tuple(r["uv"]) if "uv" in r else template[3]))
            else:
                out.append(row_from_template(template, corners))
        rows.append(out)
    return rows

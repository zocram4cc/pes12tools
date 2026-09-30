"""KTMDL geometry replacer: rebuild a KTMDL block with new vertex/index data.

Byte-splice design: every header/table/material/bone/texture byte of the
template is kept verbatim; only the stream-data buffers move. Patched fields:
per-buffer streamOffset/numVertex, header streamDataSize/size, packet
center/radius, group + bounding AABBs.

Offset provenance: pes2008-2013-tools/pes_ktmdl_importer/ktmdl.py
(HEADER_FIELDS, _parse_packet, _parse_stream_info, _parse_group/_bounding);
record sizes verified against stock ball dt0b #11 (1 bone, 1 packet).

Primitive type is NEVER changed: indices are emitted in the template packet's
own prim (balls: 0 = triangle strip, PES-Tools unwind rule).
"""
import struct

MAGIC = b"KTMDL\x00\x00\x00"  # ref parser MAGIC
PACKET_SIZE = 0x60            # _parse_packet stride
STREAMINFO_SIZE = 0x20        # _parse_stream_info stride
GROUP_SIZE = 0x30             # _parse_group/_parse_bounding stride

# header fields we read/patch (KTMDL-relative offsets)
H_PACKET_COUNT, H_PACKET_OFF = 0x28, 0x2C
H_STREAM_COUNT, H_STREAM_OFF = 0x30, 0x34
H_GROUP_COUNT, H_GROUP_OFF = 0x40, 0x44
H_BOUND_COUNT, H_BOUND_OFF = 0x58, 0x5C
H_DATA_SIZE, H_DATA_OFF = 0x74, 0x78
H_SIZE = 0x90

# packet record fields (packet-relative offsets)
P_STREAM_COUNT, P_STREAM_OFF = 0x10, 0x14
P_INDEX_COUNT, P_INDEX_OFF = 0x20, 0x24
P_CENTER, P_RADIUS = 0x50, 0x5C
P_GROUP = 0x08                # groupNo (ref parser _parse_packet)

# stream-info record fields (record-relative offsets)
S_DATA_OFF, S_COUNT = 0, 4
S_STRIDE = 9
S_TYPE = 0x14
TYPE_VERTEX, TYPE_INDEX = 0, 1  # ref parser STREAM_VERTEX/STREAM_INDEX

SEM_POSITION = 0x10  # ref parser SEMANTIC_NAMES
FMT_FLOAT3 = 0x02

ALIGN = 16  # stream-data block padded to this (stock ball: 90712 -> 90720)
BUFFER_ALIGN = 16  # each vertex/index buffer starts on this (census of 14 classes, 30-09)


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _set_u32(b, o, v):
    struct.pack_into("<I", b, o, v)


def _stream_recs(blob, table_off, count):
    """Yield (rec_off, streamOffset, numVertex, stride, type)."""
    for i in range(count):
        r = table_off + i * STREAMINFO_SIZE
        yield (r, _i32(blob, r + S_DATA_OFF), _u32(blob, r + S_COUNT),
               blob[r + S_STRIDE], _i32(blob, r + S_TYPE))


def _packet_base(blob, i):
    return _u32(blob, H_PACKET_OFF) + i * PACKET_SIZE


def packet_mesh(template, i):
    """Template packet i -> (vertex_bytes, indices, stride); for round-trip."""
    p = _packet_base(template, i)
    n_vs, vs_off = _u32(template, p + P_STREAM_COUNT), _i32(template, p + P_STREAM_OFF)
    n_is, is_off = _u32(template, p + P_INDEX_COUNT), _i32(template, p + P_INDEX_OFF)
    vbytes, stride = b"", 0
    for (r, data_rel, count, st, typ) in _stream_recs(template, p + vs_off, n_vs):
        if typ == TYPE_VERTEX and count:
            vbytes, stride = template[r + data_rel:r + data_rel + count * st], st
            break
    idx = []
    for (r, data_rel, count, _st, typ) in _stream_recs(template, p + is_off, n_is):
        if typ == TYPE_INDEX and count:
            idx = list(struct.unpack_from("<%dH" % count, template, r + data_rel))
            break
    return vbytes, idx, stride


def _position_off(template, i):
    """Byte offset of the POSITION element within packet i's vertex stride."""
    p = _packet_base(template, i)
    n_vs, vs_off = _u32(template, p + P_STREAM_COUNT), _i32(template, p + P_STREAM_OFF)
    for (r, _d, count, _st, typ) in _stream_recs(template, p + vs_off, n_vs):
        if typ == TYPE_VERTEX and count:
            decl = _u32(template, r + 0x0C)  # elementOffset, ref parser
            ne = template[r + 0x0A]          # elementCount
            for e in range(ne):
                base = r + decl + e * 4
                if template[base + 3] == SEM_POSITION:
                    if template[base + 2] != FMT_FLOAT3:
                        raise ValueError("packet %d: POSITION not FLOAT3" % i)
                    return template[base + 1]
    raise ValueError("packet %d: no POSITION element" % i)


def _bounds(pos):
    # stock balls: center = exact f32 average of min/max (NOT the f64 mean:
    # raw12 y/z differ in the last ulp); r = f32 half space-diagonal.
    lo = [min(p[k] for p in pos) for k in range(3)]
    hi = [max(p[k] for p in pos) for k in range(3)]
    # exporter writes text at %.6f: round to 6 decimals, then f32
    c = [round((a + b) / 2, 6) for a, b in zip(lo, hi)]
    r = round(sum((b - a) ** 2 for a, b in zip(lo, hi)) ** 0.5 / 2, 6)
    return lo, hi, c, r


def build(template, meshes):
    """Rebuild KTMDL block with replaced geometry.

    meshes: [{'packet': i, 'vertices': bytes (template stride),
              'indices': [int] in the template's prim type}].
    Packets not listed keep template data. Returns new block bytes.
    """
    blob = bytearray(template)
    if bytes(blob[:8]) != MAGIC:
        raise ValueError("not a KTMDL block")
    want = {m["packet"]: m for m in meshes}
    npacket = _u32(blob, H_PACKET_COUNT)
    data_off = _i32(blob, H_DATA_OFF)
    data_size = _u32(blob, H_DATA_SIZE)

    strides = {}
    for i, m in want.items():
        if not 0 <= i < npacket:
            raise ValueError("packet %d out of range" % i)
        p = _packet_base(blob, i)
        n_vs, vs_off = _u32(blob, p + P_STREAM_COUNT), _i32(blob, p + P_STREAM_OFF)
        stride = 0
        for (_r, _d, count, st, typ) in _stream_recs(blob, p + vs_off, n_vs):
            if typ == TYPE_VERTEX and count:
                stride = st
                break
        if not stride:
            raise ValueError("packet %d: no vertex stream" % i)
        if len(m["vertices"]) % stride:
            raise ValueError("packet %d: vertex bytes not a stride multiple" % i)
        nv = len(m["vertices"]) // stride
        if nv > 0xFFFF or any(x < 0 or x > 0xFFFF for x in m["indices"]):
            raise ValueError("packet %d: index data exceeds u16" % i)
        if m["indices"] and max(m["indices"]) >= nv:
            raise ValueError("packet %d: index out of range" % i)
        strides[i] = stride

    # unique buffer records (global + per-packet tables alias each other in
    # stock files; dedupe by record offset)
    recs = {}

    def collect(table_off, count):
        for (r, data_rel, count_, st, typ) in _stream_recs(blob, table_off, count):
            if typ in (TYPE_VERTEX, TYPE_INDEX) and count_ and r not in recs:
                recs[r] = dict(type=typ, start=r + data_rel,
                               length=count_ * (st if typ == TYPE_VERTEX else 2))

    collect(_u32(blob, H_STREAM_OFF), _u32(blob, H_STREAM_COUNT))
    for i in range(npacket):
        p = _packet_base(blob, i)
        collect(p + _i32(blob, p + P_STREAM_OFF), _u32(blob, p + P_STREAM_COUNT))
        collect(p + _i32(blob, p + P_INDEX_OFF), _u32(blob, p + P_INDEX_COUNT))
    ordered = sorted(recs.values(), key=lambda d: d["start"])

    new_content = {}
    for i, m in want.items():
        p = _packet_base(blob, i)
        n_vs, vs_off = _u32(blob, p + P_STREAM_COUNT), _i32(blob, p + P_STREAM_OFF)
        n_is, is_off = _u32(blob, p + P_INDEX_COUNT), _i32(blob, p + P_INDEX_OFF)
        for (r, data_rel, count, _st, typ) in _stream_recs(blob, p + vs_off, n_vs):
            if typ == TYPE_VERTEX and count:
                new_content[r + data_rel] = bytes(m["vertices"])
                break
        for (r, data_rel, count, _st, typ) in _stream_recs(blob, p + is_off, n_is):
            if typ == TYPE_INDEX and count:
                new_content[r + data_rel] = struct.pack(
                    "<%dH" % len(m["indices"]), *m["indices"])
                break

    # gap bytes between buffers must be zeros (else unknown data -> refuse)
    for a, b in zip(ordered, ordered[1:]):
        gap = bytes(blob[a["start"] + a["length"]:b["start"]])
        if any(gap):
            raise ValueError("nonzero gap at 0x%X: unknown layout" % (a["start"] + a["length"]))

    # relayout in template buffer order, every buffer start 16-aligned
    # (stock rule over all 14 census classes: each vertex/index buffer
    # starts on BUFFER_ALIGN, the data region ends on it; 30-09)
    new_data = bytearray()
    pos_of = {}
    for buf in ordered:
        new_data += b"\x00" * ((-len(new_data)) % BUFFER_ALIGN)
        pos_of[buf["start"]] = data_off + len(new_data)
        new_data += new_content.get(
            buf["start"], bytes(blob[buf["start"]:buf["start"] + buf["length"]]))
    new_data += b"\x00" * ((-len(new_data)) % ALIGN)

    for r, info in recs.items():
        _set_u32(blob, r + S_DATA_OFF, pos_of[info["start"]] - r)
        if info["start"] in new_content:
            if info["type"] == TYPE_VERTEX:
                _set_u32(blob, r + S_COUNT,
                         len(new_content[info["start"]]) // blob[r + S_STRIDE])
            else:
                _set_u32(blob, r + S_COUNT, len(new_content[info["start"]]) // 2)

    tail = bytes(blob[data_off + data_size:_u32(blob, H_SIZE)])
    out = bytearray(bytes(blob[:data_off]) + bytes(new_data) + tail)
    _set_u32(out, H_DATA_SIZE, len(new_data))
    _set_u32(out, H_SIZE, len(out))

    # Bounds. Stock packet centres/radii and group boxes are authored data:
    # no formula over the positions reproduces them (15/228 centres match the
    # AABB midpoint, 30-09), so unchanged geometry keeps them byte for byte.
    # A packet whose positions changed gets its own AABB centre/radius; its
    # group box and the global box grow to cover it (never shrink: a box too
    # large only costs culling, a box too small drops the mesh).
    grown = []
    for i, m in want.items():
        off = _position_off(bytes(out), i)
        st = strides[i]
        v = m["vertices"]
        pos = [struct.unpack_from("<3f", v, k * st + off) for k in range(len(v) // st)]
        old_v, _old_i, old_st = packet_mesh(template, i)
        old_pos = [struct.unpack_from("<3f", old_v, k * old_st + off) for k in range(len(old_v) // old_st)]
        if not pos or pos == old_pos:
            continue
        lo, hi, c, r_ = _bounds(pos)
        p = _packet_base(bytes(out), i)
        struct.pack_into("<3f", out, p + P_CENTER, *c)
        struct.pack_into("<f", out, p + P_RADIUS, r_)
        grown.append((_u32(out, p + P_GROUP), lo, hi))
    for group, lo, hi in grown:
        for table, n, only in ((H_GROUP_OFF, H_GROUP_COUNT, group), (H_BOUND_OFF, H_BOUND_COUNT, None)):
            for g in range(_u32(out, n)):
                if only is not None and g != only:
                    continue
                base = _u32(out, table) + g * GROUP_SIZE
                bh = struct.unpack_from("<3f", out, base + 0x10)
                bl = struct.unpack_from("<3f", out, base + 0x20)
                struct.pack_into("<3f", out, base + 0x10, *[max(a, b) for a, b in zip(bh, hi)])
                struct.pack_into("<3f", out, base + 0x20, *[min(a, b) for a, b in zip(bl, lo)])
    return bytes(out)

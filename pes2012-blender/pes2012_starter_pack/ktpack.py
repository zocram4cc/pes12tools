"""Template-driven KTMDL geometry packer (pure python, no Blender).

Reads the template KTMDL's own vertex declaration (never infers from
stride; 02-target-formats.md) and packs caller-supplied per-vertex
attribute dicts into ktmdl_write.build's {packet, vertices, indices}
mesh rows. Provenance: declaration semantics in
pes_ktmdl_importer/ktmdl.py (SEMANTIC_NAMES/TYPE_NAMES), vertex packing
in tools/pes12_ball.py pack_vertices, strip stitching in to_strip there.

Attribute dict per vertex (all optional except pos):
  pos (x,y,z) file coords, nrm, tan, uv [(u,v) per TEXCOORD channel],
  w [(slot, weight)] skinning, col D3DCOLOR float4 if the decl has one.

Skinning rule (tools/fmdl_to_pes12.py): n bones carry n-1 explicit
weights with the remainder on the last slot: BLENDWEIGHT FLOATn takes
the first n sorted weights, BLENDINDICES UBYTE4 the slots padded by
repeating the last. Unweighted templates keep their bytes (balls).
"""
import struct

try:
    from . import ktmdl_write as W
except ImportError:  # loaded standalone (tests import the file directly)
    import ktmdl_write as W

KTMDL_MAGIC = b'KTMDL\x00\x00\x00'

# element format -> (struct char, count); mirrors TYPE_NAMES in ktmdl.py
FORMATS = {0x00: ('f', 1), 0x01: ('f', 2), 0x02: ('f', 3), 0x0B: ('B', 4)}


def declaration(template, packet):
    """Template packet -> [(offset, fmt, semantic)] in stride order."""
    p = W._packet_base(template, packet)
    n_vs, vs_off = W._u32(template, p + W.P_STREAM_COUNT), W._i32(template, p + W.P_STREAM_OFF)
    for (r, _d, count, _st, typ) in W._stream_recs(template, p + vs_off, n_vs):
        if typ == W.TYPE_VERTEX and count:
            decl = W._u32(template, r + 0x0C)  # elementOffset, ref parser
            ne = template[r + 0x0A]  # elementCount
            out = []
            for e in range(ne):
                base = r + decl + e * 4
                out.append((template[base + 1], template[base + 2], template[base + 3]))
            return sorted(out)
    raise ValueError('packet %d: no vertex stream' % packet)


def pack_vertices(template, packet, attrs):
    """[attr dict] -> vertex bytes for template packet's declaration."""
    decl = declaration(template, packet)
    out = bytearray()
    for a in attrs:
        for off, fmt, sem in decl:
            _pack_elem(out, fmt, sem, a, off)
    stride = W.packet_mesh(template, packet)[2]
    assert len(out) == len(attrs) * stride, (len(out), len(attrs), stride)
    return bytes(out)


def _pack_elem(out, fmt, sem, a, off):
    # Vectors are written verbatim: the caller owns normalization (fresh
    # geometry should be normalized before packing; re-normalizing parsed
    # f32 values perturbs the last ulp and breaks byte round-trips).
    if sem == 0x10:  # POSITION
        out += struct.pack('<3f', *a['pos'])
    elif sem == 0x12:  # NORMAL
        out += struct.pack('<3f', *a.get('nrm', (0.0, 1.0, 0.0)))
    elif sem in (0x1E, 0x1F):  # TANGENT / BINORMAL
        n = a.get('nrm', (0.0, 1.0, 0.0))
        t = a.get('tan')
        if t is None:  # any unit perpendicular (tools/pes12_ball.py)
            n = _norm(n)
            ax = (0.0, 1.0, 0.0) if abs(n[1]) < 0.9 else (1.0, 0.0, 0.0)
            t = (ax[1] * n[2] - ax[2] * n[1], ax[2] * n[0] - ax[0] * n[2],
                 ax[0] * n[1] - ax[1] * n[0])
            lt = sum(c * c for c in t) ** 0.5 or 1.0
            t = tuple(c / lt for c in t)
        b = a.get('bin')  # else t x n (stock balls: |TxN - B| < 2e-6)
        if b is None:
            b = (t[1] * n[2] - t[2] * n[1], t[2] * n[0] - t[0] * n[2],
                 t[0] * n[1] - t[1] * n[0])
        out += struct.pack('<3f', *(t if sem == 0x1E else b))
    elif 0x16 <= sem <= 0x19:  # TEXCOORDn
        ch = sem - 0x16
        uv = (a.get('uv') or [(0.0, 0.0)])[ch] if ch < len(a.get('uv') or []) else (0.0, 0.0)
        out += struct.pack('<2f', *uv)
    elif sem == 0x20:  # BLENDWEIGHT FLOATn: PRIMARY0 (importer default):
        # slot 0 is the implicit residual; explicit floats belong to
        # slots 1..n (weights normalized, like _normalize_influences).
        n = FORMATS[fmt][1]
        ordered = sorted(a.get('w', []), key=lambda kv: -kv[1])
        slots = [x[0] for x in ordered] or [0]
        slots += [slots[-1]] * (n + 1)
        tw = sum(x[1] for x in ordered) or 1.0
        wmap = {s: w / tw for s, w in ordered}
        out += struct.pack('<%df' % n, *[(wmap.get(s, 0.0)) for s in slots[1:n + 1]])
    elif sem == 0x21:  # BLENDINDICES: slots, padded by repeating the last
        slots = [x[0] for x in sorted(a.get('w', []), key=lambda kv: -kv[1])] or [0]
        slots += [slots[-1]] * 4
        out += struct.pack('<4B', *slots[:4])
    elif fmt in FORMATS:  # unknown semantic: zeros of the right size
        ch, n = FORMATS[fmt]
        out += struct.pack('<%d%s' % (n, ch), *([0.0] * n if ch == 'f' else [0] * n))
    else:
        raise ValueError('element fmt 0x%02X at +%d: unknown size' % (fmt, off))


def _norm(v):
    ln = sum(c * c for c in v) ** 0.5 or 1.0
    return tuple(c / ln for c in v)


def to_strip(tris):
    """Triangle list -> single strip with degenerate stitches (tools/pes12_ball.py)."""
    if not tris:
        return []
    out = list(tris[0])
    for a, b, c in tris[1:]:
        out += [out[-1], a, a]
        if (len(out) - 2) % 2:
            out.append(a)
        out += [b, c]
    return out


def prim_type(template, packet):
    """Template packet -> KTMDL primitive type byte (packet+2, ref parser)."""
    return template[W._packet_base(template, packet) + 2]


def signed_volume(pos, tris):
    """Signed mesh volume (tools/pes12_ball.py); the strip unwind emits
    even triples as (a,c,b), so export flips positive-volume tris first."""
    vol = 0.0
    for a, b, c in tris:
        vol += (pos[a][0] * (pos[b][1] * pos[c][2] - pos[b][2] * pos[c][1])
                - pos[b][0] * (pos[a][1] * pos[c][2] - pos[a][2] * pos[c][1])
                + pos[c][0] * (pos[a][1] * pos[b][2] - pos[a][2] * pos[b][1]))
    return vol / 6


def mesh_row(template, packet, attrs, tris=None, prim=None):
    """attrs + triangle list -> ktmdl_write.build mesh row for packet.

    prim 0 (triangle strip, e.g. balls): to_strip with the pes12_ball
    signed-volume pre-flip. prim 1 (triangle list, e.g. boots): flat.
    Other prims raise instead of silently scrambling faces.
    """
    vb, sidx, _stride = W.packet_mesh(template, packet)
    if tris is None:  # keep template topology (topology-preserving path)
        return dict(packet=packet, vertices=pack_vertices(template, packet, attrs),
                    indices=list(sidx))
    if len(attrs) > 0xFFFF:
        raise ValueError('packet %d: %d verts exceed u16' % (packet, len(attrs)))
    prim = prim_type(template, packet) if prim is None else prim
    if prim == 0:
        pos = [a['pos'] for a in attrs]
        if signed_volume(pos, list(tris)) > 0:
            tris = [(a, c, b) for a, b, c in tris]
        indices = to_strip(list(tris))
    elif prim == 1:
        indices = [v for tri in tris for v in tri]
    else:
        raise ValueError('packet %d: unsupported primType %d' % (packet, prim))
    return dict(packet=packet, vertices=pack_vertices(template, packet, attrs),
                indices=indices)

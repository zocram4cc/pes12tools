"""Template-driven KTMDL geometry packer (pure python, no Blender).

Reads the template KTMDL's own vertex declaration (never infers from
stride) and packs caller-supplied per-vertex
attribute dicts into ktmdl_write.build's {packet, vertices, indices}
mesh rows. Provenance: declaration semantics in
pes_ktmdl_importer/ktmdl.py (SEMANTIC_NAMES/TYPE_NAMES), vertex packing
in tools/pes12_ball.py pack_vertices, strip stitching in to_strip there.

Attribute dict per vertex (all optional except pos):
  pos (x,y,z) file coords, nrm, tan, uv [(u,v) per TEXCOORD channel],
  bi [palette slot] and bw [explicit weight] skinning (positional, see
  _pack_elem), col D3DCOLOR float4 if the decl has one.

Skinning rule (PES2012's skin VS, tools/fmdl_to_pes12.py skin_pack):
slot 0 takes the remainder, BLENDWEIGHT FLOATn the weights of slots
1..n. Unweighted templates keep their bytes (balls).
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


def pack_vertices(template, packet, attrs, check=True):
    """[attr dict] -> vertex bytes for template packet's declaration."""
    decl = declaration(template, packet)
    stride = W.packet_mesh(template, packet)[2]
    # element sizes from the declaration spans: formats the reader cannot
    # size (0x12 on dt09 #342 blocks 1-2, semantic 0x13) end at the next
    # element or the stride
    ends = [o for o, _, _ in decl[1:]] + [stride]
    out = bytearray()
    for a in attrs:
        for (off, fmt, sem), end in zip(decl, ends):
            _pack_elem(out, fmt, sem, a, off, end - off)
    if check:
        assert len(out) == len(attrs) * stride, (len(out), len(attrs), stride)
    return bytes(out)


def pack_vertices_one(template, packet, a):
    """One attr dict -> one vertex's bytes (see pack_vertices)."""
    return pack_vertices(template, packet, [a], check=False)


def _pack_elem(out, fmt, sem, a, off, size):
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
        uv = a.get('uv') or {}
        out += struct.pack('<2f', *(uv.get(ch, (0.0, 0.0))))
    elif sem == 0x20:  # BLENDWEIGHT: explicit floats, positional.
        # a['bw'] = the explicit weights of BLENDINDICES positions 1..n (slot
        # 0 takes the implicit residual, PRIMARY0). Positional, not keyed by
        # index: stock verts repeat an index with different weights
        # (dt09 #349: indices [0,1,0,0], weights [0.9, 0.0]).
        n = FORMATS[fmt][1]
        bw = list(a.get('bw') or [])[:n]
        out += struct.pack('<%df' % n, *(bw + [0.0] * (n - len(bw))))
    elif sem == 0x21:  # BLENDINDICES: a['bi'], palette slots, positional
        bi = list(a.get('bi') or [0])[:4]
        out += struct.pack('<4B', *(bi + [bi[-1]] * (4 - len(bi))))
    elif fmt in FORMATS:  # unknown semantic: zeros of the right size
        ch, n = FORMATS[fmt]
        out += struct.pack('<%d%s' % (n, ch), *([0.0] * n if ch == 'f' else [0] * n))
    else:  # unknown format: the vertex's own bytes (a['raw'][off]) or zeros
        raw = (a.get('raw') or {}).get(off, b'')
        out += bytes(raw[:size]) + b'\x00' * (size - len(raw[:size]))


def _norm(v):
    ln = sum(c * c for c in v) ** 0.5 or 1.0
    return tuple(c / ln for c in v)


def _decode_strip(seq):
    """Indices -> triangle list with the ref reader's winding (its
    primitive_to_geometry for prim 0: even window (a,b,c), odd (b,a,c),
    degenerate windows skipped)."""
    out = []
    for i in range(len(seq) - 2):
        a, b, c = seq[i], seq[i + 1], seq[i + 2]
        if a == b or b == c or a == c:
            continue
        out.append((b, a, c) if i & 1 else (a, b, c))
    return out


def _restart(out, a, b, c):
    """Degenerate stitch starting triangle (a,b,c) after buffer `out`,
    verified against _decode_strip (emits exactly one new triangle equal
    to it). A parity-blind stitch emits extra or flipped triangles, which
    is what the old [x,x]-padding did (ball rebuilds decoded to 647
    triangles instead of 390, 30-09)."""
    import itertools
    base = len(_decode_strip(out))
    cands = [[out[-1]], [out[-1], out[-1]], [a], [b]]
    for v in {a, b, c} | set(out[-4:]):
        cands += [[out[-1], v], [out[-1], out[-1], v], [v]]
    for pad in cands:
        for perm in set(itertools.permutations((a, b, c))):
            got = _decode_strip(out + pad + list(perm))
            if len(got) == base + 1 and got[-1] == (a, b, c):
                return pad + list(perm)
    return None


def to_strip(tris):
    """Triangle list -> single strip: greedy edge-sharing chains, parity
    checked per emitted triangle; restarts proven by decode (_restart).
    Fuzzed over 19650 random meshes in development; every decode equals
    the input triangles."""
    from collections import defaultdict
    if not tris:
        return []
    edge = defaultdict(list)
    for i, (a, b, c) in enumerate(tris):
        for e in ((min(a, b), max(a, b)), (min(b, c), max(b, c)), (min(a, c), max(a, c))):
            edge[e].append(i)
    used = [False] * len(tris)
    out = []
    for start in range(len(tris)):
        if used[start]:
            continue
        a, b, c = tris[start]
        used[start] = True
        if not out:
            out = [a, b, c]
        else:
            seg = _restart(out, a, b, c)
            assert seg is not None, (a, b, c)
            out += seg
        while True:
            e = (min(out[-2], out[-1]), max(out[-2], out[-1]))
            extended = False
            for i in edge[e]:
                if used[i]:
                    continue
                t = list(tris[i])
                for (p, q, vt) in ((t[0], t[1], t[2]), (t[1], t[2], t[0]), (t[2], t[0], t[1]),
                                  (t[0], t[2], t[1]), (t[2], t[1], t[0]), (t[1], t[0], t[2])):
                    if (p, q) != (out[-2], out[-1]):
                        continue
                    want = (p, q, vt) if len(out) % 2 == 0 else (q, p, vt)
                    if tuple(t) == want:
                        used[i] = True
                        out.append(vt)
                        extended = True
                        break
                if extended:
                    break
            if not extended:
                # same edge, wrong parity for winding: restart it as new chain
                for i in edge[e]:
                    if not used[i]:
                        seg = _restart(out, *tris[i])
                        assert seg is not None, tris[i]
                        used[i] = True
                        out += seg
                        extended = True
                        break
            if not extended:
                break
    assert _decode_strip(out) == list(tris) or sorted(map(tuple, _decode_strip(out))) == sorted(map(tuple, tris)), len(out)
    return out

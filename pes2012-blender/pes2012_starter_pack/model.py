"""KTMDL block model (pure python, no Blender).

Parsing is the vendored moth1995 reader (pes_ktmdl_importer.ktmdl).
export_packet turns Blender face corners back into one packet's mesh row
for ktmdl_write.build; unedited corners reproduce the stock bytes.
"""
import functools
import struct

from pes_ktmdl_importer import ktmdl as V


class Model:
    def __init__(self, raw, parsed):
        self.raw = bytes(raw)
        self.parsed = parsed
        self.bones = parsed['bones']


@functools.lru_cache(maxsize=4)
def _parsed(template):
    """Parsed template, cached: export calls once per packet of one block."""
    return V.parse_bytes(template, 'template')


def read(data):
    """KTMDL block bytes -> Model. Raises from the vendored reader."""
    data = bytes(data)
    if data[:8] != b'KTMDL\x00\x00\x00':
        raise ValueError('not a KTMDL block')
    parsed = V.parse_bytes(data, 'block')
    return Model(data, parsed)


# --- export core: Blender corners -> one packet's mesh row -----------------
#
# Import gives every face corner its own Blender vertex and tags it with the
# source vertex id (vid). Export welds corners back. A corner whose visible
# values (position, UVs, normal, weights) still equal what import produced
# from its source vertex emits that vertex's stock bytes verbatim; anything
# else is packed fresh. Blender cannot reproduce stock tangent frames, UV
# flips (1 - v is not exactly invertible in float32) or authored weight
# layouts, so "unchanged" is decided by comparison, never by recomputation.

NORMAL_DOT_MIN = 1.0 - 1e-4   # Blender custom normals are stored encoded; closer than this = unedited
WEIGHT_TOL = 1e-6             # vertex-group weights are float32 sums of the imported f32 values


def _f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def flip_v(v):
    """PES V <-> Blender V, in float32 like Blender stores it."""
    return _f32(1.0 - _f32(v))


def import_view(vertex, palette):
    """What import shows for one source vertex: pos (PES), uv {ch: Blender
    (u, v)}, nrm (PES), w {node_id: weight}."""
    uv = {}
    for key, val in vertex.items():
        if key.startswith('TEXCOORD') and key[8:].isdigit():
            uv[int(key[8:])] = (_f32(val[0]), flip_v(val[1]))
    raw = vertex.get('BLENDWEIGHT')
    raw = [raw] if isinstance(raw, (int, float)) else list(raw or [])
    bi = list(vertex.get('BLENDINDICES') or [])
    w = {}
    if bi:
        for slot, x in zip(bi, [1.0 - sum(raw)] + raw):
            if x > 1e-8 and 0 <= slot < len(palette):
                w[palette[slot]] = w.get(palette[slot], 0.0) + x
    return dict(pos=tuple(vertex['POSITION']), uv=uv, nrm=tuple(vertex.get('NORMAL', (0.0, 0.0, 1.0))), w=w)


def _same(corner, view):
    if tuple(_f32(c) for c in corner['pos']) != tuple(_f32(c) for c in view['pos']):
        return False
    cuv = {ch: (_f32(u), _f32(v)) for ch, (u, v) in corner.get('uv', {}).items()}
    if cuv != view['uv']:
        return False
    a, b = corner.get('nrm'), view['nrm']
    if a is not None and not corner.get('flat'):
        la = sum(x * x for x in a) ** 0.5
        lb = sum(x * x for x in b) ** 0.5
        if not lb or not la:
            # a zero normal carries no information either way: stock stadium
            # parts have none (import sets no custom normals, Blender shows
            # computed ones), and Blender drops the custom normal it was
            # given on a few corners (03-10: slot 30 geometry #2693 b10/b21)
            pass
        elif sum(x * y for x, y in zip(a, b)) / (la * lb) < NORMAL_DOT_MIN:
            return False
    cw, vw = corner.get('w', {}), view['w']
    if set(k for k, x in cw.items() if x > WEIGHT_TOL) != set(vw):
        return False
    return all(abs(cw.get(k, 0.0) - x) <= WEIGHT_TOL for k, x in vw.items())


def export_packet(template, index, corners, tris):
    """Blender corners -> ktmdl_write.build mesh row for packet `index`.

    corners: [{vid (source vertex or None), pos (PES), uv {ch: Blender uv},
    nrm (PES), w {node_id: weight}}]; tris: corner index triples.
    Unedited corners and topology reproduce the stock packet byte for byte.
    """
    import ktmdl_write as W
    import ktpack
    parsed = _parsed(bytes(template))['packets'][index]
    src = parsed['vertices']
    palette = parsed['bonePalette']
    old_vb, old_idx, stride = W.packet_mesh(template, index)
    views = {}
    key_of = []
    for c in corners:
        vid = c.get('vid')
        if vid is not None and 0 <= vid < len(src):
            if vid not in views:
                views[vid] = import_view(src[vid], palette)
            if _same(c, views[vid]):
                key_of.append(('t', vid))
                continue
        fresh = dict(pos=c['pos'], nrm=c.get('nrm', (0.0, 0.0, 1.0)),
                     uv={ch: (u, flip_v(v)) for ch, (u, v) in c.get('uv', {}).items()})
        if vid is not None and 0 <= vid < len(src):   # keep the stock tangent frame
            fresh['tan'] = tuple(src[vid].get('TANGENT', (1.0, 0.0, 0.0)))
            fresh['bin'] = tuple(src[vid].get('BINORMAL', (0.0, 1.0, 0.0)))
        pal_index = {n: s for s, n in enumerate(palette)}
        # ponytail: bones outside the stock packet palette are dropped;
        # growing a palette means relocating the packet's palette table
        infl = sorted(((pal_index[n], x) for n, x in c.get('w', {}).items() if n in pal_index and x > 0),
                      key=lambda kv: -kv[1])[:4] or [(0, 1.0)]
        tot = sum(x for _, x in infl)
        fresh['bi'] = [s for s, _ in infl]
        fresh['bw'] = [x / tot for _, x in infl[1:]]
        key_of.append(('n', vid, ktpack.pack_vertices_one(template, index, fresh)))
    # stock vertices no stock triangle references ride along at their vid
    used_stock = {v for t in parsed['triangles'] for v in t}
    keys = list(dict.fromkeys(key_of))
    keys += [('t', v) for v in range(len(src)) if v not in used_stock and ('t', v) not in keys]
    keys.sort(key=lambda k: (k[1] is None, k[1] if k[1] is not None else 0))
    slot = {k: i for i, k in enumerate(keys)}
    verts = b''.join(old_vb[k[1] * stride:(k[1] + 1) * stride] if k[0] == 't' else k[2] for k in keys)
    new_tris = [tuple(slot[key_of[c]] for c in t) for t in tris]
    stock_tris = [tuple(t) for t in parsed['triangles']]
    if len(keys) == len(src) and all(k == ('t', i) for i, k in enumerate(keys)) and new_tris == stock_tris:
        indices = list(old_idx)
    elif parsed['primType'] == 0:
        indices = ktpack.to_strip(new_tris)
    elif parsed['primType'] == 1:
        indices = [v for t in new_tris for v in t]
    else:
        raise ValueError('packet %d: primType %d not writable' % (index, parsed['primType']))
    return {'packet': index, 'vertices': verts, 'indices': indices}

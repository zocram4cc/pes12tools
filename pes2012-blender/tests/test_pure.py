"""Pure-python tests (no Blender) against the user's own game.

Run: python3 tests/test_pure.py   (from dist/pes2012-blender/)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pes2012_starter_pack'))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import corpus  # noqa: E402
import container  # noqa: E402

_CORPUS = None


def all_entries():
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = corpus.entries()
    return _CORPUS


def test_corpus_found():
    ents = all_entries()
    assert len(ents) > 100, len(ents)
    for name, (img, idx) in corpus.CLASSES.items():
        assert corpus.read_entry(img, idx), name
    print('corpus: %d KTMDL entries' % len(ents))


def test_container_unedited_exact():
    bad = [(img, i) for img, i, raw in all_entries() if container.write(container.read(raw)) != raw]
    assert not bad, bad[:10]
    print('container unedited round trip: %d/%d exact' % (len(all_entries()), len(all_entries())))


def test_container_block_offsets():
    want = {('dt0c.img', 164): [112, 77152], ('dt0d.img', 2660): [64, 76640], ('dt09.img', 534): [890112]}
    for (img, idx), offs in want.items():
        c = container.read(corpus.read_entry(img, idx))
        got = [b.row[0] for b in c.ktmdl_blocks()]
        assert got[:len(offs)] == offs, (img, idx, got)
    print('container: KTMDL blocks at the row offsets')


def test_container_every_ktmdl_located():
    """Every entry yields at least one block starting with the KTMDL magic
    (the old magic search landed inside other data on 9 entries)."""
    none = [(img, i) for img, i, raw in all_entries() if not container.read(raw).ktmdl_blocks()]
    assert not none, none[:10]
    print('container: KTMDL block found in every entry')


def test_container_edited_rewrite():
    """Changing one block: the others read back byte-identical, rows valid."""
    for img, idx in corpus.CLASSES.values():
        raw = corpus.read_entry(img, idx)
        c = container.read(raw)
        if c.layout != 'rows':
            continue
        before = [b.data for b in c.blocks]
        k = next(i for i, b in enumerate(c.blocks) if b.kind == 'ktmdl')
        c.blocks[k].data = c.blocks[k].data + b'\x00' * 32
        c2 = container.read(container.write(c))
        assert len(c2.blocks) == len(before), (img, idx)
        for i, (b, orig) in enumerate(zip(c2.blocks, before)):
            assert b.data == (orig + b'\x00' * 32 if i == k else orig), (img, idx, i)
        assert [b.row[2] for b in c2.blocks] == [b.row[2] for b in c.blocks], (img, idx)
    print('container: edited rewrite keeps the other blocks and row fields')



def _vendor_reader():
    from pes_ktmdl_importer import ktmdl
    return ktmdl


def _class_blocks():
    out = []
    for name, (img, idx) in corpus.CLASSES.items():
        c = container.read(corpus.read_entry(img, idx))
        for k, b in enumerate(c.blocks):
            if b.kind == 'ktmdl':
                out.append((name, k, b.data))
    return out


def test_model_corpus_parses():
    """Every KTMDL block of the user's game parses, including the 5 the old
    magic-search located wrongly (dt09 #176 x4, dt0b #35)."""
    V = _vendor_reader()
    fails = []
    for img, i, raw in all_entries():
        for k, b in enumerate(container.read(raw).ktmdl_blocks()):
            try:
                V.parse_bytes(b.data, 'x')
            except Exception as e:
                fails.append((img, i, k, str(e)[:80]))
    assert not fails, fails[:10]
    print('model: every KTMDL block of %d entries parses' % len(all_entries()))


def test_model_stream_tables_are_record_relative():
    """A stream record's element table and packet's palette are offsets from
    the RECORD, not from the packet: bad176 pkt0 rec0 elementOffset 1088 lands
    1088 past its own record (a plausible vertex-element row), and read
    packet-relative it reads the region right after the header (sky floats).
    The vendored reader added the packet base to both."""
    V = _vendor_reader()
    import struct
    for name, k, data in _class_blocks():
        S = struct.unpack_from('<I', data, 0x34)[0]
        n = struct.unpack_from('<I', data, 0x30)[0]
        P = struct.unpack_from('<i', data, 0x2C)[0]
        for i in range(n):
            o = S + i * 0x20
            elc = data[o + 0x0A]
            sto = struct.unpack_from('<i', data, o + 0x0C)[0]
            if not elc:
                continue
            rel = data[o + sto:o + sto + elc * 4]
            # every row is u16 stream id 0, then (offset, format, semantics)
            rows = [(r[1], r[2], r[3]) for r in (rel[j * 4:j * 4 + 4] for j in range(elc))]
            assert rows[0] == (0, 2, 0x10), (name, k, i, rows[0])
            assert all(a <= b for a, b in zip([x[0] for x in rows], [x[0] for x in rows][1:])), (name, i, rows)
        p0 = P
        skN = struct.unpack_from('<I', data, p0 + 0x18)[0]
        skO = struct.unpack_from('<i', data, p0 + 0x1C)[0]
        if skN:
            pal = struct.unpack_from('<%dH' % skN, data, p0 + skO)
            assert len(set(pal)) == skN or skN == 1, (name, k, pal)
            assert max(pal) <= len(V.parse_bytes(data, 'x')['bones']), (name, k, pal)
    print('model: stream element tables and palettes read record-relative')





def test_skeleton_per_block():
    """One skeleton per KTMDL block: node counts, parents resolvable,
    binds equal the raw matrices. Blocks are NOT merged: dt07 #2960's
    block-1 thigh is not block-0's shinguard, and dt09 #349's node-0
    blocks disagree in bind (30-09)."""
    import model
    import skeleton
    want = {'body_349': 6, 'face_dt0c': 2, 'ball': 1, 'kit': 3, 'boots_skinned': 2,
            'hair_3bone': 2, 'face_dt0d': 2, 'body_21': 1, 'body_40': 1, 'body_multi': 3,
            'boots_static': 4}
    for name, nblocks in want.items():
        img, idx = corpus.CLASSES[name]
        c = container.read(corpus.read_entry(img, idx))
        ms = [('blk%d' % k, model.read(b.data).parsed) for k, b in enumerate(c.ktmdl_blocks())]
        sks = skeleton.build(ms)
        assert len(sks) == nblocks and len(ms) == nblocks, (name, len(sks), len(ms))
        for (blk, parsed), sk in zip(ms, sks):
            assert len(sk) == len(parsed['bones']), (name, blk)
            for b in sk.bones:
                assert b.parent_id in (-1,) or b.parent_id in sk.by_id, (name, blk, b.id)
                raw = parsed['bones'][b.id]
                flat = [float(x) for row in raw['matrix'] for x in row]
                assert all(abs(a - x) < 1e-6 for a, x in zip(b.bind, flat)), (name, blk, b.id)
    print('skeleton: one skeleton per block, binds exact')



def _blender_corners(parsed_packet):
    """What blender_io import leaves on a mesh, without Blender: one corner
    per face corner tagged with its source vid, UVs flipped in float32,
    weights summed per bone in float32 like vertex groups store them."""
    import model
    f32 = model._f32
    corners, tris = [], []
    for t in parsed_packet['triangles']:
        tri = []
        for vid in t:
            view = model.import_view(parsed_packet['vertices'][vid], parsed_packet['bonePalette'])
            corners.append(dict(vid=vid, pos=view['pos'], uv=dict(view['uv']), nrm=view['nrm'],
                                w={k: f32(x) for k, x in view['w'].items()}))
            tri.append(len(corners) - 1)
        tris.append(tuple(tri))
    return corners, tris


def test_export_unedited_byte_exact():
    """Blender-shaped corners of every packet, exported unedited through
    export_packet + build, reproduce every class block byte for byte."""
    import model
    import ktmdl_write as W
    for name, k, data in _class_blocks():
        m = model.read(data)
        rows = []
        for i, p in enumerate(m.parsed['packets']):
            corners, tris = _blender_corners(p)
            rows.append(model.export_packet(data, i, corners, tris))
        out = W.build(data, rows)
        assert out == data, (name, k, len(out), len(data))
    print('export: unedited Blender round trip byte-exact on every class block')


def test_export_edited():
    """Moving one corner and deleting a triangle: the rest stays stock, the
    moved vertex is fresh, counts follow the edit."""
    import model
    import ktmdl_write as W
    for name in ('ball', 'body_349', 'boots_skinned', 'face_dt0c'):
        img, idx = corpus.CLASSES[name]
        data = next(b.data for b in container.read(corpus.read_entry(img, idx)).ktmdl_blocks())
        m = model.read(data)
        p = m.parsed['packets'][0]
        corners, tris = _blender_corners(p)
        tris = tris[1:]                                   # delete the first triangle
        moved = tris[0][0]
        c = corners[moved]
        c['pos'] = (c['pos'][0] + 0.01, c['pos'][1], c['pos'][2])
        rows = [model.export_packet(data, 0, corners, tris)]
        out = W.build(data, rows)
        m2 = model.read(out)
        q = m2.parsed['packets'][0]
        assert len(q['triangles']) == len(p['triangles']) - 1, (name, len(q['triangles']))
        got = sorted(tuple(round(x, 5) for x in q['vertices'][v]['POSITION']) for t in q['triangles'] for v in t)
        assert tuple(round(x, 5) for x in c['pos']) in got, name
        for j in range(1, len(m.parsed['packets'])):
            assert W.packet_mesh(out, j)[0] == W.packet_mesh(data, j)[0], (name, j)
    print('export: edited packet re-reads with the edit, other packets stock')



def test_export_corpus_byte_exact():
    """EVERY entry of the game: each KTMDL block exported unedited from
    Blender-shaped corners, written back through the container, equals the
    source entry. Long (minutes): runs when PES12_FULL=1."""
    if not os.environ.get('PES12_FULL'):
        print('export corpus: skipped (PES12_FULL=1 to run)')
        return
    import model
    import ktmdl_write as W
    bad = []
    for img, i, raw in all_entries():
        c = container.read(raw)
        try:
            for b in c.ktmdl_blocks():
                m = model.read(b.data)
                rows = [model.export_packet(b.data, j, *_blender_corners(p))
                        for j, p in enumerate(m.parsed['packets'])]
                b.data = W.build(b.data, rows) if rows else b.data
            if container.write(c) != raw:
                bad.append((img, i, 'differs'))
        except Exception as e:
            bad.append((img, i, repr(e)[:80]))
    assert not bad, (len(bad), bad[:10])
    print('export corpus: %d entries byte-exact' % len(all_entries()))


def _texture_blocks():
    """(img, entry, block bytes) for every WE00 texture block of the game."""
    import textures
    out = []
    for img, i, raw in all_entries():
        for b in container.read(raw).blocks:
            if textures.is_texture(b.data):
                out.append((img, i, b.data))
    return out


# DXT is lossy: mean |error| per channel (0..255) a fresh encode may add.
# Re-encoding a stock DXT block's own decode measures 1-3 in practice.
DXT_MEAN_ERR_MAX = 6.0


def test_textures_decode_every_block():
    import textures
    blocks = _texture_blocks()
    bad = []
    for img, i, b in blocks:
        try:
            w, h, px = textures.decode(b)
            assert px.shape == (h, w, 4)
        except Exception as e:
            bad.append((img, i, textures.texture_id(b), repr(e)[:80]))
    assert not bad, (len(bad), bad[:10])
    print('textures: %d WE00 blocks decode' % len(blocks))


def test_textures_reencode_each_format():
    """One block of every format: encode(decode(b)) keeps the block size and
    header and decodes back within DXT tolerance (exact for raw/masked)."""
    import textures
    seen = {}
    for img, i, b in _texture_blocks():
        dds = textures._dds(b)
        key = dds[3] if dds else ('raw', b[textures.KIND_OFF])
        seen.setdefault(key, (img, i, b))
    for key, (img, i, b) in seen.items():
        w, h, px = textures.decode(b)
        e = textures.encode(b, px)
        assert len(e) == len(b), key
        assert e[:textures.HEADER + textures.DDS_HEADER] == b[:textures.HEADER + textures.DDS_HEADER] or not textures._dds(b), key
        _, _, back = textures.decode(e)
        err = float(np.abs(back.astype(int) - px.astype(int)).mean())
        lossless = key not in textures.DXT_BLOCK_BYTES
        assert (err == 0) if lossless else (err <= DXT_MEAN_ERR_MAX), (key, img, i, err)
        print('textures: %r (%s #%d %dx%d) re-encodes, mean error %.2f' % (key, img, i, w, h, err))


def test_textures_edit_lands():
    """A painted square appears after encode, the rest stays put."""
    import textures
    img, i, b = next(t for t in _texture_blocks()
                     if textures._dds(t[2]) and textures._dds(t[2])[3] == b'DXT1' and textures._dds(t[2])[0] >= 64)
    w, h, px = textures.decode(b)
    edit = px.copy()
    edit[8:24, 8:24] = (255, 0, 255, 255)
    _, _, back = textures.decode(textures.encode(b, edit))
    assert (np.abs(back[8:24, 8:24].astype(int) - (255, 0, 255, 255)) <= 8).all()
    rest = np.ones((h, w), bool)
    rest[4:28, 4:28] = False   # blocks touching the square may shift
    assert float(np.abs(back[rest].astype(int) - px[rest].astype(int)).mean()) <= DXT_MEAN_ERR_MAX
    print('textures: edit lands (%s #%d)' % (img, i))


TESTS = [test_corpus_found, test_container_unedited_exact, test_container_block_offsets,
         test_container_every_ktmdl_located, test_container_edited_rewrite,
         test_model_corpus_parses, test_model_stream_tables_are_record_relative,
         test_skeleton_per_block,
         test_export_unedited_byte_exact, test_export_edited,
         test_textures_decode_every_block, test_textures_reencode_each_format, test_textures_edit_lands,
         test_export_corpus_byte_exact]

if __name__ == '__main__':
    if not corpus.available():
        print('SKIP: no game at %s (set PES12_GAME)' % corpus.GAME)
        sys.exit(0)
    only = sys.argv[1:]
    for t in TESTS:
        if not only or t.__name__ in only:
            t()
    print('PURE TESTS PASS')

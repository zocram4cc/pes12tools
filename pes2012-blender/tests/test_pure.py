"""Pure-python tests (no Blender) against the user's own game.

Run: python3 tests/test_pure.py   (from dist/pes2012-blender/)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pes2012_starter_pack'))
sys.path.insert(0, HERE)

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
    """The vendored KTMDL reader without the bpy-importing package __init__."""
    import importlib.util
    here = os.path.join(HERE, '..', 'pes2012_starter_pack', 'pes_ktmdl_importer', 'ktmdl.py')
    spec = importlib.util.spec_from_file_location('pes_ktmdl_vendor', here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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




TESTS = [test_corpus_found, test_container_unedited_exact, test_container_block_offsets,
         test_container_every_ktmdl_located, test_container_edited_rewrite,
         test_model_corpus_parses, test_model_stream_tables_are_record_relative]

if __name__ == '__main__':
    if not corpus.available():
        print('SKIP: no game at %s (set PES12_GAME)' % corpus.GAME)
        sys.exit(0)
    only = sys.argv[1:]
    for t in TESTS:
        if not only or t.__name__ in only:
            t()
    print('PURE TESTS PASS')

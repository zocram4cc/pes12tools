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


TESTS = [test_corpus_found, test_container_unedited_exact, test_container_block_offsets,
         test_container_every_ktmdl_located, test_container_edited_rewrite]

if __name__ == '__main__':
    if not corpus.available():
        print('SKIP: no game at %s (set PES12_GAME)' % corpus.GAME)
        sys.exit(0)
    only = sys.argv[1:]
    for t in TESTS:
        if not only or t.__name__ in only:
            t()
    print('PURE TESTS PASS')

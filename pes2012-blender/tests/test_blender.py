"""Headless Blender tests through the add-on's own import/export.

Run (Blender 4.2+):
  blender -b --factory-startup --python tests/test_blender.py
- every class example of the user's game (tests/corpus.py) imported and
  exported unedited reproduces the stock entry byte for byte;
- a moved vertex and a deleted face survive a re-import, other packets
  stay stock;
- every PGB2 submesh flag bit (0-6, ref byte, 16-18) survives a body
  export (PES12_BODY_DIR: a built custom body, e.g. dllprobe/custom/p272101).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pes2012_starter_pack'))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

import bmesh  # noqa: E402
import bpy  # noqa: E402

import pes2012_starter_pack as P  # noqa: E402
import container  # noqa: E402
import corpus  # noqa: E402
import ktmdl_write as W  # noqa: E402
import model  # noqa: E402
import pgb2  # noqa: E402

OUT = os.environ.get('PES12_TEST_OUT', os.path.join(HERE, '_out'))
BODY = os.environ.get('PES12_BODY_DIR', os.path.join(HERE, '..', '..', '..', 'dllprobe', 'custom', 'p272101'))
EDIT_SHIFT_M = 0.01      # the edit test moves one vertex this far along X


def _fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        P.register()
    except ValueError:   # already registered in this process
        pass


def _roundtrip(raw, edit=None):
    """Entry bytes -> file -> add-on import -> optional edit(col) -> add-on
    export -> bytes."""
    os.makedirs(OUT, exist_ok=True)
    src, dst = os.path.join(OUT, 'src.bin'), os.path.join(OUT, 'out.bin')
    open(src, 'wb').write(raw)
    col, _n = P.import_bin(bpy.context, src)
    if edit:
        edit(col)
    P.export_bin(col, dst)
    return open(dst, 'rb').read()


def test_unedited_byte_exact():
    for name, (img, idx) in corpus.CLASSES.items():
        _fresh()
        raw = corpus.read_entry(img, idx)
        out = _roundtrip(raw)
        assert out == raw, (name, len(out), len(raw))
        print('blender: %-20s unedited export byte-exact' % name, flush=True)


def test_edit_survives():
    for name in ('ball', 'body_349', 'boots_skinned', 'face_dt0c', 'stadium_side'):
        _fresh()
        raw = corpus.read_entry(*corpus.CLASSES[name])
        c = container.read(raw)
        first = c.blocks.index(c.ktmdl_blocks()[0])
        seen = {}

        def edit(col):
            o = next(x for x in col.all_objects if x.type == 'MESH'
                     and x[P.BLOCK_PROP] == first and x[P.PACKET_PROP] == 0)
            seen['n'] = len(o.data.polygons)
            bm = bmesh.new()
            bm.from_mesh(o.data)
            bm.faces.ensure_lookup_table()
            moved = bm.faces[1].verts[0]
            moved.co.x += EDIT_SHIFT_M
            seen['want'] = tuple(round(x, 5) for x in P.blender_io.to_pes(moved.co))
            bmesh.ops.delete(bm, geom=[bm.faces[0]], context='FACES')
            bm.to_mesh(o.data)
            bm.free()

        out = _roundtrip(raw, edit)
        stock = c.blocks[first].data
        blk = container.read(out).blocks[first].data
        q = model.read(blk).parsed['packets'][0]
        assert len(q['triangles']) == seen['n'] - 1, (name, len(q['triangles']), seen['n'])
        pos = {tuple(round(x, 5) for x in q['vertices'][v]['POSITION']) for t in q['triangles'] for v in t}
        assert seen['want'] in pos, name
        for j in range(1, W._u32(stock, W.H_PACKET_COUNT)):
            assert W.packet_mesh(blk, j)[0] == W.packet_mesh(stock, j)[0], (name, j)
        print('blender: %-20s edit survives re-import' % name, flush=True)


def test_pgb2_flags():
    """Each material gets a different mix of flag bits; the exported body
    carries exactly those flags per submesh."""
    path = os.path.join(BODY, 'body.bin')
    if not os.path.exists(path):
        print('blender: pgb2 flags SKIP (no %s; set PES12_BODY_DIR)' % path, flush=True)
        return
    _fresh()
    obj, nsub = P.import_body(bpy.context, path)
    toggles = [name for name, _bit, _d in P.FLAG_PROPS if name != 'pgb2_face']
    want = {}
    for k, mat in enumerate(obj.data.materials):
        for j, name in enumerate(toggles):
            setattr(mat, name, bool((k + j) % 3 == 0))
        mat.pgb2_alpha_ref = k
        want[k] = P._mat_flags(mat)
    used = {p.material_index for p in obj.data.polygons}
    os.makedirs(OUT, exist_ok=True)
    dst = os.path.join(OUT, 'body.bin')
    P.export_body(dst, obj)
    got = sorted(s['flags'] for s in pgb2.parse(open(dst, 'rb').read())['subs'])
    assert got == sorted(want[k] for k in used), (got, sorted(want[k] for k in used))
    for bit in (pgb2.SUB_SHADELESS, pgb2.SUB_TOON, pgb2.SUB_HAIR):
        assert any(f & bit for f in got), hex(bit)
    print('blender: pgb2 flags, every bit round-trips (%d submeshes)' % nsub, flush=True)


def _images(col):
    out = {}
    for o in col.all_objects:
        for s in getattr(o, 'material_slots', ()):
            for n in (s.material.node_tree.nodes if s.material and s.material.use_nodes else ()):
                if getattr(n, 'image', None) is not None:
                    out[n.image.name] = n.image
    return out


def test_textures_on_import():
    """The BIN's own textures arrive as images on their packets: the ball's
    colour map (not its specular, which the packet lists first), the boots'
    colour sheet."""
    import blender_io
    for name, img_name_part in (('ball', '_tex1'), ('face_dt0c', '_tex')):
        _fresh()
        raw = corpus.read_entry(*corpus.CLASSES[name])
        os.makedirs(OUT, exist_ok=True)
        src = os.path.join(OUT, 'src.bin')
        open(src, 'wb').write(raw)
        col, _n = P.import_bin(bpy.context, src)
        imgs = _images(col)
        assert imgs and any(img_name_part in k for k in imgs), (name, list(imgs))
        for img in imgs.values():
            assert not blender_io.image_changed(img), img.name
        print('blender: %-20s textures on import %s' % (name, sorted(imgs)), flush=True)


def test_texture_edit_exports():
    """Paint a square into the ball's colour map: the exported BIN carries it
    (decoded back within DXT tolerance), its other texture blocks and the
    model are byte-identical."""
    import numpy as np
    import blender_io
    import textures
    _fresh()
    raw = corpus.read_entry(*corpus.CLASSES['ball'])
    PAINT = (255, 0, 255, 255)

    def paint(col):
        img = next(iter(_images(col).values()))
        px = blender_io.image_rgba(img)
        px[16:48, 16:48] = PAINT
        img.pixels.foreach_set((np.flipud(px).astype(np.float32) / 255.0).ravel())
        paint.block = int(img[blender_io.TEX_BLOCK_PROP])

    out = _roundtrip(raw, paint)
    a, b = container.read(raw), container.read(out)
    for k, (x, y) in enumerate(zip(a.blocks, b.blocks)):
        if k == paint.block:
            _, _, px = textures.decode(y.data)
            assert (np.abs(px[16:48, 16:48].astype(int) - PAINT) <= 8).all(), 'paint lost'
        else:
            assert x.data == y.data, ('block changed', k)
    print('blender: ball texture edit exports, other blocks byte-identical', flush=True)


if __name__ == '__main__':
    if not corpus.available():
        print('SKIP: no game at %s (set PES12_GAME)' % corpus.GAME)
    else:
        test_unedited_byte_exact()
        test_edit_survives()
        test_textures_on_import()
        test_texture_edit_exports()
    test_pgb2_flags()
    print('BLENDER TESTS PASS', flush=True)

"""Convert a ball mesh + texture into a PES2012 dt0b ball BIN.

Source mesh: PES21 .fmdl (FmdlFile, Fox coords Y-up metres), PES14-17
.model (`MODEL\0` magic, pes-model-blender ModelFile, same axes) or plain
.obj (metres, same axes). Texture: any PIL-readable image (PES14-17 DDS
included), or a PES21 .ftex.

  tools/pes12_ball.py install <src mesh> <menu index> <afs2fs root> --name=NAME
                      [--template=N] [--texture=T] [--preview=IMG]
  tools/pes12_ball.py preview <ball_N.bin> <afs2fs root> [--image=IMG]
  tools/pes12_ball.py <src mesh> <out ball_N.bin> [--template=N]
                      [--texture=T] [--texture-slot=i]

`install` writes the BIN, its balls.txt name and its menu preview (dt0b
#36, rendered from the BIN or taken from an image); see 11-balls.md.

Template = stock ball N (default 11); ball templates have 1 packet.
Geometry replaces the packet's vertex/index buffers via ktmdl_write.build
(indices stay triangle-strip: the template prim is NEVER changed).
Only one texture block is replaced (default slot 0 = color art; verified by
decoding both stock balls: file order is [color DXT1, normal DXT5,
specular DXT1], while the textureType table order is [spec, normal, color]).

Scale: uniform, source bounding-sphere radius -> stock ball radius (~0.11 m).
Axes pass through unchanged: PES21 balls are Y-up, origin-centered, same as
the stock ball (pole vertex at +y in both).
"""
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ktmdl_write  # noqa: E402

VENDOR = os.environ.get('PES12_VENDOR', os.path.join(HERE, 'vendor'))
sys.path.insert(0, VENDOR)
GAME = os.environ.get('PES12_GAME', os.path.join(HERE, 'game'))
DT0B = os.environ.get('PES12_DT0B', os.path.join(GAME, 'img', 'dt0b.img'))
WESYS_TAG = b"\x00\x01\x01WESYS"  # tools/pes12db.py WESYS_TAG
COLOR_SLOT = 0  # file-order texture block holding the color art (see above)


def stock_template(n):
    import afs
    raw = afs.read(DT0B, n)
    if raw[3:8] != b"WESYS":
        raise ValueError("template %d: not WESYS" % n)
    if zlib.decompressobj().decompress(raw[16:]) != zlib.decompress(raw[16:]):
        raise ValueError("template %d: trailing garbage" % n)
    return raw[:8], zlib.decompress(raw[16:])


def read_fmdl(path):
    import FmdlFile
    f = FmdlFile.FmdlFile()
    f.readFile(path)
    pos, nrm, uv, idx = [], [], [], []
    for m in f.meshes:
        if not len(m.faces) > 1:
            continue
        base = len(pos)
        ids = {id(v): k for k, v in enumerate(m.vertices)}
        for v in m.vertices:
            pos.append((v.position.x, v.position.y, v.position.z))
            nrm.append((v.normal.x, v.normal.y, v.normal.z))
            u = v.uv[0] if v.uv else None
            # PES21 fmdl UVs are a different format; this flip is unverified in
            # game (only the .model path was, 01-10) - do not "fix" it blindly.
            uv.append((u.u, 1.0 - u.v) if u is not None else (0.0, 0.0))
        for fa in m.faces:
            idx.extend(base + ids[id(v)] for v in fa.vertices)
    return pos, nrm, uv, idx


def read_obj(path):
    """Wavefront OBJ (Blender: File > Export > Wavefront, Y up): one vertex per
    distinct v/vt/vn corner, polygons fan-triangulated."""
    pos, uv, nrm = [], [], []
    corners, out_pos, out_uv, out_nrm, idx = {}, [], [], [], []

    def ref(i, n):          # OBJ index (1-based, negative = from the end) -> 0-based
        return int(i) - 1 if int(i) > 0 else n + int(i)

    for line in open(path):
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            pos.append(tuple(map(float, p[1:4])))
        elif p[0] == "vt":
            uv.append((float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
        elif p[0] == "vn":
            nrm.append(tuple(map(float, p[1:4])))
        elif p[0] == "f":
            poly = []
            for c in p[1:]:
                if c not in corners:
                    f = (c.split("/") + ["", ""])[:3]
                    corners[c] = len(out_pos)
                    out_pos.append(pos[ref(f[0], len(pos))])
                    out_uv.append(uv[ref(f[1], len(uv))] if f[1] else (0.0, 0.0))
                    out_nrm.append(nrm[ref(f[2], len(nrm))] if f[2] else (0.0, 1.0, 0.0))
                poly.append(corners[c])
            for k in range(1, len(poly) - 1):
                idx += [poly[0], poly[k], poly[k + 1]]
    return out_pos, out_nrm, out_uv, idx


def signed_volume(pos, tris):
    vol = 0.0
    for a, b, c in tris:
        vol += (pos[a][0] * (pos[b][1] * pos[c][2] - pos[b][2] * pos[c][1])
                - pos[b][0] * (pos[a][1] * pos[c][2] - pos[a][2] * pos[c][1])
                + pos[c][0] * (pos[a][1] * pos[b][2] - pos[a][2] * pos[b][1]))
    return vol / 6


def winding_vote(pos, nrm, tris):
    """> 0 when the triangles' winding mostly agrees with their vertex
    normals, < 0 when it mostly disagrees, 0 without real normals (OBJ
    without vn). Counted per triangle; an exact tie (a full outline hull is
    the body pushed outward, same triangle count) goes to the smaller
    triangles, i.e. the body, by a 1/area-weighted vote."""
    count, weighted = 0, 0.0
    for a, b, c in tris:
        e1 = [pos[b][i] - pos[a][i] for i in range(3)]
        e2 = [pos[c][i] - pos[a][i] for i in range(3)]
        f = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        area2 = math.sqrt(sum(x * x for x in f))
        d = sum(f[i] * (nrm[a][i] + nrm[b][i] + nrm[c][i]) for i in range(3))
        if area2 and d:
            count += 1 if d > 0 else -1
            weighted += math.copysign(1.0 / area2, d)
    return count or weighted


def to_strip(tris):
    """Triangle list -> single strip with degenerate stitches. Every real
    triple starts at an ODD index, the first one too (a leading duplicate),
    so all of them unwind with one winding. Until 08-10 the first started at
    0 and every converted ball had that one triangle backwards (culled from
    the front: 1 disagreeing triangle per ball, e.g. 779/1 on 4cc ball001)."""
    if not tris:
        return []
    out = [tris[0][0]] + list(tris[0])
    for a, b, c in tris[1:]:
        out += [out[-1], a, a]
        if (len(out) - 2) % 2:
            out.append(a)
        out += [b, c]
    return out


# pack_vertices layout: position, normal, tangent, binormal (3 floats each),
# UV0 and UV1 (2 floats each); stock balls 29-31 use a 56-byte layout instead
VERTEX_BYTES = 64


def pack_vertices(pos, nrm, uv, scale):
    out = bytearray()
    for (x, y, z), (nx, ny, nz), (u, v) in zip(pos, nrm, uv):
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / ln, ny / ln, nz / ln
        # tangent: any unit vector perpendicular to n; binormal = t x n
        # (stock balls satisfy |TxN - B| < 2e-6)
        ax = (0.0, 1.0, 0.0) if abs(ny) < 0.9 else (1.0, 0.0, 0.0)
        tx = (ax[1] * nz - ax[2] * ny, ax[2] * nx - ax[0] * nz,
              ax[0] * ny - ax[1] * nx)
        lt = math.sqrt(sum(c * c for c in tx)) or 1.0
        tx = tuple(c / lt for c in tx)
        bx = (tx[1] * nz - tx[2] * ny, tx[2] * nx - tx[0] * nz,
              tx[0] * ny - tx[1] * nx)
        out += struct.pack("<3f3f3f3f2f2f",
                           x * scale, y * scale, z * scale,
                           nx, ny, nz, *tx, *bx, u, v, u, v)
    return bytes(out)


WE00_HEAD = 16                          # WE00 header before each DDS
DDS_HEIGHT, DDS_WIDTH, DDS_MIPS, DDS_FOURCC = 12, 16, 28, 84   # DDS header field offsets (from the 128-byte header)
DDS_TAG_START, DDS_TAG_END = 32, 76     # DWReserved1: magick writes a tag, stock is zeros


def convert_texture(src, block):
    """Image/.ftex -> WE00+DDS content bytes for one texture slot, encoded at
    the slot's own size, format and mip count (read from the template's DDS
    header: every stock colour slot is 512x512 DXT1, 10 mips). Alpha in the
    source is dropped when the slot is DXT1, as the game samples it."""
    if src.lower().endswith(".ftex"):
        import ftex
        dds, ext = ftex.to_dds(open(src, "rb").read()), ".dds"
    else:
        import ktmdl
        dds, ext = ktmdl.unwesys(open(src, "rb").read()), os.path.splitext(src)[1]   # some pack DDS are wrapped
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as t:
        t.write(dds)
        src = t.name
    with tempfile.NamedTemporaryFile(suffix=".dds", delete=False) as t:
        dst = t.name
    slot = block[WE00_HEAD:]
    h, w, mips = (struct.unpack_from("<I", slot, o)[0] for o in (DDS_HEIGHT, DDS_WIDTH, DDS_MIPS))
    fmt = slot[DDS_FOURCC:DDS_FOURCC + 4].decode().lower()
    subprocess.run(["magick", src + "[0]", "-resize", "%dx%d!" % (w, h),
                    "-define", "dds:compression=" + fmt, "-define", "dds:mipmaps=%d" % (mips - 1), dst],
                   check=True, capture_output=True)
    os.unlink(src)
    dds = open(dst, "rb").read()
    os.unlink(dst)
    if len(dds) != len(slot):
        raise ValueError("texture payload %d != slot %d" % (len(dds), len(slot)))
    if dds[:4] != b"DDS ":
        raise ValueError("magick did not write a DDS")
    we = bytearray(block[:WE00_HEAD])
    struct.pack_into("<I", we, 8, len(dds))  # WE00 size field = DDS length
    return bytes(we) + dds[:DDS_TAG_START] + b"\x00" * (DDS_TAG_END - DDS_TAG_START) + dds[DDS_TAG_END:]


def split_bin(body):
    """Decompressed ball BIN -> (ktmdl, [texture contents]). Entry sizes
    count content bytes (WE00+DDS); each block is zero-padded to 16 on disk
    (proven: DXT1 slots read 174928 vs size 174920; DXT5/others already
    aligned). Entry 3's end field is 0; block 3 runs to EOF."""
    n, _, hs = struct.unpack_from("<III", body)
    if (n, hs) != (4, 64):
        raise ValueError("not a 4-entry ball BIN")
    sizes = [struct.unpack_from("<I", body, 12 + 12 * k)[0] for k in range(n)]
    ends = [struct.unpack_from("<I", body, 12 + 12 * k + 8)[0] for k in range(3)]
    bounds = [hs, *ends, len(body)]
    blocks = []
    for k in range(4):
        content = body[bounds[k]:bounds[k] + sizes[k]]
        pad = body[bounds[k] + sizes[k]:bounds[k + 1]]
        if any(pad) or len(pad) != (-sizes[k]) % 16:
            raise ValueError("block %d: unexpected pad" % k)
        blocks.append(content)
    return blocks[0], blocks[1:]


def join_bin(ktmdl, tex_blocks):
    n, hs = 4, 64
    ktmdl = bytearray(ktmdl)
    while len(ktmdl) % 16:  # keep entry size == header.size, both 16-aligned
        ktmdl += b"\x00"
    struct.pack_into("<I", ktmdl, 0x90, len(ktmdl))  # header.size, ref parser
    sizes = [len(ktmdl)] + [len(b) for b in tex_blocks]
    out = bytearray(struct.pack("<III", n, 8, hs))
    pos = hs + len(ktmdl)
    out += struct.pack("<IiI", sizes[0], -16, pos)
    for k in range(2):
        pos += sizes[k + 1] + (-sizes[k + 1]) % 16
        out += struct.pack("<IiI", sizes[k + 1], -16, pos)
    out += struct.pack("<IiI", sizes[3], -16, 0) + b"\x00" * 4
    out += ktmdl
    for b in tex_blocks:
        out += b + b"\x00" * ((-len(b)) % 16)
    return bytes(out)


# PES14-17 MODEL (pes-model-blender ModelFile, same parser as
# tools/pes15_to_pes12.py): 0 bones, world-space, Y-up metres.
def is_model_file(path):
    import ktmdl
    return ktmdl.unwesys(open(path, 'rb').read())[:6] == b'MODEL\x00'


def read_model(path):
    """PES14-17 .model -> (pos, nrm, uv, idx), world-space metres."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'pes_model_file', os.environ.get('PES12_MODEL_FILE', os.path.join(VENDOR, 'ModelFile.py')))
    MF = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(MF)
    import ktmdl
    with tempfile.NamedTemporaryFile(suffix='.model', delete=False) as t:
        t.write(ktmdl.unwesys(open(path, 'rb').read()))   # PES14-17 packs ship most .model wrapped
    try:
        r = MF.readModelFile(t.name, MF.ParserSettings())
    finally:
        os.unlink(t.name)
    m = r[0] if isinstance(r, tuple) else r
    pos, nrm, uv, idx = [], [], [], []
    for x in m.meshes:
        if not len(x.faces) > 1:
            continue
        base = len(pos)
        ids = {id(v): k for k, v in enumerate(x.vertices)}
        for v in x.vertices:
            pos.append((v.position.x, v.position.y, v.position.z))
            n = (v.normal.x, v.normal.y, v.normal.z) if v.normal else (0.0, 1.0, 0.0)
            nrm.append(n)
            u = v.uv[0] if v.uv else None
            # source UVs pass through unchanged: the PES14-17 .model layout is
            # the one PES2012 samples. In game (01-10): with a 1-v flip the ball
            # texture is scrambled, without it the 4cc balls read correctly -
            # the same convention pes12_stadium.py uses for these models.
            uv.append((u.u, u.v) if u is not None else (0.0, 0.0))
        for fa in x.faces:
            idx.extend(base + ids[id(v)] for v in fa.vertices)
    return pos, nrm, uv, idx


def convert(src_mesh, out_path, template_n=11, texture=None, slot=COLOR_SLOT):
    wtag, body = stock_template(template_n)
    kt, tex_blocks = split_bin(body)
    if src_mesh.lower().endswith(".fmdl"):
        pos, nrm, uv, idx = read_fmdl(src_mesh)
    elif is_model_file(src_mesh):
        pos, nrm, uv, idx = read_model(src_mesh)
    else:
        pos, nrm, uv, idx = read_obj(src_mesh)
    src_r = max(math.sqrt(x * x + y * y + z * z) for x, y, z in pos)
    vb, _sidx, stride = ktmdl_write.packet_mesh(kt, 0)
    if not os.path.getsize(src_mesh) and not src_mesh.endswith(".obj"):
        sys.exit("%s: empty source mesh" % src_mesh)
    if stride != VERTEX_BYTES:
        sys.exit("template ball %d has %d-byte vertices; pick a %d-byte one (any but 29-31; default 11)"
                 % (template_n, stride, VERTEX_BYTES))
    stock_max = max(
        math.sqrt(sum(c * c for c in struct.unpack_from("<3f", vb, k * stride)))
        for k in range(len(vb) // stride))
    scale = stock_max / src_r
    tris = [tuple(idx[k:k + 3]) for k in range(0, len(idx) - 2, 3)]
    # the strip unwind emits even triples as (a,c,b), which reverses the
    # source winding; flip here so the PARSER-REPORTED triangles keep
    # stock's orientation (raw11: signed volume +0.0331, every triangle's
    # winding agrees with its vertex normals). Decided by the normals where
    # the source has them: toon balls carry a larger inverted hull for their
    # outline whose volume outweighs the body, and the volume test turned
    # those bodies inside out (08-10: /vg/ knuckles, /u/ treat, /fit/ dumbbell
    # and /jp/ sweets rendered black, their hull's black texels facing out).
    vote = winding_vote(pos, nrm, tris) if len(set(nrm)) > 1 else 0   # OBJ without vn: one default normal
    if (vote > 0) if vote else signed_volume(pos, tris) > 0:
        tris = [(a, c, b) for a, b, c in tris]
    new_kt = ktmdl_write.build(
        kt, [{"packet": 0,
              "vertices": pack_vertices(pos, nrm, uv, scale),
              "indices": to_strip(tris)}])
    if texture:
        tex_blocks[slot] = convert_texture(texture, tex_blocks[slot])
    open(out_path, "wb").write(wesys_pack(wtag, join_bin(new_kt, tex_blocks)))
    print("wrote %s (%d verts, scale %.4f)" % (out_path, len(pos), scale))


def wesys_pack(tag, body):
    """Body -> WESYS entry. csize counts the zlib stream; the stock file then
    pads the BIN to 16 with zeros (dt0b#11: 7 pad bytes past the stream end)."""
    comp = zlib.compress(body, 9)
    return tag + struct.pack("<II", len(comp), len(body)) + comp + b"\x00" * ((-len(comp)) % 16)


# ---- ball menu previews ----
# dt0b #36 holds the ball menu's previews: u32 count (34), u32 8, then per
# block (offset, size, name offset); each block is WE00 + a 128x128 DXT3 DDS
# with no mips, named ball_<ball id>.tex, plus ball_base.tex (the backdrop)
# and ball_random.tex last. Preview block i shows dt0b ball BIN i + 2:
# measured 08-10 by matching every stock BIN's colour art to the 32 ball
# previews (classic -> BIN 2, Predator -> BIN 13, the two licensed balls the
# game ships as "dummy" art -> BINs 17/18). A served ball keeps the stock
# ball's preview unless this block is replaced too.
PREVIEW_ENTRY = 36
PREVIEW_FIRST_BIN = 2
PREVIEW_BALLS = 32                      # blocks 0..31; 32 = backdrop, 33 = random
PREVIEW_PX = 128
# the stock ball disc (alpha > 128) spans x 6..120, y 2..118 in every preview
PREVIEW_CENTRE_PX = (63.0, 60.0)
PREVIEW_RADIUS_PX = 57.5
PREVIEW_SUPERSAMPLE = 4                 # rendered at 4x, box-filtered down: smooth disc edge
# camera and light, model space (Y-up): looking at the ball's front from a
# little above, lit from the upper left like the stock renders
PREVIEW_VIEW_DIR = (0.25, 0.35, 1.0)
PREVIEW_LIGHT_DIR = (-0.55, 0.65, 0.55)
PREVIEW_AMBIENT = 0.45
PREVIEW_DIFFUSE = 0.7
PREVIEW_SPECULAR = 0.25
PREVIEW_SHININESS = 24.0
# ball vertex layout (pack_vertices): position at 0, normal at 12, UV0 at 48
VERTEX_POS, VERTEX_NRM, VERTEX_UV = 0, 12, 48
DDS_HEADER_BYTES = 128                  # "DDS " + 124-byte header
DXT_BLOCK_PX, DXT_BLOCK_BYTES = 4, 16   # DXT3/DXT5: 4x4 texels in 16 bytes


def _unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def render_preview(ball_body):
    """Decompressed ball BIN -> 128x128 RGBA PIL image of the ball itself
    (its own mesh and colour art), framed like the stock previews."""
    import io
    import numpy as np
    from PIL import Image
    kt, tex_blocks = split_bin(ball_body)
    vb, strip, stride = ktmdl_write.packet_mesh(kt, 0)
    if stride != VERTEX_BYTES:
        raise ValueError("ball vertices are %d bytes; previews need the %d-byte layout" % (stride, VERTEX_BYTES))
    rec = np.frombuffer(vb, dtype=np.uint8).reshape(-1, stride)
    pos = rec[:, VERTEX_POS:VERTEX_POS + 12].copy().view(np.float32).astype(np.float64)
    nrm = rec[:, VERTEX_NRM:VERTEX_NRM + 12].copy().view(np.float32).astype(np.float64)
    uv = rec[:, VERTEX_UV:VERTEX_UV + 8].copy().view(np.float32).astype(np.float64)
    art = np.asarray(Image.open(io.BytesIO(tex_blocks[COLOR_SLOT][WE00_HEAD:])).convert("RGB"), dtype=np.float64) / 255
    th, tw = art.shape[:2]
    view = _unit(PREVIEW_VIEW_DIR)
    right = _unit(np.cross((0.0, 1.0, 0.0), view))
    up = np.cross(view, right)
    size = PREVIEW_PX * PREVIEW_SUPERSAMPLE
    scale = PREVIEW_RADIUS_PX * PREVIEW_SUPERSAMPLE / np.linalg.norm(pos, axis=1).max()
    sx = PREVIEW_CENTRE_PX[0] * PREVIEW_SUPERSAMPLE + pos @ right * scale
    sy = PREVIEW_CENTRE_PX[1] * PREVIEW_SUPERSAMPLE - pos @ up * scale
    sz = pos @ view
    depth = np.full((size, size), -np.inf)
    tri_of = np.full((size, size), -1, dtype=np.int64)
    bary = np.zeros((size, size, 3))
    # strip triangles in a consistent winding (odd ones swap their first two
    # corners), then cull the ones facing away, as the game does. Front =
    # this winding's normal toward the camera: every stock ball's triangles
    # wind that way with their vertex normals (08-10, BINs 2/3/11/12/13/28:
    # 1 of 6120 the other way). Toon balls' outline hulls wind inward and
    # show only at the silhouette.
    tris = [(strip[k + 1], strip[k], strip[k + 2]) if k % 2 else (strip[k], strip[k + 1], strip[k + 2])
            for k in range(len(strip) - 2) if len({strip[k], strip[k + 1], strip[k + 2]}) == 3]
    tri = np.asarray(tris)
    face = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
    tris = [t for t, f in zip(tris, face) if f @ view > 0]
    for t, (a, b, c) in enumerate(tris):
        xs, ys = sx[[a, b, c]], sy[[a, b, c]]
        x0, x1 = max(int(xs.min()), 0), min(int(xs.max()) + 1, size - 1)
        y0, y1 = max(int(ys.min()), 0), min(int(ys.max()) + 1, size - 1)
        if x0 > x1 or y0 > y1:
            continue
        det = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if det == 0:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        w0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / det
        w1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / det
        w2 = 1 - w0 - w1
        z = w0 * sz[a] + w1 * sz[b] + w2 * sz[c]
        win = depth[y0:y1 + 1, x0:x1 + 1]
        hit = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (z > win)
        win[hit] = z[hit]
        tri_of[y0:y1 + 1, x0:x1 + 1][hit] = t
        bary[y0:y1 + 1, x0:x1 + 1][hit] = np.stack([w0, w1, w2], -1)[hit]
    cov = tri_of >= 0
    ids = np.asarray(tris)[tri_of[cov]]
    w = bary[cov][..., None]
    puv = (uv[ids] * w).sum(1)
    pn = (nrm[ids] * w).sum(1)
    pn /= np.linalg.norm(pn, axis=1, keepdims=True)
    tx = np.clip((puv[:, 0] % 1.0) * tw, 0, tw - 1).astype(int)
    ty = np.clip((puv[:, 1] % 1.0) * th, 0, th - 1).astype(int)
    light = _unit(PREVIEW_LIGHT_DIR)
    half = _unit(light + view)
    lit = PREVIEW_AMBIENT + PREVIEW_DIFFUSE * np.clip(pn @ light, 0, None)
    spec = PREVIEW_SPECULAR * np.clip(pn @ half, 0, None) ** PREVIEW_SHININESS
    rgba = np.zeros((size, size, 4))
    rgba[cov, :3] = np.clip(art[ty, tx] * lit[:, None] + spec[:, None], 0, 1)
    rgba[cov, 3] = 1
    rgba[..., :3] *= rgba[..., 3:]                        # premultiply so the box filter does not bleed black
    big = Image.fromarray((rgba * 255).round().astype(np.uint8), "RGBA")
    small = np.asarray(big.resize((PREVIEW_PX, PREVIEW_PX), Image.BOX), dtype=np.float64)
    a = small[..., 3:] / 255
    small[..., :3] = np.where(a > 0, small[..., :3] / np.maximum(a, 1e-6), 0)
    return Image.fromarray(small.round().clip(0, 255).astype(np.uint8), "RGBA")


def encode_preview(img, stock_block):
    """RGBA image -> one preview block: the stock block's WE00 + DDS headers
    byte for byte, then DXT3 pixels. ImageMagick has no DXT3 writer (asked
    for dxt3 it writes DXT5, mip flag set), so its DXT5 colour halves are
    kept - DXT3 and DXT5 colour blocks share one layout, always 4-colour -
    and the alpha halves become DXT3's explicit 4-bit alpha from the image."""
    import numpy as np
    from PIL import Image
    img = img.convert("RGBA").resize((PREVIEW_PX, PREVIEW_PX), Image.LANCZOS)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        src = t.name
    with tempfile.NamedTemporaryFile(suffix=".dds", delete=False) as t:
        dst = t.name
    img.save(src)
    subprocess.run(["magick", src, "-define", "dds:compression=dxt5",
                    "-define", "dds:mipmaps=0", dst], check=True, capture_output=True)
    dds = open(dst, "rb").read()
    os.unlink(src)
    os.unlink(dst)
    head = WE00_HEAD + DDS_HEADER_BYTES
    blocks = (PREVIEW_PX // DXT_BLOCK_PX) ** 2
    if len(stock_block) != head + blocks * DXT_BLOCK_BYTES:
        raise ValueError("stock preview block is %d bytes, not a %dpx DXT3" % (len(stock_block), PREVIEW_PX))
    colour = np.frombuffer(dds, np.uint8, blocks * DXT_BLOCK_BYTES, DDS_HEADER_BYTES).reshape(blocks, DXT_BLOCK_BYTES)[:, 8:]
    a = np.asarray(img, np.uint8)[..., 3].astype(np.uint16)
    a4 = ((a * 15 + 127) // 255).astype(np.uint8)          # 8-bit -> 4-bit, rounded
    side = PREVIEW_PX // DXT_BLOCK_PX
    a4 = a4.reshape(side, DXT_BLOCK_PX, side, DXT_BLOCK_PX).transpose(0, 2, 1, 3).reshape(blocks, 8, 2)
    alpha = a4[:, :, 0] | (a4[:, :, 1] << 4)                 # texel pairs, low nibble first
    return stock_block[:head] + np.concatenate([alpha, colour], 1).tobytes()


def preview_blocks(body):
    """dt0b #36 body -> [(offset, size, name)]."""
    n = struct.unpack_from("<I", body)[0]
    out = []
    for k in range(n):
        off, size, name = struct.unpack_from("<III", body, 8 + 12 * k)
        out.append((off, size, body[name:body.index(b"\x00", name)].decode()))
    return out


def afs2fs_entry_path(root, img, entry, prefix):
    """The override afs2fs serves for <img> entry N under `root` (any
    <name>_<N>.bin), or a new <prefix>_<N>.bin."""
    d = os.path.join(root, "img", img)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        if f.lower().endswith("_%d.bin" % entry):
            return os.path.join(d, f)
    return os.path.join(d, "%s_%d.bin" % (prefix, entry))


def set_preview(root, bin_n, img):
    """Replace ball BIN `bin_n`'s menu preview in the afs2fs `root` (composing
    with previews already written there)."""
    if not PREVIEW_FIRST_BIN <= bin_n < PREVIEW_FIRST_BIN + PREVIEW_BALLS:
        raise ValueError("BIN %d has no menu preview (BINs %d..%d do)"
                         % (bin_n, PREVIEW_FIRST_BIN, PREVIEW_FIRST_BIN + PREVIEW_BALLS - 1))
    import afs
    path = afs2fs_entry_path(root, "dt0b.img", PREVIEW_ENTRY, "ball_previews")
    raw = open(path, "rb").read() if os.path.exists(path) else afs.read(DT0B, PREVIEW_ENTRY)
    body = bytearray(zlib.decompress(raw[16:]))
    off, size, name = preview_blocks(body)[bin_n - PREVIEW_FIRST_BIN]
    body[off:off + size] = encode_preview(img, bytes(body[off:off + size]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(wesys_pack(raw[:8], bytes(body)))
    print("preview %s (BIN %d) -> %s" % (name, bin_n, path))


BALL_NAMES = "balls.txt"
BALL_NAMES_HEADER = "# Balls map (encoding must be UTF-8); index = BIN - 1"


def set_name(root, bin_n, name):
    """balls.txt line for menu index BIN - 1 (11-balls.md), others kept."""
    import re
    path = os.path.join(root, BALL_NAMES)
    lines = open(path, encoding="utf-8-sig").read().splitlines() if os.path.exists(path) else [BALL_NAMES_HEADER]
    lines = [ln for ln in lines if not re.match(r"\s*%d\s*," % (bin_n - 1), ln)]
    lines.append('%d, "%s"' % (bin_n - 1, name.replace('"', "'")))
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")


def install(src_mesh, index, root, name, preview=None, **kw):
    """One command: ball BIN for menu `index`, its balls.txt name and its
    menu preview (rendered from the BIN just written, or `preview` image)."""
    from PIL import Image
    bin_n = index + 1
    out = afs2fs_entry_path(root, "dt0b.img", bin_n, "ball")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    convert(src_mesh, out, **kw)
    set_name(root, bin_n, name)
    raw = open(out, "rb").read()
    set_preview(root, bin_n, Image.open(preview) if preview else render_preview(zlib.decompress(raw[16:])))


USAGE = """pes12_ball.py <src mesh> <out ball_N.bin> [--template=N] [--texture=T] [--texture-slot=i]
pes12_ball.py install <src mesh> <menu index> <afs2fs root> --name=NAME [--template=N] [--texture=T] [--preview=IMG]
pes12_ball.py preview <ball BIN> <afs2fs root> [--image=IMG]   (BIN number from the file name)"""


def main(argv):
    kw, args = {}, []
    for a in argv:
        key, _, val = a.partition("=")
        if key == "--template":
            kw["template_n"] = int(val)
        elif key == "--texture":
            kw["texture"] = val
        elif key == "--texture-slot":
            kw["slot"] = int(val)
        elif key in ("--name", "--preview", "--image"):
            kw[key[2:]] = val
        else:
            args.append(a)
    if args[:1] == ["install"] and len(args) == 4 and "name" in kw:
        return install(args[1], int(args[2]), args[3], **kw)
    if args[:1] == ["preview"] and len(args) == 3:
        import re
        from PIL import Image
        bin_n = int(re.search(r"_(\d+)\.bin$", args[1], re.I).group(1))
        image = kw.get("image")
        return set_preview(args[2], bin_n, Image.open(image) if image else
                           render_preview(zlib.decompress(open(args[1], "rb").read()[16:])))
    if len(args) != 2 or {"name", "preview", "image"} & set(kw):
        sys.exit(USAGE)
    convert(args[0], args[1], **kw)


if __name__ == "__main__":
    main(sys.argv[1:])



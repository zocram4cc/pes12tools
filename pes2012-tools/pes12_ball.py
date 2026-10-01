"""Convert a ball mesh + texture into a PES2012 dt0b ball BIN.

Source mesh: PES21 .fmdl (FmdlFile, Fox coords Y-up metres), PES14-17
.model (`MODEL\0` magic, pes-model-blender ModelFile, same axes) or plain
.obj (metres, same axes). Texture: any PIL-readable image (PES14-17 DDS
included), or a PES21 .ftex.

  tools/pes12_ball.py <src mesh> <out ball_N.bin> [--template=N]
                      [--texture=T] [--texture-slot=i]

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


def to_strip(tris):
    """Triangle list -> single strip with degenerate stitches. The stitch
    inserts one extra duplicate when needed so the real triple always starts
    at an even index (odd starts flip winding under the PES-Tools unwind)."""
    if not tris:
        return []
    out = list(tris[0])
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
            uv.append((u.u, 1.0 - u.v) if u is not None else (0.0, 0.0))
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
    # stock's positive signed volume (raw11: +0.0331)
    if signed_volume(pos, tris) > 0:
        tris = [(a, c, b) for a, b, c in tris]
    new_kt = ktmdl_write.build(
        kt, [{"packet": 0,
              "vertices": pack_vertices(pos, nrm, uv, scale),
              "indices": to_strip(tris)}])
    if texture:
        tex_blocks[slot] = convert_texture(texture, tex_blocks[slot])
    out = join_bin(new_kt, tex_blocks)
    comp = zlib.compress(out, 9)
    # WESYS csize counts the zlib stream; the stock file then pads the BIN
    # to 16 with zeros (dt0b#11: 7 pad bytes past the stream end)
    pad = b"\x00" * ((-len(comp)) % 16)
    open(out_path, "wb").write(
        wtag + struct.pack("<II", len(comp), len(out)) + comp + pad)
    print("wrote %s (%d verts, scale %.4f)" % (out_path, len(pos), scale))


if __name__ == "__main__":
    kw, args = {}, []
    for a in sys.argv[1:]:
        if a.startswith("--template="):
            kw["template_n"] = int(a.split("=")[1])
        elif a.startswith("--texture="):
            kw["texture"] = a.split("=")[1]
        elif a.startswith("--texture-slot="):
            kw["slot"] = int(a.split("=")[1])
        else:
            args.append(a)
    convert(args[0], args[1], **kw)

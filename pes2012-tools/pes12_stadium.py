"""PES2012 licensed stadium slots: install a model, toggle stock assets,
replace the pitch art, name and thumbnail. See USAGE (bottom) for commands.

install <source>: a PES15/17 stadium folder (common/bg/model/bg/stadium/stNNN:
           its st_*.xml pick the models, the .mtl files resolve each
           material's DiffuseMap; sibling bg/pitch and bg/sky folders are
           imported too), or a PES21 scene .fmdl (textures looked up as .ftex
           under --textures, an extracted cpk root). Editing a slot's stock
           geometry and textures in Blender: pes2012-blender, stadium slots.
hide/show: crowd, pitch (the stock pitch base), props = per slot;
           boards, staff = shared entries, i.e. every stadium at once.
pitch:     an RGBA image over +/-60 x +/-40 m (GF pitch_overlay convention)
           composited over the slot's stock art, or a GF stadium folder
           (pitch_overlay.png over turf.png's colour).
name:      patches pes2012.exe (backup kept); thumb: dt06 previews.
All BIN output is afs2fs overrides under <root>/img/<img>/; commands read
the overrides already there, so they compose; an override equal to stock
is removed.

Where a stadium lives (10-stadiums.md, all read from the game, nothing
hardcoded per stadium): pes2012.exe carries one record per licensed stadium
as u32 (img << 24 | entry), img 07 = dt07.img:
  header  (standTexFirst, standTexLast, geometry, g+32, g+64, extra)
  6 x     (pitchTex, lightmapFirst, lightmapLast, props, x, w, w)
one 7-field record per time/weather variant. `geometry` is one entry with
every stand/roof/board part of the stadium as KTMDL blocks; `props` is a
per-variant KTMDL entry of extra parts. KTMDL packets bind textures by
numeric id (block textureNameIds), resolved against the WE00 texture
blocks of the entries loaded with the stadium; each WE00 header carries its
id at +4.

Install, per slot: every packet of the geometry and props entries is
emptied (one degenerate triangle), then each scene material gets one
geometry block whose first packet is a stock stride-40 diffuse+lightmap
packet (POSITION, NORMAL, TEXCOORD0 diffuse, TEXCOORD1 lightmap). That
block's texture table is repointed at two stand-texture slots: the
material's own image, re-encoded into a stand WE00 block at that block's
stock size/format, and a white block standing in for the lightmap (the
stock lightmaps are per variant and would shade our UV1=0 black). Stand
textures are loaded with the stadium in every variant, so one write covers
all six.

Coordinates: KTMDL is metres, Y-up, pitch in the y=0 plane (stock display
pitch entry 1567 spans 125 x 83 m). Fox-era sources (PES15-21) map
(x, y, z) -> (x, y, -z); triangles are then wound to the stock convention
(cross(b-a, c-a) along the vertex normal), per mesh, from the data.
"""
import glob
import io
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import afs  # noqa: E402
import ktmdl_write  # noqa: E402

GAME = os.environ.get('PES12_GAME', os.path.join(HERE, 'game'))
EXE = os.path.join(GAME, "pes2012.exe")
DT07 = os.path.join(GAME, "img", "dt07.img")
# fmdl/ftex/KTMDL/.model readers: the vendored copies (vendor/, VENDORED.md)
VENDOR = os.environ.get('PES12_VENDOR', os.path.join(HERE, 'vendor'))
GF_TOOLS = FMDL_LIB = VENDOR
KTMDL_READER = os.environ.get('PES12_KTMDL_READER', os.path.join(VENDOR, 'ktmdl_moth.py'))
PES_MODEL_READER = os.environ.get('PES12_MODEL_FILE', os.path.join(VENDOR, 'ModelFile.py'))

# Slot table (see module doc). Records are u32 with the img number in the
# top 16 bits: 0x0700xxxx = dt07 entry xxxx.
DT07_TAG = 0x0700
HEADER_FIELDS, VARIANT_FIELDS, VARIANTS = 6, 7, 6
# header record: geometry, geometry+32, geometry+64 are the stadium's three
# per-slot entries (KTMDL parts, per-slot data, per-slot texture), so the
# fixed strides identify a header anywhere in the exe.
SLOT_STRIDE = 32
# variant record field 5: the W entry holding the sky strip (texture id
# SKY_TEXTURE_ID, `e_sky.png` of the shared sky dome, dt07 entry 1 block 0).
SKY_FIELD = 5
SKY_DOME_ENTRY, SKY_DOME_BLOCK = 1, 0
SKY_TEXTURE_ID = 10692
# Raw WE00 texture (the sky strip): 'WE00', u16 id, u8 format, u8 bpp,
# u16 width, u16 height, 6 pad; then top-down RGB rows (stock 2298: 1024x256
# x 3 + 16 = 786448 bytes, decodes as a sky with the horizon fog at the bottom).
RAW_HEADER = 16
RAW_RGB_BPP = 24
# Stock pitch packets (p_grass_s, p_base_s, p_face_s): kept, the pitch is
# the playing surface in every stadium, and p_grass_s draws black at
# distance without the base under it.
PITCH_TEXTURE_PREFIX = "p_"
# lon/lat map resolution for reprojecting a source sky dome.
SKY_MAP_W, SKY_MAP_H = 2048, 1024
# Installed sky strip size relative to the stock strip (1024 x 256 raw RGB,
# ~2.8 px per degree of the panorama). Owner report 30-09: the reprojected
# skies read low-resolution. 2 gives 2048 x 512, the source panorama's own
# resolution for st004 (2048 x 1024 over 180 deg). Provisional: the engine
# accepting a larger raw strip is established only by the in-game check.
SKY_STRIP_SCALE = 2
# Source scene lift above the stock pitch plane (y=0) so a model's own
# playing deck occludes the stock grass instead of z-fighting it. Provisional:
# 2 cm was the first value tried (st030's deck sits exactly at y=0); raise it
# if the grass shows through at broadcast distance.
SCENE_LIFT_M = 0.02
# Source decks this close below y = 0 count as ground level and are lifted
# onto it (Karasuno: -0.06 m). Provisional: 0.5 m, well above modelling
# noise and well below real sunken parts (st030's 40 m deep structure).
GROUND_TOLERANCE_M = 0.5
# Source pitch meshes (lines) sit this far above the model's deck, like a
# decal. Provisional: 1 cm, the first value tried.
PITCH_DECAL_LIFT_M = 0.01
# Triangles are subdivided (longest edge at its midpoint) until no edge is
# longer than this, so tiles can be local: st030's deck is a handful of
# 150 m triangles. Provisional: 20 m, the first value tried.
MAX_EDGE_M = 20.0
# Per-slot data entry (geometry + SLOT_STRIDE) block 2: crowd. u32 table
# size T, (T-4)/4 section offsets, and one more section at offset T.
# Section: u32 rows, f3 centre, f3 extent (28 B), then 100 B per row of
# absolute seat quads (Allianz 2725: 58 + 41 + 35 + ... rows). The stock
# no-crowd stadium (slot 0, entry 2695) stores every section as an empty
# 28-byte stub right after the table; hiding writes exactly that layout.
# Zeroing T instead hangs the match load; moving section boxes away only
# culls them in some cameras (the rows are drawn from their own coords).
CROWD_BLOCK = 2
CROWD_SECTION_HEADER = 28
# Stock pitch packets (p_base_s + p_face_s, per slot). The shared grass
# overlay (entry 53, p_grass_s) draws only where it matches the pitch
# base's depth: with the base kept it covers a model's deck from the game
# and replay cameras; with the base removed and the deck lifted it has
# nothing to match and disappears (with the deck at y=0 it matched the
# deck and drew black).
# Shared (all-stadium) parts, found by their stock texture debug names:
# ad boards (c_bill_body frames, d_bill_face faces: dt07 2920-2925) and
# staff (staff*.psd/.dds: 2954, 2955, per-stadium lines 2958-3021).
SHARED_PARTS = {"boards": ("c_bill", "d_bill"), "staff": ("staff",)}

U16_MAX = 0xFFFF
# Stock stand/roof packet layout: stride 40, POSITION f3, NORMAL f3,
# TEXCOORD0 f2 (diffuse), TEXCOORD1 f2 (lightmap). Semantics per
# pes_ktmdl_importer SEMANTIC_NAMES; formats FLOAT3 = 2, FLOAT2 = 1.
SEM_POSITION, SEM_NORMAL, SEM_TEXCOORD = 0x10, 0x12, 0x16
FMT_FLOAT3, FMT_FLOAT2 = 0x02, 0x01
TEMPLATE_STRIDE = 40
TEXCOORD_DIFFUSE, TEXCOORD_LIGHTMAP = 0, 1
WE00_ID_OFF = 4          # WE00 header: 'WE00', u16 texture id
DDS_HEADER = 128         # 'DDS ' + 124-byte header (no DX10 in stock)
# The lightmap stand-in. A full-white stand-in blew st028's teal ground out
# to white (28-09); 64 (a x4 scale inferred from the ~50/255 stock lightmap
# averages) drew Karasuno's walls near black; 128 (a x2 scale) matches its
# PES17 thumbnail's wood tones (29-09 frames). Provisional: seen on st033
# only; the other installed stadiums still need a look at 128.
NEUTRAL_LIGHTMAP = (128, 128, 128, 255)
# PES15/17 stadium .mtl: the sampler that is the colour texture.
DIFFUSE_SAMPLER = "DiffuseMap"
# A Turf-shader pitch carries no DiffuseMap: its grass is TurfDiffuseMap and
# its LINES are DecalMap. PES2012 draws the lines from the slot's pitch art
# (PITCH_ART_ID), so the decal is the texture that has to reach the slot.
DECAL_SAMPLER = "DecalMap"
# PES21 fmdl texture roles that carry the colour texture.
FMDL_DIFFUSE_ROLES = ("Base_Tex_SRGB", "Base_Tex_LIN")

Mesh = namedtuple("Mesh", "name pos nrm uv tris image role blend", defaults=("scene", False))
"""pos/nrm: KTMDL-frame f3 lists; uv: TEXCOORD0 list; tris: index triples;
image: PIL RGBA image, or None (drawn white); role: "scene", "overlay" for
painted pitch content, or "lines" for Turf-shader marking strips (both baked
into the slot's pitch art by bake_pitch_art);
blend: the source material is alpha-blended (drawn after the opaque pieces)."""


def _ktmdl_reader():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pes_ktmdl", KTMDL_READER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- containers -----------------------------------------------------------

# WESYS header byte 2: 1 = zlib body, 0 = stored body (the per-variant pitch
# art entries, e.g. dt07 208: 1398288-byte body, header size field, 0).
WESYS_ZLIB = 1


def _override(root, index, img):
    name = os.path.basename(img)
    return os.path.join(root, "img", name, "%s_%d.bin" % (name.split(".")[0], index))


def read_entry(index, img=DT07, root=None):
    """AFS entry -> (WESYS tag bytes, body). With `root`, an afs2fs
    override already written there is read instead of the stock entry, so
    successive edits (install, then toggles) compose."""
    path = _override(root, index, img) if root else None
    raw = open(path, "rb").read() if path and os.path.exists(path) else afs.read(img, index)
    if raw[3:8] != b"WESYS":
        raise ValueError("%s #%d: not a WESYS entry" % (os.path.basename(img), index))
    if raw[2] == WESYS_ZLIB:
        return raw[:8], zlib.decompress(raw[16:])
    return raw[:8], raw[16:16 + struct.unpack_from("<I", raw, 8)[0]]


def write_entry(out_root, index, tag, body, img=DT07):
    """Write an afs2fs override; if it equals the stock entry, remove the
    override instead (a toggle switched back on leaves no file behind)."""
    dest = _override(out_root, index, img)
    if read_entry(index, img)[1] == body:
        if os.path.exists(dest):
            os.remove(dest)
        return None
    if tag[2] == WESYS_ZLIB:
        comp = zlib.compress(body, 9)
        out = tag + struct.pack("<II", len(comp), len(body)) + comp + b"\x00" * (-len(comp) % 16)
    else:
        out = tag + struct.pack("<II", len(body), 0) + body
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(out)
    return dest


def split_bin(body):
    """Decompressed entry -> [block bytes] via the inner TOC: u32 count,
    u32 8, then per block (offset, size, nameOff) with offsets relative to
    the body start."""
    n = struct.unpack_from("<I", body)[0]
    return [body[o:o + s] for o, s in
            (struct.unpack_from("<II", body, 8 + 12 * k) for k in range(n))]


def block_offsets(body):
    n = struct.unpack_from("<I", body)[0]
    return [struct.unpack_from("<I", body, 8 + 12 * k)[0] for k in range(n)]


def join_bin(blocks):
    """Inverse of split_bin, byte-exact on stock dt07/dt08 entries: header
    8+12n padded to 16, nameOff = -16, each block 16-aligned."""
    align = lambda v: (v + 15) & ~15  # noqa: E731
    hs = align(8 + 12 * len(blocks))
    out = bytearray(struct.pack("<II", len(blocks), 8))
    pos = hs
    for b in blocks:
        out += struct.pack("<IIi", pos, len(b), -16)
        pos += align(len(b))
    out += b"\x00" * (hs - len(out))
    for b in blocks:
        out += b + b"\x00" * (align(len(b)) - len(b))
    return bytes(out)


# --- slot table -----------------------------------------------------------

Slot = namedtuple("Slot", "number geometry stand props lightmaps skies pitches")
PITCH_FIELD = 0          # variant field 0: the pitch art entry (texture PITCH_ART_ID)


def _is_ktmdl_entry(index):
    try:
        _tag, body = read_entry(index)
    except ValueError:
        return False
    return split_bin(body)[0][:5] == b"KTMDL"


def read_slots(exe=EXE):
    """pes2012.exe -> [Slot] sorted by geometry entry. A record counts only
    if its geometry entry holds KTMDL blocks (the per-slot data entries at
    g+32 fit the same stride pattern). Slots whose variants all have an
    empty lightmap range (first > last) are placeholders and are left out."""
    data = open(exe, "rb").read()
    heads = {}
    for o in range(0, len(data) - 4 * HEADER_FIELDS, 4):
        w = struct.unpack_from("<%dI" % HEADER_FIELDS, data, o)
        if any(x >> 16 != DT07_TAG for x in w):
            continue
        e = [x & 0xFFFF for x in w]
        if e[3] != e[2] + SLOT_STRIDE or e[4] != e[2] + 2 * SLOT_STRIDE:
            continue
        vs = []
        for k in range(VARIANTS):
            v = struct.unpack_from("<%dI" % VARIANT_FIELDS, data,
                                   o + 4 * HEADER_FIELDS + 4 * VARIANT_FIELDS * k)
            if any(x >> 16 != DT07_TAG for x in v):
                break
            vs.append([x & 0xFFFF for x in v])
        if len(vs) == VARIANTS:
            heads.setdefault(e[2], (e, vs))
    slots = []
    for n, g in enumerate(sorted(heads)):
        e, vs = heads[g]
        lms = [(v[1], v[2]) for v in vs]
        if all(a > b for a, b in lms) or not _is_ktmdl_entry(g):
            continue
        slots.append(Slot(n, g, (e[0], e[1]), sorted({v[3] for v in vs}), lms,
                          sorted({v[SKY_FIELD] for v in vs}),
                          sorted({v[PITCH_FIELD] for v in vs})))
    return slots


# --- sources --------------------------------------------------------------

def fox_to_ktmdl(p):
    x, y, z = p
    return (x, y, -z)


_TEX_CACHE = {}

def _open_texture(path):
    """WESYS-wrapped or plain image path -> PIL RGBA, cached by resolved
    path: meshes sharing one .dds share one object, so install()'s material
    count is distinct textures, not meshes (st006: 50 meshes, far fewer
    files)."""
    from PIL import Image
    key = os.path.normcase(os.path.abspath(path)) if path else None
    if key not in _TEX_CACHE:
        blob = _source_bytes(path)
        if path.lower().endswith(".ftex"):
            sys.path.insert(0, GF_TOOLS)
            try:
                import ftex
                blob = ftex.to_dds(blob)
            finally:
                sys.path.remove(GF_TOOLS)
        _TEX_CACHE[key] = Image.open(io.BytesIO(blob)).convert("RGBA")
    return _TEX_CACHE[key]

def _fox_mesh(name, verts, faces, image):
    ids = {id(v): k for k, v in enumerate(verts)}
    pos = [fox_to_ktmdl((v.position.x, v.position.y, v.position.z)) for v in verts]
    nrm = [fox_to_ktmdl((v.normal.x, v.normal.y, v.normal.z)) if v.normal
           else (0.0, 1.0, 0.0) for v in verts]
    uv = [(v.uv[0].u, v.uv[0].v) if v.uv else (0.0, 0.0) for v in verts]
    tris = []
    for fa in faces:
        vs = [ids[id(v)] for v in fa.vertices]
        tris += [(vs[0], vs[k], vs[k + 1]) for k in range(1, len(vs) - 1)]
    return Mesh(name, pos, nrm, uv, tris, image)


def _find_nocase(path):
    """PES runs on Windows: './texture/df/sky.dds' opens 'sky.DDS'."""
    if os.path.exists(path):
        return path
    d, base = os.path.split(path)
    for f in os.listdir(d) if os.path.isdir(d) else []:
        if f.lower() == base.lower():
            return os.path.join(d, f)
    return None


def _source_bytes(path):
    """Raw file bytes with PES's WESYS wrapper (zlib) removed - stadium cpks
    ship some .mtl/.model files still wrapped (st007 st_df.mtl)."""
    raw = open(path, "rb").read()
    if raw[3:8] == b"WESYS":
        return zlib.decompress(raw[16:])
    return raw

def _source_text(path):
    return _source_bytes(path).decode("utf-8", "replace")

def read_pes15_models(folder, xml_glob, fallback_sampler=None):
    """PES15/17 bg folder -> [Mesh]: exactly the models its xml files load,
    each material's DiffuseMap from the xml's .mtl."""
    import importlib.util
    import tempfile
    spec = importlib.util.spec_from_file_location("pes_model_file", PES_MODEL_READER)
    MF = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(MF)
    models = {}
    # xml model paths are relative to the cpk root (st007's sky points at
    # model/bg/sky/common/model/sky.model, st030's probe copy at ./model/):
    # resolve them against the folder, then up. PES path matching is
    # case-insensitive, like _find_nocase.
    for xml in sorted(glob.glob(os.path.join(folder, xml_glob))):
        here = os.path.dirname(xml)
        for tag in re.findall(r"<model\b[^>]*>", _source_text(xml)):
            attr = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if "path" not in attr or "material" not in attr:
                continue
            mtl = _find_nocase(os.path.normpath(os.path.join(here, attr["material"])))
            if mtl is None or not os.path.isfile(mtl):
                continue  # SEd.mtl-style entries the cpk never ships
            cand = os.path.normpath(os.path.join(here, attr["path"]))
            if not os.path.isfile(cand):
                # xml model paths are relative to the cpk root; resolve them
                # against the folder, then up, case-insensitively.
                up = here
                for _ in range(6):
                    up = os.path.dirname(up)
                    cand = _find_nocase(os.path.normpath(
                        os.path.join(up, attr["path"].lstrip("./"))))
                    if cand and os.path.isfile(cand):
                        break
                    cand = None
                if not cand:
                    # Skies pointing at a shared model the cpk never ships
                    # fall back to a model bundled in the folder itself
                    # (st006's own sky.model). Stadium models stay skipped:
                    # a missing stand-in must not alias an arbitrary stand.
                    if attr.get("type") == "sky":
                        own = sorted(glob.glob(os.path.join(folder, "model", "*.model")))
                        cand = own[0] if own else None
                if not cand:
                    continue
            # every variant xml (df/nf/dr/nr) names its own .mtl for the
            # same model; keep them all, in xml order (st004's "pitch"
            # material is only in st_nf.mtl)
            if mtl not in models.setdefault(cand, []):
                models[cand].append(mtl)
    if not models:
        raise ValueError("%s: no %s <model> entries" % (folder, xml_glob))
    meshes = []
    for dst, mtls in models.items():
        diffuse, blended = {}, set()
        for mtl in mtls:
            mtl_dir = os.path.dirname(mtl)
            for name, body in re.findall(r'<material name="([^"]+)"[^>]*>(.*?)</material>',
                                         _source_text(mtl), re.S):
                m = re.search(r'name="%s"\s+path="([^"]+)"' % DIFFUSE_SAMPLER, body) \
                    or (re.search(r'name="%s"\s+path="([^"]+)"' % fallback_sampler, body)
                        if fallback_sampler else None)
                if m and name not in diffuse:
                    diffuse[name] = _find_nocase(os.path.normpath(os.path.join(mtl_dir, m.group(1))))
                if re.search(r'name="alphablend"\s+value="1"', body):
                    blended.add(name)
        raw = open(dst, "rb").read()
        src = dst
        if raw[3:8] == b"WESYS":
            with tempfile.NamedTemporaryFile(suffix=".model", delete=False) as f:
                f.write(_source_bytes(dst))
                src = f.name
        try:
            r = MF.readModelFile(src, MF.ParserSettings())
        finally:
            if src != dst:
                os.unlink(src)
        model = r[0] if isinstance(r, tuple) else r
        for x in model.meshes:
            if not x.faces:
                continue
            tex = diffuse.get(x.material)
            meshes.append(_fox_mesh(x.material, x.vertices, x.faces,
                                    _open_texture(tex) if tex else None)
                          ._replace(blend=x.material in blended))
    return meshes


def read_pes15_folder(folder):
    """PES15/17 stadium folder (bg/stadium/stNNN) -> (scene [Mesh], sky
    [Mesh] or None). The pitch (lines etc.) and sky are the sibling
    bg/pitch/stNNN and bg/sky/stNNN folders; pitch meshes are raised
    PITCH_DECAL_LIFT_M so they sit on the model's deck."""
    bg, name = os.path.dirname(os.path.dirname(folder)), os.path.basename(os.path.normpath(folder))
    scene = read_pes15_models(folder, "st_*.xml")
    pitch_dir, sky_dir = os.path.join(bg, "pitch", name), os.path.join(bg, "sky", name)
    if os.path.isdir(pitch_dir):
        # A pitch mesh with a DiffuseMap is painted content over the ground
        # ("overlay": st004's picture, st030's and st028's line strips),
        # opaque or blended. A Turf-shader pitch has none - its markings are
        # DecalMap strips ("lines", st033). Both are baked by bake_pitch_art.
        # Treating an opaque one as the whole pitch picture stretched st028's
        # 128 x 8 line texture over the pitch (cream, 30-09); dropping the
        # Turf pitch, as this did until 30-09, left st033 with no lines.
        lift = lambda m, role: m._replace(pos=[(x, y + PITCH_DECAL_LIFT_M, z) for x, y, z in m.pos],
                                          role=role)
        painted = read_pes15_models(pitch_dir, "pitch_*.xml")
        scene += [lift(m, "overlay") for m in painted if m.image is not None]
        if any(m.image is None for m in painted):
            scene += [lift(m, "lines")
                      for m in read_pes15_models(pitch_dir, "pitch_*.xml", DECAL_SAMPLER)
                      if m.image is not None
                      and all(p.name != m.name or p.image is None for p in painted)]
    try:
        sky = read_pes15_models(sky_dir, "sky_*.xml") if os.path.isdir(sky_dir) else None
    except ValueError:
        sky = None  # sky dome model not in the cpk (st028): stock sky stays
    return scene, sky


def read_fmdl(path, textures=None, gf_tools=GF_TOOLS, fmdl_lib=FMDL_LIB):
    """PES21 scene .fmdl -> [Mesh]; diffuse .ftex resolved under `textures`
    (an extracted cpk root) by the fmdl's own texture directory+filename."""
    for p in (gf_tools, fmdl_lib):
        sys.path.insert(0, p)
    try:
        import FmdlFile
        f = FmdlFile.FmdlFile()
        f.readFile(path)
    finally:
        sys.path.remove(fmdl_lib)
        sys.path.remove(gf_tools)
    meshes = []
    for m in f.meshes:
        if not m.faces:
            continue
        image, mi = None, m.materialInstance
        for role, tex in (mi.textures if mi else []):
            if role in FMDL_DIFFUSE_ROLES and textures:
                stem = os.path.splitext(tex.filename)[0] + ".ftex"
                cand = os.path.join(textures, tex.directory.lstrip("/"), stem)
                if os.path.exists(cand):
                    image = _open_texture(cand)
                    break
        meshes.append(_fox_mesh(mi.name if mi else "mesh", m.vertices, m.faces, image))
    return meshes


# --- scene preparation ----------------------------------------------------

def wind_to_stock(mesh):
    """Stock stadium triangles face the visible side: cross(b-a, c-a) along
    the vertex normal. Flip the mesh's winding if most of it disagrees."""
    agree = 0
    P, N = mesh.pos, mesh.nrm
    for a, b, c in mesh.tris:
        u = [P[b][k] - P[a][k] for k in range(3)]
        v = [P[c][k] - P[a][k] for k in range(3)]
        cr = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        n = [N[a][k] + N[b][k] + N[c][k] for k in range(3)]
        agree += 1 if sum(cr[k] * n[k] for k in range(3)) >= 0 else -1
    if agree >= 0:
        return mesh
    return mesh._replace(tris=[(a, c, b) for a, b, c in mesh.tris])


def chunk(mesh):
    """Split a mesh so no piece exceeds u16 vertex indices. -> [Mesh]."""
    out, cur, remap = [], [], {}
    def flush():
        used = sorted(remap, key=remap.get)
        out.append(mesh._replace(pos=[mesh.pos[v] for v in used],
                                 nrm=[mesh.nrm[v] for v in used],
                                 uv=[mesh.uv[v] for v in used],
                                 tris=list(cur)))
    for t in mesh.tris:
        new = [v for v in t if v not in remap]
        if len(remap) + len(new) > U16_MAX + 1:
            flush()
            cur, remap = [], {}
            new = list(t)
        for v in new:
            remap.setdefault(v, len(remap))
        cur.append(tuple(remap[v] for v in t))
    if cur:
        flush()
    return out


def subdivide(mesh, max_edge):
    """Split every triangle at the midpoint of its longest edge until no
    edge exceeds max_edge. Midpoints are shared per edge, so the mesh stays
    watertight; position/normal/UV interpolate linearly (exact on planes)."""
    pos, nrm, uv = list(mesh.pos), list(mesh.nrm), list(mesh.uv)
    mid = {}
    def midpoint(a, b):
        key = (min(a, b), max(a, b))
        if key not in mid:
            mid[key] = len(pos)
            pos.append(tuple((pos[a][k] + pos[b][k]) / 2 for k in range(3)))
            nrm.append(tuple((nrm[a][k] + nrm[b][k]) / 2 for k in range(3)))
            uv.append(tuple((uv[a][k] + uv[b][k]) / 2 for k in range(2)))
        return mid[key]
    def length(a, b):
        return math.dist(pos[a], pos[b])
    out, todo = [], list(mesh.tris)
    while todo:
        a, b, c = todo.pop()
        edges = [(length(a, b), 0), (length(b, c), 1), (length(c, a), 2)]
        longest, which = max(edges)
        if longest <= max_edge:
            out.append((a, b, c))
            continue
        a, b, c = [(a, b, c), (b, c, a), (c, a, b)][which]   # longest edge = a-b
        m = midpoint(a, b)
        todo += [(a, m, c), (m, b, c)]
    return mesh._replace(pos=pos, nrm=nrm, uv=uv, tris=out)


def _subset(mesh, tris):
    used = sorted({v for t in tris for v in t})
    remap = {v: k for k, v in enumerate(used)}
    return mesh._replace(pos=[mesh.pos[v] for v in used], nrm=[mesh.nrm[v] for v in used],
                         uv=[mesh.uv[v] for v in used],
                         tris=[tuple(remap[v] for v in t) for t in tris])


def _extent(mesh):
    return [max(p[k] for p in mesh.pos) - min(p[k] for p in mesh.pos) for k in range(3)]


def _concat(meshes):
    """Meshes sharing texture/role/blend -> one mesh (indices re-based)."""
    pos, nrm, uv, tris = [], [], [], []
    for m in meshes:
        off = len(pos)
        pos += m.pos; nrm += m.nrm; uv += m.uv
        tris += [(x + off, y + off, z + off) for x, y, z in m.tris]
    return meshes[0]._replace(pos=pos, nrm=nrm, uv=uv, tris=tris)


def _bounds(mesh):
    """-> (min xyz, max xyz)."""
    return ([min(p[k] for p in mesh.pos) for k in range(3)],
            [max(p[k] for p in mesh.pos) for k in range(3)])


# A source deck counts as "ground" (cut_ground, bake_pitch_art) when its
# triangle lies within GROUND_TOLERANCE_M of y = 0 and faces up this steeply
# (cos of the normal against +y): walls, steps and the raised stage never do.
GROUND_UP_COS = 0.9


def is_ground(pos, t):
    p = [pos[v] for v in t]
    if any(abs(q[1]) > GROUND_TOLERANCE_M for q in p):
        return False
    ux, uy, uz = (p[1][k] - p[0][k] for k in range(3))
    vx, vy, vz = (p[2][k] - p[0][k] for k in range(3))
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = sum(c * c for c in n) ** 0.5
    return length > 0 and abs(n[1]) / length >= GROUND_UP_COS


def cut_ground(mesh, rect):
    """`mesh` minus the part of its ground-level decks inside the xz `rect`
    (x0, z0, x1, z1): the stock pitch draws there instead. Triangles are
    clipped exactly against the four edges (pos/nrm/uv interpolated), so the
    deck meets the pitch without gaps or overlap; everything that is not a
    flat ground triangle is kept untouched."""
    x0, z0, x1, z1 = rect
    pos, nrm, uv = list(mesh.pos), list(mesh.nrm), list(mesh.uv)

    def lerp(i, j, t):
        pos.append(tuple(a + (b - a) * t for a, b in zip(pos[i], pos[j])))
        nrm.append(tuple(a + (b - a) * t for a, b in zip(nrm[i], nrm[j])))
        uv.append(tuple(a + (b - a) * t for a, b in zip(uv[i], uv[j])))
        return len(pos) - 1

    def split(poly, axis, edge, keep_below):
        """poly -> (part on the keep side, the rest) of the plane p[axis] = edge."""
        keep, rest = [], []
        for k, i in enumerate(poly):
            j = poly[(k + 1) % len(poly)]
            di, dj = pos[i][axis] - edge, pos[j][axis] - edge
            (keep if (di < 0) == keep_below else rest).append(i)
            if (di < 0) != (dj < 0) and di != dj:
                v = lerp(i, j, di / (di - dj))
                keep.append(v)
                rest.append(v)
        return keep, rest

    def ground(t):
        return is_ground(pos, t)

    tris = []
    for t in mesh.tris:
        if not ground(t):
            tris.append(t)
            continue
        inner, kept = list(t), []
        for axis, edge, below in ((0, x0, True), (0, x1, False), (2, z0, True), (2, z1, False)):
            outside, inner = split(inner, axis, edge, below)
            if len(outside) >= 3:
                kept.append(outside)
            if len(inner) < 3:
                break
        for poly in kept:
            tris += [(poly[0], poly[k], poly[k + 1]) for k in range(1, len(poly) - 1)]
    return mesh._replace(pos=pos, nrm=nrm, uv=uv, tris=tris)


def cut_under_pitch(mesh, rect):
    """`mesh` without the triangles lying under the deck (every vertex below
    -GROUND_TOLERANCE_M) whose xz footprint overlaps the pitch `rect`. With
    the source ground cut out (cut_ground) the stock pitch is the only floor
    there, and it does not occlude what lies beneath it, so st030's pit
    walls, 5-48 m under the deck, drew as grey grids standing on the pitch
    (30-09). Testing only triangles wholly inside `rect` kept the pit walls,
    which run past the pitch edge. Outside `rect` the source's own deck
    covers the pit, and anything reaching above the deck is kept."""
    x0, z0, x1, z1 = rect
    def under(t):
        p = [mesh.pos[v] for v in t]
        return (all(q[1] < -GROUND_TOLERANCE_M for q in p)
                and min(q[0] for q in p) < x1 and max(q[0] for q in p) > x0
                and min(q[2] for q in p) < z1 and max(q[2] for q in p) > z0)
    return mesh._replace(tris=[t for t in mesh.tris if not under(t)])


def tile(pieces, budget):
    """Split the largest piece at its triangle-centroid median along its
    longest axis until there are `budget` pieces. Stock stand blocks are
    compact (one side, one tier); from the broadcast camera the engine
    drops stand blocks whose bounds are huge (a whole-scene block holding
    the camera is not drawn), so every block must be local."""
    # one mesh per texture/role/blend first: chunk() and subdivide() cut in
    # triangle order, so their pieces each span the scene; only a spatial
    # split below yields compact blocks.
    groups = {}
    for p in pieces:
        groups.setdefault((id(p.image), p.role, p.blend), []).append(p)
    pieces = [_concat(g) for g in groups.values()]
    # ground split: stock stand blocks all sit at y >= 0, and before tiling
    # only the block whose bounds stayed above ground was drawn from the
    # broadcast camera.
    split = []
    for p in pieces:
        above = [t for t in p.tris if sum(p.pos[v][1] for v in t) >= 0]
        below = [t for t in p.tris if sum(p.pos[v][1] for v in t) < 0]
        split += [_subset(p, ts) for ts in (above, below) if ts]
    pieces = split
    # median splits along the longest axis: every piece within u16 indices,
    # then the most extended piece until the budget is used up
    while True:
        over = [p for p in pieces if len(p.pos) > U16_MAX + 1]
        splittable = [p for p in pieces if len(p.tris) > 1]
        if over:
            big = max(over, key=lambda p: len(p.pos))
        elif len(pieces) < budget and splittable:
            big = max(splittable, key=lambda p: max(_extent(p)))
        else:
            break
        ax = max(range(3), key=lambda k: _extent(big)[k])
        cen = sorted(((sum(big.pos[v][ax] for v in t), t) for t in big.tris), key=lambda c: c[0])
        half = len(cen) // 2
        pieces.remove(big)
        pieces += [_subset(big, [t for _c, t in cen[:half]]), _subset(big, [t for _c, t in cen[half:]])]
    # over budget (more texture groups than blocks, or u16 splits): merge the
    # pair that shares texture, role and blend, fits u16 and makes the most
    # compact union (merging the two SMALLEST, a2bccfd, glued far walls into
    # culled scene-wide blocks)
    while len(pieces) > budget:
        best = None
        boxes = [_bounds(p) for p in pieces]
        for i in range(len(pieces)):
            for j in range(i + 1, len(pieces)):
                a, b = pieces[i], pieces[j]
                if (a.image is b.image and a.role == b.role and a.blend == b.blend
                        and len(a.pos) + len(b.pos) <= U16_MAX + 1):
                    lo = [min(x, y) for x, y in zip(boxes[i][0], boxes[j][0])]
                    hi = [max(x, y) for x, y in zip(boxes[i][1], boxes[j][1])]
                    span = max(h - l for l, h in zip(lo, hi))
                    if best is None or span < best[0]:
                        best = (span, i, j)
        if best is None:
            break
        _span, i, j = best
        merged = _concat([pieces[i], pieces[j]])
        pieces = [q for k, q in enumerate(pieces) if k not in (i, j)] + [merged]
    return pieces


# --- KTMDL editing --------------------------------------------------------

def _decl(block, packet):
    """-> (stride, [(semantic, format, offset)]) of a packet's vertex stream."""
    p = ktmdl_write._packet_base(block, packet)
    n_vs = ktmdl_write._u32(block, p + ktmdl_write.P_STREAM_COUNT)
    vs_off = ktmdl_write._i32(block, p + ktmdl_write.P_STREAM_OFF)
    for (r, _d, count, st, typ) in ktmdl_write._stream_recs(block, p + vs_off, n_vs):
        if typ == ktmdl_write.TYPE_VERTEX and count:
            decl = ktmdl_write._u32(block, r + 0x0C)
            elems = [(block[r + decl + e * 4 + 3], block[r + decl + e * 4 + 2],
                      block[r + decl + e * 4 + 1]) for e in range(block[r + 0x0A])]
            return st, elems
    return None, []


def pack_vertices(elems, stride, pos, nrm, uv):
    """KTMDL-frame verts -> packet bytes. TEXCOORD0 = diffuse UV; every
    other element (TEXCOORD1 lightmap UV included) is zero: the lightmap
    slot is bound to an all-white texture."""
    uv_off = next((o for s, f, o in elems if s == SEM_TEXCOORD and f == FMT_FLOAT2), None)
    out = bytearray(stride * len(pos))
    for k, ((x, y, z), n, t) in enumerate(zip(pos, nrm, uv)):
        base = k * stride
        for sem, fmt, off in elems:
            if sem == SEM_POSITION and fmt == FMT_FLOAT3:
                struct.pack_into("<3f", out, base + off, x, y, z)
            elif sem == SEM_NORMAL and fmt == FMT_FLOAT3:
                struct.pack_into("<3f", out, base + off, *n)
        if uv_off is not None:
            struct.pack_into("<2f", out, base + uv_off, *t)
    return bytes(out)


def _packet_textures(K, block):
    """-> {packet index: [texture debug names]} (stock blocks keep their
    authoring names, e.g. 'p_base_s.psd', next to the numeric ids)."""
    m = K.parse_bytes(block)
    names = m["names"].get("textureNames", [])
    return {pk["index"]: [names[r["id"]] for r in pk["activeTextureRefs"] if r["id"] < len(names)]
            for pk in m["packets"]}


def is_pitch(names):
    return any(n.startswith(PITCH_TEXTURE_PREFIX) for n in names)


EMPTY_BLOCK_RADIUS_M = 1.0   # stock board packets carry radius 0 after
# empty_block, and the stadium loader culls on it: a zero-radius packet
# inside an INSTALLED 400-block geometry entry crashes the match load
# (dt07 2664, 01-10). Kept non-zero so the entry still loads.

def empty_block(K, block, keep_pitch=True):
    """Every packet with a vertex stream -> one degenerate triangle at the
    origin (keeps the block's tables and the engine's packet counts);
    stock pitch packets survive unless keep_pitch is False."""
    want = []
    for i, names in _packet_textures(K, block).items():
        stride, _ = _decl(block, i)
        if stride and not (keep_pitch and is_pitch(names)):
            want.append({"packet": i, "vertices": bytes(3 * stride), "indices": [0, 1, 2]})
    out = ktmdl_write.build(block, want) if want else block
    if out is not block:
        out = _raise_radii(out, EMPTY_BLOCK_RADIUS_M)
    return out


def _raise_radii(blob, radius):
    """Every packet record's radius field -> `radius` (the build writes the
    collapsed packet's own AABB, i.e. 0)."""
    out = bytearray(blob)
    n = ktmdl_write._u32(out, ktmdl_write.H_PACKET_COUNT)
    for i in range(n):
        base = ktmdl_write._packet_base(out, i)
        struct.pack_into("<f", out, base + ktmdl_write.P_RADIUS, radius)
    return bytes(out)


TEMPLATE_FLAGS = 0                 # packet flags of the plain lit stand material
LIGHTMAP_TEXTURE_PREFIX = "e_lmap"  # stock lightmap texture debug names (e_lmap_03.psd ...)

# Texture table rows the diffuse / lightmap must sit in, and the texParam
# each must carry, for repoint_textures to be able to reach them: it writes
# textureNameIds by the ref id the packet names.
TEXROW_DIFFUSE, TEXROW_LIGHTMAP = 0, 1
TEXPARAM_DIFFUSE, TEXPARAM_LIGHTMAP = 1, 2


def template_packet(K, block):
    """-> packet index of a stock diffuse+lightmap packet in `block`, or
    None. Blocks holding pitch packets are never templates (they are kept)."""
    m = K.parse_bytes(block)
    texnames = _packet_textures(K, block)
    if any(is_pitch(n) for n in texnames.values()):
        return None
    for pk in m["packets"]:
        refs = pk["activeTextureRefs"]
        names = texnames.get(pk["index"], [])
        # only the plain lit stand material: flags 0 and a real lightmap as
        # the second texture. Staff/bench packets (flags 704, e_cmap cube
        # map) and flags-256 variants have the same stride and texcoords but
        # another shader - Karasuno pieces placed on them drew white or
        # half as bright as the same texture elsewhere (29-09).
        # refs[0] must be the diffuse row and refs[1] the lightmap row:
        # repoint_textures writes the two textures into the rows the refs
        # name, so a template pointing anywhere else - or, on the staff
        # blocks, at an all-zero table - samples rows nobody wrote and
        # draws black (Karasuno's walls, 29-09).
        if (pk["vertexDescriptor"].get("stride") == TEMPLATE_STRIDE and len(refs) == 2
                and refs[0]["id"] == TEXROW_DIFFUSE and refs[1]["id"] == TEXROW_LIGHTMAP
                and refs[0]["texParam"] == TEXPARAM_DIFFUSE
                and refs[1]["texParam"] == TEXPARAM_LIGHTMAP
                and refs[0]["texCoordIndex"] == TEXCOORD_DIFFUSE
                and refs[1]["texCoordIndex"] == TEXCOORD_LIGHTMAP
                and pk["flags"] == TEMPLATE_FLAGS
                and len(names) == 2 and names[1].startswith(LIGHTMAP_TEXTURE_PREFIX)):
            return pk["index"]
    return None


def repoint_textures(K, block, packet, diffuse_id, lightmap_id):
    """Rewrite the block's textureNameIds rows the packet's two refs use."""
    out = bytearray(block)
    m = K.parse_bytes(block)
    ids = m["textureNameIds"]
    refs = m["packets"][packet]["activeTextureRefs"]
    for ref, tid in ((refs[0], diffuse_id), (refs[1], lightmap_id)):
        row = ids[ref["id"]]
        struct.pack_into("<I", out, row["offset"] + 8, tid)  # rawHex: hi u64, lo u32
    return bytes(out)


# --- textures -------------------------------------------------------------

TexSlot = namedtuple("TexSlot", "entry block id w h four mips")


def stand_texture_slots(slot):
    out = []
    for e in range(slot.stand[0], slot.stand[1] + 1):
        _tag, body = read_entry(e)
        for bi, blk in enumerate(split_bin(body)):
            if blk[:4] != b"WE00":
                continue
            d = blk.find(b"DDS ")
            h, w = struct.unpack_from("<II", blk, d + 12)
            mips = max(struct.unpack_from("<I", blk, d + 28)[0], 1)
            out.append(TexSlot(e, bi, struct.unpack_from("<H", blk, WE00_ID_OFF)[0],
                               w, h, blk[d + 84:d + 88], mips))
    return out


def encode_like(image, block):
    """PIL image -> WE00 block bytes identical in size/format to `block`:
    stock WE00+DDS headers kept, pixel payload re-encoded by magick."""
    d = block.find(b"DDS ")
    h, w = struct.unpack_from("<II", block, d + 12)
    mips = max(struct.unpack_from("<I", block, d + 28)[0], 1)
    four = block[d + 84:d + 88]
    comp = {b"DXT1": "dxt1", b"DXT3": "dxt3", b"DXT5": "dxt5"}.get(four)
    if comp is None:
        raise ValueError("stand texture format %r not supported" % four)
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.dds")
        image.convert("RGBA").resize((w, h)).save(src)
        subprocess.run(["magick", src, "-define", "dds:compression=" + comp,
                        "-define", "dds:mipmaps=%d" % (mips - 1), dst],
                       check=True, capture_output=True)
        dds = open(dst, "rb").read()
    payload = dds[DDS_HEADER:]
    if len(payload) != len(block) - d - DDS_HEADER:
        raise ValueError("re-encoded %dx%d %s: %d bytes, stock slot %d"
                         % (w, h, four, len(payload), len(block) - d - DDS_HEADER))
    return block[:d + DDS_HEADER] + payload


# Textures are re-encoded at the source's own size, rounded down to a power
# of two and capped: the stock stand blocks are as small as 32 px, which made
# st004/st033 unreadably blurry. Larger dt07 entries load: st004's geometry
# entry is 2.85 MB decompressed against 2.2 MB stock, seen in game 28-09.
MAX_TEXTURE_PX = 2048
WE00_SIZE_OFF = 8        # WE00 header u32: DDS byte length (pes12_ball.py)
DDS_RESERVED = slice(32, 76)  # DWReserved1: magick writes a tag, stock is zeros


def _pow2_floor(v):
    return 1 << max(0, int(v).bit_length() - 1)


def encode_block(image, block):
    """PIL image -> a new WE00 block with `block`'s header (texture id kept):
    DXT5 when the image has transparency, else DXT1, at the image's own
    power-of-two size capped at MAX_TEXTURE_PX, full mip chain."""
    from PIL import Image
    d = block.find(b"DDS ")
    img = image.convert("RGBA")
    w = min(_pow2_floor(img.size[0]), MAX_TEXTURE_PX)
    h = min(_pow2_floor(img.size[1]), MAX_TEXTURE_PX)
    comp = "dxt5" if img.getchannel("A").getextrema()[0] < 255 else "dxt1"
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.dds")
        img.resize((w, h), Image.LANCZOS).save(src)
        subprocess.run(["magick", src, "-define", "dds:compression=" + comp, dst],
                       check=True, capture_output=True)
        dds = bytearray(open(dst, "rb").read())
    dds[DDS_RESERVED] = bytes(DDS_RESERVED.stop - DDS_RESERVED.start)
    we = bytearray(block[:d])
    struct.pack_into("<I", we, WE00_SIZE_OFF, len(dds))
    return bytes(we) + bytes(dds)


def replace_blocks(out_root, entry, repl, img=DT07):
    """Entry with blocks {index: new bytes} swapped in (sizes may change)."""
    tag, body = read_entry(entry, img, out_root)
    blocks = split_bin(body)
    for bi, b in repl.items():
        blocks[bi] = b
    write_entry(out_root, entry, tag, join_bin(blocks), img)


def _lonlat(d):
    import numpy as np
    d = d / np.linalg.norm(d, axis=-1, keepdims=True)
    return np.arctan2(d[..., 0], d[..., 2]), np.arcsin(np.clip(d[..., 1], -1, 1))


def _raster(tris2d, values, w, h, out):
    """Scanline-free triangle fill: for each triangle (pixel-space 2D verts)
    write barycentric-interpolated `values` (per-vertex arrays) into out."""
    import numpy as np
    for (p, vals) in zip(tris2d, values):
        x0, y0 = np.floor(p.min(0)).astype(int)
        x1, y1 = np.ceil(p.max(0)).astype(int)
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w - 1), min(y1, h - 1)
        if x1 < x0 or y1 < y0:
            continue
        ys, xs = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        q = np.stack([xs + 0.5, ys + 0.5], -1)
        (ax, ay), (bx, by), (cx, cy) = p
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-12:
            continue
        l1 = ((by - cy) * (q[..., 0] - cx) + (cx - bx) * (q[..., 1] - cy)) / den
        l2 = ((cy - ay) * (q[..., 0] - cx) + (ax - cx) * (q[..., 1] - cy)) / den
        l3 = 1 - l1 - l2
        inside = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        v = l1[..., None] * vals[0] + l2[..., None] * vals[1] + l3[..., None] * vals[2]
        sub = out[y0:y1 + 1, x0:x1 + 1]
        sub[inside] = v[inside]


def sky_lonlat_map(sky_meshes):
    """Source sky dome(s) -> RGB lon/lat map as seen from the origin (any
    dome that is star-shaped around the pitch centre)."""
    import numpy as np
    W, H = SKY_MAP_W, SKY_MAP_H
    out = np.zeros((H, W, 3), np.float32)
    for m in sky_meshes:
        if m.image is None:
            continue
        img = np.asarray(m.image.convert("RGB"), np.float32)
        ih, iw = img.shape[:2]
        P, U = np.array(m.pos, np.float64), np.array(m.uv, np.float64)
        lon, lat = _lonlat(P)
        tris2d, vals = [], []
        for t in m.tris:
            lo = lon[list(t)].copy()
            if lo.max() - lo.min() > math.pi:      # straddles the seam
                lo[lo < 0] += 2 * math.pi
            xy = np.stack([(lo + math.pi) / (2 * math.pi) * W,
                           (math.pi / 2 - lat[list(t)]) / math.pi * H], -1)
            uv = U[list(t)]   # UV interpolated per pixel, texture sampled after
            tris2d.append(xy)
            vals.append(uv)
            if lo.max() > math.pi:                  # the wrapped copy
                tris2d.append(xy - [W, 0])
                vals.append(uv)
        uvmap = np.full((H, W, 2), np.nan, np.float32)
        _raster(tris2d, vals, W, H, uvmap)
        ok = ~np.isnan(uvmap[..., 0])
        u, v = uvmap[..., 0][ok] % 1, uvmap[..., 1][ok] % 1
        out[ok] = img[np.clip((v * ih).astype(int), 0, ih - 1),
                      np.clip((u * iw).astype(int), 0, iw - 1)]
    return out


def render_sky_strip(K, lonlat, w, h):
    """Stock sky dome (entry 1 block 0) UV grid w x h -> RGB bytes: each
    texel looks up the source sky in the direction the dome shows it."""
    import numpy as np
    _tag, body = read_entry(SKY_DOME_ENTRY)
    pk = K.parse_bytes(split_bin(body)[SKY_DOME_BLOCK])["packets"][0]
    P = np.array([v["POSITION"] for v in pk["vertices"]], np.float64)
    U = np.array([v["TEXCOORD0"] for v in pk["vertices"]], np.float64)
    pos = np.full((h, w, 3), np.nan, np.float64)
    _raster([U[list(t)] * [w, h] for t in pk["triangles"]],
            [P[list(t)] for t in pk["triangles"]], w, h, pos)
    ok = ~np.isnan(pos[..., 0])
    lon, lat = _lonlat(pos[ok])
    H, W = lonlat.shape[:2]
    # bilinear: nearest lookup put the map's pixel grid on screen as blocks
    fx = ((lon + math.pi) / (2 * math.pi) * W - 0.5) % W
    fy = np.clip((math.pi / 2 - lat) / math.pi * H - 0.5, 0, H - 1)
    x0, y0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
    x1, y1 = (x0 + 1) % W, np.minimum(y0 + 1, H - 1)
    tx, ty = (fx - x0)[:, None], (fy - y0)[:, None]
    top = lonlat[y0, x0] * (1 - tx) + lonlat[y0, x1] * tx
    bot = lonlat[y1, x0] * (1 - tx) + lonlat[y1, x1] * tx
    rgb = np.zeros((h, w, 3), np.float32)
    rgb[ok] = top * (1 - ty) + bot * ty
    return np.clip(rgb, 0, 255).astype(np.uint8).tobytes()


def install_sky(K, sky_meshes, slot, out_root):
    """Every variant's sky strip (raw RGB WE00, id SKY_TEXTURE_ID) redrawn
    from the source sky at SKY_STRIP_SCALE times its stock size."""
    lonlat = sky_lonlat_map(sky_meshes)
    for e in slot.skies:
        tag, body = read_entry(e)
        blocks = split_bin(body)
        for bi, blk in enumerate(blocks):
            if blk[:4] != b"WE00" or struct.unpack_from("<H", blk, WE00_ID_OFF)[0] != SKY_TEXTURE_ID:
                continue
            if blk[7] != RAW_RGB_BPP:
                raise ValueError("dt07 #%d: sky is not raw %d-bit" % (e, RAW_RGB_BPP))
            w, h = (SKY_STRIP_SCALE * v for v in struct.unpack_from("<HH", blk, 8))
            head = bytearray(blk[:RAW_HEADER])
            struct.pack_into("<HH", head, 8, w, h)
            blocks[bi] = bytes(head) + render_sky_strip(K, lonlat, w, h)
        write_entry(out_root, e, tag, join_bin(blocks))


# --- per-slot and shared assets -------------------------------------------

SLOT_ASSETS = ("crowd", "pitch", "props")
ASSETS = SLOT_ASSETS + tuple(SHARED_PARTS)


def _crowd_hidden(crowd):
    """Stock no-crowd layout of a crowd block (see CROWD_BLOCK)."""
    table = struct.unpack_from("<I", crowd)[0]
    n = (table - 4) // 4 + 1
    stubs = [table + CROWD_SECTION_HEADER * (k + 1) for k in range(n - 1)]
    return struct.pack("<%dI" % n, table, *stubs) + bytes(CROWD_SECTION_HEADER * n)


def _set_crowd(K, slot, root, on):
    e = slot.geometry + SLOT_STRIDE
    tag, body = read_entry(e, root=root)
    blocks = split_bin(body)
    blocks[CROWD_BLOCK] = (split_bin(read_entry(e)[1])[CROWD_BLOCK] if on
                           else _crowd_hidden(blocks[CROWD_BLOCK]))
    write_entry(root, e, tag, join_bin(blocks))


def _set_pitch(K, slot, root, on):
    """Stock pitch base packets (per slot) back to stock, or emptied."""
    tag, body = read_entry(slot.geometry, root=root)
    blocks, stock = split_bin(body), split_bin(read_entry(slot.geometry)[1])
    for bi, b in enumerate(blocks):
        if b[:5] != b"KTMDL":
            continue
        shown, hidden = [], []
        for i, names in _packet_textures(K, stock[bi]).items():
            if is_pitch(names):
                v, idx, stride = ktmdl_write.packet_mesh(stock[bi], i)
                shown.append({"packet": i, "vertices": v, "indices": idx})
                hidden.append({"packet": i, "vertices": bytes(3 * stride), "indices": [0, 1, 2]})
        if not shown:
            continue
        if not on:
            blocks[bi] = ktmdl_write.build(b, hidden)
        elif b == ktmdl_write.build(stock[bi], hidden):
            # untouched since hidden: the stock block itself (build()
            # recomputes packet bounds, off from stock in the last float bits)
            blocks[bi] = stock[bi]
        else:
            blocks[bi] = ktmdl_write.build(b, shown)
    write_entry(root, slot.geometry, tag, join_bin(blocks))


def _set_props(K, slot, root, on):
    for e in slot.props:
        tag, body = read_entry(e)
        write_entry(root, e, tag, body if on else join_bin(
            [empty_block(K, b) if b[:5] == b"KTMDL" else b for b in split_bin(body)]))


def _shared_entries(K, kind):
    """dt07 entries made only of `kind` parts (SHARED_PARTS): every textured
    KTMDL block uses those stock textures. Stadium geometry entries also
    carry a few staff/board packets and never match."""
    prefixes = SHARED_PARTS[kind]
    for e in range(len(afs.entries(DT07))):
        try:
            tag, body = read_entry(e)
            blocks = split_bin(body)
        except (ValueError, zlib.error, struct.error):
            continue
        named = [n for n in (K.parse_bytes(b)["names"].get("textureNames", [])
                             for b in blocks if b[:5] == b"KTMDL") if n]
        if named and all(any(t.startswith(prefixes) for t in n) for n in named):
            yield e, tag, body, blocks


def set_asset(asset, slots, root, on, log=print):
    """Show (on) or hide one asset. Slot assets act on each slot; shared
    assets (boards, staff) are one set of entries for every stadium."""
    K = _ktmdl_reader()
    if asset in SHARED_PARTS:
        done = []
        for e, tag, body, blocks in _shared_entries(K, asset):
            write_entry(root, e, tag, body if on else join_bin(
                [empty_block(K, b) if b[:5] == b"KTMDL" else b for b in blocks]))
            done.append(e)
        log("%s %s in every stadium (dt07 %s)" % (asset, "shown" if on else "hidden", done))
        return
    fn = {"crowd": _set_crowd, "pitch": _set_pitch, "props": _set_props}[asset]
    for s in slots:
        fn(K, s, root, on)
        log("slot %d: %s %s" % (s.number, asset, "shown" if on else "hidden"))


# --- pitch art ------------------------------------------------------------

# The per-slot pitch art (texture PITCH_ART_ID, 2048x1024 DXT1, one entry per
# variant: Allianz 208..212) is the base packet's TEXCOORD1 layer: mowing
# bands, lines and baked roof shadow, spanning the pitch once; TEXCOORD0
# tiles the shared grey grain (11205) over it, and the shared grass overlay
# (entry 53) draws its detail on top.
PITCH_ART_ID = 11204
# GF convention (GameplayFootball src/onthepitch/proceduralpitch.cpp,
# gametypes.hpp pitchFullHalfW/H): pitch art images span +/-60 m by +/-40 m,
# column -> x, row -> lateral, row 0 at -40 m. GF's lateral axis is Fox -z,
# i.e. KTMDL +z (the same basis as fox_to_ktmdl).
PITCH_ART_HALF_W_M, PITCH_ART_HALF_H_M = 60.0, 40.0
GF_OVERLAY, GF_TURF = "pitch_overlay.png", "turf.png"
# Baked top-down pitch art: output size, and the supersampling factor. The
# output is the stock pitch art's 2048 x 1024, so set_pitch_art composites it
# without resampling. st033's markings are 0.10 m strips - 1.7 px at output
# resolution - so a single sample per pixel dropped about half of every line
# (dashed, faint lines, 30-09); rasterising SUPERSAMPLE times finer and box-
# filtering down gives each strip its true coverage.
PITCH_DECAL_W_PX, PITCH_DECAL_H_PX = 2048, 1024
PITCH_DECAL_SUPERSAMPLE = 4
# Pitch line paint: the white of st033's line.dds marks, fully opaque.
PITCH_LINE_RGBA = (1.0, 1.0, 1.0, 1.0)


def bake_pitch_art(ground, overlays, lines):
    """Everything flat over the pitch, seen from above -> one RGBA over the
    PITCH_ART_HALF_* extent (column -> x, row -> z, row 0 at
    -PITCH_ART_HALF_H_M), or None when there is nothing to draw there.
    Painted bottom-up, each through its own UVs: `ground` (the source's
    ground-level decks, only their flat ground triangles), then `overlays`
    (blended pitch pictures: st004's pixel-art markings, st030's lines),
    alpha-composited. `lines` are Turf-shader strips (st033): the geometry
    IS the line work - its DecalMap is sampled by a Turf shader whose UVs
    sweep across gapped columns of line.dds, which PES2012 cannot reproduce
    (looked up per texel the halfway line came out dashed, 30-09, where PES15
    draws it solid) - so each strip is painted solid PITCH_LINE_RGBA."""
    import numpy as np
    from PIL import Image
    layers = [(m, "ground") for m in ground] + [(m, "overlay") for m in overlays] \
        + [(m, "line") for m in lines]
    layers = [(m, k) for m, k in layers if m.image is not None and m.tris]
    if not any(k != "ground" for _m, k in layers):
        return None
    ss = PITCH_DECAL_SUPERSAMPLE
    W, H = PITCH_DECAL_W_PX * ss, PITCH_DECAL_H_PX * ss
    kx, ky = W / (2 * PITCH_ART_HALF_W_M), H / (2 * PITCH_ART_HALF_H_M)
    out = np.zeros((H, W, 4), np.float32)
    for m, kind in layers:
        P = np.array(m.pos, np.float64)
        U = np.array(m.uv, np.float64)
        tex = None if kind == "line" else np.asarray(m.image.convert("RGBA"), np.float32) / 255.0
        sx = (P[:, 0] + PITCH_ART_HALF_W_M) * kx
        sy = (P[:, 2] + PITCH_ART_HALF_H_M) * ky
        for t in m.tris:
            if kind == "ground" and not is_ground(m.pos, t):
                continue
            a, b, c = t
            xa, ya, xb, yb, xc, yc = sx[a], sy[a], sx[b], sy[b], sx[c], sy[c]
            area = (xb - xa) * (yc - ya) - (yb - ya) * (xc - xa)
            if area == 0:
                continue
            x0, x1 = max(0, int(min(xa, xb, xc))), min(W - 1, int(max(xa, xb, xc)) + 1)
            y0, y1 = max(0, int(min(ya, yb, yc))), min(H - 1, int(max(ya, yb, yc)) + 1)
            if x1 < x0 or y1 < y0:
                continue
            py, px = np.mgrid[y0:y1 + 1, x0:x1 + 1] + 0.5
            w0 = ((xc - xb) * (py - yb) - (yc - yb) * (px - xb)) / area
            w1 = ((xa - xc) * (py - yc) - (ya - yc) * (px - xc)) / area
            w2 = 1.0 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            region = out[y0:y1 + 1, x0:x1 + 1]
            if tex is None:
                region[inside] = PITCH_LINE_RGBA
                continue
            th, tw = tex.shape[:2]
            u = (w0 * U[a, 0] + w1 * U[b, 0] + w2 * U[c, 0])[inside] % 1.0
            v = (w0 * U[a, 1] + w1 * U[b, 1] + w2 * U[c, 1])[inside] % 1.0
            texel = tex[(v * (th - 1)).astype(int), (u * (tw - 1)).astype(int)]
            if kind == "ground":
                texel[:, 3] = 1.0          # decks draw opaque, as in game
            alpha = texel[:, 3:]
            region[inside] = np.concatenate(
                [texel[:, :3] * alpha + region[inside][:, :3] * (1 - alpha),
                 alpha + region[inside][:, 3:] * (1 - alpha)], 1)
    # box filter: every output pixel averages its ss x ss samples, with the
    # colour premultiplied so empty samples dim a mark only by coverage
    rgb, alpha = out[..., :3] * out[..., 3:], out[..., 3:]
    def shrink(a):
        return a.reshape(PITCH_DECAL_H_PX, ss, PITCH_DECAL_W_PX, ss, -1).mean((1, 3))
    alpha_s = shrink(alpha)
    rgb_s = np.where(alpha_s > 0, shrink(rgb) / np.maximum(alpha_s, 1e-6), 0)
    return Image.fromarray((np.concatenate([rgb_s, alpha_s], -1) * 255).astype(np.uint8), "RGBA")


def stock_pitch_rect(K, slot):
    """xz bounds (x0, z0, x1, z1) of the slot's stock pitch base packets."""
    _tag, body = read_entry(slot.geometry)
    xs, zs = [], []
    for b in split_bin(body):
        if b[:5] != b"KTMDL":
            continue
        m = K.parse_bytes(b)
        for i, names in _packet_textures(K, b).items():
            if is_pitch(names):
                for v in m["packets"][i]["vertices"]:
                    xs.append(v["POSITION"][0])
                    zs.append(v["POSITION"][2])
    if not xs:
        raise ValueError("slot %d: no stock pitch base packet" % slot.number)
    return min(xs), min(zs), max(xs), max(zs)


def _pitch_uv_map(K, slot):
    """Stock base packet: (x = a*u + b, z = c*v + d) fitted over its verts."""
    _tag, body = read_entry(slot.geometry)
    for b in split_bin(body):
        if b[:5] != b"KTMDL":
            continue
        m = K.parse_bytes(b)
        for i, names in _packet_textures(K, b).items():
            if is_pitch(names):
                V = m["packets"][i]["vertices"]
                fit = []
                for ax, uvk in ((0, 0), (2, 1)):
                    us = [v["TEXCOORD1"][uvk] for v in V]
                    xs = [v["POSITION"][ax] for v in V]
                    n, mu, mx = len(us), sum(us) / len(us), sum(xs) / len(xs)
                    a = (sum((u - mu) * (x - mx) for u, x in zip(us, xs))
                         / sum((u - mu) ** 2 for u in us))
                    fit += [a, mx - a * mu]
                return fit
    raise ValueError("slot %d: no stock pitch base packet" % slot.number)


def _pitch_source(src):
    """-> (RGBA art over the PITCH_ART_HALF_* extent, base colour or None).
    A GF stadium folder gives its pitch_overlay.png over its turf.png's
    average colour (how GF paints it, pitchturf.hpp); a plain image is
    composited over the slot's own stock art."""
    from PIL import Image
    if isinstance(src, Image.Image):
        return src.convert("RGBA"), None
    if os.path.isdir(src):
        turf = Image.open(os.path.join(src, GF_TURF)).convert("RGB")
        base = tuple(int(c) for c in turf.resize((1, 1), Image.BOX).getpixel((0, 0)))
        return Image.open(os.path.join(src, GF_OVERLAY)).convert("RGBA"), base
    return Image.open(src).convert("RGBA"), None


def set_pitch_art(slots, root, src, log=print, base=None):
    """Every variant's pitch art redrawn from `src` (path or RGBA image);
    src 'stock' restores."""
    from PIL import Image
    K = _ktmdl_reader()
    art, src_base = (_pitch_source(src) if not (isinstance(src, str) and src == "stock")
                     else (None, None))
    base = base or src_base
    for s in slots:
        a, b, c, d = _pitch_uv_map(K, s)
        for e in s.pitches:
            tag, body = read_entry(e)
            if art is not None:
                body = bytearray(body)
                for off, blk in zip(block_offsets(body), split_bin(bytes(body))):
                    if blk[:4] != b"WE00" or struct.unpack_from("<H", blk, WE00_ID_OFF)[0] != PITCH_ART_ID:
                        continue
                    dd = blk.find(b"DDS ")
                    h, w = struct.unpack_from("<II", blk, dd + 12)
                    canvas = (Image.new("RGBA", (w, h), base + (255,)) if base
                              else Image.open(io.BytesIO(blk[dd:])).convert("RGBA"))
                    sw, sh = art.size
                    # output pixel (i, j) -> u = i/w, v = j/h -> metres -> art pixel
                    kx, ky = sw / (2 * PITCH_ART_HALF_W_M), sh / (2 * PITCH_ART_HALF_H_M)
                    over = art.transform((w, h), Image.AFFINE,
                                         (a / w * kx, 0, b * kx + sw / 2,
                                          0, c / h * ky, d * ky + sh / 2),
                                         resample=Image.BILINEAR)
                    canvas.alpha_composite(over)
                    body[off:off + len(blk)] = encode_like(canvas, blk)
                body = bytes(body)
            write_entry(root, e, tag, body)
        shared = [o.number for o in read_slots() if o.number != s.number and set(o.pitches) & set(s.pitches)]
        log("slot %d: pitch art %s in dt07 %s%s" % (
            s.number, src, s.pitches, " (also used by slots %s)" % shared if shared else ""))


# --- names (pes2012.exe) --------------------------------------------------

# One 0xF0-byte record per named stadium (28 in v1.06): u8 id, 0x13 bytes,
# Japanese name (0x61), English name (0x63, the one every European language
# shows), 6 pointers (region/country/city, Japanese + English). Found by
# the pointer block: 6 u32 VAs into the image, each at a NUL-terminated string.
NAME_JP_LEN, NAME_EN_LEN = 0x61, 0x63
NAME_POINTERS = 6
NAME_ID_OFF = 4 * NAME_POINTERS      # id byte right after the pointer block
NAME_STRING_MAX = 64
EXE_BACKUP = EXE + ".stadium-names.bak"


def _pe_sections(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec, opt = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    base = struct.unpack_from("<I", data, pe + 24 + 28)[0]
    secs = []
    for i in range(nsec):
        s = pe + 24 + opt + 40 * i
        vsize, va, rsize, raw = struct.unpack_from("<4I", data, s + 8)
        secs.append((base + va, base + va + max(vsize, rsize), raw - va - base))
    return secs


NAME_RECORD = 0xF0
def _padded_text(data, off, length):
    """Field = UTF-8 text, then only NULs to the end of the field."""
    end = data.find(b"\0", off, off + length)
    if end <= off or any(data[end:off + length]):
        return None
    try:
        return data[off:end].decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_names(exe=EXE):
    """-> {stadium id: English name offset in the exe file}. A record counts
    when both name fields are text + NUL padding, every pointer reaches a
    UTF-8 string, and a neighbour record sits NAME_RECORD away (the table)."""
    import numpy as np
    data = open(exe, "rb").read()
    secs = _pe_sections(data)
    lo, hi = min(s[0] for s in secs), max(s[1] for s in secs)
    words = np.frombuffer(data[:len(data) // 4 * 4], "<u4")
    ok = (words >= lo) & (words < hi)
    run = np.convolve(ok.astype(np.int8), np.ones(NAME_POINTERS, np.int8), "valid") == NAME_POINTERS
    found = {}
    for w in np.nonzero(run)[0]:
        o = int(w) * 4
        en = o - NAME_EN_LEN
        if en < NAME_JP_LEN or not _padded_text(data, en, NAME_EN_LEN) \
                or not _padded_text(data, en - NAME_JP_LEN, NAME_JP_LEN):
            continue
        good = True
        for p in struct.unpack_from("<%dI" % NAME_POINTERS, data, o):
            f = next((p + d for a, b, d in secs if a <= p < b), None)
            end = data.find(b"\0", f, f + NAME_STRING_MAX) if f is not None else -1
            try:
                good = f is not None and end > f and bool(data[f:end].decode("utf-8"))
            except UnicodeDecodeError:
                good = False
            if not good:
                break
        if good:
            found[en] = data[o + NAME_ID_OFF]
    return {i: en for en, i in found.items()
            if en - NAME_RECORD in found or en + NAME_RECORD in found}


def set_name(ids, name, exe=EXE, log=print):
    """English name of each stadium name id (see `names`), patched into
    pes2012.exe (the first write keeps EXE_BACKUP); name 'stock' restores."""
    import shutil
    if subprocess.run(["pgrep", "-f", "pes2012.exe"], capture_output=True).stdout:
        raise RuntimeError("pes2012.exe is running; close the game before patching names")
    if not os.path.exists(EXE_BACKUP):
        shutil.copy2(exe, EXE_BACKUP)
    names = read_names(EXE_BACKUP)
    data, stock = bytearray(open(exe, "rb").read()), open(EXE_BACKUP, "rb").read()
    for i in ids:
        if i not in names:
            raise ValueError("no stadium name id %d (see `names`)" % i)
        o = names[i]
        if name == "stock":
            data[o:o + NAME_EN_LEN] = stock[o:o + NAME_EN_LEN]
        else:
            raw = name.encode("utf-8")
            if len(raw) >= NAME_EN_LEN:
                raise ValueError("name longer than %d bytes" % (NAME_EN_LEN - 1))
            data[o:o + NAME_EN_LEN] = raw.ljust(NAME_EN_LEN, b"\0")
        log("id %d name %r -> %r" % (i, stock[o:stock.index(b"\0", o)].decode(),
                                     data[o:data.index(b"\0", o)].decode()))
    open(exe, "wb").write(bytes(data))


# --- thumbnails (dt06) ----------------------------------------------------

DT06 = os.path.join(GAME, "img", "dt06.img")
# Stadium-select previews: dt06 #1133 (match settings, with sta_random) and
# #194 (edit mode, with sta_new), one WE00 512x256 DXT1 block per stadium.
# The block is picked by POSITION, not by its sta_x name: the stadium with
# the k-th smallest name id (ids in the menu) shows block k of #1133 - read
# off a labelled test set 28-09 (ID 2 -> block 0 ... ID 30 -> 18 ... ID 61
# -> 29). The menu lists 31 entries (30 stadiums + Random): every name id
# except these two.
THUMB_ENTRIES = (1133, 194)
MATCH_THUMBS = 1133
NOT_IN_MENU = (1, 70)    # CLUB HOUSE (training ground), FUSSBALL ARENA MÜNCHEN (Allianz's alias)
NAME_SLOT = 16


def split_named(body):
    n = struct.unpack_from("<I", body)[0]
    rows = [struct.unpack_from("<III", body, 8 + 12 * k) for k in range(n)]
    return ([body[o:o + s] for o, s, _n in rows],
            [body[no:body.index(b"\0", no)].decode() for _o, _s, no in rows])


def join_named(blocks, names):
    """Inverse of split_named (byte-exact on dt06 #194/#1133): header
    8+12n padded to 16, blocks 16-aligned, then 16-byte name slots."""
    align = lambda v: (v + 15) & ~15  # noqa: E731
    hs = align(8 + 12 * len(blocks))
    pos, offs = hs, []
    for b in blocks:
        offs.append(pos)
        pos += align(len(b))
    out = bytearray(struct.pack("<II", len(blocks), 8))
    for k, b in enumerate(blocks):
        out += struct.pack("<III", offs[k], len(b), pos + NAME_SLOT * k)
    out += b"\0" * (hs - len(out))
    for b in blocks:
        out += b + b"\0" * (align(len(b)) - len(b))
    for nm in names:
        out += nm.encode().ljust(NAME_SLOT, b"\0")
    return bytes(out)


def set_thumbnail(ids, root, src, log=print):
    """Preview of each stadium name id replaced; src 'stock' restores. #1133
    by menu position (see THUMB_ENTRIES); #194 lists fewer stadiums, so its
    block is matched by the sta_x name the #1133 block carries."""
    from PIL import Image
    menu = sorted(i for i in read_names(EXE_BACKUP) if i not in NOT_IN_MENU)
    _t, stock_match = read_entry(MATCH_THUMBS, DT06)
    match_names = split_named(stock_match)[1]
    for e in THUMB_ENTRIES:
        tag, body = read_entry(e, DT06, root)
        blocks, names = split_named(body)
        stock_blocks = split_named(read_entry(e, DT06)[1])[0]
        for i in ids:
            if i not in menu:
                raise ValueError("name id %d is not in the stadium menu" % i)
            nm = match_names[menu.index(i)]
            if nm not in names:
                log("dt06 #%d: no %s block (id %d not in this list)" % (e, nm, i))
                continue
            k = names.index(nm)
            blocks[k] = (stock_blocks[k] if src == "stock"
                         else encode_like(Image.open(src).convert("RGBA"), blocks[k]))
            log("dt06 #%d id %d -> %s" % (e, i, nm))
        write_entry(root, e, tag, join_named(blocks, names), DT06)


# --- install --------------------------------------------------------------

UV_ATLAS_SLACK = 1e-3   # UVs this far outside 0..1 still count as untiled
# Blended source materials draw opaque here (the stand shader has no blend).
# Their triangles whose texels are mostly transparent - st033's curtain
# shadows (floorboarding, alpha <= 49) and the glow blob in upper_details -
# then painted solid black or white slabs over the court (29-09 frames), so
# they are dropped; the rest (trusses, nets) stays as a cutout.
OVERLAY_ALPHA_KEEP = 128


def opaque_part(m):
    """Blended mesh -> only the triangles with a texel at or above
    OVERLAY_ALPHA_KEEP at a corner or the centroid; other meshes as is."""
    if not m.blend or m.image is None:
        return m
    import numpy as np
    a = np.asarray(m.image.getchannel("A"))
    h, w = a.shape
    def alpha(u, v):
        return a[int((v % 1) * (h - 1)), int((u % 1) * (w - 1))]
    keep = []
    for t in m.tris:
        uv = [m.uv[k] for k in t]
        c = (sum(u for u, _ in uv) / 3, sum(v for _, v in uv) / 3)
        if max(alpha(u, v) for u, v in uv + [c]) >= OVERLAY_ALPHA_KEEP:
            keep.append(t)
    if len(keep) < len(m.tris):
        print("%s: %d of %d blended triangles dropped (transparent overlay)"
              % (m.name, len(m.tris) - len(keep), len(m.tris)))
    return m._replace(tris=keep)


def atlas_pair(pieces, images, slot_number):
    """Two stand images -> one, side by side, when the slot has fewer stand
    texture blocks than the model has materials (st004 on slot 29: 7 for 8).
    Only images whose pieces never tile (UVs within 0..1) can share: the
    pair with the fewest pixels goes, their pieces' U is squeezed into each
    half. -> (pieces, images) with the pair replaced by the atlas."""
    from PIL import Image
    def untiled(img):
        return all(-UV_ATLAS_SLACK <= c <= 1 + UV_ATLAS_SLACK
                   for p in pieces if p.image is img for uv in p.uv for c in uv)
    ok = sorted((i for i in images if untiled(i)), key=lambda i: i.size[0] * i.size[1])
    if len(ok) < 2:
        raise ValueError("slot %d: too few stand textures and no untiled pair to atlas" % slot_number)
    a, b = ok[:2]
    h = max(a.size[1], b.size[1])
    wa, wb = (round(i.size[0] * h / i.size[1]) for i in (a, b))
    atlas = Image.new("RGBA", (wa + wb, h))
    atlas.paste(a.convert("RGBA").resize((wa, h)), (0, 0))
    atlas.paste(b.convert("RGBA").resize((wb, h)), (wa, 0))
    w = wa + wb
    remap = {id(a): (0.0, wa / w), id(b): (wa / w, wb / w)}
    out = [p._replace(image=atlas, uv=[(remap[id(p.image)][0] + u * remap[id(p.image)][1], v) for u, v in p.uv])
           if id(p.image) in remap else p for p in pieces]
    return out, [i for i in images if i is not a and i is not b] + [atlas]


def install(meshes, slot, out_root, sky=None, log=print):
    """Write `meshes` ([Mesh], KTMDL frame) into one stadium slot, from the
    stock geometry (stock pitch base kept: toggle it with set_asset); `sky`
    ([Mesh] dome around the origin) replaces the sky strip when given. The
    slot's per-variant props are hidden (they are stock stadium parts)."""
    from PIL import Image
    K = _ktmdl_reader()
    tag, body = read_entry(slot.geometry)
    stock = split_bin(body)
    candidates = [(bi, pk) for bi, b in enumerate(stock) if b[:5] == b"KTMDL"
                  for pk in [template_packet(K, b)] if pk is not None]
    # The engine skips blocks reaching below y = 0 from the broadcast camera
    # (see tile()). Karasuno's floor sits at y -0.06..-0.03, so every floor
    # block was below ground and a quarter of the court vanished per camera
    # (29-09). Decks within GROUND_TOLERANCE_M of 0 are lifted onto it.
    near = [y for m in meshes for _x, y, _z in m.pos if -GROUND_TOLERANCE_M < y < 0]
    lift = SCENE_LIFT_M - (min(near) if near else 0.0)
    # Pitch meshes are markings, drawn by the slot's pitch-art mask over its
    # own grass - never as stand geometry (as geometry the decal's atlas drew
    # its transparent texels as black and cream slabs over the court, 30-09).
    decal = bake_pitch_art([m for m in meshes if m.role == "scene"],
                           [m for m in meshes if m.role == "overlay"],
                           [m for m in meshes if m.role == "lines"])
    if decal is not None:
        # The source's own ground covers the pitch (Karasuno's floor sits
        # 2-4 cm above the stock pitch base, so no line ever showed, 30-09):
        # it is cut out where the stock pitch draws; the bake above already
        # carries that ground, under the markings, as the pitch art.
        rect = stock_pitch_rect(K, slot)
        meshes = [cut_under_pitch(cut_ground(m, rect), rect) if m.role == "scene" else m
                  for m in meshes]
    pieces = tile([c._replace(pos=[(x, y + lift, z) for x, y, z in c.pos])
                   for m in [opaque_part(m) for m in meshes if m.role == "scene"]
                   for c in chunk(subdivide(wind_to_stock(m), MAX_EDGE_M))
                   if c.tris], len(candidates))
    # tile() cannot merge pieces of different images, so it can stop over
    # budget; refuse before any override is written, or the slot is left
    # with its old geometry wearing the new textures and pitch art (P2-2).
    if len(pieces) > len(candidates):
        raise ValueError("slot %d: %d mesh pieces, only %d template blocks"
                         % (slot.number, len(pieces), len(candidates)))

    # textures: every distinct stand image takes one stand-texture block,
    # re-encoded at its own size (encode_block); the last stand block becomes
    # the white lightmap. Pitch content went into the decal (above).
    tslots = stand_texture_slots(slot)
    images = []
    for p in pieces:
        if p.image is not None and all(p.image is not i for i in images):
            images.append(p.image)
    while len(images) + 1 > len(tslots):
        pieces, images = atlas_pair(pieces, images, slot.number)
    if len(images) + 1 > len(tslots):
        raise ValueError("slot %d: %d materials need %d stand textures, stadium has %d"
                         % (slot.number, len(images), len(images) + 1, len(tslots)))
    white = tslots[-1]
    tex_id = {id(img): t.id for img, t in zip(images, tslots)}
    writes = {}  # entry -> {block index: image}
    for img, t in zip(images, tslots):
        writes.setdefault(t.entry, {})[t.block] = img
    writes.setdefault(white.entry, {})[white.block] = Image.new("RGBA", (4, 4), NEUTRAL_LIGHTMAP)
    for e, repl in writes.items():
        blocks = split_bin(read_entry(e)[1])
        replace_blocks(out_root, e, {bi: encode_block(img, blocks[bi]) for bi, img in repl.items()})
    if decal is not None:
        set_pitch_art([slot], out_root, decal, log=lambda _m: None)

    # geometry: empty everything, then one template block per piece.
    new = [empty_block(K, b) if b[:5] == b"KTMDL" else b for b in stock]
    # Each piece takes the template block whose STOCK geometry sits nearest
    # (greedy, largest pieces first): the engine decides per block, from
    # where that block's stock stand is, whether to draw it for a camera, so
    # a piece parked in a block from the other end of the stadium vanished
    # whenever that end was off screen (Karasuno: walls, roof, half the
    # court missing by camera, 29-09).
    centre = {}
    for bi, pk in candidates:
        V = K.parse_bytes(stock[bi])["packets"][pk]["vertices"]
        centre[bi] = [sum(v["POSITION"][k] for v in V) / len(V) for k in range(3)]
    free, order = list(candidates), []
    for p in sorted(pieces, key=lambda p: -len(p.tris)):
        lo, hi = _bounds(p)
        c = [(a + b) / 2 for a, b in zip(lo, hi)]
        best = min(free, key=lambda cand: sum((x - y) ** 2
                                             for x, y in zip(c, centre[cand[0]])))
        free.remove(best)
        order.append((p, best))
    for p, (bi, pk) in order:
        stride, elems = _decl(stock[bi], pk)
        want = [{"packet": pk, "vertices": pack_vertices(elems, stride, p.pos, p.nrm, p.uv),
                 "indices": [v for t in p.tris for v in t]}]
        blk = ktmdl_write.build(new[bi], want)
        tid = tex_id.get(id(p.image), white.id)
        new[bi] = repoint_textures(K, blk, pk, tid, white.id)
    write_entry(out_root, slot.geometry, tag, join_bin(new))
    # A slot install must leave no residue: the previous stadium's sky strip,
    # pitch art and unused stand-texture blocks are the slot's own entries and
    # the engine still draws them, so a slot reused for another stadium (the
    # Karasuno slot 1 that had briefly held Final PEStination) rendered a
    # hybrid of the two (30-09). Drop the overrides this install does not
    # write, so each of those entries goes back to stock.
    stale = ([e for e in slot.skies] if not sky else []) \
        + ([e for e in slot.pitches] if decal is None else []) \
        + sorted({t.entry for t in tslots} - set(writes))
    for e in stale:
        p = _override(out_root, e, DT07)
        if os.path.exists(p):
            os.remove(p)
    _set_props(K, slot, out_root, on=False)
    if sky:
        install_sky(K, sky, slot, out_root)
    log("slot %d: geometry dt07 #%d (%d pieces), props %s, textures %s, pitch art %s, sky %s"
        % (slot.number, slot.geometry, len(pieces), slot.props, sorted(writes),
           slot.pitches if decal is not None else "stock", slot.skies if sky else "stock"))


def read_source(src, textures=None):
    """-> (stand [Mesh], sky [Mesh] or None)."""
    if os.path.isdir(src):
        return read_pes15_folder(src)
    if src.lower().endswith(".fmdl"):
        # ponytail: PES21 skies ship as separate scene files; add a reader
        # for them when a PES21 stadium is converted with its sky.
        return read_fmdl(src, textures), None
    raise ValueError("%s: expected a PES15/17 stadium folder or a .fmdl" % src)


def pick_slots(spec, slots):
    if spec == "all":
        return slots
    want = {int(s) for s in spec.split(",")}
    got = [s for s in slots if s.number in want]
    if len(got) != len(want):
        raise ValueError("unknown slot(s) %s (have %s)"
                         % (sorted(want - {s.number for s in got}), [s.number for s in slots]))
    return got


USAGE = """usage:
  pes12_stadium.py list                                    slots (geometry, pitch art, props)
  pes12_stadium.py names                                   stadium name ids and names
  pes12_stadium.py install <source> <slots> <root> [--textures=<dir>] [--retexture=<material>:<image>,...]
  pes12_stadium.py hide|show <slots> <root> <asset,...>     assets: %s
  pes12_stadium.py pitch <slots> <root> <image|GF stadium dir|stock>
  pes12_stadium.py thumb <ids> <root> <image|stock>
  pes12_stadium.py name <ids> <"NAME"|stock>
<slots>: 'all' or comma-separated slot numbers; <ids>: comma-separated name
ids (`names`); <root>: the afs2fs root. Slot <-> name id is known only for
Allianz (slot 30 = id 61); find others with tools/afs_readlog.py while the
game loads the stadium (its geometry entry = the slot).""" % ", ".join(ASSETS)


def main(argv):
    opts = dict(a[2:].split("=", 1) for a in argv if a.startswith("--"))
    args = [a for a in argv if not a.startswith("--")]
    cmd, rest = (args[0], args[1:]) if args else (None, [])
    need = {"list": 0, "names": 0, "install": 3, "hide": 3, "show": 3,
            "pitch": 3, "thumb": 3, "name": 2}
    if need.get(cmd) != len(rest):
        sys.exit(USAGE)
    if cmd == "list":
        for s in read_slots():
            print("slot %2d: geometry %d, pitch art %s, props %s"
                  % (s.number, s.geometry, s.pitches, s.props))
        return
    if cmd == "names":
        data = open(EXE, "rb").read()
        for i, o in sorted(read_names(EXE).items()):
            print("id %2d: %s" % (i, data[o:data.index(b"\0", o)].decode()))
        return
    if cmd == "name":
        return set_name([int(i) for i in rest[0].split(",")], rest[1])
    if cmd == "thumb":
        return set_thumbnail([int(i) for i in rest[0].split(",")], rest[1], rest[2])
    if cmd in ("hide", "show"):
        slots, root = pick_slots(rest[0], read_slots()), rest[1]
        assets = rest[2].split(",")
        bad = set(assets) - set(ASSETS)
        if bad:
            sys.exit("unknown asset(s) %s; have %s" % (sorted(bad), ", ".join(ASSETS)))
        for a in assets:
            set_asset(a, slots, root, on=cmd == "show")
        return
    if cmd == "pitch":
        set_pitch_art(pick_slots(rest[0], read_slots()), rest[1], rest[2])
        return
    meshes, sky = read_source(rest[0], opts.get("textures"))
    # --retexture=<material>:<image>[,...]: that material's texture replaced
    # by an image (path relative to the source folder), e.g. Karasuno's
    # floor:texture/common/tex_bm_06.dds - the clean court its stage uses,
    # where the floor texture has PES17's turf rectangle painted over it.
    for spec in filter(None, opts.get("retexture", "").split(",")):
        mat, path = spec.split(":", 1)
        img = _open_texture(_find_nocase(os.path.join(rest[0], path)) or os.path.join(rest[0], path))
        hit = [k for k, m in enumerate(meshes) if m.name == mat]
        if not hit:
            sys.exit("--retexture: no material %r (have %s)" % (mat, sorted({m.name for m in meshes})))
        for k in hit:
            meshes[k] = meshes[k]._replace(image=img)
    print("scene: %d meshes, %d tris, sky %s" % (
        len(meshes), sum(len(m.tris) for m in meshes), "%d meshes" % len(sky) if sky else "none"))
    for s in pick_slots(rest[1], read_slots()):
        install(meshes, s, rest[2], sky)


if __name__ == "__main__":
    main(sys.argv[1:])

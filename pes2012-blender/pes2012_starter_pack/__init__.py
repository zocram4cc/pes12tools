# SPDX-License-Identifier: GPL-3.0-or-later
"""PES2012 Blender toolkit: KTMDL models (balls, stadiums, boots, faces,
hair, bodies, kits) and PGB2 custom bodies.

Pure-python layers (importable without Blender; tests/ exercise them):
container (BIN wrappers), model (KTMDL read/export), skeleton (per-block
bones), textures (WE00/DDS), ktpack/ktmdl_write (packing/rebuild), pgb2.
blender_io is the only bpy-facing layer besides this file. The vendored
KTMDL reader is moth1995/pes2008-2013-tools (marqisspes6, GPL-3.0; see
pes_ktmdl_importer/README.md).

World scale: PES units are metres, same as Blender.
"""
bl_info = {
    "name": "PES2012 Toolkit",
    "author": "PES2012 modding toolchain",
    "version": (2, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Import/Export > PES2012",
    "description": "Byte-exact import/export of PES2012 KTMDL BINs and PGB2 custom bodies",
    "category": "Import-Export",
    "license": "GPL-3.0-or-later",
}

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty  # noqa: E402
from bpy_extras.io_utils import ExportHelper, ImportHelper  # noqa: E402

from . import blender_io, container, ktmdl_write, model, pgb2, skeleton, stadium, textures  # noqa: E402

SOURCE_PROP = 'pes12_source'    # collection: absolute path of the imported BIN
BLOCK_PROP = 'pes12_block'      # object: index into the container's blocks
PACKET_PROP = 'pes12_packet'    # mesh object: packet index in its block
SLOT_PROP = 'pes12_slot'          # collection: slot number
GAME_PROP = 'pes12_game'          # collection: the game folder it was read from
ROOT_PROP = 'pes12_root'          # collection: the afs2fs root it reads overrides from
ENTRY_PROP = 'pes12_entry'        # sub-collection: its dt07 entry
TEX_ENTRY_PROP = 'pes12_tex_entry'  # image: the dt07 entry holding its block
BIN_SELF = -1                     # image of a single-BIN import: its own BIN


TEX_ROW_ID_OFF = 8     # textureNameIds row: u32 texture id at +8 (pes12_stadium.repoint_textures)
# authoring-name markers of the non-colour maps (stock names: ball000_s.tif,
# ek9061_nml.psd, lm_e10_bs1_00.psd, head_occlusion.psd, ...)
NOT_COLOUR = re.compile(r'(_s|_n|_nml|_spe|_nrm|normal|specular|occlusion|reflection)\b|^(lm|e_lmap|e_cmap)_', re.I)


def _row_ids(block_data, parsed):
    """The model's texture rows: row index -> texture id (u32 at row + 8)."""
    return [struct.unpack_from('<I', block_data, r['offset'] + TEX_ROW_ID_OFF)[0]
            for r in parsed.get('textureNameIds', [])]


def _diffuse_row(packet, names):
    """The packet's colour texture: the first ref on UV channel 0 whose name
    is not a normal/specular/light map (the ball lists specular first)."""
    refs = [r for r in packet['activeTextureRefs'] if r['uvNo'] == 0]
    for r in refs:
        stem = os.path.splitext(names[r['id']])[0] if r['id'] < len(names) else ''
        if not NOT_COLOUR.search(stem):
            return r['id']
    return refs[0]['id'] if refs else None


def _activate(context, col, parent_layer):
    """Make col the collection new objects link into (the blender_io helpers
    link into context.collection)."""
    layer = parent_layer.children[col.name]
    context.view_layer.active_layer_collection = layer
    return layer


def _import_container(context, col, stem, c, texture_of):
    """Meshes of every KTMDL block of container c into the active collection
    (col). texture_of(row id, block datas) -> Blender image or None."""
    datas = [b.data for b in c.blocks]
    n = 0
    for k, b in ((k, b) for k, b in enumerate(c.blocks) if b.kind == 'ktmdl'):
        m = model.read(b.data)
        sk = skeleton.build_one('b%d' % k, m.parsed)
        arm = blender_io.make_armature('%s_b%d' % (stem, k), sk)
        arm[BLOCK_PROP] = k
        row_ids = _row_ids(b.data, m.parsed)
        names = m.parsed['names'].get('textureNames', [])
        for i, p in enumerate(m.parsed['packets']):
            o = blender_io.make_mesh('%s_b%d_p%03d' % (stem, k, i), p, p['bonePalette'], sk)
            o[BLOCK_PROP] = k
            o[PACKET_PROP] = i
            row = _diffuse_row(p, names)
            img = texture_of(row_ids[row], datas) if row is not None and row < len(row_ids) else None
            if img is not None and 'UV0' in o.data.uv_layers:
                blender_io.assign_image(o, img, 'UV0')
            blender_io.parent_to_armature(o, arm)
            o.parent = arm
            n += 1
    return n


def import_bin(context, filepath):
    """BIN -> collection: per KTMDL block one armature (its own skeleton)
    and one mesh per packet; every texture the BIN carries as a packed image,
    on the packets that sample it. Returns (collection, packet count)."""
    c = container.read(open(filepath, 'rb').read())
    if not c.ktmdl_blocks():
        raise ValueError('no KTMDL block in %s' % filepath)
    stem = os.path.splitext(os.path.basename(filepath))[0]
    col = bpy.data.collections.new(stem)
    context.scene.collection.children.link(col)
    col[SOURCE_PROP] = os.path.abspath(filepath)
    layer = context.view_layer.layer_collection.children[col.name]
    context.view_layer.active_layer_collection = layer
    datas = [b.data for b in c.blocks]
    rows = [r for b in c.ktmdl_blocks() for r in _row_ids(b.data, model.read(b.data).parsed)]
    bound = textures.bind(datas, rows)   # over the whole BIN, as the rules were measured
    images = {}

    def texture_of(row_id, _datas):
        k = bound.get(row_id)
        if k is None:
            return None
        if k not in images:
            _, _, px = textures.decode(datas[k])
            images[k] = blender_io.make_image('%s_tex%d' % (stem, k), k, px)
            images[k][TEX_ENTRY_PROP] = BIN_SELF
        return images[k]

    n = _import_container(context, col, stem, c, texture_of)
    return col, n


def _export_container(c, objs, imgs, entry):
    """Apply Blender edits to container c: the packets of objs ({block:
    {packet: obj}}) and the changed images among imgs that live in this
    entry (TEX_ENTRY_PROP)."""
    for k, packets in objs.items():
        b = c.blocks[k]
        m = model.read(b.data)
        sk = skeleton.build_one('b%d' % k, m.parsed)
        rows = [model.export_packet(b.data, i, *blender_io.read_corners(o, sk))
                for i, o in sorted(packets.items())]
        b.data = ktmdl_write.build(b.data, rows)
    for img in imgs:
        if int(img.get(TEX_ENTRY_PROP, -1)) == entry and blender_io.image_changed(img):
            k = int(img[blender_io.TEX_BLOCK_PROP])
            c.blocks[k].data = textures.encode(c.blocks[k].data, blender_io.image_rgba(img))


def _packet_objects(col):
    objs = {}
    for o in col.all_objects:
        if o.type == 'MESH' and BLOCK_PROP in o and PACKET_PROP in o:
            objs.setdefault(int(o[BLOCK_PROP]), {})[int(o[PACKET_PROP])] = o
    return objs


def _collection_images(col):
    """Every imported image the collection's materials use, once."""
    seen = {}
    for o in col.all_objects:
        for slot in getattr(o, 'material_slots', ()):
            nt = slot.material.node_tree if slot.material and slot.material.use_nodes else None
            for node in (nt.nodes if nt else ()):
                img = getattr(node, 'image', None)
                if img is not None and blender_io.TEX_BLOCK_PROP in img:
                    seen[img.name] = img
    return list(seen.values())


def export_bin(col, filepath):
    """Collection from import_bin -> BIN at filepath, byte-exact where the
    user changed nothing. A packet whose mesh object was deleted keeps its
    stock data; extra objects are ignored (one object per packet). Images
    painted since import go back into their blocks; untouched ones keep
    their bytes (DXT re-encoding is lossy)."""
    src = col.get(SOURCE_PROP)
    if not src or not os.path.exists(src):
        raise ValueError('collection %s was not imported from a PES2012 BIN' % col.name)
    c = container.read(open(src, 'rb').read())
    _export_container(c, _packet_objects(col), _collection_images(col), BIN_SELF)
    open(filepath, 'wb').write(container.write(c))
    return filepath


# --- whole stadium slots ---



def import_slot(context, game, number, root=None):
    """A licensed stadium slot -> one collection, a sub-collection per model
    entry (geometry, then the per-variant props), every packet textured from
    the slot's texture entries (stand, lightmaps, sky, pitch, shared)."""
    sl = {s.number: s for s in stadium.slots(game)}
    if number not in sl:
        raise ValueError('no licensed stadium slot %d (have %s)' % (number, sorted(sl)))
    s = sl[number]
    col = bpy.data.collections.new('slot%02d' % number)
    context.scene.collection.children.link(col)
    col[SLOT_PROP], col[GAME_PROP], col[ROOT_PROP] = number, os.path.abspath(game), os.path.abspath(root) if root else ''
    top = _activate(context, col, context.view_layer.layer_collection)
    index = stadium.texture_index(game, s, root)
    cache, images = {}, {}

    def texture_of(row_id, _datas):
        where = index.get(row_id)
        if where is None:
            return None
        if where not in images:
            e, k = where
            if e not in cache:
                cache[e] = container.read(stadium.read_raw(game, e, root))
            _, _, px = textures.decode(cache[e].blocks[k].data)
            img = blender_io.make_image('dt07_%d_tex%d' % (e, k), k, px)
            img[TEX_ENTRY_PROP] = e
            images[where] = img
        return images[where]

    roles = stadium.slot_entries(s)
    n = 0
    for role in ('geometry', 'props'):
        for e in roles[role]:
            sub = bpy.data.collections.new('%s_dt07_%d' % (role, e))
            col.children.link(sub)
            sub[ENTRY_PROP] = e
            _activate(context, sub, top)
            n += _import_container(context, sub, 'dt07_%d' % e, container.read(stadium.read_raw(game, e, root)), texture_of)
    return col, n


def export_slot(col, root):
    """Slot collection -> afs2fs overrides in root (kitserver/4cc-dlc) for
    every model entry and every texture entry an edit touched; entries equal
    to stock leave no file. -> written paths."""
    if SLOT_PROP not in col:
        raise ValueError('collection %s is not a stadium slot import' % col.name)
    game, src_root = col[GAME_PROP], col.get(ROOT_PROP) or None
    imgs = _collection_images(col)
    out = []
    for sub in col.children:
        if ENTRY_PROP not in sub:
            continue
        e = int(sub[ENTRY_PROP])
        c = container.read(stadium.read_raw(game, e, src_root))
        _export_container(c, _packet_objects(sub), imgs, e)
        p = stadium.write_raw(game, root, e, container.write(c))
        if p:
            out.append(p)
    for e in sorted({int(i[TEX_ENTRY_PROP]) for i in imgs if blender_io.image_changed(i)} - {int(s[ENTRY_PROP]) for s in col.children if ENTRY_PROP in s}):
        c = container.read(stadium.read_raw(game, e, src_root))
        _export_container(c, {}, imgs, e)
        p = stadium.write_raw(game, root, e, container.write(c))
        if p:
            out.append(p)
    return out


def _collection_of(context):
    """Imported collection of the active object, else the active one."""
    o = context.active_object
    cols = list(o.users_collection) if o else []
    cols.append(context.view_layer.active_layer_collection.collection)
    for col in cols:
        if SOURCE_PROP in col:
            return col
    return None


class IMPORT_OT_pes12_bin(bpy.types.Operator, ImportHelper):
    bl_idname = 'import_scene.pes12_bin'
    bl_label = 'Import PES2012 BIN'
    bl_options = {'UNDO', 'PRESET'}
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})

    def execute(self, context):
        try:
            col, n = import_bin(context, self.filepath)
        except Exception as e:  # reader errors carry the offending field
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Imported %d packet(s) into %s' % (n, col.name))
        return {'FINISHED'}


class EXPORT_OT_pes12_bin(bpy.types.Operator, ExportHelper):
    bl_idname = 'export_scene.pes12_bin'
    bl_label = 'Export PES2012 BIN'
    bl_options = {'PRESET'}
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})

    def execute(self, context):
        col = _collection_of(context)
        if col is None:
            self.report({'ERROR'}, 'Select an object of an imported PES2012 BIN')
            return {'CANCELLED'}
        try:
            out = export_bin(col, self.filepath)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Wrote %s' % out)
        return {'FINISHED'}


# --- PGB2 bodies ---

PGB2_PIECES = tuple((n, n, 'Keep the stock %s drawn' % n, 1 << i) for i, n in enumerate(pgb2.PIECES))
# material property -> submesh flag bit (every bit PGB2 defines; the ref
# byte, bits 8-15, is pgb2_alpha_ref)
FLAG_PROPS = (
    ('pgb2_alpha_test', pgb2.SUB_ALPHATEST, 'Alpha test (bit 0)'),
    ('pgb2_blend', pgb2.SUB_BLEND, 'Alpha blend, drawn last (bit 1)'),
    ('pgb2_twosided', pgb2.SUB_TWOSIDED, 'Two-sided (bit 2)'),
    ('pgb2_nozwrite', pgb2.SUB_NOZWRITE, 'No depth write (bit 3)'),
    ('pgb2_kit_slot', pgb2.SUB_KIT, 'Kit slot: UVs on the worn kit sheet (bit 4)'),
    ('pgb2_outline', pgb2.SUB_OUTLINE, 'Toon outline shell (bit 5)'),
    ('pgb2_face', pgb2.SUB_FACE, 'Face part: head-local verts (bit 6)'),
    ('pgb2_shadeless', pgb2.SUB_SHADELESS, 'Shadeless pixel shader (bit 16)'),
    ('pgb2_toon', pgb2.SUB_TOON, 'Toon (Pony) pixel shader (bit 17)'),
    ('pgb2_hair', pgb2.SUB_HAIR, 'Hair: opaque core plus alpha fringe pass (bit 18)'),
)


def _ensure_props():
    if not hasattr(bpy.types.Object, 'pgb2_keep'):
        bpy.types.Object.pgb2_keep = EnumProperty(
            name='Stock pieces kept', items=PGB2_PIECES, options={'ENUM_FLAG'}, default=set(),
            description='Stock pieces drawn with this body (drawlogic PIECE_NAMES); none = a whole figure')
    if not hasattr(bpy.types.Material, 'pgb2_alpha_ref'):
        bpy.types.Material.pgb2_alpha_ref = IntProperty(
            name='Alpha ref', default=0, min=0, max=255,
            description='Alpha-test threshold, bits 8-15 (pass alpha > ref)')
    for name, bit, desc in FLAG_PROPS:
        if not hasattr(bpy.types.Material, name):
            setattr(bpy.types.Material, name, BoolProperty(name=name, default=False, description=desc))


def _mat_flags(mat):
    # RNA storage (Blender 5.0 moved bpy.props out of IDProperty dicts,
    # so mat.get('pgb2_blend') reads None even when set: use getattr).
    f = 0
    for name, bit, _desc in FLAG_PROPS:
        if getattr(mat, name, False):
            f |= bit
    if f & pgb2.SUB_ALPHATEST:
        f |= int(getattr(mat, 'pgb2_alpha_ref', 0)) << pgb2.SUB_REF_SHIFT
    return f


def import_body(context, filepath):
    """PGB2 body.bin -> (object, submesh count). Face-part verts are stored
    head-local; import adds pgb2.HEAD_POS back, and their weights land on
    face_XX groups (the 27-slot face palette), body verts on SLOT_BONES."""
    parsed = pgb2.parse(open(filepath, 'rb').read())
    stem = os.path.splitext(os.path.basename(filepath))[0]
    col = bpy.data.collections.new(stem)
    context.scene.collection.children.link(col)
    mesh = bpy.data.meshes.new(stem)
    face_verts = {i for s in parsed['subs'] if s['flags'] & pgb2.SUB_FACE
                  for i in parsed['idx'][s['first']:s['first'] + s['count']]}
    positions = []
    for vi, v in enumerate(parsed['verts']):
        p = v['pos']
        if vi in face_verts:  # head-local storage: add the head joint back
            p = (p[0] + pgb2.HEAD_POS[0], p[1] + pgb2.HEAD_POS[1],
                 p[2] + pgb2.HEAD_POS[2])
        positions.append(blender_io.to_blender(p))
    mesh.from_pydata(
        positions, [],
        [tuple(parsed['idx'][k:k + 3])
         for s in parsed['subs'] for k in range(s['first'], s['first'] + s['count'], 3)])
    mesh.update()
    for ch in (0, 1):
        layer = mesh.uv_layers.new(name='UV%d' % ch)
        for loop in mesh.loops:
            v = parsed['verts'][loop.vertex_index]
            layer.data[loop.index].uv = v['uv0'] if ch == 0 else v['uv1']
    obj = bpy.data.objects.new(stem, mesh)
    col.objects.link(obj)
    for name in list(pgb2.SLOT_BONES) + [
            pgb2.FACE_GROUP_FMT % i for i in range(pgb2.N_FACE_SLOTS)]:
        obj.vertex_groups.new(name=name)
    for vi, v in enumerate(parsed['verts']):
        infl = pgb2.slots_to_influences(v['slots'], v['weights'])
        for slot, w in infl.items():
            if vi in face_verts:
                name = pgb2.FACE_GROUP_FMT % slot
            elif slot < len(pgb2.SLOT_BONES):
                name = pgb2.SLOT_BONES[slot]
            else:
                continue  # body vert on an out-of-range slot: cannot name
            obj.vertex_groups[name].add([vi], w, 'REPLACE')
    obj.pgb2_keep = {n for i, n in enumerate(pgb2.PIECES) if parsed['keep'] >> i & 1}
    obj['pgb2_source'] = os.path.abspath(filepath)
    for mi, s in enumerate(parsed['subs']):
        mat = bpy.data.materials.new('%s_sub%d' % (stem, mi))
        obj.data.materials.append(mat)
        for key, bit, _desc in FLAG_PROPS:
            setattr(mat, key, bool(s['flags'] & bit))
        mat.pgb2_alpha_ref = (s['flags'] >> pgb2.SUB_REF_SHIFT) & 0xFF
        mat['pgb2_tex'] = s['tex']
    # polygon k belongs to the submesh whose index range holds it
    tri_no = 0
    for mi, s in enumerate(parsed['subs']):
        for _k in range(s['count'] // 3):
            if tri_no < len(mesh.polygons):
                mesh.polygons[tri_no].material_index = mi
            tri_no += 1
    # textures stay external (body_<k>.tex next to body.bin); not embedded
    return obj, len(parsed['subs'])


def export_body(filepath, obj):
    """Body mesh object -> PGB2 body.bin bytes on disk. One submesh per
    used material slot, blended ones last (tools/pes15_to_pes12.py).

    Vertices split per (vertex, uv0, uv1) corner over loop_triangle
    loops, so UV seams survive; pack_body emits one vert per corner.
    """
    mesh = obj.data
    mesh.calc_loop_triangles()
    mat3 = obj.matrix_world.to_3x3()
    pos_of = {v.index: blender_io.to_pes(obj.matrix_world @ v.co) for v in mesh.vertices}
    nrm_of = {v.index: blender_io.to_pes((mat3 @ v.normal).normalized()) for v in mesh.vertices}
    try:
        mesh.calc_tangents()
        has_tan = True
    except Exception:
        has_tan = False
    uv_layers = [mesh.uv_layers.get('UV%d' % ch) for ch in range(2)]
    vert_data, tri_by_mat = {}, {}
    for t in mesh.loop_triangles:
        corners = []
        for li in t.loops:
            loop = mesh.loops[li]
            vi = loop.vertex_index
            uvs = []
            for layer in uv_layers:
                if layer is None:
                    uvs.append((0.0, 0.0))
                else:
                    u, v = layer.data[li].uv
                    uvs.append((u, v))
            key = (vi, uvs[0], uvs[1])
            if key not in vert_data:
                infl, face = {}, {}
                for g in mesh.vertices[vi].groups:
                    name = obj.vertex_groups[g.group].name
                    if name in pgb2.SLOT_OF:
                        infl[pgb2.SLOT_OF[name]] = infl.get(
                            pgb2.SLOT_OF[name], 0.0) + g.weight
                    elif name.startswith('face_'):
                        try:
                            face[int(name[5:])] = face.get(
                                int(name[5:]), 0.0) + g.weight
                        except ValueError:
                            pass
                if has_tan:
                    tan = blender_io.to_pes(loop.tangent)
                    try:
                        bin_ = blender_io.to_pes(loop.bitangent)
                    except Exception:
                        bin_ = (0.0, 0.0, 1.0)
                else:
                    tan, bin_ = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
                vert_data[key] = dict(pos=pos_of[vi], nrm=nrm_of[vi],
                                      tan=tan, bin=bin_,
                                      uv0=uvs[0], uv1=uvs[1],
                                      infl=infl, face=face)
            corners.append(key)
        tri_by_mat.setdefault(t.material_index, []).append(tuple(corners))
    mats = list(obj.data.materials)
    mat_flags, mat_tex, mat_face = {}, {}, {}
    for mi, mat in enumerate(mats):
        if mat is None:
            continue
        mat_flags[mi] = _mat_flags(mat)
        mat_tex[mi] = int(mat['pgb2_tex']) if 'pgb2_tex' in mat else 0
        mat_face[mi] = bool(mat.pgb2_face)
    keep = sum(1 << pgb2.PIECES.index(n) for n in obj.pgb2_keep)
    open(filepath, 'wb').write(pgb2.pack_body(
        {mi: tris for mi, tris in tri_by_mat.items() if tris},
        vert_data, mat_flags, mat_tex, mat_face, keep))
    return sum(len(t) for t in tri_by_mat.values())


class IMPORT_OT_pgb2(bpy.types.Operator, ImportHelper):
    bl_idname = 'import_scene.pgb2_body'
    bl_label = 'Import PGB2 body'
    bl_options = {'UNDO', 'PRESET'}
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})

    def execute(self, context):
        try:
            obj, nsub = import_body(context, self.filepath)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Imported PGB2 %s (%d submeshes)'
                    % (obj.name, nsub))
        return {'FINISHED'}


class EXPORT_OT_pgb2(bpy.types.Operator, ExportHelper):
    bl_idname = 'export_scene.pgb2_body'
    bl_label = 'Export PGB2 body'
    bl_options = {'PRESET'}
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if len(objs) != 1:
            self.report({'ERROR'}, 'Select exactly one body mesh')
            return {'CANCELLED'}
        try:
            nv = export_body(self.filepath, objs[0])
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Wrote PGB2 %s (%d verts)' % (self.filepath, nv))
        return {'FINISHED'}


class IMPORT_OT_pes12_slot(bpy.types.Operator):
    """Import every model of a licensed stadium slot, textured from the slot's own texture entries"""
    bl_idname = 'import_scene.pes12_slot'
    bl_label = 'Import PES2012 stadium slot'
    bl_options = {'UNDO'}
    game: StringProperty(name='Game folder', subtype='DIR_PATH',
                         description='The PES2012 folder (pes2012.exe, img/)')
    root: StringProperty(name='afs2fs root', subtype='DIR_PATH',
                         description='kitserver/4cc-dlc: overrides there are read instead of stock (optional)')
    slot: IntProperty(name='Slot', default=0, min=0, description='Licensed stadium slot number (pes12_stadium.py list)')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            col, n = import_slot(context, bpy.path.abspath(self.game), self.slot, bpy.path.abspath(self.root) or None)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Imported slot %d: %d packet(s) into %s' % (self.slot, n, col.name))
        return {'FINISHED'}


class EXPORT_OT_pes12_slot(bpy.types.Operator):
    """Write the edited entries of an imported stadium slot as afs2fs overrides"""
    bl_idname = 'export_scene.pes12_slot'
    bl_label = 'Export PES2012 stadium slot'
    root: StringProperty(name='afs2fs root', subtype='DIR_PATH',
                         description='kitserver/4cc-dlc: overrides are written to <root>/img/dt07.img/')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        col = _slot_collection(context)
        if col is None:
            self.report({'ERROR'}, 'Select an object of an imported stadium slot')
            return {'CANCELLED'}
        try:
            out = export_slot(col, bpy.path.abspath(self.root))
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Wrote %d override(s)' % len(out))
        return {'FINISHED'}


def _slot_collection(context):
    """The slot collection holding the active object (its entry
    sub-collection's parent), else the active collection if it is one."""
    o = context.active_object
    for c in bpy.data.collections:
        if SLOT_PROP in c and (o is None or o.name in c.all_objects):
            return c
    return None


def menu_import(self, context):
    self.layout.operator(IMPORT_OT_pes12_bin.bl_idname, text='PES2012 BIN (.bin)')
    self.layout.operator(IMPORT_OT_pgb2.bl_idname, text='PGB2 body (.bin)')
    self.layout.operator(IMPORT_OT_pes12_slot.bl_idname, text='PES2012 stadium slot')


def menu_export(self, context):
    self.layout.operator(EXPORT_OT_pes12_bin.bl_idname, text='PES2012 BIN (.bin)')
    self.layout.operator(EXPORT_OT_pgb2.bl_idname, text='PGB2 body (.bin)')
    self.layout.operator(EXPORT_OT_pes12_slot.bl_idname, text='PES2012 stadium slot (afs2fs)')


classes = (IMPORT_OT_pes12_bin, EXPORT_OT_pes12_bin, IMPORT_OT_pgb2, EXPORT_OT_pgb2,
           IMPORT_OT_pes12_slot, EXPORT_OT_pes12_slot)


def register():
    _ensure_props()
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    for menu, fn in ((bpy.types.TOPBAR_MT_file_import, menu_import),
                     (bpy.types.TOPBAR_MT_file_export, menu_export)):
        try:
            menu.remove(fn)
        except Exception:
            pass
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == '__main__':
    register()

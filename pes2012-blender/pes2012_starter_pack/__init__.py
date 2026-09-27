# SPDX-License-Identifier: GPL-3.0-or-later
"""PES 2008-2013 Starter Pack: KTMDL import/export + PGB2 custom bodies.

KTMDL import re-exports the vendored pes_ktmdl_importer package from
moth1995/pes2008-2013-tools (KTMDL importer by marqisspes6; GPL-3.0;
see pes_ktmdl_importer/README.md and LICENSE).
KTMDL export for new topology and PGB2 body import/export are
implemented here on the pure-python ktpack/binwrap/pgb2 modules
(importable without Blender, which is how tests/ exercises them).

World scale: PES units are metres, same as Blender, so no scale factor.
"""
bl_info = {
    "name": "PES 2008-2013 Starter Pack",
    "author": "PES2012 modding toolchain",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > PES (.bin)",
    "description": "Import stock KTMDL BINs, export edited geometry back "
                   "into template BINs, and author PGB2 custom bodies",
    "warning": "",
    "doc_url": "",
    "category": "Import-Export",
    "license": "GPL-3.0-or-later",
}

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import binwrap, ktpack, pgb2
from .pes_ktmdl_importer import IMPORT_OT_pes_ktmdl
from .pes_ktmdl_importer import ktmdl as _ktmdl
from .pes_ktmdl_importer import (
    _basis_matrix,
    _create_armature,
    _create_mesh_object,
    _source_stem,
    _store_collection_metadata,
)


def _to_blender(p):
    x, y, z = p  # PES (X, Y, Z) -> Blender (X, -Z, Y)
    return (x, -z, y)


def _to_pes(p):
    x, y, z = p  # inverse of the import basis (vendored _basis_matrix)
    return (x, z, -y)


def _template_body(path):
    """Template BIN path -> (tag8, body); BIN may be WESYS or raw."""
    raw = open(path, 'rb').read()
    return bytes(raw[:8]), binwrap.unwesys(raw)


def import_bin(context, filepath, flip_v=False):
    """BIN bytes -> (model-ish report, collection, objects): one temp .ktmdl
    per KTMDL block, each imported through the vendored importer."""
    import tempfile
    tag, body = _template_body(filepath)
    kind, blocks = _split_template(body)
    hits = binwrap.find_ktmdl(blocks)
    if not hits:
        raise _ktmdl.KTMDLError('no KTMDL block in %s' % filepath)
    stem = _source_stem(filepath)
    collection = bpy.data.collections.new(stem)
    context.scene.collection.children.link(collection)
    basis = _basis_matrix('PES_TO_BLENDER')
    objects, total = [], 0
    for h in hits:
        with tempfile.NamedTemporaryFile(suffix='.ktmdl', delete=False) as t:
            t.write(blocks[h])
            tmp = t.name
        try:
            model = _ktmdl.parse_file(tmp)
            arm_obj, bone_names = None, {}
            if model['bones']:
                arm_obj, bone_names = _create_armature(
                    context, collection, model, basis, 1.0)
            for packet in model['packets']:
                o = _create_mesh_object(
                    context, collection, model, packet, basis, 1.0, flip_v,
                    True, arm_obj, bone_names, 'PRIMARY0', {})
                o['pes12_template'] = os.path.abspath(filepath)
                o['pes12_block'] = h
                objects.append(o)
            total += len(model['packets'])
        finally:
            os.unlink(tmp)
    _store_collection_metadata(collection, model, None, None, filepath,
                               'PES_TO_BLENDER', 1.0, flip_v, 'PRIMARY0')
    return {'blocks': len(hits), 'parts': total, 'kind': kind}, collection, objects


def _palette_of(template, packet):
    """Template packet palette -> {vertex-group name: palette slot}.

    KTMDL BLENDINDICES index the packet's own palette (bone node ids),
    so groups map through the vendored importer bone names
    (bone_%03d_%016X), not the PGB2 21-slot table. Falls back to SLOT_OF
    when the template carries no palette (unskinned packets).
    """
    from .pes_ktmdl_importer import _bone_name
    from .pes_ktmdl_importer import ktmdl as _k
    model = _k.parse_bytes(bytes(template), 'template.ktmdl')
    pal = model['packets'][packet].get('skeletonIndices', [])
    node_of = {b['index']: b for b in model['bones']}
    out = {}
    for slot, node in enumerate(pal):
        b = node_of.get(node)
        if b is not None:
            out[_bone_name(b)] = slot
    return out or dict(pgb2.SLOT_OF)


def _mesh_data(obj, flip_v, template=None, packet=0):
    """Blender mesh -> (attrs, tris): split verts by loop seams.

    One attr per (vertex, uv chans, loop normal) key over loop_triangle
    loops, so UV/normal seams survive; tris index the split verts. w is
    [(palette slot, weight)] via the template packet palette.
    """
    mesh = obj.data
    mesh.calc_loop_triangles()
    mat3 = obj.matrix_world.to_3x3()
    pal = _palette_of(template, packet) if template is not None else dict(pgb2.SLOT_OF)
    layers = [mesh.uv_layers.get('UV%d' % ch) for ch in range(4)]
    attrs, index_of, tris = [], {}, []
    for t in mesh.loop_triangles:
        tri = []
        for li in t.loops:
            loop = mesh.loops[li]
            vi = loop.vertex_index
            v = mesh.vertices[vi]
            uv = []
            for layer in layers:
                if layer is None:
                    continue
                u, vv = layer.data[li].uv
                uv.append((u, 1.0 - vv if flip_v else vv))
            n = (mat3 @ v.normal).normalized()
            key = (vi, tuple(uv))
            if key not in index_of:
                index_of[key] = len(attrs)
                infl = []
                for g in v.groups:
                    name = obj.vertex_groups[g.group].name
                    if name in pal:
                        infl.append((pal[name], g.weight))
                p = obj.matrix_world @ v.co
                attrs.append(dict(pos=_to_pes(p), nrm=_to_pes(n),
                                  uv=(uv or [(0.0, 0.0)]), w=infl))
            tri.append(index_of[key])
        tris.append(tuple(tri))
    return attrs, tris


def _split_template(body):
    """Template body -> (kind, blocks): ball, dt08 stadium side, generic.

    The dialect is chosen by join(split(x)) == x (ball, then stadium,
    then generic): row layouts overlap, so header-shape guessing
    mis-routes (dt08 sides share n=4/hs=64 with balls; texture
    companions parse as stadium rows). 0-packet reserved slots pass
    through untouched.
    """
    from . import ktmdl_write as _W  # noqa: F401 (kept for export scope)
    try:
        kt, tex = binwrap.split_ball(body)
        if binwrap.join_ball(kt, tex) == bytes(body):
            return 'ball', [kt, *tex]
    except ValueError:
        pass
    try:
        blocks = binwrap.split_stadium(body)
        if binwrap.join_stadium(blocks) == bytes(body):
            return 'stadium', blocks
    except ValueError:
        pass
    return 'generic', binwrap.split_generic(body)


def export_bin(filepath, template_path, objects, flip_v=False, afs_entry=None):
    """Mesh objects + template BIN -> WESYS BIN at filepath.

    Objects carry pes12_block / ktmdl_packet_index from import_bin; the
    template supplies header/materials/bones/declaration. Returns the
    written path (defaults to <name>_<entry>.bin for afs2fs when the
    chosen name has no underscore suffix).
    """
    from . import ktmdl_write as W
    tag, body = _template_body(template_path)
    kind, blocks = _split_template(body)
    rows_by_block = {}
    for o in objects:
        block = int(o.get('pes12_block', 0))
        template = blocks[block]
        packet = int(o.get('ktmdl_packet_index', o.get('ktmdl_part_index', 0)))
        attrs, tris = _mesh_data(o, flip_v, template, packet)
        decl = ktpack.declaration(template, packet)
        if not any(s in (0x20, 0x21) for _, _, s in decl):
            for a in attrs:
                a.pop('w', None)
        rows_by_block.setdefault(block, []).append(
            ktpack.mesh_row(template, packet, attrs, list(tris)))
    new_blocks = list(blocks)
    for block, rows in rows_by_block.items():
        # untouched packets ride along as template rows: ktmdl_write
        # rebuilds group/bounding AABBs from the replaced packets only,
        # so a partial export would otherwise shrink the block bounds.
        have = {r['packet'] for r in rows}
        npacket = W._u32(blocks[block], W.H_PACKET_COUNT)
        for pi in range(npacket):
            if pi not in have:
                vb, idx, _st = W.packet_mesh(blocks[block], pi)
                rows.append({'packet': pi, 'vertices': bytes(vb),
                             'indices': list(idx)})
        new_blocks[block] = W.build(blocks[block], rows)
    if kind == 'ball':
        out_body = binwrap.join_ball(new_blocks[0], new_blocks[1:])
    elif kind == 'stadium':
        out_body = binwrap.join_stadium(new_blocks)
    else:
        n, _, hs = struct.unpack_from('<III', body)
        out_body = binwrap.join_generic(new_blocks, compact=(hs == 12 + 12 * n))
    out = filepath
    if afs_entry is not None and '_' not in os.path.splitext(os.path.basename(out))[0]:
        out = os.path.join(os.path.dirname(out), '%s_%d.bin' % (
            os.path.splitext(os.path.basename(out))[0], afs_entry))
    wrapped = binwrap.wesys_wrap(out_body)
    open(out, 'wb').write(bytes(tag) + bytes(wrapped[8:]) if tag[3:8] == b'WESYS'
                          else bytes(out_body))
    return out


class IMPORT_OT_pes12_bin(bpy.types.Operator, ImportHelper):
    bl_idname = 'import_scene.pes12_bin'
    bl_label = 'Import PES2012 BIN (KTMDL)'
    bl_options = {'UNDO', 'PRESET'}
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})
    flip_v: BoolProperty(name='Flip UV V', default=False)

    def execute(self, context):
        try:
            rep, _c, objs = import_bin(context, self.filepath, self.flip_v)
        except _ktmdl.KTMDLError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Imported %d part(s) (%s BIN)' % (rep['parts'], rep['kind']))
        return {'FINISHED'}


class EXPORT_OT_pes12_bin(bpy.types.Operator, ExportHelper):
    bl_idname = 'export_scene.pes12_bin'
    bl_label = 'Export PES2012 BIN (KTMDL from template)'
    bl_options = {'PRESET'}
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})
    flip_v: BoolProperty(name='Flip UV V', default=False)
    afs_entry: IntProperty(
        name='AFS entry number', default=11,
        description='dtXX entry this BIN replaces; the file name defaults '
                    'to <name>_<entry>.bin for afs2fs')

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            self.report({'ERROR'}, 'Select mesh objects to export')
            return {'CANCELLED'}
        templates = {o.get('pes12_template', '') for o in objs}
        if len(templates) != 1 or not next(iter(templates)):
            self.report({'ERROR'},
                        'All selected objects must come from one Import PES2012 BIN')
            return {'CANCELLED'}
        try:
            out = export_bin(self.filepath, next(iter(templates)), objs,
                             self.flip_v, self.afs_entry)
        except (_ktmdl.KTMDLError, ValueError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Wrote %s' % out)
        return {'FINISHED'}


# --- PGB2 bodies ---

PGB2_MODES = tuple((m, m, m) for m in pgb2.MODES)


def _ensure_props():
    if not hasattr(bpy.types.Object, 'pgb2_mode'):
        bpy.types.Object.pgb2_mode = EnumProperty(
            name='PGB2 mode', items=PGB2_MODES, default='body',
            description='Which stock parts this body replaces (drawlogic MODE_*)')
    if not hasattr(bpy.types.Material, 'pgb2_alpha_ref'):
        bpy.types.Material.pgb2_alpha_ref = IntProperty(
            name='Alpha ref', default=0, min=0, max=255,
            description='Alpha-test threshold, bits 8-15 (pass alpha > ref)')
    for name, default, desc in (
            ('pgb2_alpha_test', False, 'Alpha test (bit0)'),
            ('pgb2_blend', False, 'Alpha blend, drawn last (bit1)'),
            ('pgb2_twosided', False, 'Two-sided (bit2)'),
            ('pgb2_nozwrite', False, 'No depth write (bit3)'),
            ('pgb2_kit_slot', False, 'Kit slot: UVs on the worn kit sheet (bit4)'),
            ('pgb2_outline', False, 'Toon outline shell (bit5)'),
            ('pgb2_face', False, 'Face part: head-local verts (bit6)')):
        if not hasattr(bpy.types.Material, name):
            setattr(bpy.types.Material, name,
                    BoolProperty(name=name, default=default, description=desc))


def _mat_flags(mat):
    # RNA storage (Blender 5.0 moved bpy.props out of IDProperty dicts,
    # so mat.get('pgb2_blend') reads None even when set: use getattr).
    f = 0
    if getattr(mat, 'pgb2_alpha_test', False):
        f |= pgb2.SUB_ALPHATEST | (int(getattr(mat, 'pgb2_alpha_ref', 0)) << pgb2.SUB_REF_SHIFT)
    if getattr(mat, 'pgb2_blend', False):
        f |= pgb2.SUB_BLEND
    if getattr(mat, 'pgb2_twosided', False):
        f |= pgb2.SUB_TWOSIDED
    if getattr(mat, 'pgb2_nozwrite', False):
        f |= pgb2.SUB_NOZWRITE
    if getattr(mat, 'pgb2_kit_slot', False):
        f |= pgb2.SUB_KIT
    if getattr(mat, 'pgb2_outline', False):
        f |= pgb2.SUB_OUTLINE
    if getattr(mat, 'pgb2_face', False):
        f |= pgb2.SUB_FACE
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
        positions.append(_to_blender(p))
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
    obj.pgb2_mode = pgb2.MODES[parsed['mode']]
    obj['pgb2_source'] = os.path.abspath(filepath)
    for mi, s in enumerate(parsed['subs']):
        mat = bpy.data.materials.new('%s_sub%d' % (stem, mi))
        obj.data.materials.append(mat)
        for bit, key in ((1, 'pgb2_alpha_test'), (2, 'pgb2_blend'),
                         (4, 'pgb2_twosided'), (8, 'pgb2_nozwrite'),
                         (16, 'pgb2_kit_slot'), (32, 'pgb2_outline'),
                         (64, 'pgb2_face')):
            setattr(mat, key, bool(s['flags'] & bit))
        mat.pgb2_alpha_ref = (s['flags'] >> 8) & 0xFF
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
    pos_of = {v.index: _to_pes(obj.matrix_world @ v.co) for v in mesh.vertices}
    nrm_of = {v.index: _to_pes((mat3 @ v.normal).normalized()) for v in mesh.vertices}
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
                    tan = _to_pes(loop.tangent)
                    try:
                        bin_ = _to_pes(loop.bitangent)
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
    mode = pgb2.MODES.index(obj.pgb2_mode)
    open(filepath, 'wb').write(pgb2.pack_body(
        {mi: tris for mi, tris in tri_by_mat.items() if tris},
        vert_data, mat_flags, mat_tex, mat_face, mode))
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


def menu_import(self, context):
    self.layout.operator(IMPORT_OT_pes12_bin.bl_idname, text='PES2012 BIN (.bin)')
    self.layout.operator(IMPORT_OT_pgb2.bl_idname, text='PGB2 body (.bin)')


def menu_export(self, context):
    self.layout.operator(EXPORT_OT_pes12_bin.bl_idname, text='PES2012 BIN (.bin)')
    self.layout.operator(EXPORT_OT_pgb2.bl_idname, text='PGB2 body (.bin)')


classes = (IMPORT_OT_pes12_bin, EXPORT_OT_pes12_bin, IMPORT_OT_pgb2, EXPORT_OT_pgb2)


def register():
    _ensure_props()
    for c in list(classes) + [IMPORT_OT_pes_ktmdl]:
        try:
            bpy.utils.register_class(c)
        except ValueError:
            pass  # vendored importer self-registers on Blender reload
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    for menu, fn in ((bpy.types.TOPBAR_MT_file_import, menu_import),
                     (bpy.types.TOPBAR_MT_file_export, menu_export)):
        try:
            menu.remove(fn)
        except Exception:
            pass
    for c in reversed([*classes, IMPORT_OT_pes_ktmdl]):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == '__main__':
    register()

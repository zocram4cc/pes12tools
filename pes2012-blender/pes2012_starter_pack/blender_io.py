"""Blender I/O: Model/Skeleton <-> objects (bpy only).

Conventions (fixed for the package):
- basis PES -> Blender: (x, y, z) -> (x, -z, y). The inverse on export.
- V: Blender UV V = 1 - PES V.
- one armature per KTMDL block, bones named by the skeleton (bone_NNN,
  body Fox names, face_NN); tails along local +Y like the old importer.
- vertex groups named by bone name; export resolves groups to node ids
  through the skeleton, then to each packet's palette slots.
- custom properties on the collection: pes12_img, pes12_entry,
  pes12_wesys (whether the source entry was WESYS-wrapped).
- per-object: pes12_block (block index), per-mesh: pes12_packet.
"""
import numpy as np

VID_ATTR = 'pes12_vid'   # per-vertex source vertex id (model.export_packet)
BASIS = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))   # Blender = B rows . PES col
BASIS_T = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))  # PES = B_T rows . Blender col


def to_blender(p):
    x, y, z = p
    return (x, -z, y)


def to_pes(p):
    x, y, z = p
    return (x, z, -y)


def _bind_to_mat(bind):
    """16 row-major floats (PES) -> Blender Matrix (row-major 4x4)."""
    import mathutils
    m = mathutils.Matrix([tuple(bind[r * 4:(r + 1) * 4]) for r in range(4)]).transposed()
    return mathutils.Matrix(BASIS).to_4x4() @ m @ mathutils.Matrix(BASIS_T).to_4x4()


def make_armature(name, skel):
    import bpy
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        for b in skel.bones:
            eb = data.edit_bones.new(b.name)
            mat = _bind_to_mat(b.bind)
            eb.head = mat.translation
            d = mat.to_3x3() @ __import__('mathutils').Vector((0, 1, 0))
            if d.length_squared <= 1e-20:
                d = __import__('mathutils').Vector((0, 1, 0))
            else:
                d.normalize()
            eb.tail = eb.head + d * 0.05
            try:
                eb.align_roll(mat.to_3x3() @ __import__('mathutils').Vector((0, 0, 1)))
            except Exception:
                pass
        for b in skel.bones:
            if b.parent_id >= 0 and b.parent_id in skel.by_id:
                data.edit_bones[b.name].parent = data.edit_bones[skel.by_id[b.parent_id].name]
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    return obj


def _decl_uvs(parsed_packet):
    out = []
    for e in parsed_packet['vertexDeclaration']:
        n = e.get('semanticsName', '')
        if isinstance(n, str) and n.startswith('TEXCOORD'):
            try:
                out.append(int(n[8:]))
            except ValueError:
                pass
    return sorted(set(out))


def _vert_influences(v, palette, node_names):
    bw = v.get('BLENDWEIGHT')
    bi = v.get('BLENDINDICES')
    if isinstance(bw, (int, float)):
        bw = [bw]
    # no BLENDWEIGHT = one influence: index 0 takes the whole residual
    # (face packets: BLENDINDICES only). Defaulting to [1.0] moved every
    # such vertex onto index 1.
    bw = list(bw or [])
    if not bi:            # unskinned (balls, stadium parts): no vertex groups
        return []
    bi = list(bi)
    res = 1.0 - float(np.sum(bw))
    out = []
    for slot, w in zip(bi, [res] + [float(x) for x in bw]):
        if w <= 1e-8 or slot < 0 or slot >= len(palette):
            continue
        out.append((node_names.get(int(palette[slot]), 'bone_%03d' % int(palette[slot])), w))
    return out


def make_mesh(name, parsed_packet, palette, skel, uv_flip=True):
    """One packet -> mesh object: one vertex per face corner, so UV seams
    and per-corner weights survive import exactly."""
    import bpy
    verts = parsed_packet['vertices']
    tris = parsed_packet['triangles']
    pos = [to_blender(verts[vi]['POSITION']) for t in tris for vi in t]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(pos, [], [(3 * i, 3 * i + 1, 3 * i + 2) for i in range(len(tris))])
    mesh.update()
    uvs = _decl_uvs(parsed_packet)
    uvdata = {ch: mesh.uv_layers.new(name='UV%d' % ch).data for ch in uvs}
    node_names = {i: skel.by_id[i].name for i in skel.by_id}
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    groups = {}
    for i, t in enumerate(tris):
        for j, vi in enumerate(t):
            li = 3 * i + j
            v = verts[vi]
            for ch in uvs:
                key = 'TEXCOORD%d' % ch
                if key in v:
                    u, vv = v[key]
                    uvdata[ch][li].uv = (u, 1.0 - vv) if uv_flip else (u, vv)
            for gname, w in _vert_influences(v, palette, node_names):
                if gname not in groups:   # (a VertexGroup is falsy: no `or` here)
                    groups[gname] = obj.vertex_groups.new(name=gname)
                groups[gname].add([li], w, 'ADD')
    vid = mesh.attributes.new(VID_ATTR, 'INT', 'POINT')
    vid.data.foreach_set('value', [vi for t in tris for vi in t])
    nrm = [to_blender(verts[vi].get('NORMAL', (0.0, 0.0, 1.0))) for t in tris for vi in t]
    for poly in mesh.polygons:
        poly.use_smooth = True
    if any(any(c for c in n) for n in nrm):   # stock stadium parts carry zero normals
        mesh.normals_split_custom_set(nrm)
    return obj


def parent_to_armature(obj, arm):
    mod = obj.modifiers.new(name='Armature', type='ARMATURE')
    mod.object = arm
    return mod


def read_corners(obj, skel):
    """Blender mesh -> (corners, tris) for model.export_packet: one corner
    per face corner, pos/nrm in PES space, uv as Blender stores it, w by
    node id, vid from the import tag (None on new geometry)."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    mw = obj.matrix_world
    mat3 = mw.to_3x3()
    node_of = {b.name: b.id for b in skel.bones}
    names = {g.index: g.name for g in obj.vertex_groups}
    vids = mesh.attributes.get(VID_ATTR)
    normals = mesh.corner_normals
    layers = []
    for layer in mesh.uv_layers:
        if layer.name[:2] == 'UV' and layer.name[2:].isdigit():
            layers.append((int(layer.name[2:]), layer.data))
    corners, tris = [], []
    for t in mesh.loop_triangles:
        tri = []
        for li in t.loops:
            vi = mesh.loops[li].vertex_index
            v = mesh.vertices[vi]
            w = {}
            for g in v.groups:
                nid = node_of.get(names.get(g.group))
                if nid is not None and g.weight > 0:
                    w[nid] = w.get(nid, 0.0) + g.weight
            corners.append(dict(
                vid=vids.data[vi].value if vids is not None else None,
                pos=to_pes(mw @ v.co),
                nrm=to_pes((mat3 @ normals[li].vector).normalized()),
                uv={ch: tuple(data[li].uv) for ch, data in layers},
                w=w,
                # zero-area faces cannot hold a custom normal: Blender shows a
                # fallback there, so the stock normal is not comparable
                flat=t.area == 0.0))
            tri.append(len(corners) - 1)
        tris.append(tuple(tri))
    return corners, tris


# --- textures ---------------------------------------------------------------

TEX_BLOCK_PROP = 'pes12_tex_block'   # image: index of its WE00 block in the BIN
TEX_HASH_PROP = 'pes12_tex_hash'     # image: digest of the pixels at import (unchanged -> block kept)


def _pixels_digest(px):
    import hashlib
    return hashlib.sha1(px.tobytes()).hexdigest()


def make_image(name, block_index, rgba):
    """Decoded WE00 pixels (HxWx4 uint8, rows top-down) -> packed Blender
    image (Blender rows run bottom-up)."""
    import bpy
    import numpy as np
    h, w = rgba.shape[:2]
    img = bpy.data.images.new(name, w, h, alpha=True)
    img.pixels.foreach_set((np.flipud(rgba).astype(np.float32) / 255.0).ravel())
    img.pack()
    img[TEX_BLOCK_PROP] = block_index
    img[TEX_HASH_PROP] = _pixels_digest(image_rgba(img))
    return img


def image_rgba(img):
    """Blender image -> HxWx4 uint8, rows top-down."""
    import numpy as np
    w, h = img.size
    f = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(f)
    return np.flipud(np.clip(np.rint(f.reshape(h, w, 4) * 255.0), 0, 255).astype(np.uint8))


def image_changed(img):
    return _pixels_digest(image_rgba(img)) != img.get(TEX_HASH_PROP)


def assign_image(obj, img, uv_name):
    """One material showing img through uv_name (preview + the export link)."""
    import bpy
    mat = bpy.data.materials.new(obj.name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image = img
    uv = nt.nodes.new('ShaderNodeUVMap')
    uv.uv_map = uv_name
    nt.links.new(uv.outputs['UV'], tex.inputs['Vector'])
    nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
    obj.data.materials.append(mat)
    return mat

# SPDX-License-Identifier: GPL-3.0-or-later
bl_info = {
    "name": "PES KTMDL Importer / Exporter",
    "author": "marqisspes6",
    "version": (1, 0, 0),
    "blender": (2, 80, 0),
    "location": "File > Import/Export > PES KTMDL (.ktmdl)",
    "description": "Import and topology-preserving export of standalone PES 2008-2013 KTMDL models",
    "warning": "Format is reverse engineered; export preserves the original binary layout",
    "doc_url": "",
    "category": "Import-Export",
}


import base64
import json
import os
import struct

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Vector

from . import ktmdl

AXIS_PES_TO_BLENDER = Matrix(
    (
        (1.0, 0.0, 0.0),
        (0.0, 0.0, -1.0),
        (0.0, 1.0, 0.0),
    )
)


def _safe_name(value, fallback):
    if not value:
        return fallback
    return str(value).replace("/", "_").replace("\\", "_")


def _source_stem(source_name):
    """Return the Blender base name for a standalone KTMDL source file."""
    return _safe_name(os.path.splitext(os.path.basename(source_name))[0], "KTMDL")


def _part_name(source_name, packet_index):
    """Canonical object name: <source filename without extension>_<part index>."""
    return "%s_%03d" % (_source_stem(source_name), int(packet_index))


def _set_prop(owner, key, value):
    try:
        if isinstance(value, bool):
            owner[key] = bool(value)
        elif isinstance(value, int):
            owner[key] = int(value)
        elif isinstance(value, float):
            owner[key] = float(value)
        elif isinstance(value, str):
            owner[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(v, (int, float, bool)) for v in value
        ):
            owner[key] = list(value)
        elif value is None:
            owner[key] = "null"
        else:
            owner[key] = json.dumps(value, separators=(",", ":"), sort_keys=True)
    except Exception:
        try:
            owner[key] = str(value)
        except Exception:
            pass


def _matrix_from_file(rows):
    # KTMDL uses row-vector matrix storage (translation in row 3).
    return Matrix(rows).transposed()


def _matrix_to_file(matrix):
    return matrix.transposed()


def _basis_matrix(mode):
    return (
        AXIS_PES_TO_BLENDER.copy() if mode == "PES_TO_BLENDER" else Matrix.Identity(3)
    )


def _transform_point(value, basis, scale):
    return tuple((basis @ Vector(value)) * scale)


def _inverse_point(value, basis, scale):
    v = Vector(value)
    if abs(scale) > 1.0e-20:
        v = v / scale
    return tuple(basis.inverted() @ v)


def _transform_vector(value, basis):
    v = basis @ Vector(value)
    if v.length_squared > 1.0e-20:
        v.normalize()
    return tuple(v)


def _inverse_vector(value, basis):
    v = basis.inverted() @ Vector(value)
    if v.length_squared > 1.0e-20:
        v.normalize()
    return tuple(v)


def _transform_matrix(file_matrix, basis, scale):
    m = _matrix_from_file(file_matrix)
    c = basis.to_4x4()
    converted = c @ m @ c.inverted()
    converted.translation = converted.translation * scale
    return converted


def _inverse_transform_matrix(blender_matrix, basis, scale):
    m = blender_matrix.copy()
    if abs(scale) > 1.0e-20:
        m.translation = m.translation / scale
    c = basis.to_4x4()
    return c.inverted() @ m @ c


def _score_winding(vertices, faces, normals):
    score, used = 0.0, 0
    for face in faces[:2048]:
        if len(face) != 3:
            continue
        a, b, c = face
        if max(face) >= len(vertices):
            continue
        cross = (Vector(vertices[b]) - Vector(vertices[a])).cross(
            Vector(vertices[c]) - Vector(vertices[a])
        )
        if cross.length_squared <= 1e-20:
            continue
        n = Vector(normals[a]) + Vector(normals[b]) + Vector(normals[c])
        if n.length_squared <= 1e-20:
            continue
        cross.normalize()
        n.normalize()
        score += cross.dot(n)
        used += 1
    return score / float(used) if used else 0.0


def _fix_winding(vertices, faces, normals):
    if not normals or len(normals) != len(vertices):
        return faces
    if _score_winding(vertices, faces, normals) >= 0.0:
        return faces
    out = []
    for f in faces:
        if len(f) == 3:
            out.append([f[0], f[2], f[1]])
        else:
            out.append(list(reversed(f)))
    return out


def _create_point_vector_attribute(mesh, name, values):
    attrs = getattr(mesh, "attributes", None)
    if attrs is None or not values:
        return False
    try:
        attr = attrs.get(name) or attrs.new(
            name=name, type="FLOAT_VECTOR", domain="POINT"
        )
        for i, v in enumerate(values):
            attr.data[i].vector = v
        return True
    except Exception:
        return False


def _create_point_float_attribute(mesh, name, values):
    attrs = getattr(mesh, "attributes", None)
    if attrs is None or not values:
        return False
    try:
        attr = attrs.get(name) or attrs.new(name=name, type="FLOAT", domain="POINT")
        for i, v in enumerate(values):
            attr.data[i].value = float(v)
        return True
    except Exception:
        return False


def _apply_custom_normals(mesh, normals):
    fn = getattr(mesh, "normals_split_custom_set_from_vertices", None)
    if fn is None or not normals or len(normals) != len(mesh.vertices):
        return False
    try:
        if hasattr(mesh, "use_auto_smooth"):
            mesh.use_auto_smooth = True
        fn(normals)
        return True
    except Exception:
        return False


def _make_uv_layers(mesh, packet, flip_v):
    verts = packet["vertices"]
    for channel in range(4):
        semantic = "TEXCOORD%d" % channel
        if not verts or semantic not in verts[0]:
            continue
        layer = mesh.uv_layers.new(name="UV%d" % channel)
        for loop in mesh.loops:
            uv = verts[loop.vertex_index].get(semantic)
            if not isinstance(uv, (list, tuple)) or len(uv) < 2:
                continue
            u, v = float(uv[0]), float(uv[1])
            if flip_v:
                v = 1.0 - v
            layer.data[loop.index].uv = (u, v)


def _normalize_influences(packet, vertex, implicit_mode="PRIMARY0"):
    indices = vertex.get("BLENDINDICES")
    if not isinstance(indices, (list, tuple)) or not indices:
        return []
    explicit = vertex.get("BLENDWEIGHT")
    if explicit is None:
        weights = [1.0]
    else:
        ex = (
            [float(explicit)]
            if isinstance(explicit, (int, float))
            else [float(v) for v in explicit]
        )
        residual = 1.0 - sum(ex)
        # Empirical player-body behaviour strongly favours PRIMARY0, while the
        # IDA-cross-checked template describes the residual as the final weight.
        # Keep both interpretations selectable and preserve raw weights in metadata.
        weights = (
            ([residual] + ex) if implicit_mode == "PRIMARY0" else (ex + [residual])
        )
    palette = packet.get("skeletonIndices", packet.get("bonePalette", []))
    merged = {}
    for i in range(min(len(indices), len(weights))):
        pi = int(indices[i])
        w = max(0.0, float(weights[i]))
        if w <= 1e-8 or pi < 0 or pi >= len(palette):
            continue
        node = int(palette[pi])
        merged[node] = merged.get(node, 0.0) + w
    total = sum(merged.values())
    if total <= 1e-20:
        return []
    return [(n, w / total) for n, w in merged.items()]


def _bone_name(bone):
    return "bone_%03d_%016X" % (bone["index"], bone["nameId"])


def _create_armature(context, collection, model, basis, scale):
    bones = model["bones"]
    if not bones:
        return None, {}
    name = _source_stem(model["sourceName"]) + "_Armature"
    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    collection.objects.link(arm_obj)
    if hasattr(arm_obj, "show_in_front"):
        arm_obj.show_in_front = True
    for obj in context.view_layer.objects:
        try:
            obj.select_set(False)
        except Exception:
            pass
    arm_obj.select_set(True)
    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    edit = {}
    for bone in bones:
        mat = _transform_matrix(bone["matrix"], basis, scale)
        eb = arm_data.edit_bones.new(_bone_name(bone))
        eb.head = mat.translation
        direction = mat.to_3x3() @ Vector((0, 1, 0))
        if direction.length_squared <= 1e-20:
            direction = Vector((0, 1, 0))
        else:
            direction.normalize()
        eb.tail = eb.head + direction * max(0.01 * scale, 0.0001)
        try:
            eb.align_roll(mat.to_3x3() @ Vector((0, 0, 1)))
        except Exception:
            pass
        edit[bone["index"]] = eb
    for bone in bones:
        if bone["parentNo"] in edit:
            edit[bone["index"]].parent = edit[bone["parentNo"]]
            edit[bone["index"]].use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    names = {}
    for src in bones:
        n = _bone_name(src)
        names[src["index"]] = n
        b = arm_data.bones.get(n)
        if not b:
            continue
        for k, v in (
            ("ktmdl_node_index", src["index"]),
            ("ktmdl_bone_index", src["index"]),
            ("ktmdl_name_id", src["nameId"]),
            ("ktmdl_parent_index", src["parentNo"]),
            ("ktmdl_pad0", src["pad0"]),
            ("ktmdl_pad1", src["pad1"]),
            ("ktmdl_bounds_min", src["boundMin"]),
            ("ktmdl_bounds_max", src["boundMax"]),
            ("ktmdl_matrixA", [x for row in src["matrix"] for x in row]),
            ("ktmdl_matrixB", [x for row in src["invertMatrix"] for x in row]),
            ("ktmdl_matrix", [x for row in src["matrix"] for x in row]),
            ("ktmdl_invert_matrix", [x for row in src["invertMatrix"] for x in row]),
        ):
            _set_prop(b, k, v)
    return arm_obj, names


def _assign_skinning(obj, packet, bone_names, implicit_mode):
    if not bone_names or not packet.get("skeletonIndices"):
        return 0
    groups = {}
    assigned = 0
    for vi, v in enumerate(packet["vertices"]):
        for node, w in _normalize_influences(packet, v, implicit_mode):
            bname = bone_names.get(node)
            if not bname:
                continue
            g = groups.get(node)
            if g is None:
                g = obj.vertex_groups.get(bname) or obj.vertex_groups.new(name=bname)
                groups[node] = g
            g.add([vi], w, "REPLACE")
            assigned += 1
    return assigned


TEXTURE_FILE_EXTENSIONS = (
    ".dds",
    ".png",
    ".tga",
    ".bmp",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".exr",
    ".hdr",
    ".webp",
    ".psd",
)


def _texture_basename(value):
    """Return a case-folded texture base name, deliberately ignoring the KTMDL extension.

    PES debug names often carry logical/game extensions such as .psd or .pic.  The
    sibling file used by Blender may instead be .dds/.png/.tga/etc., so extension
    matching is intentionally not part of texture resolution.
    """
    if not value:
        return ""
    name = os.path.basename(str(value).strip().replace("\\", "/"))
    return os.path.splitext(name)[0].casefold()


def _build_texture_file_index(ktmdl_filepath):
    """Index image files next to the imported KTMDL by basename (case-insensitive)."""
    folder = os.path.dirname(os.path.abspath(ktmdl_filepath))
    out = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    priority = {ext: i for i, ext in enumerate(TEXTURE_FILE_EXTENSIONS)}
    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(name)
        ext = ext.casefold()
        if ext not in priority:
            continue
        out.setdefault(stem.casefold(), []).append(path)
    for paths in out.values():
        paths.sort(
            key=lambda x: (
                priority.get(os.path.splitext(x)[1].casefold(), 999),
                os.path.basename(x).casefold(),
            )
        )
    return out


def _texture_id_name_candidates(model, tt):
    """Fallback basename candidates for archives that name textures by IDs/hashes."""
    candidates = []
    tex_index = int(tt.get("textureIndex", -1))
    if 0 <= tex_index < len(model.get("textureNameIds", [])):
        tex_id = model["textureNameIds"][tex_index]
        hi = int(tex_id.get("hi", 0))
        lo = int(tex_id.get("lo", 0))
        candidates.extend(
            (
                "%016x%016x" % (hi, lo),
                "%016x_%016x" % (hi, lo),
                "%016x-%016x" % (hi, lo),
                "%016x" % hi,
                "%016x" % lo,
            )
        )
    try:
        candidates.append("%016x" % int(tt.get("nameId", 0)))
    except Exception:
        pass
    return [c.casefold() for c in candidates if c and set(c) != {"0"}]


def _resolve_texture_file(model, tt, texture_file_index):
    """Resolve one KTMDL texture to a sibling image without trusting its extension."""
    candidates = []
    logical = _texture_basename(tt.get("textureName"))
    if logical:
        candidates.append(logical)
    candidates.extend(_texture_id_name_candidates(model, tt))
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        paths = texture_file_index.get(candidate)
        if paths:
            return paths[0], candidate
    return None, (candidates[0] if candidates else "")


def _load_texture_image(path):
    if not path:
        return None
    try:
        return bpy.data.images.load(path, check_existing=True)
    except Exception:
        return None


def _socket(node, *names):
    for name in names:
        try:
            found = node.inputs.get(name)
            if found is not None:
                return found
        except Exception:
            pass
    return None


def _first_node(nodes, bl_idname):
    for node in nodes:
        if getattr(node, "bl_idname", "") == bl_idname:
            return node
    return None


def _ensure_material_core(mat):
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    output = _first_node(nodes, "ShaderNodeOutputMaterial")
    if output is None:
        output = nodes.new("ShaderNodeOutputMaterial")
    output.name = "KTMDL_MaterialOutput"
    output.location = (760, 80)
    bsdf = _first_node(nodes, "ShaderNodeBsdfPrincipled")
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.name = "KTMDL_Principled"
    bsdf.location = (470, 80)
    surface = _socket(output, "Surface")
    if surface is not None:
        for link in list(surface.links):
            links.remove(link)
        try:
            links.new(bsdf.outputs.get("BSDF"), surface)
        except Exception:
            pass
    return bsdf, output


def _uv_node(nodes, uv_nodes, uv_no, packet):
    if uv_no in uv_nodes:
        return uv_nodes[uv_no]
    if uv_no < 0 or uv_no > 3:
        return None
    semantic = "TEXCOORD%d" % uv_no
    verts = packet.get("vertices", [])
    if not verts or semantic not in verts[0]:
        return None
    node = nodes.new("ShaderNodeUVMap")
    node.name = "KTMDL_UV%d" % uv_no
    node.label = "KTMDL UV%d" % uv_no
    node.uv_map = "UV%d" % uv_no
    node.location = (-900, 300 - 150 * uv_no)
    uv_nodes[uv_no] = node
    return node


def _set_non_color(image):
    if image is None:
        return
    try:
        image.colorspace_settings.name = "Non-Color"
    except Exception:
        pass


def _create_section_material(model, packet, texture_file_index):
    mi = packet["materialNo"]
    src = model["materials"][mi] if 0 <= mi < len(model["materials"]) else None
    base = _safe_name(src.get("name") if src else None, "material_%03d" % mi)
    mat = bpy.data.materials.new("%s__pkt_%03d" % (base, packet["index"]))
    mat.use_nodes = True
    _set_prop(mat, "ktmdl_packet_index", packet["index"])
    _set_prop(mat, "ktmdl_source_material_index", mi)
    if src:
        for k in (
            "nameId",
            "nameIdHex",
            "pad",
            "padHex",
            "nParam",
            "shaderId",
            "shaderIdHex",
            "pad2",
            "param",
            "shaderName",
        ):
            _set_prop(mat, "ktmdl_" + k, src.get(k))
    # Material param[8][4] has shader-specific semantics.  Do not reinterpret
    # param[0] as RGB; keep every vector as metadata until those shaders are RE'd.
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf, _output = _ensure_material_core(mat)
    uv_nodes = {}
    texture_nodes = []
    loaded = 0
    missing = []
    y = 300
    for ri, ref in enumerate(packet.get("activeTextureRefs", [])):
        ti = ref["id"]
        if ti < 0 or ti >= len(model["textureTypes"]):
            continue
        tt = model["textureTypes"][ti]
        tex_id = None
        tex_index = int(tt.get("textureIndex", -1))
        if 0 <= tex_index < len(model.get("textureNameIds", [])):
            tex_id = model["textureNameIds"][tex_index]
        node = nodes.new("ShaderNodeTexImage")
        node.name = "KTMDL_%s_%02d" % (tt.get("modeName", "TEXTURE"), ri)
        node.label = "%s | %s | UV%s | texParam %d" % (
            tt.get("textureName", "texture"),
            tt.get("modeName", "UNKNOWN"),
            ref["uvNo"],
            ref["texParam"],
        )
        node.location = (-560, y)
        y -= 220
        resolved, resolution_key = _resolve_texture_file(model, tt, texture_file_index)
        image = _load_texture_image(resolved)
        if image is not None:
            node.image = image
            loaded += 1
        else:
            missing.append(tt.get("textureName") or "texture_%03d" % tex_index)
        mode = int(tt.get("mode", -1))
        uv_no = int(ref.get("uvNo", 0))
        if mode in (1, 2, 3, 4):
            _set_non_color(image)
        uv = _uv_node(nodes, uv_nodes, uv_no, packet)
        if uv is not None:
            try:
                links.new(uv.outputs.get("UV"), node.inputs.get("Vector"))
            except Exception:
                pass
        for k, v in (
            ("ktmdl_texture_name", tt.get("textureName")),
            ("ktmdl_texture_type_index", ti),
            ("ktmdl_texture_index", tex_index),
            ("ktmdl_texture_type_name_id", tt.get("nameId")),
            ("ktmdl_texture_type_name_id_hex", tt.get("nameIdHex")),
            ("ktmdl_mode", mode),
            ("ktmdl_mode_name", tt.get("modeName")),
            ("ktmdl_uv_no", uv_no),
            ("ktmdl_tex_param", ref["texParam"]),
            ("ktmdl_addr_u", tt["addrU"]),
            ("ktmdl_addr_v", tt["addrV"]),
            ("ktmdl_filter_mag", tt["filterMag"]),
            ("ktmdl_filter_min", tt["filterMin"]),
            ("ktmdl_filter_mip", tt["filterMip"]),
            ("ktmdl_param", tt["param"]),
            ("ktmdl_resolved_texture_path", resolved or ""),
            ("ktmdl_texture_resolution_key", resolution_key),
            ("ktmdl_image_loaded", image is not None),
        ):
            _set_prop(node, k, v)
        if tex_id:
            for k in ("hi", "hiHex", "lo", "loHex", "rawHex"):
                _set_prop(node, "ktmdl_texture_id_" + k, tex_id.get(k))
        texture_nodes.append((node, mode, uv_no, ref, tt))

    base_input = _socket(bsdf, "Base Color")
    normal_input = _socket(bsdf, "Normal")
    spec_input = _socket(bsdf, "Specular IOR Level", "Specular")
    diffuse_nodes = [x for x in texture_nodes if x[1] == 0]
    normal_nodes = [x for x in texture_nodes if x[1] == 1]
    spec_nodes = [x for x in texture_nodes if x[1] == 2]
    ao_nodes = [x for x in texture_nodes if x[1] == 3]

    # First color/diffuse texture is the conservative Level-1 base.  Extra color
    # layers remain present but uncombined because texParam blend semantics are
    # still unknown and should not be invented.
    diffuse_output = None
    if diffuse_nodes:
        diffuse_output = diffuse_nodes[0][0].outputs.get("Color")
        _set_prop(diffuse_nodes[0][0], "ktmdl_link_status", "base_color")
        for extra in diffuse_nodes[1:]:
            _set_prop(
                extra[0], "ktmdl_link_status", "unlinked_extra_color_texparam_unknown"
            )

    # AO is an approximation: multiply the first AO texture with the first base
    # color texture.  It is deliberately only used when a diffuse source exists.
    if diffuse_output is not None and ao_nodes and base_input is not None:
        mix = nodes.new("ShaderNodeMixRGB")
        mix.name = "KTMDL_AO_Multiply"
        mix.label = "KTMDL AO x Base Color"
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = 1.0
        mix.location = (160, 250)
        try:
            links.new(diffuse_output, mix.inputs[1])
            links.new(ao_nodes[0][0].outputs.get("Color"), mix.inputs[2])
            links.new(mix.outputs.get("Color"), base_input)
        except Exception:
            pass
        _set_prop(ao_nodes[0][0], "ktmdl_link_status", "ao_multiply_approximation")
        for extra in ao_nodes[1:]:
            _set_prop(extra[0], "ktmdl_link_status", "unlinked_extra_ao")
    elif diffuse_output is not None and base_input is not None:
        try:
            links.new(diffuse_output, base_input)
        except Exception:
            pass

    if normal_nodes and normal_input is not None:
        normal = nodes.new("ShaderNodeNormalMap")
        normal.name = "KTMDL_NormalMap"
        normal.label = "KTMDL Normal Map"
        normal.location = (180, -120)
        try:
            links.new(
                normal_nodes[0][0].outputs.get("Color"), normal.inputs.get("Color")
            )
            links.new(normal.outputs.get("Normal"), normal_input)
        except Exception:
            pass
        _set_prop(normal_nodes[0][0], "ktmdl_link_status", "normal_map")
        for extra in normal_nodes[1:]:
            _set_prop(extra[0], "ktmdl_link_status", "unlinked_extra_normal")

    if spec_nodes and spec_input is not None:
        bw = nodes.new("ShaderNodeRGBToBW")
        bw.name = "KTMDL_SpecularToValue"
        bw.label = "KTMDL Specular (approx.)"
        bw.location = (180, -370)
        try:
            links.new(spec_nodes[0][0].outputs.get("Color"), bw.inputs.get("Color"))
            links.new(bw.outputs.get("Val"), spec_input)
        except Exception:
            pass
        _set_prop(spec_nodes[0][0], "ktmdl_link_status", "specular_approximation")
        for extra in spec_nodes[1:]:
            _set_prop(extra[0], "ktmdl_link_status", "unlinked_extra_specular")

    # Reflection and special/generated UV modes are intentionally preserved but
    # not guessed here.  They belong to the Level-2 shader reverse engineering.
    for node, mode, uv_no, _ref, _tt in texture_nodes:
        if not node.get("ktmdl_link_status"):
            if uv_no > 3:
                _set_prop(node, "ktmdl_link_status", "unlinked_special_uv_%d" % uv_no)
            elif mode == 4:
                _set_prop(
                    node, "ktmdl_link_status", "unlinked_reflection_semantics_unknown"
                )
            else:
                _set_prop(node, "ktmdl_link_status", "unlinked")
    _set_prop(mat, "ktmdl_loaded_texture_count", loaded)
    _set_prop(mat, "ktmdl_missing_texture_names", missing)
    return mat


def _create_bounds_collection(parent_collection, model, basis, scale):
    records = [("Group", r) for r in model["groups"]] + [
        ("Bounding", r) for r in model["bounding"]
    ]
    if not records:
        return None
    col = bpy.data.collections.new("KTMDL_Bounds")
    parent_collection.children.link(col)
    abs_basis = Matrix([[abs(basis[r][c]) for c in range(3)] for r in range(3)])
    for prefix, rec in records:
        vmin, vmax = Vector(rec["boundMin"]), Vector(rec["boundMax"])
        center = basis @ ((vmin + vmax) * 0.5)
        half = abs_basis @ ((vmax - vmin) * 0.5)
        e = bpy.data.objects.new("%s_%03d" % (prefix, rec["index"]), None)
        col.objects.link(e)
        e.empty_display_type = "CUBE"
        e.empty_display_size = 1.0
        e.location = center * scale
        e.scale = Vector(
            (
                max(abs(half.x) * scale, 1e-6),
                max(abs(half.y) * scale, 1e-6),
                max(abs(half.z) * scale, 1e-6),
            )
        )
        _set_prop(e, "ktmdl_collection_name", parent_collection.name)
        _set_prop(e, "ktmdl_record_type", prefix)
        _set_prop(e, "ktmdl_record_index", rec["index"])
        for k, v in rec.items():
            if k not in ("index", "offset"):
                _set_prop(e, "ktmdl_" + k, v)
    try:
        col.hide_viewport = True
    except Exception:
        pass
    return col


def _create_locators(parent_collection, model, basis, scale, arm_obj, bone_names):
    if not model["locators"]:
        return None
    col = bpy.data.collections.new("KTMDL_Locators")
    parent_collection.children.link(col)
    for loc in model["locators"]:
        obj = bpy.data.objects.new(
            "locator_%03d_%016X" % (loc["index"], loc["nameId"]), None
        )
        col.objects.link(obj)
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = max(0.03 * scale, 0.001)
        obj.matrix_world = _transform_matrix(loc["matrix"], basis, scale)
        _set_prop(obj, "ktmdl_collection_name", parent_collection.name)
        _set_prop(obj, "ktmdl_locator_index", loc["index"])
        _set_prop(obj, "ktmdl_name_id", loc["nameId"])
        _set_prop(obj, "ktmdl_parent_no", loc["parentNo"])
        _set_prop(obj, "ktmdl_pad", loc["pad"])
        _set_prop(obj, "ktmdl_matrix", [x for row in loc["matrix"] for x in row])
        if arm_obj is not None and loc["parentNo"] in bone_names:
            try:
                world = obj.matrix_world.copy()
                obj.parent = arm_obj
                obj.parent_type = "BONE"
                obj.parent_bone = bone_names[loc["parentNo"]]
                obj.matrix_world = world
            except Exception:
                pass
    return col


def _create_metadata_text(model, stem):
    name = stem + ".ktmdl_metadata.json"
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(json.dumps(model, indent=2, sort_keys=True))
    return text


def _create_source_text(filepath, stem):
    raw = open(filepath, "rb").read()
    name = stem + ".ktmdl_source.b64"
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(base64.b64encode(raw).decode("ascii"))
    return text


def _store_collection_metadata(
    collection,
    model,
    metadata_text,
    source_text,
    filepath,
    coordinate_mode,
    scale,
    flip_v,
    implicit_mode,
):
    _set_prop(collection, "ktmdl_source", model["sourceName"])
    _set_prop(collection, "ktmdl_source_path", os.path.abspath(filepath))
    _set_prop(collection, "ktmdl_endian", model["endian"])
    _set_prop(
        collection, "ktmdl_metadata_text", metadata_text.name if metadata_text else ""
    )
    _set_prop(
        collection, "ktmdl_source_data_text", source_text.name if source_text else ""
    )
    _set_prop(collection, "ktmdl_coordinate_mode", coordinate_mode)
    _set_prop(collection, "ktmdl_import_scale", float(scale))
    _set_prop(collection, "ktmdl_flip_v", bool(flip_v))
    _set_prop(collection, "ktmdl_implicit_weight_mode", implicit_mode)
    _set_prop(collection, "ktmdl_importer_version", "1.0.0")
    for k, v in model["header"].items():
        if k != "id":
            _set_prop(collection, "header_" + k, v)
    _set_prop(collection, "texture_names", model["debugInfo"].get("textureNames", []))
    _set_prop(collection, "shader_names", model["debugInfo"].get("shaderNames", []))


def _packet_custom_props(obj, packet, collection_name, index_stream_index=0):
    _set_prop(obj, "ktmdl_collection_name", collection_name)
    _set_prop(obj, "ktmdl_packet_index", packet["index"])
    _set_prop(obj, "ktmdl_part_index", packet["index"])
    _set_prop(obj, "ktmdl_part_name", obj.name)
    _set_prop(obj, "ktmdl_index_stream_index", index_stream_index)
    for k in (
        "flag",
        "primType",
        "primTypeName",
        "materialNo",
        "groupNo",
        "blockNo",
        "streamInfoCount",
        "streamInfoOffset",
        "skeletonCount",
        "skeletonOffset",
        "indexCount",
        "indexInfoOffset",
        "extraOffset",
        "nTexture",
        "offset3",
        "r",
        "skeletonIndices",
    ):
        _set_prop(obj, "ktmdl_" + k, packet.get(k))
    _set_prop(obj, "ktmdl_texture_refs", packet.get("tex", []))
    _set_prop(obj, "ktmdl_vertex_streams", packet.get("vertexStreams", []))
    _set_prop(obj, "ktmdl_index_streams", packet.get("indexStreams", []))


def _create_mesh_object(
    context,
    collection,
    model,
    packet,
    basis,
    scale,
    flip_v,
    normals_enabled,
    arm_obj,
    bone_names,
    implicit_mode,
    texture_file_index,
):
    verts = packet["vertices"]
    positions_raw = [v.get("POSITION") for v in verts]
    if not positions_raw or any(p is None for p in positions_raw):
        raise ktmdl.KTMDLError(
            "Packet %d has no decodable POSITION stream" % packet["index"]
        )
    positions = [_transform_point(p, basis, scale) for p in positions_raw]
    normals = (
        [_transform_vector(v["NORMAL"], basis) for v in verts]
        if verts and "NORMAL" in verts[0]
        else []
    )
    faces = [f for f in packet.get("faces", []) if f and max(f) < len(positions)]
    edges = [e for e in packet.get("edges", []) if e and max(e) < len(positions)]
    faces = _fix_winding(positions, faces, normals)
    name = _part_name(model["sourceName"], packet["index"])
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(positions, edges, faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    _packet_custom_props(obj, packet, collection.name, 0)
    _make_uv_layers(mesh, packet, flip_v)
    if normals and normals_enabled:
        _apply_custom_normals(mesh, normals)
    if verts and "TANGENT" in verts[0]:
        _create_point_vector_attribute(
            mesh, "KT_TANGENT", [_transform_vector(v["TANGENT"], basis) for v in verts]
        )
    if verts and "BINORMAL" in verts[0]:
        _create_point_vector_attribute(
            mesh,
            "KT_BINORMAL",
            [_transform_vector(v["BINORMAL"], basis) for v in verts],
        )
    if verts and "BLENDWEIGHT" in verts[0]:
        sample = verts[0]["BLENDWEIGHT"]
        if isinstance(sample, (int, float)):
            _create_point_float_attribute(
                mesh, "KT_WEIGHT0", [v["BLENDWEIGHT"] for v in verts]
            )
        elif isinstance(sample, (list, tuple)):
            for c in range(len(sample)):
                _create_point_float_attribute(
                    mesh, "KT_WEIGHT%d" % c, [v["BLENDWEIGHT"][c] for v in verts]
                )
    mat = _create_section_material(model, packet, texture_file_index)
    mesh.materials.append(mat)
    assigned = _assign_skinning(obj, packet, bone_names, implicit_mode)
    _set_prop(obj, "ktmdl_skin_assignments", assigned)
    if arm_obj is not None and assigned > 0:
        mod = obj.modifiers.new(name="KTMDL Armature", type="ARMATURE")
        mod.object = arm_obj
        obj.parent = arm_obj
    return obj


def import_ktmdl(
    context,
    filepath,
    coordinate_mode="PES_TO_BLENDER",
    scale=1.0,
    flip_v=False,
    create_armature=True,
    import_bounds=True,
    import_locators=True,
    store_metadata=True,
    embed_source=True,
    use_custom_normals=True,
    implicit_weight_mode="PRIMARY0",
):
    model = ktmdl.parse_file(filepath)
    stem = _source_stem(filepath)
    collection = bpy.data.collections.new(stem)
    context.scene.collection.children.link(collection)
    basis = _basis_matrix(coordinate_mode)
    texture_file_index = _build_texture_file_index(filepath)
    arm_obj, bone_names = (None, {})
    if create_armature and model["bones"]:
        arm_obj, bone_names = _create_armature(context, collection, model, basis, scale)
        if arm_obj:
            for k, v in (
                ("ktmdl_collection_name", collection.name),
                ("ktmdl_coordinate_mode", coordinate_mode),
                ("ktmdl_import_scale", float(scale)),
                ("ktmdl_implicit_weight_mode", implicit_weight_mode),
                ("ktmdl_importer_version", "1.2.0"),
            ):
                _set_prop(arm_obj, k, v)
    objects = []
    for packet in model["packets"]:
        objects.append(
            _create_mesh_object(
                context,
                collection,
                model,
                packet,
                basis,
                scale,
                flip_v,
                use_custom_normals,
                arm_obj,
                bone_names,
                implicit_weight_mode,
                texture_file_index,
            )
        )
    if import_bounds:
        _create_bounds_collection(collection, model, basis, scale)
    if import_locators:
        _create_locators(collection, model, basis, scale, arm_obj, bone_names)
    metadata = _create_metadata_text(model, stem) if store_metadata else None
    source = _create_source_text(filepath, stem) if embed_source else None
    _store_collection_metadata(
        collection,
        model,
        metadata,
        source,
        filepath,
        coordinate_mode,
        scale,
        flip_v,
        implicit_weight_mode,
    )
    if objects:
        for o in context.selected_objects:
            try:
                o.select_set(False)
            except Exception:
                pass
        for o in objects:
            o.select_set(True)
        context.view_layer.objects.active = objects[0]
    return model, collection, objects


# ---------------------------------------------------------------------------
# Topology-preserving exporter
# ---------------------------------------------------------------------------


def _find_ktmdl_collection(context):
    active = context.view_layer.objects.active
    preferred = None
    if active:
        cname = active.get("ktmdl_collection_name")
        if cname and bpy.data.collections.get(cname):
            preferred = bpy.data.collections.get(cname)
    if preferred:
        return preferred
    for col in bpy.data.collections:
        if col.get("ktmdl_source_data_text"):
            if active and col.objects.get(active.name):
                return col
    candidates = [c for c in bpy.data.collections if c.get("ktmdl_source_data_text")]
    if len(candidates) == 1:
        return candidates[0]
    raise ktmdl.KTMDLError(
        "Select an object/armature from the KTMDL you want to export"
    )


def _source_bytes_from_collection(col):
    tname = col.get("ktmdl_source_data_text")
    if tname and bpy.data.texts.get(tname):
        try:
            return base64.b64decode(bpy.data.texts[tname].as_string().encode("ascii"))
        except Exception:
            pass
    path = col.get("ktmdl_source_path")
    if path and os.path.isfile(path):
        return open(path, "rb").read()
    raise ktmdl.KTMDLError(
        "Original KTMDL bytes are unavailable. Reimport with 'Embed Original Binary' enabled"
    )


def _find_packet_object(col, packet_index):
    for obj in col.objects:
        try:
            if (
                obj.type == "MESH"
                and int(obj.get("ktmdl_packet_index", -1)) == packet_index
            ):
                return obj
        except Exception:
            pass
    return None


def _armature_for_collection(col):
    for obj in col.objects:
        if obj.type == "ARMATURE" and obj.get("ktmdl_collection_name") == col.name:
            return obj
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.get("ktmdl_collection_name") == col.name:
            return obj
    return None


def _uv_per_vertex(mesh, layer_name, flip_v):
    layer = mesh.uv_layers.get(layer_name)
    out = [None] * len(mesh.vertices)
    if not layer:
        return out
    for loop in mesh.loops:
        vi = loop.vertex_index
        if out[vi] is None:
            uv = layer.data[loop.index].uv
            out[vi] = (float(uv.x), 1.0 - float(uv.y) if flip_v else float(uv.y))
    return out


def _attribute_vector(mesh, name, index):
    attrs = getattr(mesh, "attributes", None)
    if attrs is None:
        return None
    a = attrs.get(name)
    if a is None or index >= len(a.data):
        return None
    try:
        return tuple(a.data[index].vector)
    except Exception:
        return None


def _bone_index_map(arm):
    out = {}
    if not arm:
        return out
    for b in arm.data.bones:
        try:
            out[b.name] = int(b.get("ktmdl_node_index"))
        except Exception:
            pass
    return out


def _vertex_group_influences(obj, vi, arm):
    bmap = _bone_index_map(arm)
    out = []
    for gref in obj.data.vertices[vi].groups:
        if gref.group >= len(obj.vertex_groups):
            continue
        g = obj.vertex_groups[gref.group]
        node = bmap.get(g.name)
        if node is not None and gref.weight > 1e-8:
            out.append((node, float(gref.weight)))
    s = sum(w for _, w in out)
    return [(n, w / s) for n, w in out] if s > 1e-20 else []


def _encode_skin(packet, obj, vi, explicit_count, arm, implicit_mode):
    pal = packet.get("skeletonIndices", [])
    reverse = {int(n): i for i, n in enumerate(pal)}
    inf = [(n, w) for n, w in _vertex_group_influences(obj, vi, arm) if n in reverse]
    if not inf:
        return None, None
    inf.sort(key=lambda x: x[1], reverse=True)
    inf = inf[: explicit_count + 1]
    while len(inf) < explicit_count + 1:
        inf.append((inf[-1][0] if inf else pal[0], 0.0))
    total = sum(w for _, w in inf)
    if total <= 1e-20:
        return None, None
    inf = [(n, w / total) for n, w in inf]
    if implicit_mode == "PRIMARY0":
        ordered = inf
        explicit = [w for _, w in ordered[1 : explicit_count + 1]]
    else:
        # Put the strongest influence in the implicit final slot.
        ordered = inf[1 : explicit_count + 1] + [inf[0]]
        explicit = [w for _, w in ordered[:explicit_count]]
    indices = [reverse[n] for n, _ in ordered]
    while len(indices) < 4:
        indices.append(indices[-1] if indices else 0)
    return indices[:4], explicit


def _pack_element(buf, endian, base, elem, value):
    if value is None:
        return
    off = base + elem["offset"]
    fmt = elem["format"]
    if fmt == 0x00:
        struct.pack_into(endian + "f", buf, off, float(value))
    elif fmt == 0x01:
        struct.pack_into(endian + "2f", buf, off, float(value[0]), float(value[1]))
    elif fmt == 0x02:
        struct.pack_into(
            endian + "3f", buf, off, float(value[0]), float(value[1]), float(value[2])
        )
    elif fmt == 0x0B:
        buf[off : off + 4] = bytes([max(0, min(255, int(x))) for x in value[:4]])


def _update_mesh_streams(buf, model, col, basis, scale, flip_v, implicit_mode):
    endian = ">" if model["endian"] == "big" else "<"
    arm = _armature_for_collection(col)
    for packet in model["packets"]:
        obj = _find_packet_object(col, packet["index"])
        if not obj:
            continue
        mesh = obj.data
        if len(mesh.vertices) != len(packet["vertices"]):
            raise ktmdl.KTMDLError(
                "Packet %d vertex count changed (%d -> %d); exporter is topology-preserving"
                % (packet["index"], len(packet["vertices"]), len(mesh.vertices))
            )
        uv_cache = {c: _uv_per_vertex(mesh, "UV%d" % c, flip_v) for c in range(4)}
        for stream in packet.get("vertexStreams", []):
            info = stream["info"]
            if info["type"] != ktmdl.STREAM_VERTEX:
                continue
            data_base = info["offset"] + info["streamOffset"]
            stride = info["stride"]
            decl = stream["declaration"]
            weight_elem = next((e for e in decl if e["semantics"] == 0x20), None)
            index_elem = next((e for e in decl if e["semantics"] == 0x21), None)
            explicit_count = (
                {0x00: 1, 0x01: 2, 0x02: 3}.get(weight_elem["format"], 0)
                if weight_elem
                else 0
            )
            for vi in range(info["numVertex"]):
                base = data_base + vi * stride
                skin_indices, skin_weights = (None, None)
                if index_elem and explicit_count:
                    skin_indices, skin_weights = _encode_skin(
                        packet, obj, vi, explicit_count, arm, implicit_mode
                    )
                for elem in decl:
                    sem = elem["semantics"]
                    val = None
                    if sem == 0x10:
                        val = _inverse_point(mesh.vertices[vi].co, basis, scale)
                    elif sem == 0x12:
                        val = _inverse_vector(mesh.vertices[vi].normal, basis)
                    elif sem in (0x16, 0x17, 0x18, 0x19):
                        val = uv_cache[sem - 0x16][vi]
                    elif sem == 0x1E:
                        x = _attribute_vector(mesh, "KT_TANGENT", vi)
                        val = _inverse_vector(x, basis) if x else None
                    elif sem == 0x1F:
                        x = _attribute_vector(mesh, "KT_BINORMAL", vi)
                        val = _inverse_vector(x, basis) if x else None
                    elif sem == 0x20 and skin_weights is not None:
                        val = (
                            skin_weights[0] if elem["format"] == 0x00 else skin_weights
                        )
                    elif sem == 0x21 and skin_indices is not None:
                        val = skin_indices
                    _pack_element(buf, endian, base, elem, val)


def _update_bones(buf, model, col, basis, scale):
    arm = _armature_for_collection(col)
    if not arm:
        return
    endian = ">" if model["endian"] == "big" else "<"
    by_index = {}
    for b in arm.data.bones:
        try:
            by_index[int(b.get("ktmdl_node_index"))] = b
        except Exception:
            pass
    for src in model["bones"]:
        b = by_index.get(src["index"])
        if not b:
            continue
        file_col = _inverse_transform_matrix(b.matrix_local, basis, scale)
        file_rows = _matrix_to_file(file_col)
        inv_rows = _matrix_to_file(file_col.inverted_safe())
        flat = [x for row in file_rows for x in row]
        inv = [x for row in inv_rows for x in row]
        struct.pack_into(endian + "16f", buf, src["offset"] + 0x10, *flat)
        struct.pack_into(endian + "16f", buf, src["offset"] + 0x50, *inv)


def _update_locators(buf, model, col, basis, scale):
    endian = ">" if model["endian"] == "big" else "<"
    for loc in model["locators"]:
        obj = None
        for candidate in bpy.data.objects:
            try:
                if (
                    int(candidate.get("ktmdl_locator_index", -1)) == loc["index"]
                    and candidate.get("ktmdl_collection_name", col.name) == col.name
                ):
                    obj = candidate
                    break
            except Exception:
                pass
        if not obj:
            continue
        file_col = _inverse_transform_matrix(obj.matrix_world, basis, scale)
        rows = _matrix_to_file(file_col)
        struct.pack_into(
            endian + "16f", buf, loc["offset"] + 0x10, *[x for row in rows for x in row]
        )


def _update_bounds(buf, model, col, basis, scale):
    endian = ">" if model["endian"] == "big" else "<"
    inv_basis = basis.inverted()
    records = {"Group": model["groups"], "Bounding": model["bounding"]}
    for obj in bpy.data.objects:
        if obj.get("ktmdl_collection_name") != col.name:
            continue
        kind = obj.get("ktmdl_record_type")
        if kind not in records:
            continue
        try:
            idx = int(obj.get("ktmdl_record_index", -1))
        except Exception:
            continue
        if idx < 0 or idx >= len(records[kind]):
            continue
        rec = records[kind][idx]
        center = inv_basis @ (
            obj.location / scale if abs(scale) > 1e-20 else obj.location
        )
        # These helpers are axis-aligned boxes; transform absolute extents back
        # through the orthonormal axis permutation.
        half_b = Vector((abs(obj.scale.x), abs(obj.scale.y), abs(obj.scale.z)))
        if abs(scale) > 1e-20:
            half_b = half_b / scale
        abs_inv = Matrix([[abs(inv_basis[r][c]) for c in range(3)] for r in range(3)])
        half = abs_inv @ half_b
        vmin = center - half
        vmax = center + half
        struct.pack_into(endian + "3f", buf, rec["offset"] + 0x10, *vmax)
        struct.pack_into(endian + "3f", buf, rec["offset"] + 0x20, *vmin)


def export_ktmdl(
    context, filepath, update_skeleton=True, update_locators=True, update_bounds=True
):
    col = _find_ktmdl_collection(context)
    raw = _source_bytes_from_collection(col)
    model = ktmdl.parse_bytes(raw, col.get("ktmdl_source", "source.ktmdl"))
    buf = bytearray(raw)
    mode = col.get("ktmdl_coordinate_mode", "PES_TO_BLENDER")
    scale = float(col.get("ktmdl_import_scale", 1.0))
    flip_v = bool(col.get("ktmdl_flip_v", False))
    implicit = col.get("ktmdl_implicit_weight_mode", "PRIMARY0")
    basis = _basis_matrix(mode)
    _update_mesh_streams(buf, model, col, basis, scale, flip_v, implicit)
    if update_skeleton:
        _update_bones(buf, model, col, basis, scale)
    if update_locators:
        _update_locators(buf, model, col, basis, scale)
    if update_bounds:
        _update_bounds(buf, model, col, basis, scale)
    # Re-parse before writing so obvious out-of-range corruption is caught.
    ktmdl.parse_bytes(bytes(buf), os.path.basename(filepath))
    open(filepath, "wb").write(buf)
    return model, col


class IMPORT_OT_pes_ktmdl(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.pes_ktmdl"
    bl_label = "Import PES KTMDL"
    bl_options = {"UNDO", "PRESET"}
    filename_ext = ".ktmdl"
    filter_glob: StringProperty(default="*.ktmdl", options={"HIDDEN"})
    coordinate_mode: EnumProperty(
        name="Coordinates",
        items=(
            ("PES_TO_BLENDER", "PES to Blender", "Convert (X,Y,Z) to (X,-Z,Y)"),
            ("RAW", "Raw KTMDL", "Keep file coordinates"),
        ),
        default="PES_TO_BLENDER",
    )
    scale: FloatProperty(name="Scale", default=1.0, min=0.000001, soft_max=100.0)
    flip_v: BoolProperty(name="Flip UV V", default=False)
    create_armature: BoolProperty(name="Create Armature", default=True)
    use_custom_normals: BoolProperty(name="Import Normals", default=True)
    import_bounds: BoolProperty(name="Import Groups / Bounds", default=True)
    import_locators: BoolProperty(name="Import Locators", default=True)
    implicit_weight_mode: EnumProperty(
        name="Implicit Skin Weight",
        description="Empirical player models favour slot 0; IDA template terminology describes a final residual",
        items=(
            (
                "PRIMARY0",
                "Primary / slot 0 (recommended)",
                "Residual weight is BLENDINDICES slot 0",
            ),
            ("LAST", "Final slot", "Residual weight follows the explicit weights"),
        ),
        default="PRIMARY0",
    )
    store_metadata: BoolProperty(name="Store Full Metadata", default=True)
    embed_source: BoolProperty(
        name="Embed Original Binary",
        description="Required for safe round-trip export without depending on the original file path",
        default=True,
    )

    def execute(self, context):
        try:
            model, _c, objs = import_ktmdl(
                context,
                self.filepath,
                self.coordinate_mode,
                self.scale,
                self.flip_v,
                self.create_armature,
                self.import_bounds,
                self.import_locators,
                self.store_metadata,
                self.embed_source,
                self.use_custom_normals,
                self.implicit_weight_mode,
            )
        except ktmdl.KTMDLError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, "KTMDL import failed: %s" % e)
            raise
        self.report(
            {"INFO"},
            "Imported %d part(s), %d bone(s), %d locator(s), %s-endian"
            % (len(objs), len(model["bones"]), len(model["locators"]), model["endian"]),
        )
        return {"FINISHED"}


class EXPORT_OT_pes_ktmdl(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.pes_ktmdl"
    bl_label = "Export PES KTMDL"
    bl_options = {"PRESET"}
    filename_ext = ".ktmdl"
    filter_glob: StringProperty(default="*.ktmdl", options={"HIDDEN"})
    update_skeleton: BoolProperty(
        name="Export Rest Skeleton",
        description="Write the current Blender armature rest matrices back into KTMDL",
        default=True,
    )
    update_locators: BoolProperty(name="Export Locators", default=True)
    update_bounds: BoolProperty(name="Export Groups / Bounds", default=True)

    def execute(self, context):
        try:
            model, col = export_ktmdl(
                context,
                self.filepath,
                self.update_skeleton,
                self.update_locators,
                self.update_bounds,
            )
        except ktmdl.KTMDLError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, "KTMDL export failed: %s" % e)
            raise
        self.report(
            {"INFO"},
            "Exported topology-preserving KTMDL from %s (%d packets)"
            % (col.name, len(model["packets"])),
        )
        return {"FINISHED"}


def menu_import(self, context):
    self.layout.operator(IMPORT_OT_pes_ktmdl.bl_idname, text="PES KTMDL (.ktmdl)")


def menu_export(self, context):
    self.layout.operator(EXPORT_OT_pes_ktmdl.bl_idname, text="PES KTMDL (.ktmdl)")


classes = (IMPORT_OT_pes_ktmdl, EXPORT_OT_pes_ktmdl)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    for menu, fn in (
        (bpy.types.TOPBAR_MT_file_import, menu_import),
        (bpy.types.TOPBAR_MT_file_export, menu_export),
    ):
        try:
            menu.remove(fn)
        except Exception:
            pass
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()

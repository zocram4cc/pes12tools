# SPDX-License-Identifier: GPL-3.0-or-later
"""PES 2008-2013 standalone KTMDL parser.

This parser follows the IDA-cross-checked structure names in
PES_KTMDL.bt.  It is Blender-independent so it can be validated
with ordinary CPython.
"""

from __future__ import print_function

import os
import struct

MAGIC = b"KTMDL\x00\x00\x00"
HEADER_SIZE = 0xC0

SEMANTIC_NAMES = {
    0x10: "POSITION",
    0x12: "NORMAL",
    0x16: "TEXCOORD0",
    0x17: "TEXCOORD1",
    0x18: "TEXCOORD2",
    0x19: "TEXCOORD3",
    0x1E: "TANGENT",
    0x1F: "BINORMAL",
    0x20: "BLENDWEIGHT",
    0x21: "BLENDINDICES",
}

TYPE_NAMES = {
    0x00: "FLOAT1",
    0x01: "FLOAT2",
    0x02: "FLOAT3",
    0x0B: "UBYTE4",
}
TYPE_SIZES = {0x00: 4, 0x01: 8, 0x02: 12, 0x0B: 4}

TEXTURE_MODE_NAMES = {
    0: "COLOR_OR_DIFFUSE",
    1: "NORMAL",
    2: "SPECULAR",
    3: "OCCLUSION",
    4: "REFLECTION",
}

PRIMITIVE_NAMES = {
    0: "TRIANGLESTRIP",
    1: "TRIANGLELIST",
    2: "TRIANGLEFAN",
    3: "LINELIST",
    4: "LINESTRIP",
    5: "POINT",
    6: "QUADLIST",
    7: "QUADSTRIP",
    8: "RECTLIST",
}

STREAM_VERTEX = 0
STREAM_INDEX = 1

HEADER_FIELDS = [
    ("majorVersion", 0x08, "I"),
    ("minorVersion", 0x0C, "I"),
    ("endianMarker", 0x10, "2s"),
    ("configNo", 0x12, "B"),
    ("pad", 0x13, "B"),
    ("flag", 0x14, "I"),
    ("boneCount", 0x18, "I"),
    ("boneOffset", 0x1C, "i"),
    ("skeletonIndicesCount", 0x20, "I"),
    ("skeletonIndicesOffset", 0x24, "i"),
    ("packetCount", 0x28, "I"),
    ("packetOffset", 0x2C, "i"),
    ("streamInfoCount", 0x30, "I"),
    ("streamInfoOffset", 0x34, "i"),
    ("locatorCount", 0x38, "I"),
    ("locatorOffset", 0x3C, "i"),
    ("groupCount", 0x40, "I"),
    ("groupOffset", 0x44, "i"),
    ("materialCount", 0x48, "I"),
    ("materialOffset", 0x4C, "i"),
    ("textureTypeCount", 0x50, "I"),
    ("textureTypeOffset", 0x54, "i"),
    ("boundingCount", 0x58, "I"),
    ("boundingOffset", 0x5C, "i"),
    ("debugInfoOffset", 0x60, "i"),
    ("extraOffset", 0x64, "i"),
    ("morphNameIdCount", 0x68, "I"),
    ("morphNameIdOffset", 0x6C, "i"),
    ("morphIndexOffset", 0x70, "i"),
    ("streamDataSize", 0x74, "I"),
    ("streamDataOffset", 0x78, "i"),
    ("textureNameIdCount", 0x7C, "I"),
    ("textureNameIdOffset", 0x80, "i"),
    ("morphIndexCount", 0x84, "I"),
    ("vertexElementCount", 0x88, "I"),
    ("vertexElementOffset", 0x8C, "i"),
    ("size", 0x90, "I"),
    ("extraCount", 0x94, "I"),
]


class KTMDLError(Exception):
    pass


class Reader(object):
    def __init__(self, data, endian):
        self.data = data
        self.endian = endian
        self.size = len(data)

    def check(self, offset, size=1):
        if offset < 0 or size < 0 or offset + size > self.size:
            raise KTMDLError(
                "Read outside KTMDL: offset=0x%X size=0x%X file=0x%X"
                % (offset, size, self.size)
            )

    _check = check

    def unpack(self, fmt, offset):
        full = self.endian + fmt
        size = struct.calcsize(full)
        self.check(offset, size)
        return struct.unpack_from(full, self.data, offset)

    def u8(self, offset):
        self.check(offset, 1)
        return self.data[offset]

    def i8(self, offset):
        return self.unpack("b", offset)[0]

    def u16(self, offset):
        return self.unpack("H", offset)[0]

    def i16(self, offset):
        return self.unpack("h", offset)[0]

    def u32(self, offset):
        return self.unpack("I", offset)[0]

    def i32(self, offset):
        return self.unpack("i", offset)[0]

    def u64(self, offset):
        return self.unpack("Q", offset)[0]

    def f32(self, offset):
        return self.unpack("f", offset)[0]

    def bytes(self, offset, size):
        self.check(offset, size)
        return self.data[offset : offset + size]

    def cstring(self, offset):
        self.check(offset, 1)
        end = self.data.find(b"\x00", offset)
        if end < 0:
            end = self.size
        raw = self.data[offset:end]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", "replace")


def _detect_endian(data):
    if len(data) < HEADER_SIZE:
        raise KTMDLError("File is smaller than the 0xC0-byte KTMDL header")
    if data[:8] != MAGIC:
        raise KTMDLError("Not a standalone KTMDL: file must begin with KTMDL\\0\\0\\0")
    marker = data[0x10:0x12]
    if marker == b"\x01\x00":
        return "<"
    if marker == b"\x00\x01":
        return ">"
    # Unknown variant: match header size against actual file.
    le = struct.unpack_from("<I", data, 0x90)[0]
    be = struct.unpack_from(">I", data, 0x90)[0]
    if be == len(data) and le != len(data):
        return ">"
    return "<"


def _matrix4(reader, offset):
    values = reader.unpack("16f", offset)
    return [list(values[i : i + 4]) for i in range(0, 16, 4)]


def _vec(reader, offset, count):
    return list(reader.unpack("%df" % count, offset))


def _hex(reader, start, end):
    if start < 0 or end <= start or start >= reader.size:
        return ""
    end = min(end, reader.size)
    return reader.bytes(start, end - start).hex()


def _parse_header(reader):
    out = {"id": MAGIC.decode("ascii", "ignore")}
    for name, off, fmt in HEADER_FIELDS:
        value = reader.unpack(fmt, off)[0]
        if name == "endianMarker":
            value = value.hex()
        out[name] = value
    out["system_reserve"] = list(reader.unpack("10I", 0x98))
    # Compatibility aliases used by the original Blender importer.
    out.update(
        {
            "nodeCount": out["boneCount"],
            "nodeTableOffset": out["boneOffset"],
            "sectionCount": out["packetCount"],
            "sectionTableOffset": out["packetOffset"],
            "bufferDescriptorCount": out["streamInfoCount"],
            "bufferDescriptorOffset": out["streamInfoOffset"],
            "boundsACount": out["groupCount"],
            "boundsAOffset": out["groupOffset"],
            "boundsBCount": out["boundingCount"],
            "boundsBOffset": out["boundingOffset"],
            "materialTableOffset": out["materialOffset"],
            "textureBindingCount": out["textureTypeCount"],
            "textureBindingTableOffset": out["textureTypeOffset"],
            "nameTableOffset": out["debugInfoOffset"],
            "geometryDataSize": out["streamDataSize"],
            "geometryDataOffset": out["streamDataOffset"],
            "textureCount": out["textureNameIdCount"],
            "textureTableOffset": out["textureNameIdOffset"],
            "vertexElementTableOffset": out["vertexElementOffset"],
            "fileSize": out["size"],
        }
    )
    return out


def _parse_extra(reader, offset, index):
    reader.check(offset, 0x10)
    chunk_size = reader.u32(offset + 8)
    if chunk_size < 0x10:
        raise KTMDLError("Invalid ktModelDataExtraHead.chunkSize at 0x%X" % offset)
    reader.check(offset, chunk_size)
    return {
        "index": index,
        "offset": offset,
        "type": reader.u32(offset),
        "localId": reader.u16(offset + 4),
        "flag": reader.u16(offset + 6),
        "chunkSize": chunk_size,
        "reserved": reader.u32(offset + 0x0C),
        "payloadHex": reader.bytes(offset + 0x10, chunk_size - 0x10).hex(),
        "rawHex": reader.bytes(offset, chunk_size).hex(),
    }


def _parse_bone(reader, offset, index):
    name_id = reader.u64(offset)
    pad0 = reader.u64(offset + 8)
    return {
        "index": index,
        "offset": offset,
        "nameId": name_id,
        "nameIdHex": "0x%016X" % name_id,
        "pad0": pad0,
        "pad0Hex": "0x%016X" % pad0,
        "matrix": _matrix4(reader, offset + 0x10),
        "invertMatrix": _matrix4(reader, offset + 0x50),
        "boundMax": _vec(reader, offset + 0x90, 3),
        "pad1": reader.u32(offset + 0x9C),
        "boundMin": _vec(reader, offset + 0xA0, 3),
        "parentNo": reader.i32(offset + 0xAC),
        # Compatibility aliases.
        "identifier": int(name_id & 0xFFFFFFFF),
        "identifierHex": "0x%016X" % name_id,
        "matrixA": _matrix4(reader, offset + 0x10),
        "matrixB": _matrix4(reader, offset + 0x50),
        "boundsMax": _vec(reader, offset + 0x90, 3),
        "boundsMin": _vec(reader, offset + 0xA0, 3),
        "parentIndex": reader.i32(offset + 0xAC),
    }


def _parse_packet_texture_ref(reader, offset):
    tex_id = reader.u16(offset)
    tex_param = reader.u8(offset + 2)
    uv_no = reader.u8(offset + 3)
    return {
        "id": tex_id,
        "texParam": tex_param,
        "uvNo": uv_no,
        # aliases
        "bindingIndex": tex_id,
        "layerMode": tex_param,
        "texCoordIndex": uv_no,
    }


def _parse_packet(reader, offset, index):
    refs = [_parse_packet_texture_ref(reader, offset + 0x30 + i * 4) for i in range(8)]
    prim = reader.u8(offset + 2)
    return {
        "index": index,
        "offset": offset,
        "flag": reader.u16(offset),
        "primType": prim,
        "primTypeName": PRIMITIVE_NAMES.get(prim, "UNKNOWN_%d" % prim),
        "pad": reader.u8(offset + 3),
        "materialNo": reader.u32(offset + 4),
        "groupNo": reader.u32(offset + 8),
        "blockNo": reader.u32(offset + 0x0C),
        "streamInfoCount": reader.u32(offset + 0x10),
        "streamInfoOffset": reader.i32(offset + 0x14),
        "skeletonCount": reader.u32(offset + 0x18),
        "skeletonOffset": reader.i32(offset + 0x1C),
        "indexCount": reader.u32(offset + 0x20),
        "indexInfoOffset": reader.i32(offset + 0x24),
        "extraOffset": reader.i32(offset + 0x28),
        "nTexture": reader.u8(offset + 0x2C),
        "pad4": list(reader.bytes(offset + 0x2D, 3)),
        "tex": refs,
        "offset3": _vec(reader, offset + 0x50, 3),
        "r": reader.f32(offset + 0x5C),
        # compatibility aliases
        "flags": reader.u16(offset),
        "materialIndex": reader.u32(offset + 4),
        "vertexBufferCount": reader.u32(offset + 0x10),
        "vertexBufferOffset": reader.i32(offset + 0x14),
        "bonePaletteCount": reader.u32(offset + 0x18),
        "bonePaletteOffset": reader.i32(offset + 0x1C),
        "indexBufferCount": reader.u32(offset + 0x20),
        "indexBufferOffset": reader.i32(offset + 0x24),
        "textureBindingCount": reader.u8(offset + 0x2C),
        "textureRefs": refs,
        "unknown50": _vec(reader, offset + 0x50, 4),
    }


def _parse_stream_info(reader, offset, index=None):
    out = {
        "offset": offset,
        "streamOffset": reader.i32(offset),
        "numVertex": reader.u32(offset + 4),
        "numTarget": reader.u8(offset + 8),
        "stride": reader.u8(offset + 9),
        "elementCount": reader.u8(offset + 0x0A),
        "numShaderTarget": reader.u8(offset + 0x0B),
        "elementOffset": reader.i32(offset + 0x0C),
        "targetIndexOffset": reader.i32(offset + 0x10),
        "type": reader.i32(offset + 0x14),
        "pad": [reader.i32(offset + 0x18), reader.i32(offset + 0x1C)],
    }
    if index is not None:
        out["index"] = index
    # aliases for old parser fields
    out.update(
        {
            "dataOffset": out["streamOffset"],
            "count": out["numVertex"],
            "descriptorTag": out["numTarget"],
            "vertexStride": out["stride"],
            "vertexElementCount": out["elementCount"],
            "streamCount": out["numShaderTarget"],
            "declarationOffset": out["elementOffset"],
            "unknown10": out["targetIndexOffset"],
            "unknown14": out["type"],
            "unknown18": out["pad"][0],
            "unknown1C": out["pad"][1],
        }
    )
    return out


def _parse_vertex_element(reader, offset, index=None):
    fmt = reader.u8(offset + 2)
    sem = reader.u8(offset + 3)
    out = {
        "offsetInFile": offset,
        "stream": reader.u8(offset),
        "offset": reader.u8(offset + 1),
        "format": fmt,
        "formatName": TYPE_NAMES.get(fmt, "UNKNOWN_0x%02X" % fmt),
        "semantics": sem,
        "semanticsName": SEMANTIC_NAMES.get(sem, "SEMANTIC_0x%02X" % sem),
        # aliases
        "type": fmt,
        "typeName": TYPE_NAMES.get(fmt, "UNKNOWN_0x%02X" % fmt),
        "semantic": sem,
        "semanticName": SEMANTIC_NAMES.get(sem, "SEMANTIC_0x%02X" % sem),
    }
    if index is not None:
        out["index"] = index
    return out


def _parse_group(reader, offset, index):
    name_id = reader.u64(offset)
    return {
        "index": index,
        "offset": offset,
        "nameId": name_id,
        "nameIdHex": "0x%016X" % name_id,
        "flag": reader.u32(offset + 8),
        "parent": reader.i32(offset + 0x0C),
        "boundMax": _vec(reader, offset + 0x10, 3),
        "pad1": reader.u32(offset + 0x1C),
        "boundMin": _vec(reader, offset + 0x20, 3),
        "pad2": reader.u32(offset + 0x2C),
        # AABB aliases
        "max": _vec(reader, offset + 0x10, 3),
        "min": _vec(reader, offset + 0x20, 3),
    }


def _parse_bounding(reader, offset, index):
    name_id = reader.u64(offset)
    return {
        "index": index,
        "offset": offset,
        "nameId": name_id,
        "nameIdHex": "0x%016X" % name_id,
        "nBlock": reader.u32(offset + 8),
        "blockOffset": reader.u32(offset + 0x0C),
        "boundMax": _vec(reader, offset + 0x10, 3),
        "pad1": reader.u32(offset + 0x1C),
        "boundMin": _vec(reader, offset + 0x20, 3),
        "pad2": reader.u32(offset + 0x2C),
        "max": _vec(reader, offset + 0x10, 3),
        "min": _vec(reader, offset + 0x20, 3),
    }


def _parse_locator(reader, offset, index):
    name_id = reader.u64(offset)
    return {
        "index": index,
        "offset": offset,
        "nameId": name_id,
        "nameIdHex": "0x%016X" % name_id,
        "parentNo": reader.i32(offset + 8),
        "pad": reader.u32(offset + 0x0C),
        "matrix": _matrix4(reader, offset + 0x10),
    }


def _parse_material(reader, offset, index):
    name_id = reader.u64(offset)
    pad = reader.u64(offset + 8)
    n_param = reader.u32(offset + 0x10)
    shader_id = reader.u32(offset + 0x14)
    params = [_vec(reader, offset + 0x20 + i * 0x10, 4) for i in range(8)]
    return {
        "index": index,
        "offset": offset,
        "nameId": name_id,
        "nameIdHex": "0x%016X" % name_id,
        "pad": pad,
        "padHex": "0x%016X" % pad,
        "nParam": n_param,
        "shaderId": shader_id,
        "shaderIdHex": "0x%08X" % shader_id,
        "pad2": [reader.u32(offset + 0x18), reader.u32(offset + 0x1C)],
        "param": params,
        # compatibility aliases for old UI
        "identifier": name_id,
        "unknown08": pad,
        "unknown10": n_param,
        "nameIdLegacy": shader_id,
        "vector0": params[0],
        "vector1": params[1],
        "vector2": params[2],
    }


def _parse_texture_type(reader, offset, index):
    name_id = reader.u64(offset)
    mode = reader.u8(offset + 0x0D)
    texture_index = reader.u16(offset + 0x0E)
    params = [_vec(reader, offset + 0x10 + i * 0x10, 4) for i in range(4)]
    return {
        "index": index,
        "offset": offset,
        "nameId": name_id,
        "nameIdHex": "0x%016X" % name_id,
        "addrU": reader.u8(offset + 8),
        "addrV": reader.u8(offset + 9),
        "filterMag": reader.u8(offset + 0x0A),
        "filterMin": reader.u8(offset + 0x0B),
        "filterMip": reader.u8(offset + 0x0C),
        "mode": mode,
        "modeName": TEXTURE_MODE_NAMES.get(mode, "UNKNOWN_%d" % mode),
        "textureIndex": texture_index,
        "param": params,
        # compatibility aliases
        "identifier": name_id,
        "sampler0": reader.u8(offset + 8),
        "sampler1": reader.u8(offset + 9),
        "sampler2": reader.u8(offset + 0x0A),
        "usage": mode,
        "usageName": TEXTURE_MODE_NAMES.get(mode, "UNKNOWN_%d" % mode),
        "scaleX": params[0][0],
        "scaleY": params[0][1],
    }


def _parse_texture_id(reader, offset, index):
    hi = reader.u64(offset)
    lo = reader.u64(offset + 8)
    return {
        "index": index,
        "offset": offset,
        "hi": hi,
        "lo": lo,
        "hiHex": "0x%016X" % hi,
        "loHex": "0x%016X" % lo,
        "rawHex": reader.bytes(offset, 0x10).hex(),
    }


def _parse_debug_info(reader, header):
    base = header["debugInfoOffset"]
    empty = {
        "offset": base,
        "flag": 0,
        "textureNameOffset": 0,
        "shaderNameOffset": 0,
        "shaderNameCount": 0,
        "shaderIdOffset": 0,
        "textureNameOffsets": [],
        "textureNames": [],
        "shaderIds": [],
        "shaderNameOffsets": [],
        "shaderNames": [],
    }
    if base <= 0 or base + 0x14 > reader.size:
        return empty
    out = dict(empty)
    out.update(
        {
            "flag": reader.u32(base),
            "textureNameOffset": reader.i32(base + 4),
            "shaderNameOffset": reader.i32(base + 8),
            "shaderNameCount": reader.u32(base + 0x0C),
            "shaderIdOffset": reader.i32(base + 0x10),
        }
    )
    tex_count = header["textureNameIdCount"]
    if tex_count and out["textureNameOffset"] > 0:
        table = base + out["textureNameOffset"]
        reader.check(table, tex_count * 4)
        for i in range(tex_count):
            rel = reader.u32(table + i * 4)
            out["textureNameOffsets"].append(rel)
            out["textureNames"].append(reader.cstring(table + rel))
    scount = out["shaderNameCount"]
    if scount and out["shaderIdOffset"] > 0:
        table = base + out["shaderIdOffset"]
        reader.check(table, scount * 4)
        out["shaderIds"] = list(reader.unpack("%dI" % scount, table))
    if scount and out["shaderNameOffset"] > 0:
        table = base + out["shaderNameOffset"]
        reader.check(table, scount * 4)
        for i in range(scount):
            rel = reader.u32(table + i * 4)
            out["shaderNameOffsets"].append(rel)
            out["shaderNames"].append(reader.cstring(table + rel))
    return out


def _decode_element(reader, vertex_base, elem, inferred_size):
    off = vertex_base + elem["offset"]
    fmt = elem["format"]
    if fmt == 0x00:
        return reader.f32(off)
    if fmt == 0x01:
        return list(reader.unpack("2f", off))
    if fmt == 0x02:
        return list(reader.unpack("3f", off))
    if fmt == 0x0B:
        return list(reader.bytes(off, 4))
    return {"rawHex": reader.bytes(off, inferred_size).hex(), "size": inferred_size}


def _element_spans(elements, stride):
    ordered = sorted(elements, key=lambda e: e["offset"])
    out = []
    for i, elem in enumerate(ordered):
        size = TYPE_SIZES.get(elem["format"])
        if size is None:
            nxt = ordered[i + 1]["offset"] if i + 1 < len(ordered) else stride
            size = max(0, nxt - elem["offset"])
        out.append((elem, elem["offset"], size))
    return out


def _unmapped_ranges(elements, stride):
    covered = [False] * max(0, stride)
    for elem in elements:
        size = TYPE_SIZES.get(elem["format"])
        if size is None:
            continue
        for p in range(max(0, elem["offset"]), min(stride, elem["offset"] + size)):
            covered[p] = True
    out = []
    i = 0
    while i < stride:
        if covered[i]:
            i += 1
            continue
        s = i
        while i < stride and not covered[i]:
            i += 1
        out.append((s, i - s))
    return out


def _parse_vertex_stream(reader, info, elements):
    count, stride = info["numVertex"], info["stride"]
    data_base = info["offset"] + info["streamOffset"]
    if stride <= 0 or count <= 0:
        return []
    reader.check(data_base, count * stride)
    spans = _element_spans(elements, stride)
    unmapped = _unmapped_ranges(elements, stride)
    verts = []
    for vi in range(count):
        base = data_base + vi * stride
        v = {}
        for elem, _start, size in spans:
            key = elem["semanticsName"]
            if key in v:
                key = "%s_%d" % (key, elem.get("index", 0))
            v[key] = _decode_element(reader, base, elem, size)
        if unmapped:
            v["_unmapped"] = [
                {"offset": s, "size": n, "hex": reader.bytes(base + s, n).hex()}
                for s, n in unmapped
            ]
        verts.append(v)
    return verts


def _parse_index_stream(reader, info):
    count = info["numVertex"]
    base = info["offset"] + info["streamOffset"]
    if count <= 0:
        return []
    reader.check(base, count * 2)
    return list(reader.unpack("%dH" % count, base))


def _split_restart(indices):
    chunks, cur = [], []
    for x in indices:
        if x == 0xFFFF:
            if cur:
                chunks.append(cur)
            cur = []
        else:
            cur.append(x)
    if cur:
        chunks.append(cur)
    return chunks


def primitive_to_geometry(indices, prim_type):
    """Return (faces, edges) for ktPrimitiveType using uint16 indices."""
    faces, edges = [], []
    chunks = _split_restart(indices)
    for seq in chunks:
        if prim_type == 0:  # triangle strip
            for i in range(len(seq) - 2):
                a, b, c = seq[i], seq[i + 1], seq[i + 2]
                if a == b or b == c or a == c:
                    continue
                faces.append([b, a, c] if (i & 1) else [a, b, c])
        elif prim_type == 1:  # triangle list
            for i in range(0, len(seq) - 2, 3):
                a, b, c = seq[i : i + 3]
                if a != b and b != c and a != c:
                    faces.append([a, b, c])
        elif prim_type == 2:  # triangle fan
            if len(seq) >= 3:
                root = seq[0]
                for i in range(1, len(seq) - 1):
                    a, b, c = root, seq[i], seq[i + 1]
                    if a != b and b != c and a != c:
                        faces.append([a, b, c])
        elif prim_type == 3:  # line list
            for i in range(0, len(seq) - 1, 2):
                if seq[i] != seq[i + 1]:
                    edges.append([seq[i], seq[i + 1]])
        elif prim_type == 4:  # line strip
            for i in range(len(seq) - 1):
                if seq[i] != seq[i + 1]:
                    edges.append([seq[i], seq[i + 1]])
        elif prim_type == 5:  # points: vertices already exist
            pass
        elif prim_type == 6:  # quad list
            for i in range(0, len(seq) - 3, 4):
                q = seq[i : i + 4]
                if len(set(q)) >= 3:
                    faces.append(q)
        elif prim_type == 7:  # quad strip: pairs (a,b),(c,d) => a,b,d,c
            for i in range(0, len(seq) - 3, 2):
                q = [seq[i], seq[i + 1], seq[i + 3], seq[i + 2]]
                if len(set(q)) >= 3:
                    faces.append(q)
        elif prim_type == 8:
            # RECTLIST semantics are renderer-specific; retain indices but do not
            # invent topology without a confirmed sample.
            pass
    return faces, edges


def triangle_strip_to_triangles(indices):
    return primitive_to_geometry(indices, 0)[0]


def _next_known_offset(header, start, reader_size):
    candidates = [reader_size, header.get("size", reader_size)]
    for name in (
        "boneOffset",
        "skeletonIndicesOffset",
        "packetOffset",
        "streamInfoOffset",
        "locatorOffset",
        "groupOffset",
        "materialOffset",
        "textureTypeOffset",
        "boundingOffset",
        "debugInfoOffset",
        "extraOffset",
        "morphNameIdOffset",
        "morphIndexOffset",
        "streamDataOffset",
        "textureNameIdOffset",
        "vertexElementOffset",
    ):
        v = header.get(name, 0)
        if isinstance(v, int) and v > start:
            candidates.append(v)
    return min(candidates)


def _resolve_names(result):
    debug = result["debugInfo"]
    tex_names = debug.get("textureNames", [])
    shader_ids = debug.get("shaderIds", [])
    shader_names = debug.get("shaderNames", [])
    shader_map = {}
    for i, sid in enumerate(shader_ids):
        if i < len(shader_names):
            shader_map[sid] = shader_names[i]

    for tex_id in result["textureNameIds"]:
        i = tex_id["index"]
        tex_id["name"] = tex_names[i] if i < len(tex_names) else "texture_%03d" % i
    for tt in result["textureTypes"]:
        ti = tt["textureIndex"]
        tt["textureName"] = (
            tex_names[ti] if ti < len(tex_names) else "texture_%03d" % ti
        )
    for mat in result["materials"]:
        mat["shaderName"] = shader_map.get(
            mat["shaderId"], "shader_%08X" % mat["shaderId"]
        )
        # There is no confirmed human-readable material-name table in the new
        # IDA layout; use shader name + material index for Blender display.
        mat["name"] = "%s_mat_%03d" % (mat["shaderName"], mat["index"])

    # Compatibility object used by the old Blender UI.
    result["names"] = {
        "textureNames": tex_names,
        "textureNameOffsets": debug.get("textureNameOffsets", []),
        "materialNameIds": shader_ids,
        "materialNames": shader_names,
        "materialNameOffsets": debug.get("shaderNameOffsets", []),
        "shaderIds": shader_ids,
        "shaderNames": shader_names,
    }
    result["textures"] = result["textureNameIds"]
    result["textureBindings"] = result["textureTypes"]


def _merge_vertex_streams(streams):
    vertex_streams = [s for s in streams if s["info"]["type"] == STREAM_VERTEX]
    if not vertex_streams:
        return []
    count = max(len(s.get("vertices", [])) for s in vertex_streams)
    merged = [{} for _ in range(count)]
    for stream_index, stream in enumerate(vertex_streams):
        for vi, values in enumerate(stream.get("vertices", [])):
            for key, value in values.items():
                out_key = key
                if out_key in merged[vi] and out_key != "_unmapped":
                    out_key = "%s_STREAM%d" % (key, stream_index)
                if key == "_unmapped":
                    merged[vi].setdefault("_unmapped_streams", []).append(
                        {"streamIndex": stream_index, "ranges": value}
                    )
                else:
                    merged[vi][out_key] = value
    return merged


def parse_bytes(data, source_name="<memory>"):
    endian = _detect_endian(data)
    r = Reader(data, endian)
    header = _parse_header(r)
    if header["size"] > len(data):
        raise KTMDLError("KTMDL header.size is larger than actual file")

    result = {
        "sourceName": os.path.basename(source_name),
        "actualFileSize": len(data),
        "endian": "big" if endian == ">" else "little",
        "header": header,
        "extras": [],
        "bones": [],
        "nodes": [],
        "skeletonIndicesRegionHex": "",
        "bonePaletteRegionHex": "",
        "packets": [],
        "sections": [],
        "streamInfos": [],
        "bufferDescriptors": [],
        "vertexElements": [],
        "locators": [],
        "groups": [],
        "bounding": [],
        "boundsA": [],
        "boundsB": [],
        "materials": [],
        "textureNameIds": [],
        "textureTypes": [],
        "debugInfo": {},
        "names": {},
        "morphNameIdsRawHex": "",
        "morphIndicesRawHex": "",
    }

    # Extras are independently chunk-sized.
    if header["extraCount"] > 0 and 0 <= header["extraOffset"] < header["size"]:
        p = header["extraOffset"]
        for i in range(header["extraCount"]):
            extra = _parse_extra(r, p, i)
            result["extras"].append(extra)
            p += extra["chunkSize"]

    for i in range(header["boneCount"]):
        result["bones"].append(_parse_bone(r, header["boneOffset"] + i * 0xB0, i))
    result["nodes"] = result["bones"]

    so, po = header["skeletonIndicesOffset"], header["packetOffset"]
    if 0 <= so < po <= len(data):
        result["skeletonIndicesRegionHex"] = r.bytes(so, po - so).hex()
        result["bonePaletteRegionHex"] = result["skeletonIndicesRegionHex"]

    for i in range(header["packetCount"]):
        result["packets"].append(_parse_packet(r, header["packetOffset"] + i * 0x60, i))
    result["sections"] = result["packets"]

    for i in range(header["streamInfoCount"]):
        result["streamInfos"].append(
            _parse_stream_info(r, header["streamInfoOffset"] + i * 0x20, i)
        )
    result["bufferDescriptors"] = result["streamInfos"]

    for i in range(header["vertexElementCount"]):
        result["vertexElements"].append(
            _parse_vertex_element(r, header["vertexElementOffset"] + i * 4, i)
        )

    for i in range(header["locatorCount"]):
        result["locators"].append(
            _parse_locator(r, header["locatorOffset"] + i * 0x50, i)
        )
    for i in range(header["groupCount"]):
        result["groups"].append(_parse_group(r, header["groupOffset"] + i * 0x30, i))
    result["boundsA"] = result["groups"]
    for i in range(header["boundingCount"]):
        result["bounding"].append(
            _parse_bounding(r, header["boundingOffset"] + i * 0x30, i)
        )
    result["boundsB"] = result["bounding"]

    for i in range(header["materialCount"]):
        result["materials"].append(
            _parse_material(r, header["materialOffset"] + i * 0xA0, i)
        )
    for i in range(header["textureNameIdCount"]):
        result["textureNameIds"].append(
            _parse_texture_id(r, header["textureNameIdOffset"] + i * 0x10, i)
        )
    for i in range(header["textureTypeCount"]):
        result["textureTypes"].append(
            _parse_texture_type(r, header["textureTypeOffset"] + i * 0x50, i)
        )

    result["debugInfo"] = _parse_debug_info(r, header)
    _resolve_names(result)

    if header["morphNameIdCount"] > 0 and header["morphNameIdOffset"] > 0:
        end = _next_known_offset(header, header["morphNameIdOffset"], len(data))
        result["morphNameIdsRawHex"] = _hex(r, header["morphNameIdOffset"], end)
    if header["morphIndexCount"] > 0 and header["morphIndexOffset"] > 0:
        end = _next_known_offset(header, header["morphIndexOffset"], len(data))
        result["morphIndicesRawHex"] = _hex(r, header["morphIndexOffset"], end)

    # Expand exact per-packet linked data.
    for packet in result["packets"]:
        pbase = packet["offset"]
        palette = []
        if packet["skeletonCount"]:
            pp = pbase + packet["skeletonOffset"]
            r.check(pp, packet["skeletonCount"] * 2)
            palette = list(r.unpack("%dH" % packet["skeletonCount"], pp))
        packet["skeletonIndices"] = palette
        packet["bonePalette"] = palette

        streams = []
        if packet["streamInfoCount"]:
            info_base = pbase + packet["streamInfoOffset"]
            for si in range(packet["streamInfoCount"]):
                info = _parse_stream_info(r, info_base + si * 0x20, si)
                entry = {"info": info, "declaration": [], "vertices": [], "indices": []}
                if info["elementCount"] > 0:
                    dp = info["offset"] + info["elementOffset"]
                    entry["declaration"] = [
                        _parse_vertex_element(r, dp + ei * 4, ei)
                        for ei in range(info["elementCount"])
                    ]
                if info["type"] == STREAM_VERTEX:
                    entry["vertices"] = _parse_vertex_stream(
                        r, info, entry["declaration"]
                    )
                elif info["type"] == STREAM_INDEX:
                    entry["indices"] = _parse_index_stream(r, info)
                streams.append(entry)
        packet["vertexStreams"] = streams

        index_streams = []
        if packet["indexCount"]:
            info_base = pbase + packet["indexInfoOffset"]
            for ii in range(packet["indexCount"]):
                info = _parse_stream_info(r, info_base + ii * 0x20, ii)
                entry = {
                    "info": info,
                    "indices": (
                        _parse_index_stream(r, info)
                        if info["type"] == STREAM_INDEX
                        else []
                    ),
                }
                index_streams.append(entry)
        packet["indexStreams"] = index_streams

        packet["vertices"] = _merge_vertex_streams(streams)
        # Compatibility: first vertex descriptor/declaration and first index descriptor.
        first_vs = next(
            (s for s in streams if s["info"]["type"] == STREAM_VERTEX), None
        )
        packet["vertexDescriptor"] = first_vs["info"] if first_vs else {}
        packet["vertexDeclaration"] = first_vs["declaration"] if first_vs else []
        first_is = next(
            (s for s in index_streams if s["info"]["type"] == STREAM_INDEX), None
        )
        packet["indexDescriptor"] = first_is["info"] if first_is else {}
        packet["indices"] = first_is["indices"] if first_is else []
        faces, edges = primitive_to_geometry(packet["indices"], packet["primType"])
        packet["faces"] = faces
        packet["edges"] = edges
        packet["triangles"] = [f for f in faces if len(f) == 3]
        packet["activeTextureRefs"] = packet["tex"][: min(packet["nTexture"], 8)]

    return result


def parse_file(path):
    with open(path, "rb") as handle:
        return parse_bytes(handle.read(), os.path.basename(path))

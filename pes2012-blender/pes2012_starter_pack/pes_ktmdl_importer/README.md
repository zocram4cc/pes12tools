# PES KTMDL Importer / Exporter v1.0.0 for Blender 2.80–5.x

Standalone `.ktmdl` only. The file must start with `KTMDL\0\0\0`.


## Blender object organization

Each KTMDL packet/part is always imported as a **separate Blender mesh object**. There is no automatic packet merging. This preserves the original material/texture boundary, vertex declaration, bone palette, primitive type, and stream/index organization for round-trip export.

Object names come from the source filename. For example, `player_body.ktmdl` imports as:

```text
player_body_000
player_body_001
player_body_002
...
player_body_Armature
```

The numeric suffix is the original KTMDL packet index. The exporter does not depend on the visible object name; it uses the stored `ktmdl_packet_index`, so objects may still be renamed manually after import. Vertices from different packets are never welded or deduplicated.

## Updated IDA-backed support

The importer now follows the IDA-cross-checked structures from `PES_KTMDL.bt`:

- fixed `0xC0` `ktModelDataHeader`, explicit endian marker, versions/config/flags/system reserve
- `ktModelDataExtraHead` chunks with raw payload preservation
- `ktModelDataBone` matrix/inverse matrix, AABB and parent hierarchy
- `ktModelDataPacket` material/group/block IDs, primitive type, texture refs, packet offsets/radius field
- all packet vertex stream-info records and all packet index stream-info records
- declaration-driven vertex elements and all currently known semantics/formats
- primitive types: triangle strip/list/fan, line list/strip, point, quad list/strip; RECTLIST is preserved but not guessed
- packet skeleton tables and skinning
- locators as Blender empties
- groups/bounding records as hidden AABB empties
- materials with `nParam`, `shaderId`, `param[8][4]` and resolved shader names
- texture IDs, texture filenames, sampler/address/filter/mode data and `param[4][4]`
- debug texture/shader tables and exact shader-ID mapping
- morph-related regions preserved raw because their payload layout is not yet confirmed
- unknown/unmapped vertex bytes preserved in JSON metadata

The skin residual convention remains selectable. `Primary / slot 0` is the default because it matches the supplied player-body deformation tests; `Final slot` is available to match the template terminology on other variants.

## Export

The exporter is deliberately **topology preserving**. On import, enable **Embed Original Binary** (default). Export starts from those original bytes and patches fields Blender can safely represent while preserving every unknown byte and all unmodified structures.

Currently exported back:

- vertex positions
- vertex normals
- UV0–UV3
- tangent/binormal attributes when present
- blend indices and blend weights from Blender vertex groups
- rest-skeleton matrices and inverse matrices
- locator matrices
- group/bounding AABBs

Index buffers, packet layout, stream layout, material/texture layout, sequence of chunks and unknown bytes are preserved from the original source. Vertex count/topology must not change.

## Install

Install the ZIP as a normal Blender add-on. It targets Blender 2.80 through 5.x.

- Import: **File → Import → PES KTMDL (.ktmdl)**
- Export: **File → Export → PES KTMDL (.ktmdl)**

The importer stores a full `<name>.ktmdl_metadata.json` Text datablock and, when requested, `<name>.ktmdl_source.b64` for round-trip export.

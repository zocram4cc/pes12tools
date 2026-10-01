# PES2012 Toolkit for Blender (4.2 LTS and newer)

Import any PES2012 model BIN into Blender, edit it, export it back.
Balls, stadium parts, boots, faces, hair, bodies and kits all use the
same flow. An unedited export is **byte-identical** to the file you
imported, so what you change is the only thing that changes.

Custom player bodies for `drawlogic.dll` (PGB2 `body.bin`) have their own
import/export with every submesh flag.

GPL-3.0-or-later (the KTMDL reader it vendors is).

## Install

Blender 4.2 or newer, Windows or Linux:

1. Download `pes2012_toolkit-<version>.zip` from the releases, or build it:
   `blender --command extension build --source-dir pes2012_starter_pack`
2. Blender > Edit > Preferences > Get Extensions > the drop-down (top
   right) > **Install from Disk...**, pick the zip.
3. Enable "PES2012 Toolkit". No other dependencies.

On Linux with the Flatpak Blender, give it access to your game folder:
`flatpak override --user --filesystem=/path/to/games org.blender.Blender`.

## Models: import, edit, export

1. Get the BIN you want to edit, e.g. with pes2012-tools:
   `python3 afs.py extract "<PES2012>/img/dt0b.img" 2 ball_2.bin`
   (`extract` decompresses; the add-on reads compressed and raw BINs alike).
2. **File > Import > PES2012 BIN**. You get one collection named after the
   file: per KTMDL block one armature, per packet one mesh under it.
   Every face corner is its own vertex, so UVs, normals and weights are
   exactly the stock ones.
3. Edit freely: move, delete, add geometry, repaint weights, edit UVs
   (layers `UV0`..`UV3`).
4. Select any object of that collection, **File > Export > PES2012 BIN**.
   Serve the result through kitserver afs2fs, e.g.
   `kitserver/4cc-dlc/img/dt0b.img/dt0b_2.bin`. Never write the stock `.img`.

What export does:

- A vertex you did not touch is written back as its original bytes
  (tangent frame, UVs, weights included). Changed or new vertices are
  packed from what Blender holds; a changed vertex keeps its source
  vertex's tangent/binormal.
- A packet whose topology did not change keeps its stock index stream;
  otherwise strips are rebuilt (parity-checked, every triangle decodes back
  with its winding) and lists are written flat.
- Bounds are only recomputed for packets whose positions changed; their
  group and global boxes grow to cover them.
- A packet whose mesh object you deleted keeps its stock data.

Limits:

- Weights may only use bones already in that packet's palette (the
  vertex groups import creates); other groups are dropped on export.
- At most 4 influences per vertex (UBYTE4 BLENDINDICES), heaviest first.
- A packet holds at most 65535 vertices (u16 indices); export refuses more.

### Skeletons

Each KTMDL block is its own armature, bones `bone_NNN` by node index,
parented and posed at the stock bind matrices. Blocks are **not** merged:
equal bone ids across blocks carry different binds (face #164 node 24 sits
0.8 mm apart in its two blocks) and different bones (dt07 #2960 block 1's
thigh is not block 0's shinguard). Vertex groups are named after the bone
of their block's armature.

### Textures

Every texture the BIN itself carries (WE00 blocks: DXT1/3/5, the one
uncompressed DDS, the two raw ball textures) is imported as a packed image,
each on the packets whose colour map it is, through `UV0`. Paint it in
Texture Paint, or replace its pixels, and export: changed images are
re-encoded into their own block (same size, format and mip count, mips
rebuilt), unchanged ones keep their bytes (DXT is lossy, so they are never
re-encoded). The image must keep its size.

Which block a model row means is decided from the BIN itself, by three
rules each checked on every stock entry: a block whose id equals the row
id; a BIN with as many blocks as distinct row ids (balls, boots: rows in id
order = blocks in file order); a face BIN's one texture is its skin row
(200). Rows with no texture in their own BIN (stadiums, kits, the eyes and
mouth of faces) sample shared textures elsewhere in the game and get no
image.

### Object properties

| where | property | meaning |
|---|---|---|
| collection | `pes12_source` | the BIN it was imported from (export re-reads it) |
| object | `pes12_block` | block index in that BIN |
| mesh | `pes12_packet` | packet index in its block |
| mesh attribute | `pes12_vid` | source vertex of each corner (keeps unedited bytes) |
| image | `pes12_tex_block` / `pes12_tex_hash` | its block in the BIN / pixels at import (unchanged -> bytes kept) |

## Stadium slots

**File > Import > PES2012 stadium slot** asks for the game folder, an
optional afs2fs root (`kitserver/4cc-dlc`: overrides there are read instead
of stock, so you edit what the game actually loads) and the slot number
(`pes12_stadium.py list`). You get one collection `slotNN` with a
sub-collection per model entry: the geometry entry, then each per-variant
props entry. Every packet is textured from the slot's texture entries
(stand textures, lightmaps, sky, pitch art, then the shared pitch-side and
adboard entries), resolved by texture id as the game does.

Edit geometry and paint textures as for a single BIN, then **File > Export >
PES2012 stadium slot (afs2fs)** with the afs2fs root: every model entry and
every texture entry you changed is written as
`<root>/img/dt07.img/dt07_<entry>.bin`; an entry equal to stock leaves no
file (an existing override for it is removed). Stock `.img` files are never
written. The slot table is read from your own `pes2012.exe`, with the same
rules as pes2012-tools `pes12_stadium.py`.

## PGB2 custom bodies

`dllprobe/custom/p<pid>/body.bin` (+ `body_<k>.dds` textures) as built by
pes2012-tools.

- **File > Import > PGB2 body**: one mesh, one material per submesh, vertex
  groups for the 21 body slots and the 27 face slots (`face_00`..`face_26`).
- Set the object's **PGB2 mode** and per-material flags, then select the
  mesh and **File > Export > PGB2 body**.

| object `pgb2_mode` | stock parts drawlogic still draws |
|---|---|
| `body` | nothing (whole characters) |
| `head` | everything but the head |
| `kit` | shirt, sleeves, shorts, socks, boots |
| `boots` | boots only |

| material property | bit | meaning |
|---|---|---|
| `pgb2_alpha_test` + `pgb2_alpha_ref` (0-255) | 0, 8-15 | alpha test, pass alpha > ref |
| `pgb2_blend` | 1 | alpha blend, drawn last |
| `pgb2_twosided` | 2 | two-sided |
| `pgb2_nozwrite` | 3 | no depth write |
| `pgb2_kit_slot` | 4 | UVs on the worn kit sheet |
| `pgb2_outline` | 5 | toon outline shell |
| `pgb2_face` | 6 | face part: head-local verts on the face slots |
| `pgb2_shadeless` | 16 | drawlogic's shadeless pixel shader |
| `pgb2_toon` | 17 | drawlogic's toon (Pony) pixel shader |
| `pgb2_hair` | 18 | hair: opaque core plus alpha fringe pass |
| `pgb2_tex` (int) | - | texture slot `body_<k>` |

Body slots, in order: `sk_thigh_r sk_leg_r dsk_hip sk_thigh_l sk_leg_l
sk_foot_l sk_foot_r sk_hand_r sk_forearm_r sk_upperarm_r sk_shoulder_r
sk_hand_l sk_forearm_l sk_upperarm_l sk_shoulder_l sk_belly sk_chest
sk_neck sk_head fingers_l fingers_r`. Weights follow PES2012's skin
shader: slot 0 takes 1 - (w1 + w2 + w3).

## Tests

The tests use **your own** PES2012 install as data (nothing from the game
ships here). Point `PES12_GAME` at the game folder (default: the parent
checkout's `Pro Evolution Soccer 2012`).

```text
python3 tests/test_pure.py              # no Blender: containers, reader, skeletons, export
PES12_FULL=1 python3 tests/test_pure.py # + every KTMDL entry of the game exported byte-exact (~13 min)
python3 tests/test_roundtrip.py         # PGB2 body codec (PES12_BODY_DIR = a built body)
blender -b --factory-startup --python tests/test_blender.py   # through the add-on itself
```

Results on PES2012 v1.06 (30-09-26): 5152 KTMDL entries, 11388 blocks,
all parse; all 5152 export byte-exact unedited; 14 class examples
byte-exact through Blender 5.2's own import/export, textures attached;
edits (moved vertex, deleted face) survive a re-import on ball, stadium,
boots, face and body; every PGB2 flag bit round-trips. Textures (01-10):
all 5137 WE00 blocks decode (DXT1/DXT5 identical to ImageMagick); every
format re-encodes in place (DXT mean error 0.6-1.2/255, others exact); a
square painted into the ball's colour map in Blender exports with every
other block byte-identical.

Stadium slots (01-10): the slot table equals pes12_stadium.read_slots on
all 31 slots; slot 30 imports as 6 entries, 71 packets, 11 textures and an
untouched export writes no file; a moved vertex plus a painted stand texture
write exactly the geometry and that stand entry, each re-reading with the
edit. Not yet seen in game: an edited slot served to a match.

Not in the add-on yet: the `balls.txt` install step (pes2012-tools
`pes12_ball.py`).

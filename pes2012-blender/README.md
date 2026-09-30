# PES 2008-2013 Starter Pack (Blender 4.x/5.x)

Blender add-on for the PES2012 modding toolchain: import stock KTMDL
BINs, export edited geometry back into template BINs served by kitserver
afs2fs, and author PGB2 custom bodies for drawlogic.dll.

Acceptance is the headless Blender smoke (`tests/blender_smoke.py`) plus
the pure-python core (`tests/test_roundtrip.py`); in Blender use the
same flows through File > Import/Export. The add-on is GPL-3.0-or-later
(the KTMDL importer it vendors is).

## Install

1. Zip the `pes2012_starter_pack/` folder (the folder containing
   `__init__.py` is the add-on root).
2. Blender > Edit > Preferences > Add-ons > Install from Disk, enable
   "PES 2008-2013 Starter Pack".
3. No dependencies beyond Blender (verified headless on 5.2).

## Workflows

### Ball: import stock ball, edit, export

1. Extract the template BIN (example: dt0b entry 11):
   `python3 tools/afs.py extract "Pro Evolution Soccer 2012/img/dt0b.img" 11 dllprobe/blender_test/ball11_stock.bin`
2. Blender: File > Import > PES2012 BIN (.bin), pick the BIN. Each KTMDL
   packet becomes one mesh object (ball templates have 1 packet).
3. Edit the mesh freely (new topology is fine). Keep UVs (UV0/UV1 layers).
4. Select the mesh object(s), File > Export > PES2012 BIN. The template
   supplies header/materials/bones/declaration; only vertex/index buffers
   are replaced (tools/ktmdl_write.py `build`). The file name defaults to
   `<name>_<entry>.bin`, e.g. `ball_11.bin`.
5. Serve it: drop `ball_11.bin` into
   `Pro Evolution Soccer 2012/kitserver/4cc-dlc/img/dt0b.img/`, and add
   the display name to `kitserver/4cc-dlc/balls.txt`
   (index = dt0b BIN - 1, so BIN 11 is ball 10).

### Generic single-model BIN

Same flow with any other template: File > Import > PES2012 BIN reads
ball BINs, generic `(size, flag, end)` TOC BINs (e.g. dt07 boots), and
dt08 stadium entries (`(offset, size, flag)` rows),
export splices the rebuilt KTMDL block(s) back and re-wraps WESYS.
Multi-KTMDL BINs import every KTMDL block; each exported object writes
back to its own block (`pes12_block`).

### Stadium side (dt08)

One side = 4 KTMDL blocks in one entry (e00 back stand = entry 58;
block roles 3/1/4/2 packets: base, boards, upper+roof, seats). Import
the side entry BIN: each packet becomes one mesh object (10 parts for
entry 58), all static (single bone `0x21CF6015543CD044`, no skinning,
TRIANGLELIST). Edit, select, export: each packet rebuilds through its
own declaration/material/texture slots via `ktmdl_write.build`
(textures stay stock: geometry entries hold KTMDL only, companions
hold the WE00 DDS). 0-packet reserved slots pass through untouched;
packets over 65535 verts are rejected. Serve as
`Pro Evolution Soccer 2012/kitserver/4cc-dlc/img/dt08.img/dt08_<n>.bin`.
Units are metres 1:1, same basis as the rest of the add-on.

### Player body (PGB2)

`dllprobe/custom/<name>/` layout (drawlogic.dll):

```text
dllprobe/custom/p272101/
  body.bin      # PGB2: header magic nv ni stride nsub mode + submesh table
  body_0.tex    # PGT1 textures, one per texture slot
  body_1.tex
  ...
```

- Blender: File > Import > PGB2 body (.bin), pick `body.bin`. Weights
  arrive as vertex groups named after the 21 palette slots (see
  Property reference), UV0/UV1 layers preserved.
- Edit, then select exactly the body mesh and File > Export > PGB2 body.
  `body.bin` is written; convert textures with PIL/OpenImageIO to
  `body_<k>.tex` PGT1 (see `pgb2.build_tex`).
- Set the object's `pgb2_mode` (`body`/`head`/`kit`/`boots`) and per-
  material flags before export.

## Property reference

### Object: PGB2 mode (`pgb2_mode`, on the body object)

| value | drawlogic MODE | stock drawn |
|---|---|---|
| `body` (0) | MODE_BODY | nothing (whole characters) |
| `head` (1) | MODE_HEAD | everything but the head (face-slot players) |
| `kit` (2) | MODE_KIT | shirt, sleeves, shorts, socks, boots |
| `boots` (3) | MODE_BOOTS | boots only (model stops at the ankle) |

Overridable per-folder at serve time by `dllprobe/custom/<name>/mode`.

### Material: submesh flags (one material per submesh)

| property | bit | meaning |
|---|---|---|
| `pgb2_alpha_test` + `pgb2_alpha_ref` (0-255) | 0 + bits 8-15 | alpha test, pass alpha > ref |
| `pgb2_blend` | 1 | alpha blend (submesh drawn last) |
| `pgb2_twosided` | 2 | two-sided |
| `pgb2_nozwrite` | 3 | no depth write |
| `pgb2_kit_slot` | 4 | kit slot: UVs on the worn kit sheet |
| `pgb2_outline` | 5 | toon outline shell |
| `pgb2_face` | 6 | face part: head-local verts on the face palette |
| `pgb2_tex` (int) | - | texture slot index (`body_<k>.tex`) |

### Vertex groups: body slots + face slots (weights)

Two weight tables; which one a vert uses follows its submesh's
`pgb2_face` flag (face verts index the face palette, body verts the
body palette):

- Body (`pgb2.SLOT_BONES`, 21 slots; slots 0-18 invert
  `probe/body349b2_bones.json`'s bone->slot map through
  `tools/fmdl_to_pes12.py`'s `FOX_TO_PES12` names; slots 19/20 are the
  finger bones, `dllprobe/kitmap.h` `CU_SRC_*`):

  `sk_thigh_r sk_leg_r dsk_hip sk_thigh_l sk_leg_l sk_foot_l sk_foot_r
  sk_hand_r sk_forearm_r sk_upperarm_r sk_shoulder_r sk_hand_l
  sk_forearm_l sk_upperarm_l sk_shoulder_l sk_belly sk_chest sk_neck
  sk_head fingers_l fingers_r`

- Face (`face_00`..`face_26`, 27 slots in stock face-packet
  `bonePalette` order, `tools/pes12_rig.py` `face_rig`). Only verts of
  `pgb2_face` submeshes use these; their positions are head-local
  (minus `HEAD_POS`) on export.

Weights follow PES2012's skin VS (tools/fmdl_to_pes12.py skin_pack):
slot 0 takes the remainder 1 - (w0 + w1 + w2), slot k + 1 takes w[k].

### KTMDL objects (from PES2012 BIN import)

`pes12_template` (template BIN path), `pes12_block` (KTMDL block index
in the BIN), `ktmdl_packet_index` (packet index; used to route the
export back into the right packet). UV layers `UV0..UV3` map to
TEXCOORD0..3; `flip_v` on import/export toggles the V convention.

## Acceptance

Headless Blender 5.2 smoke (`tests/blender_smoke.py`, shipped; the
sandbox needs `--filesystem=` pointed at the checkout):

```text
$ flatpak run --filesystem="$PWD" --command=blender org.blender.Blender \
    -b --factory-startup --python "$PWD/dist/pes2012-blender/tests/blender_smoke.py" -- "$PWD"
register ok
ball import: 1 parts, 1328 verts
ball export: .../dllprobe/blender_test/ball_11.bin
ball re-parse verts: 1328 vol +0.005499 (stock +0.005510)
body import: body tris=18741 mats=20
body export tris: 18741
body re-parse: 20 subs mode boots
stadium import: 10 parts, 4422 verts (stadium)
stadium export: .../dllprobe/blender_test/dt08_58.bin
stadium re-parse verts: 4422 bounds x[-62.4,62.4] y[0.1,21.8] z[-83.8,-39.2]
SMOKE PASS
```

The smoke asserts: ball re-parse keeps 1328 verts and the stock
signed-volume sign (no inside-out export); body re-parse keeps mode,
the (tex, flags) submesh multiset, and the triangle count of p272101;
stadium re-parse keeps 4422 verts and the entry-58 bounds.
`ball_11.bin` / `dt08_58.bin` prove the `<name>_<entry>.bin` afs2fs naming.

Pure-python core (`python3 tests/test_roundtrip.py <repo-root>`,
Blender-free) agrees:

```text
ball: 1328 verts round-trip OK
ball edited-topology: 106 verts / 100 tris OK
generic dt07#1: 3 blocks round-trip OK
pgb2 p272101: 19806 verts 18741 tris 20 subs mode boots OK
pack_body p272101: 56223 split verts influence+pos match (worst 0) OK
stadium dt08#58: 4422 verts 10 packets bounds x[-62.4,62.4] y[0.1,21.8] z[-83.8,-39.2] OK
stadium edited-topology: 200 verts / 100 tris OK
ALL ROUND-TRIPS PASS
```

- Ball: stock dt0b #11 split, packet 0 rebuilt from parsed attributes
  through the declaration-driven packer, `build`, re-join, re-parse:
  vertex count matches; stock/rebuilt BINs under
  `dllprobe/blender_test/`.
- Generic: dt07 #1 split/join round-trips byte-identically (compact
  header preserved).
- PGB2: `dllprobe/custom/p272101/body.bin` parse-compare
  byte-identical; `body_0.tex` PGT1 rebuild identical.
- No absolute paths / personal names in the whole dist tree
  (README/LICENSE/vendored files included):

```text
$ grep -rn "/m[n]t\|/h[o]me/\|/U[s]ers/\|pass[w]d\|wh[o]ami" . --exclude-dir=__pycache__
(no output)
```

Blender-layer notes: `import_bin`/`export_bin`/`import_body`/
`export_body` run against real Blender above (SMOKE PASS). The smoke
drives the same functions the operators call; residual limits: KTMDL
skinned-packet weights map through the template packet palette (falls
back to the PGB2 table only when the template has none), and PGB2
export emits one vertex per triangle corner (56223 split verts from
p272101's 19806), so unreferenced source verts are dropped and the
export is larger than the source.

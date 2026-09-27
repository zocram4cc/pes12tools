# pes2012-tools

4cc PES2012 toolchain: PES2015 face/kit/team exports and PES21 fmdl meshes
into PES2012 custom bodies (`drawlogic.dll` PGB1/PGB2), kits, balls, and
the 4cc DLC base. Self-contained source: every tool plus its vendored
deps; nothing here is measured from game files.

## Layout

| path | what |
|---|---|
| `pes12_import_team.py` | team import: squad, formation, bodies, kits |
| `pes15_to_pes12.py` | PES2015 face folder -> custom body |
| `fmdl_to_pes12.py` | PES21 fmdl -> custom body |
| `pes15export.py`, `pes15crypt.py` | 4cc tactical export reader + crypto |
| `pes15_kits.py` | kit textures -> PES2012 kitserver kits |
| `pes12_rig.py` | **extract rig tables from your game** (run first) |
| `pes12_ball.py` | ball mesh + texture -> dt0b ball BIN |
| `ktmdl_write.py` | KTMDL geometry replacer (balls) |
| `pes12_4cc_dlc.py` (+ `pes12db`, `pes12emblem`, `pes12strings`, `cpk`, `afs`, `ktmdl`) | 4cc DLC builder |
| `mark_face.py` | stamp custom-body marker into a face.bin |
| `pes12player.py` | PES2012 player record fields |
| `pgb_preview.py` | front/side preview of a body |
| `vendor/` | vendored deps (see `VENDORED.md`) |
| `runtime/` | player runtime sources + build + prebuilt DLLs |
| `requirements.txt` | `numpy`, `scipy`, `Pillow` |

## Setup (your PES2012 install)

```sh
pip install -r requirements.txt
python3 pes12_rig.py "<game dir>"     # -> rig/face_rig.json + rig/body349b2_bones.json
```

`pes12_rig.py` reads `img/dt0c.img` entry 132 (face rig: joints in
head-local space + the face packet's 27-slot palette) and `img/dt09.img`
entry 349 block 2 (body: bone index, palette slot, parent, id, pos) with
the vendored KTMDL reader. Both tables come from your game; none ship here.

## Convert one face

```sh
python3 pes15_to_pes12.py "<Faces>/<pid> - <name>/" <kit.dds> <out dir> [hide|keep]
```

The kit layout tables (`kitmap/kitmap.npz`) build once from your PES2021
Data dir: pass it as the first arg of `pes15_kits.py` (or the team
importer `--pes21=`); without a cache the tools exit telling you so.

## Team import

```sh
python3 pes12_import_team.py <export.bin> <Faces dir> <kit.dds> <db dir> \
    <custom dir> <GDB dir> <first marker> [--pes21=<PES2021 Data dir>]
```

The player runtime lives at `<game>/kitserver/4cc-players/`
(`custom/`, `flags/`, `custom/kits/`, `rig/`; see `runtime/drawhook.cpp`
`initRoot`). Copy `runtime/draw*.dll` there, or rebuild with
`runtime/build.sh` (`i686-w64-mingw32-g++ -O2 -shared -static`;
`drawlogic` needs `-ld3d9`, `drawhook` `-lwinmm`).

## Paths

Every path is an argument or relative to the package: `PES12_GAME`
(defaults `<pkg>/game`), `PES12_RIG` (`<pkg>/rig`), `PES12_KITMAP`
(`<pkg>/kitmap`), `PES12_BODY_BONES` (`<rig>/body349b2_bones.json`),
`PES12_VENDOR` (`<pkg>/vendor`), `PES12_MODEL_FILE`
(`<vendor>/ModelFile.py`), `PES12_DT0B` (`<game>/img/dt0b.img`).

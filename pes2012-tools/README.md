# pes2012-tools

4cc PES2012 toolchain: PES2015/PES2017 team, face and kit exports, PES21 fmdl
meshes and PES15/PES21 stadiums into PES2012 custom bodies (`drawlogic.dll`
PGB1/PGB2), kits, balls, stadiums and the 4cc DLC base. Self-contained
source: every tool plus its vendored deps; nothing here is measured from game
files, and nothing PES- or 4cc-derived ships.

## Layout

| path | what |
|---|---|
| `pes12_import_team.py` | team import: squad, lineup, formation, bodies, kits |
| `pes15_to_pes12.py` | PES2015 face folder -> custom body |
| `fmdl_to_pes12.py` | PES21 fmdl -> custom body; skin-weight packing (`skin_pack`) |
| `pes15export.py`, `pes15crypt.py` | 4cc PES2015 tactical export reader + crypto |
| `pes17export.py`, `pes17crypt.py` | PES2017 TEXPORT reader + crypto (`--pes17`) |
| `pes12edit.py`, `pes12crypt.py` | the game's `save/EDIT.bin` (squads, names, stats) |
| `pes15_kits.py` | kit textures -> PES2012 kitserver kits + the pack's own PES14+ sheet |
| `pes12_rig.py` | **extract rig tables from your game** (run first) |
| `pes12_ball.py` | ball mesh + texture -> dt0b ball BIN |
| `pes12_stadium.py` | PES15/PES21 stadium -> dt07/dt08 overrides of one slot |
| `ktmdl_write.py` | KTMDL geometry replacer (balls, stadiums) |
| `pes12_4cc_dlc.py` (+ `pes12db`, `pes12emblem`, `pes12strings`, `cpk`, `afs`, `ktmdl`) | 4cc DLC builder |
| `mark_face.py` | stamp the custom-body marker (player id, u24) into a face.bin |
| `pes12player.py` | PES2012 player record fields |
| `pgb_preview.py` | front/side preview of a body |
| `vendor/` | vendored deps (see `VENDORED.md`) |
| `runtime/` | player runtime sources + build scripts + prebuilt DLLs |
| `requirements.txt` | `numpy`, `scipy`, `Pillow` |

External programs: `7z` (the importer unpacks `.7z`/`.zip`/`.rar` packs),
ImageMagick `magick` (DXT re-encoding of textures that are not already
DXT), and, only to rebuild the runtime, `i686-w64-mingw32-g++` and
`vkd3d-compiler`.

## Setup (your PES2012 install)

```sh
pip install -r requirements.txt
python3 pes12_rig.py "<game dir>"     # -> rig/face_rig.json + rig/body349b2_bones.json + rig/hand_rig.json
```

`pes12_rig.py` reads `img/dt0c.img` entry 132 (face rig: joints in
head-local space + the face packet's 27-slot palette) and `img/dt09.img`
entry 349 block 2 (body: bone index, palette slot, parent, id, pos) and
`img/dt0d.img` entry 589 (the stock bare hands: a 12-bone hand rig per hand)
with the vendored KTMDL reader. All tables come from your game; none ship
here.

The kit layout tables (`kitmap/kitmap.npz`, `kitmap/fwd.bin`) build once
from your **PES2015** Data dir (its own kit garments: shirt, collar, short
and long sleeves, shorts, socks): pass it as the last argument of
`pes15_kits.py` or as the importer's `--pes15=`; without a cache the tools
exit telling you so. Point `PES12_KITMAP` at
`<game>/kitserver/4cc-players/kitmap` (or copy `fwd.bin` there):
drawlogic reads `fwd.bin` from that folder to draw stock-bodied 4cc
players in the pack's own PES14+ kit sheet. `--pes21=<PES2021 Data dir>`
supplies the engine textures some 4cc faces name (eyelashes).

## Convert one face

```sh
python3 pes15_to_pes12.py "<Faces>/<pid> - <name>/" <kit.dds> <out dir> [hide|keep]
```

The body's header says which stock pieces still draw with it (none for a
whole figure, everything but the head for a face-slot player, the boots for a
figure stopping at the ankle; a `gloveL`/`gloveR` part drops the stock
hands). Override it per player with a `mode` file of piece names - see the
toolchain page's player settings. A hand rigged on PES15's finger bones is
also written on the stock hand rig, so its fingers follow the game's hand
animation.

## Team import

```sh
python3 pes12_import_team.py <aesthetics folder or archive> <save/EDIT.bin> \
    <game>/kitserver/4cc-players/custom <game>/kitserver/GDB \
    [--export=<tactical export>] [--tid=N] [--pes17] [--tactics=<dt04 dir>] \
    [--rename=<name>] [--all] [--pes21=<PES2021 Data dir>] [--pes15=<PES2015 Data dir>]
```

Squads, names and stats go into `EDIT.bin`; the importer refuses to run
while `pes2012.exe` is up (the game would overwrite the save). The DLC
base keeps its PLACEHOLDER rows. Bodies and kits land in the custom dir,
faces and markers in the GDB. See the script's docstring for every option.
After reinstalling a team's kits, restart the game: it keeps the old kit
sheets in memory across matches.

## Referees

```sh
python3 pes12_import_referees.py <PES2015 download/4cc_35_referees.cpk> <game>
```

Converts the pack's referees and kits into `custom/p2999NN` and
`custom/kits/999/`; the runtime puts a random referee on every official, per
match. Needs the `lod.ref.*` pins below.

## Balls, stadiums, adboards

```sh
python3 pes12_ball.py ...      # see its docstring
python3 pes12_stadium.py ...   # see its docstring; install takes a stadium folder or <pack.cpk>:<stNNN>
python3 pes12_adboards.py <game> ad1.png [ad2.png ...]   # pitch-side ads; --off = stock
```

## Runtime

The player runtime lives at `<game>/kitserver/4cc-players/`: `custom/p<pid>/`
(bodies), `custom/kits/<tid>/` (`<slot>.tex` + `<slot>_hi.dds`), `kitmap/`
(`fwd.bin`), `flags/` (control files; see `runtime/drawlogic.cpp`), and the
debug outputs `shots/`, `grab/`, `shaders/`. Copy `runtime/draw*.dll` there,
or rebuild with `runtime/build.sh` (`i686-w64-mingw32-g++ -O2 -shared
-static`; `drawlogic` needs `-ld3d9`, `drawhook` `-lwinmm`). The Pony /
Shadeless pixel shaders are `runtime/custom_ps.hlsl`, compiled into
`custom_ps.h` by `runtime/build_shaders.sh` (vkd3d-compiler); rerun it after
editing the HLSL, before `build.sh`.

## Build a whole 4cc DLC

The step-by-step build (32-64 teams with full aesthetics, all stadiums,
adboards, referees, balls, and how to ship it) is the wiki page
[**DLC build**](12-dlc-build.md).

## Install the player runtime (one command)

The 4cc DLC base loads only `afsio` + `afs2fs`. Custom players,
officials and adboards add three more modules and the LOD pins (see the
`[lodmixer]` block below, written by the tool):

```sh
python3 pes12_runtime.py install "<PES2012 game dir>"
```

It copies `fserv.dll` and `lodmixer.dll` from `pes2012-4cc/kitserver/`
into the game's `kitserver/`, installs `drawhook.dll` +
`4cc-players/drawlogic.dll` from `runtime/`, creates
`4cc-players/{custom,custom/kits,flags,kitmap}/`, adds
`dll = fserv, lodmixer, drawhook` to `[kload]`, the LOD pins to
`[lodmixer]`, and sets the large-address-aware flag on `pes2012.exe`
(backup `pes2012.exe.pre-laa.bak`; the entrance LOD pin needs it). Idempotent;
the game must be closed. On Linux/Wine point the game folder at the
directory holding `pes2012.exe` (the one with `img/` next to it).

PES12_ENVIRONMENT for the tools below (bash):

    export PES12_GAME="<PES2012 game dir>"                       # default <pkg>/game
    export PES12_KITMAP="$PES12_GAME/kitserver/4cc-players/kitmap"   # drawlogic reads fwd.bin here
    export PES12_RIG="$PWD/rig"                                  # pes12_rig.py output (optional)
    export PES12_BODY_BONES="$PES12_RIG/body349b2_bones.json"    # optional


```ini
[lodmixer]
lod.players.entrance.s1 = 0.001
lod.players.entrance.s2 = 0.001
lod.players.entrance.s3 = 0.001
lod.players.inplay.s1 = 0.001
lod.players.inplay.s2 = 0.001
lod.players.inplay.s3 = 0.001
lod.players.misc.s1 = 0.001
lod.players.misc.s2 = 0.001
lod.players.misc.s3 = 0.001
lod.players.replay.s1 = 0.001
lod.players.replay.s2 = 0.001
lod.players.replay.s3 = 0.001
lod.active.player.ck.s1 = 0.001
lod.active.player.ck.s2 = 0.001
lod.active.player.ck.s3 = 0.001
lod.active.player.fk.s1 = 0.001
lod.active.player.fk.s2 = 0.001
lod.active.player.fk.s3 = 0.001
lod.ref.inplay = 0.001
lod.ref.replay = 0.001
```

Values at or below 0.0001 are ignored by lodmixer; 0.001 never steps down.
`runtime/officialmap.h` (the officials' model against the custom rig) is
generated from your game by `pes12_rig.py <game dir>`; rebuild the runtime
after regenerating it.

## Paths

Every path is an argument or relative to the package: `PES12_GAME`
(defaults `<pkg>/game`), `PES12_RIG` (`<pkg>/rig`), `PES12_KITMAP`
(`<pkg>/kitmap`), `PES12_BODY_BONES` (`<rig>/body349b2_bones.json`),
`PES12_VENDOR` (`<pkg>/vendor`), `PES12_MODEL_FILE`
(`<vendor>/ModelFile.py`), `PES12_KTMDL_READER` (`<vendor>/ktmdl_moth.py`),
`PES12_DT0B` (`<game>/img/dt0b.img`).

# pes12tools - the 4chan Cup toolchain for PES2012 (PC v1.06)

Four folders, each standalone:

| folder | what | install |
|---|---|---|
| `pes2012-4cc/` | ready-made 4chan Cup base: kitserver 12 + the `4cc-dlc` override root (210 4cc teams, placeholder squads, crests, league names); no game file is modified | copy `kitserver/` into the game folder ([README](pes2012-4cc/README.md)) |
| `pes2012-editapp/` | EDIT.bin editor (PySide6): teams, squads, every player field, squad CSV import/export | Python 3.10+, `pip install -r requirements.txt`; Windows and Linux steps in [its README](pes2012-editapp/README.md) |
| `pes2012-tools/` | the toolchain: PES2015/PES2017 team, body, kit, ball and stadium conversion; the custom-body runtime (`kitserver/4cc-players/`) | Python 3 + `requirements.txt`, `7z`, ImageMagick; a PES2012 install, a PES2015 Data dir (kit layouts), a PES2021 Data dir (engine textures), your 4cc exports |
| `pes2012-blender/` | Blender 4.2+ extension: byte-exact import/export of every PES2012 model BIN (balls, stadiums, boots, faces, hair, bodies, kits) and PGB2 player bodies, with tests | install the zip from Blender's Get Extensions ([README](pes2012-blender/README.md)) |

`pes2012-editapp` imports `pes2012-tools` from its sibling folder: keep the
layout when copying them out.

Building a whole DLC from your own 4cc packs (32-64 teams with full
aesthetics, every stadium, adboards, referees and balls) is the step-by-step
tutorial [DLC build](12-dlc-build.md).

`pes2012-4cc/` ships database tables derived from PES2012 itself (its
`dt04`/`dt06` overrides start from the stock tables). Everything the tools
produce from PES2015/PES2017/PES2021 data or from 4cc packs is never
shipped: it rebuilds from your own installs by a script (each folder's
README says how)..

## Build order (from a bare checkout)

1. Base game content: `pes2012-4cc/` is built by `pes2012-tools/pes12_4cc_dlc.py`; the player table there holds flat-40 CB
   placeholders, one row per 4cc team.
2. Rig tables: `python3 pes12_rig.py "<game dir>"` extracts the face rig
   and the 19 body bones from your game (face/bin ids are StrCode hashes;
   the bones are matched to the PES15/Fox skeleton by bind position, never
   by name).
3. Teams: `python3 pes12_import_team.py <aesthetics> <save/EDIT.bin>
   <custom dir> <GDB dir> [--export=<tactical export>] [--pes15=<PES2015
   Data dir>] ...` imports squad (into EDIT.bin; the DLC base keeps its
   placeholders), lineup, formation (coordinates clamped to the role's stock
   ranges - out-of-range values crash the game on team select), bodies, and
   kits (see `pes2012-tools/README.md`).
4. Balls/stadiums: `python3 pes12_ball.py` / `python3 pes12_stadium.py`
   (in `pes2012-tools/`; usage in their docstrings).

## Player settings (custom bodies)

Each player's folder (`kitserver/4cc-players/custom/p<pid>/`) holds
`body.bin` (PGB2: header magic/nv/ni/stride/nsub/**keep** + submesh table
+ 80-byte verts + u32 indices), `body_<k>.tex` (a DXT DDS, or PGT1), and optionally a
`mode` file. `keep` has one bit per stock piece drawn with the model; the
`mode` file, if present, overrides it with the names of the pieces to keep
(an empty file keeps none):

| piece | the stock draw |
|---|---|
| `shorts`, `shirt`, `sleeves`, `socks`, `neck`, `gloves`, `head`, `boots`, `other` | the kit run's part classes (`gloves` also the keeper's detail gloves, `boots` the detail boots) |
| `skin` | the stride-76 skin draw (arms, legs, neck) |
| `hands` | the stock hands: the bare detail hands and the skin draw's own (cut at the wrist when `skin` stays) |

Default at import: nothing for a whole figure (PES15 hides its body for short
socks + tucked shirt), everything but `head` for a face-slot player, `boots`
alone for a figure stopping above the floor. A model that wears the stock kit
and nothing else: `shirt sleeves shorts socks`. The EDIT editor
(`pes2012-editapp/`) ticks each player's pieces and writes the `mode` file;
`flags/reload` applies it live.

## DLC compilation

The 4cc content is never written into stock `.img` files (growing one
crashes the game): it ships as kitserver afs2fs override roots, each a
folder with `img/<dtXX>.img/<name>_<entry>.bin` replacing that AFS entry
(later `img.dir` roots in `kitserver/config.txt` win):

- `4cc-dlc/img/dt04.img/`, `dt06.img/`: the team/player/formation tables,
  crests, league names;
- `4cc-dlc/img/dt0b.img/ball_<n>.bin` + `4cc-dlc/balls.txt`
  (`<index = BIN-1>, "<name>"`): balls;
- `4cc-dlc/img/dt08.img/dt08_<n>.bin`: stadium parts.

`tools/afs.py` reads/rewrites the stock archives (WESYS zlib BINs).
Rebuilding the tables needs the user's own PES2015/PES2021 installs; the
crests and league text come from your 4cc packs.

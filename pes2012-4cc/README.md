# 4CC for PES2012 (PC v1.06)

Ready-made 4chan Cup base for Pro Evolution Soccer 2012: 210 4cc teams on
their own team ids (701+), 23 PLACEHOLDER players each (every ability 40,
CB), the 71 board crests, the cloverleaf and /vg/ League badges, and the
league names 4chan Cup / Backup Teams / /vg/ League / Invitational Teams.

Nothing in the game folder is modified: everything is loaded through
kitserver 12's afs2fs override folder, `kitserver/4cc-dlc`.

## Install

1. Update the game to v1.06.
2. Copy the `kitserver` folder from here into the game folder (next to
   `pes2012.exe`). If you already use kitserver 12, copy only
   `kitserver/4cc-dlc` and add these lines to your `kitserver/config.txt`:

       [afs2fs]
       img.dir = "4cc-dlc"

       [kload]
       dll = afsio
       dll = afs2fs

   (`img.dir = "4cc-dlc"` must be the LAST `img.dir` line; later roots win.)
3. Run `kitserver/manager.exe`, select `pes2012.exe`, click Install.
4. A save `Documents/KONAMI/Pro Evolution Soccer 2012/save/EDIT.bin` made
   before the install shadows the base DB and hides the 4cc teams: delete
   it, or replace it with the 4cc one. The game does not create a save on
   its own (in game, 01-10), so a new install needs one from the cup pack
   or from another PES2012 install before squads can be imported.

## Uninstall

Run `kitserver/manager.exe` and click Remove, or delete the `4cc-dlc`
folder and its `img.dir` line.

## Editing

`pes2012-editapp/` (next to this folder) is the EDIT.bin editor (teams,
squads, every player field, team CSV import/export); install and use:
`pes2012-editapp/README.md` (Windows and Linux).

## Contents

| file | what |
|---|---|
| `kitserver/kload.dll`, `afsio.dll`, `afs2fs.dll`, `zlib1.dll`, `manager.exe`, `config.exe`, `lang_eng.txt`, `docs/` | kitserver 12 (Juce, Robbie; see `docs/license.txt`); loaded by `config.txt` |
| `kitserver/fserv.dll`, `lodmixer.dll` | the same kitserver 12 release, not loaded by the base; `pes12_runtime.py` copies them in when you install the player runtime (custom players, referees, adboards) |
| `kitserver/config.txt` | loads afsio + afs2fs, registers the 4cc-dlc root |
| `kitserver/4cc-dlc/img/dt04.img/dt04_26.bin` | players |
| `.../dt04_29.bin`, `dt04_30.bin`, `dt04_31.bin`, `dt04_32.bin` | formations, teams, rosters, team names |
| `.../dt04_33.bin` | league lists |
| `.../dt04_36.bin`, `dt04_41.bin` | 4chan Cup and /vg/ League badges |
| `.../dt04_65.bin` | team crests |
| `kitserver/4cc-dlc/img/dt06.img/dt06_4.bin`, `dt06_17.bin` | menu league names |

Rebuilt by `tools/pes12_4cc_dlc.py` in pes2012-tools/.

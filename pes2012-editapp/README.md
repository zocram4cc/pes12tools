# 4CC EDIT Editor for PES2012

A PySide6 editor for PES2012's `EDIT.bin`: teams (rename, abbreviation),
squads (shirt numbers), and every player field — abilities, positions,
player index cards, motion, physique, face and hair, accessories — plus a
raw-byte tab for what has no label yet. Copy/paste a player or only their
appearance, export/import a whole squad as CSV, apply a 4cc `team_list.txt`
naming plan, and set the drawlogic mode of a player's custom model.

No rules (AATF) checking.

It needs `pes2012-tools/` next to this folder (it uses `pes12crypt`,
`pes12edit` and `pes12player` from there), which is how the pes12tools
repository is laid out.

## Install

### Windows

1. Install Python 3.10 or newer from https://www.python.org/downloads/
   (tick "Add python.exe to PATH").
2. Open a Command Prompt in this folder (`pes2012-editapp`) and run:

       py -m pip install -r requirements.txt

3. Start it:

       py src\main.py

   or open a file directly:

       py src\main.py "%USERPROFILE%\Documents\KONAMI\Pro Evolution Soccer 2012\save\EDIT.bin"

### Linux

PES2012 runs under Wine (Bottles, Lutris, plain Wine); the save lives in
that prefix:

    <prefix>/drive_c/users/<you>/Documents/KONAMI/Pro Evolution Soccer 2012/save/EDIT.bin

(Bottles: `~/.local/share/bottles/bottles/<bottle>/drive_c/...`.)

1. Python 3.10+ and a virtual environment (keeps PySide6 out of the system
   Python):

       python3 -m venv .venv
       .venv/bin/pip install -r requirements.txt

2. Start it:

       .venv/bin/python src/main.py "<path to EDIT.bin>"

If Qt fails with `Could not load the Qt platform plugin "xcb"`, install
`libxcb-cursor0` (Debian/Ubuntu) or `xcb-util-cursor` (Arch, Fedora).
On Wayland it runs natively; `QT_QPA_PLATFORM=xcb` forces X11 if needed.

## Use

- **File > Open EDIT.bin**. Left: teams (filter by name, abbreviation or
  id; "4cc teams only" shows ids 701+). Middle: the selected team's squad;
  double-click a number to change it. Right: the selected player, one tab
  per field group.
- Edits apply immediately to the open file in memory; **File > Save**
  writes it. The first save over an existing file keeps the original as
  `EDIT.bin.bak`.
- **Player > Copy / Paste player** copies every field except the ids;
  **Paste appearance only** copies physique, face, hair and accessories.
- **Team > Export / Import team CSV**: one row per roster slot, columns =
  field keys. This is how a whole squad gets filled in at once.
- **File > Apply team_list plan**: renames teams from a 4cc `team_list.txt`
  (asks for the game folder, which it reads for the stock team names).
- **Custom model** (needs **File > Set 4cc-players folder**, i.e.
  `kitserver/4cc-players` of the custom-body runtime): shows whether the
  player has `custom/p<id>/body.bin`, ticks which stock pieces stay drawn
  with it (written to its `mode` file), and **Reload in game** makes drawlogic reload models within a second.

Settings (last folder, game folder, 4cc-players folder) are kept in
`%APPDATA%\PES12EditApp\config.yaml` on Windows and
`~/.config/PES12EditApp/config.yaml` on Linux.

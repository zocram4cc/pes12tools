# Vendored KTMDL reader

`ktmdl.py` is the parser from
[moth1995/pes2008-2013-tools](https://github.com/moth1995/pes2008-2013-tools)
(KTMDL importer by marqisspes6, GPL-3.0-or-later). Its Blender importer and
exporter are not used: the toolkit imports and exports through
`model.py` / `blender_io.py`, which reproduce every KTMDL entry of PES2012
byte for byte.

## Local changes

- Debug name tables are validated before they are followed
  (`_name_table`). KTMDL 2.1 files such as dt0b #35 keep a texture-name
  count where 2.2 files hold a shader count; following the fields read past
  the end of the block.
- Comments mark the packet palette, stream-info and element tables as
  offsets relative to their own record.

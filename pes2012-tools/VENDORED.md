# Vendored dependencies

Whole files, unmodified (verified with `diff` against each source).

| file | source | what is used |
|---|---|---|
| `vendor/retarget.py` | `GameplayFootball/tools/pes21_import/retarget.py` @ `4b082b0` (+ `LICENSE.GameplayFootball-Apache-2.0` = repo `LICENSE`) | `PES_RENDER_BIND`, `PES_ALIGN`, `_q_rot`, `GF_NODES`, `resolve_bone` (Fox render-bind re-pose in `fmdl_to_pes12`, `pes15_kits`, `pes15_to_pes12`) |
| `vendor/fpk.py` | same repo/commit | `extract` (PES21 `common_package.fpk` in `pes15_kits.extract_pes21`) |
| `vendor/ftex.py` | same repo/commit | `to_dds` (engine `.ftex` textures: eyelashes in `pes15_kits`, ball textures in `pes12_ball`, PES21 stadium textures in `pes12_stadium`) |
| `vendor/FmdlFile.py` | `GameplayFootball/4cc Blender Starter Pack/scripts/addons/pes-fmdl/FmdlFile.py` @ `4b082b0` | `FmdlFile` (read `.fmdl` in `fmdl_to_pes12`, `pes12_ball`, `pes12_stadium`) |
| `vendor/ModelFile.py` | `pes-model-blender/pes-model/ModelFile.py` (this repo) | `readModelFile`, `ParserSettings` (read PES14-17 `.model` in `pes15_to_pes12`, `pes15_kits` via it (PES2015 kit garments), `pes12_ball`, `pes12_stadium`) |
| `vendor/ktmdl_moth.py` | `pes2008-2013-tools/pes_ktmdl_importer/ktmdl.py` @ `0a0284f` (+ `LICENSE.ktmdl_moth-GPL-3.0`) | `parse_bytes` (`bones[].matrix/parentIndex/nameIdHex`, `packets[].bonePalette`) in `pes12_rig.py`; stock stadium KTMDL reading in `pes12_stadium.py` |

`tools/` files already in this package (not vendored): `cpk.py`
(CRI CPK extractor, used by `pes12_4cc_dlc` and `pes15_kits`), `afs.py`,
`ktmdl.py` (probe reader), `ktmdl_write.py`, `pes12db.py`, `pes12emblem.py`,
`pes12strings.py`, `pes12edit.py`, `pes12crypt.py`, `pes17export.py`,
`pes17crypt.py`. The whole package is refreshed from the repo by
`tools/sync_dist.py` (repo-only), which replaces developer paths with the
package-relative ones above.

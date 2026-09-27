"""Headless Blender smoke: import_bin -> export_bin + import_body -> export_body.

Ships in tests/ (dllprobe/ is gitignored). Repo root follows '--' on
the Blender command line, else PES12_REPO env, else the checkout.
Outputs land in <repo>/dllprobe/blender_test/.

Run (from the checkout):
  flatpak run --filesystem="$PWD" --command=blender org.blender.Blender \\
    -b --factory-startup --python "$PWD/dist/pes2012-blender/tests/blender_smoke.py" -- "$PWD"
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
# Blender consumes argv up to '--'; the repo root follows it (README
# passes -- "$PWD"). Fall back to PES12_REPO env, then the checkout.
_tail = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
REPO = _tail[0] if _tail and os.path.isdir(_tail[0]) else os.environ.get('PES12_REPO')
REPO = os.path.abspath(REPO or os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, PACK)

import bpy
import pes2012_starter_pack as P
from pes2012_starter_pack import binwrap, ktpack, pgb2
from pes2012_starter_pack.pes_ktmdl_importer import ktmdl as V

P.register()
print("register ok", flush=True)
ctx = bpy.context
OUT = os.path.join(REPO, "dllprobe", "blender_test")
os.makedirs(OUT, exist_ok=True)

# --- ball (stock dt0b #11 template) ---
ball = os.path.join(OUT, "ball11_stock.bin")
assert os.path.exists(ball), ball
rep, col, objs = P.import_bin(ctx, ball)
nv = sum(len(o.data.vertices) for o in objs)
print("ball import: %d parts, %d verts" % (len(objs), nv), flush=True)
assert nv == 1328, nv
for o in objs:
    o.select_set(True)
# afs2fs naming: export under the <name>_<entry>.bin workflow name
out_name = os.path.join(OUT, "ball_11.bin")
if os.path.exists(out_name):
    os.remove(out_name)
got = P.export_bin(os.path.join(OUT, "ball"), ball, objs, False, 11)
print("ball export:", got, flush=True)
assert os.path.basename(got) == "ball_11.bin", got
# stock winding reference: parser-reported triangles of the template
_tm = V.parse_bytes(
    binwrap.split_body(binwrap.unwesys(open(ball, "rb").read()))[1][0],
    "stock")
stock_pos = [tuple(v["POSITION"]) for v in _tm["packets"][0]["vertices"]]
stock_vol = ktpack.signed_volume(stock_pos, _tm["packets"][0]["triangles"])
body = binwrap.unwesys(open(got, "rb").read())
kind, blocks = binwrap.split_body(body)
m = V.parse_bytes(blocks[0], "ball11-re")
re_pos = [tuple(v["POSITION"]) for v in m["packets"][0]["vertices"]]
re_vol = ktpack.signed_volume(re_pos, m["packets"][0]["triangles"])
print("ball re-parse verts: %d vol %+f (stock %+f)" % (
    len(m["packets"][0]["vertices"]), re_vol, stock_vol), flush=True)
assert len(m["packets"][0]["vertices"]) == nv
assert (re_vol > 0) == (stock_vol > 0), "winding flipped: %+f vs %+f" % (re_vol, stock_vol)
for o in list(objs):
    bpy.data.objects.remove(o, do_unlink=True)

# --- body (p272101) ---
src = os.path.join(REPO, "dllprobe", "custom", "p272101", "body.bin")
ref = pgb2.parse(open(src, "rb").read())
obj, nsub = P.import_body(ctx, src)
print("body import: %s tris=%d mats=%d" % (
    obj.name, len(obj.data.polygons), len(obj.data.materials)), flush=True)
assert nsub == len(ref["subs"]) and len(obj.data.materials) == len(ref["subs"])
obj.select_set(True)
ctx.view_layer.objects.active = obj
out_body = os.path.join(OUT, "p272101_smoke.bin")
if os.path.exists(out_body):
    os.remove(out_body)
ntris = P.export_body(out_body, obj)
print("body export tris:", ntris, flush=True)
parsed = pgb2.parse(open(out_body, "rb").read())
print("body re-parse: %d subs mode %s" % (
    len(parsed["subs"]), pgb2.MODES[parsed["mode"]]), flush=True)
assert len(parsed["subs"]) == len(ref["subs"]), len(parsed["subs"])
assert parsed["mode"] == ref["mode"]
key = lambda s: (s["tex"], s["flags"])
assert sorted(map(key, parsed["subs"])) == sorted(map(key, ref["subs"]))
assert (sum(s["count"] for s in parsed["subs"]) // 3 ==
        sum(s["count"] for s in ref["subs"]) // 3 ==
        len(obj.data.polygons))
print("SMOKE PASS", flush=True)

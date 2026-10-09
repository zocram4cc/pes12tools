"""UVStep trailer + frame math (offline, no game).

Run: python3 tools/test_uvstep.py  (from the repo root)

- trailer: tools/uvstep.py build/parse/strip round trip, incl. after a
  synthetic HRIG section (sections scan by magic);
- math: the pure-python mirror (tools/uvstep.py) against the wiki rules -
  uniform stepping incl. zero-cycle, R bands incl. 0/255 edges, Tiles_Used
  cap, scale flag, timing scroll incl. zero-cycle axes, alpha-0 hides;
- body: the offline test model (tools/build_uvstep_test.py) parses, its
  trailer matches, and each rendered frame (tools/render_uvstep.py) is the
  authored colour (uniform) / the band's colour (timing).
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# inside the repo: tools/ next to ../dist; inside the dist package: HERE itself
REPO = os.path.dirname(HERE) if os.path.basename(HERE) == 'tools' else os.path.join(HERE, '..', '..')
REPO = os.path.abspath(REPO)
sys.path.insert(0, HERE)
for _t in (os.path.join(REPO, 'dist', 'pes2012-tools'),
           os.path.join(HERE, '..', 'pes2012-blender', 'pes2012_starter_pack')):
    if os.path.exists(os.path.join(_t, 'fmdl_to_pes12.py' if _t.endswith('pes2012-tools') else 'pgb2.py')):
        sys.path.insert(0, os.path.normpath(_t))
# repo layout: the Blender add-on lives in dist/
_p = os.path.join(REPO, 'dist', 'pes2012-blender', 'pes2012_starter_pack')
if os.path.isdir(_p):
    sys.path.insert(0, _p)

import uvstep as U  # noqa: E402
import pgb2  # noqa: E402

VIS = os.path.join(REPO, 'work_visual', 'UVStep')
MODEL = os.path.join(VIS, 'model')


def test_trailer():
    a = U.make_params(tile_u=2, tile_v=2, tiles_used=4, sec_cycle=4.0)
    b = U.make_params(tile_u=4, tile_v=1, tiles_used=3, use_timing=True,
                      sec_u=2.0, timing_tex=1, scale=True)
    t = U.build_trailer({0: a, 5: b})
    assert U.parse_trailer(t) == {0: a, 5: b}, U.parse_trailer(t)
    assert U.build_trailer({}) == b''
    # an HRIG section ahead of it is skipped by magic, not position
    hrig = b'HRIG' + bytes(2 * 12 * (1 + 1 + 12))
    assert U.parse_trailer(hrig + t) == {0: a, 5: b}
    assert U.strip_trailer(hrig + t) == hrig
    assert U.strip_trailer(hrig) == hrig
    # pgb2 agrees (the Blender add-on's codec is the same layout)
    assert pgb2.parse_ustep(t) == {0: a, 5: b}
    assert pgb2.strip_ustep(hrig + t) == hrig
    try:
        U.make_params(tile_u=0)
        raise AssertionError('tile 0 accepted')
    except ValueError:
        pass
    try:
        U.make_params(tile_u=2, tile_v=2, tiles_used=5)
        raise AssertionError('used > U*V accepted')
    except ValueError:
        pass
    print('trailer: round trip, HRIG-first, pgb2 agrees, bad grids refused')


def test_math():
    uni = U.make_params(tile_u=2, tile_v=2, tiles_used=4, sec_cycle=4.0)
    assert [U.frame_at(uni, t)[0] for t in (0.0, 1.0, 2.0, 3.0, 3.999)] == [0, 1, 2, 3, 3]
    zero = U.make_params(tile_u=2, tile_v=2, tiles_used=4, sec_cycle=0.0)
    assert U.frame_at(zero, 123.0) == (0, True)
    # R bands: R*Used/256, 0 first, 255 last
    assert [U.r_to_frame(r, 4) for r in (0, 63, 64, 127, 128, 191, 192, 255)] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert [U.r_to_frame(r, 3) for r in (0, 85, 170, 255)] == [0, 0, 1, 2]
    # Tiles_Used caps the grid: a 2x2 grid using 3 never shows frame 3
    assert U.r_to_frame(255, 3) == 2
    # scale flag: authored UVs span the whole texture
    p = U.make_params(tile_u=2, tile_v=2, tiles_used=4, scale=True)
    assert U.tile_uv(p, (1.0, 1.0), 3) == (0.75, 0.75), U.tile_uv(p, (1.0, 1.0), 3)
    assert U.tile_uv(p, (1.0, 1.0), 0) == (0.25, 0.25)
    q = U.make_params(tile_u=2, tile_v=2, tiles_used=4)
    assert U.tile_uv(q, (0.5, 0.5), 0) == (0.25, 0.25)
    assert U.tile_uv(q, (0.5, 0.5), 3) == (0.75, 0.75)
    # timing: scroll moves the sample, zero-cycle axes stand still
    from PIL import Image
    strip = U.build_timing(U.parse_timing_spec('0:1.0,1:1.0,2:1.0,3:1.0'), 4)
    assert U.timing_lookup(strip, (0.125, 0.5), 0.0, 4.0, 0.0)[0] < 64
    assert U.timing_lookup(strip, (0.125, 0.5), 1.0, 4.0, 0.0)[0] >= 64
    assert U.timing_lookup(strip, (0.125, 0.5), 9.0, 4.0, 0.0) == \
        U.timing_lookup(strip, (0.125, 0.5), 1.0, 4.0, 0.0)
    # alpha 0 hides
    hide = U.build_timing(U.parse_timing_spec('0:1.0:0,1:1.0'), 2)
    r, al = U.timing_lookup(hide, (0.1, 0.5), 0.0, 1.0, 0.0)
    assert U.frame_at(U.make_params(tile_u=2, tile_v=1, tiles_used=2,
                                     use_timing=True, timing_tex=0),
                      0.0, r, al) == (0, False)
    print('math: uniform, zero-cycle, R bands, used cap, scale, scroll, hide')


def test_body():
    import numpy as np
    from PIL import Image
    import render_uvstep as R
    parsed, pos, uv0, uv1, idx, texs = R.load(MODEL)
    assert texs[0].shape[:2] == (256, 256) and texs[1].shape[0] == 8
    ustep = pgb2.parse_ustep(parsed['trailer'])
    assert set(ustep) == {0, 1} and not ustep[0]['use_timing'] and ustep[1]['use_timing']
    assert ustep[1]['timing_tex'] == 1
    # uniform sub: the frame colour at t=0..3 is the authored one
    names = {(220, 40, 40): 0, (40, 180, 60): 1, (50, 120, 230): 2, (230, 180, 30): 3}
    for t, want in ((0.0, 0), (1.5, 1), (2.5, 2), (3.9, 3)):
        im = np.asarray(R.render_face(parsed, pos, uv0, uv1, idx, texs, t,
                                      only=0).convert('RGB')).astype(int)
        dom = tuple(int(v) for v in np.median(im[100:156, 100:156].reshape(-1, 3), 0))
        got = min(names, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, dom)))
        assert names[got] == want, (t, dom)
    # timing sub: the left edge reads band 0 (red), the right edge the band
    # the scroll puts there (at t=0: band 2, blue); the red sliver is thin
    # (bands are 1/8 of the face width), so count colours, not medians
    im = np.asarray(R.render_face(parsed, pos, uv0, uv1, idx, texs, 0.0,
                                  only=1).convert('RGB')).astype(int)
    red = ((im[:, :, 0] > 150) & (im[:, :, 1] < 100) & (im[:, :, 2] < 100)).sum()
    blue = ((im[:, :, 2] > 150) & (im[:, :, 0] < 100)).sum()   # authored blue carries some green
    assert red > 1000 and blue > 5000, (red, blue)
    print('body: trailer, uniform frames, timing bands render the authored colours')


if __name__ == '__main__':
    test_trailer()
    test_math()
    test_body()
    print('UVSTEP TESTS PASS')

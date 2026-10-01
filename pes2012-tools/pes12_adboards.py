"""Custom pitch-side adboards for PES2012.

    python3 tools/pes12_adboards.py <game dir> ad1.png [ad2.png ...]
    python3 tools/pes12_adboards.py <game dir> --off

The pitch-side boards sample the game's DoubleFusion ad sheet, a texture
built at runtime that no game file holds. drawlogic.dll binds
<game>/kitserver/4cc-players/custom/boards/board_0.tex in its place on
every board face draw (dllprobe/drawlogic.cpp isBoardDecl/isAdSheet). This
writes that file from your ad images, cycled over all the sheet's slots;
--off removes it (stock boards).

Sheet layout, measured from the live sheet (texdump 01-10): 1024x512, a 4x8
grid of 256x64 ads, one ad per board panel. Which slots each board shows
depends on the stadium's board layout (10-stadiums.md "Adboards"), so every
slot is filled.
"""
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdl_to_pes12 import write_tex  # noqa: E402

SHEET_W, SHEET_H = 1024, 512
AD_W, AD_H = 256, 64
COLS, ROWS = SHEET_W // AD_W, SHEET_H // AD_H
BOARD_REL = os.path.join('kitserver', '4cc-players', 'custom', 'boards', 'board_0.tex')


def sheet(ads):
    """Ad images -> the 1024x512 sheet, ads cycled row-major over the slots."""
    out = Image.new('RGBA', (SHEET_W, SHEET_H))
    tiles = [ad.convert('RGBA').resize((AD_W, AD_H), Image.LANCZOS) for ad in ads]
    for k in range(COLS * ROWS):
        out.paste(tiles[k % len(tiles)], ((k % COLS) * AD_W, (k // COLS) * AD_H))
    return out


def install(game, paths):
    dst = os.path.join(game, BOARD_REL)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.png'
    sheet([Image.open(p) for p in paths]).save(tmp)
    write_tex(tmp, dst)
    os.remove(tmp)
    return dst


def demo():
    red, blue = Image.new('RGB', (10, 10), 'red'), Image.new('RGB', (10, 10), 'blue')
    s = sheet([red, blue])
    assert s.size == (SHEET_W, SHEET_H)
    assert s.getpixel((10, 10))[:3] == (255, 0, 0)          # slot 0
    assert s.getpixel((AD_W + 10, 10))[:3] == (0, 0, 255)   # slot 1
    assert s.getpixel((10, AD_H + 10))[:3] == (255, 0, 0)   # slot 4 = row 1, cycles back
    print('demo ok')


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    if sys.argv[2] == '--off':
        p = os.path.join(sys.argv[1], BOARD_REL)
        if os.path.exists(p):
            os.remove(p)
        print('stock boards')
    elif sys.argv[2] == '--demo':
        demo()
    else:
        print('wrote', install(sys.argv[1], sys.argv[2:]))

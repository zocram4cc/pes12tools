"""PES2012 emblem importer: team crests (dt04 entry 65) and league badges (entries 36-62).

Findings (25-09, all verified in game on v1.06):
- League badge = single-chunk WESYS around table container + ONE WE00 texture:
  `WE00 u8 0x3, u8 8, u8 0, u16 1, u16 0x100, u16 4`, 1024B palette (256 RGBA),
  65536 indices top-down, 16B trailer. Verbatim header + full custom palette
  boots fine (earlier exit-5 crashes were guessed header bytes).
  Entry map (decoded all 27): 36 English, 37 Ligue 1, 38 Campionato, 39 Eredivisie,
  40 Liga BBVA, 41 PES League (= VGL slot), 42 Portuguesa, 43 D2, ... (see all27 sheet).
- Team crest store = entry 65, multi-chunk WESYS: 348-row directory of
  (offset, length, name_offset); name table holds 20B `emblem_8080_<tid>_<s>` and
  16B `flag_8080_<code>` keys. Directory row = team-table slot - 81 (club
  crests; national flags after them), and a row's key is the string that
  starts 16 B AFTER its third word (236/240 stock rows fit; the earlier
  "keystart+4" reading is off by one row, which is why swaps landed on the
  neighbouring team - Serbia wearing a Scotland swap, /a/ wearing /3/).
  One row per team, keeping the victim row's `_r`/`_f` suffix.
- Container rebuild MUST be append-only: stock zlib streams bleed into the
  following chunk's first bytes (recorded lengths are short), so re-laying-out
  truncates every downstream subfile (this corrupted all national flags once).
  Keep stock bytes for untouched rows, append new/changed chunks + strings.

Usage:
  pes12emblem.py league <entry> <png> <out.bin>      # league badge builder
  pes12emblem.py teams <game> <out.bin> TIDSPEC ...  # team crest rebuild
    TIDSPEC = tid:pngpath:suffix  (suffix r|f; give both per tid)
  Special TIDSPEC: swap:ROW:pngpath  (overwrite existing row's pixels,
    keeps leader+key; e.g. swap:292:/3/.png for tests)
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import afs  # noqa: E402

DT04 = 'img/dt04.img'
BIN_TEAMS = 65
TABLE_HDR = struct.pack('<8I', 1, 8, 0x20, 0, 0xFFFFFFF0, 0, 0, 0)  # len patched per body
INNER_TAG = b'\x00\x00\x01WESYS'
WESYS_TAG = b'\x00\x01\x01WESYS'
BLEED_ZONE = 64
CREST_ROW_SLOT_OFFSET = 81  # club crest row = team slot - 81 (236 of 240 stock rows; in-game 25-09)
KEY_AFTER_THIRD = 16        # a row's key string starts 16 B after its third word
CREST_PX = 128  # stock crest textures: 128x128 palettised (17424 B body)


def _quantize(logo, ncolors=255):
    """-> (flat RGB palette padded to 256 entries, indices). Pillow returns
    only the colours it used, so simple logos come back short."""
    from PIL import Image
    q = logo.convert('RGB').quantize(colors=ncolors, method=Image.MEDIANCUT)
    pal = q.getpalette()[:768]
    return pal + [0] * (768 - len(pal)), list(q.getdata())


def build_league(game, entry, pngpath, outpath, size=256):
    """Replace one league badge: verbatim header/trailer, full custom palette."""
    from PIL import Image
    raw = afs.read(os.path.join(game, DT04), entry)
    data = zlib.decompress(raw[16:])[0x20:]
    we00hdr, trailer = data[:16], data[16 + 1024 + size * size:]
    pal = data[16:16 + 1024]
    bgidx = data[16 + 1024]
    logo = Image.open(pngpath).convert('RGBA').resize((size, size), Image.LANCZOS)
    pal256, idx = _quantize(logo)
    alp = list(logo.split()[3].getdata())
    palbytes = b''.join(bytes([pal256[k * 3], pal256[k * 3 + 1], pal256[k * 3 + 2], 255])
                         for k in range(255)) + bytes(pal[bgidx * 4:bgidx * 4 + 4])
    idx = [255 if a < 128 else v for v, a in zip(idx, alp)]
    body = we00hdr + palbytes + bytes(idx) + trailer
    assert len(body) == len(data), (len(body), len(data))
    full = TABLE_HDR[:12] + struct.pack('<I', len(body)) + TABLE_HDR[16:] + body
    comp = zlib.compress(full, 9)
    open(outpath, 'wb').write(WESYS_TAG + struct.pack('<II', len(comp), len(full)) + comp)
    print('league entry %d <- %s (%d bytes)' % (entry, pngpath, len(comp) + 16))


def _dir_rows(raw):
    n = struct.unpack_from('<I', raw, 16)[0]
    return n, [struct.unpack_from('<III', raw, 24 + i * 12) for i in range(n)]


def _getblob(raw, rows, i):
    from PIL import Image  # noqa
    o, l, _ = rows[i]
    cl = struct.unpack_from('<I', raw, o + 24)[0]
    return zlib.decompress(raw[o + 32:o + 32 + cl + 64])


def build_teams(game, outpath, specs, slots=None):
    """Append-only entry-65 rebuild. specs: [(tid,png,suffix)] + [(None,png,row)] swaps.
    slots: {tid: team-table slot}. The game indexes the crest directory BY
    TEAM SLOT: club crest row = slot - CREST_ROW_SLOT_OFFSET (national flags
    sit after the clubs).
    Each team gets exactly one row, its victim's own; suffix = the victim
    row's (the key the game asks for). Without slots, empty rows are used
    (key lookup, fine for one or two tids; misaligns at scale)."""
    from PIL import Image
    stock = afs.read(os.path.join(game, DT04), BIN_TEAMS)
    sn, srows = _dir_rows(stock)
    blobs, keyreq = {}, {}
    for tid, png, suffix in specs:
        if tid is None:  # swap: suffix field holds the row
            row = suffix
            b = _getblob(stock, srows, row)
            logo = Image.open(png).convert('RGBA')
            pal256, idx = _quantize(logo)
            alp = list(logo.split()[3].getdata())
            palbytes = b''.join(bytes([pal256[k * 3], pal256[k * 3 + 1], pal256[k * 3 + 2], 255])
                                 for k in range(255)) + b'\x00\x00\x00\x00'
            idx = [255 if a < 128 else v for v, a in zip(idx, alp)]
            blobs[row] = (b[:16], palbytes, bytes(idx))
            continue
        b = _getblob(stock, srows, _template_row(stock, srows))
        logo = Image.open(png).convert('RGBA').resize((CREST_PX, CREST_PX), Image.LANCZOS)
        pal256, idx = _quantize(logo)
        alp = list(logo.split()[3].getdata())
        palbytes = b''.join(bytes([pal256[k * 3], pal256[k * 3 + 1], pal256[k * 3 + 2], 255])
                             for k in range(255)) + b'\x00\x00\x00\x00'
        idx = [255 if a < 128 else v for v, a in zip(idx, alp)]
        if slots is not None:
            row = slots[tid] - CREST_ROW_SLOT_OFFSET
            old = _row_key(stock, srows[row])
            if old.startswith(b'emblem_8080_'):
                suffix = old[-1:].decode()
        else:
            row = _empty_row(srows, used=set(blobs))
        blobs[row] = (b[:16], palbytes, bytes(idx))
        keyreq[row] = 'emblem_8080_%04d_%s' % (tid, suffix)
    _assemble(stock, sn, srows, blobs, keyreq, outpath)


def _template_row(stock, srows):
    for i, (o, l, _) in enumerate(srows):
        if l != 0:
            return i
    raise ValueError('no template row')


def _row_key(stock, row):
    """Emblem row key (`emblem_8080_<tid>_<s>` at third + KEY_AFTER_THIRD), else b''."""
    _, l, third = row
    at = third + KEY_AFTER_THIRD
    key = stock[at:at + 20].split(b'\0')[0] if l and third else b''
    return key if key.startswith(b'emblem_8080_') else b''


def _empty_row(srows, used):
    for i, (o, l, _) in enumerate(srows):
        if l == 0 and i not in used:
            return i
    raise ValueError('directory full')


def _assemble(stock, sn, srows, blobs, keyreq, outpath):
    """Append-only layout: stock bytes untouched, new chunks + strings appended."""
    chunks = {}
    for row, (hdr, pal, idx) in blobs.items():
        oo, ll, _ = srows[row]
        lead = stock[oo:oo + 16] if ll != 0 else stock[srows[88][0]:srows[88][0] + 16]
        body = hdr + pal + idx
        comp = zlib.compress(body, 9)
        chunks[row] = lead + INNER_TAG + struct.pack('<II', len(comp), len(body)) + comp
    old_str = min(r[2] for r in srows if r[2] != 0)
    out = bytearray(stock[:old_str])
    out += stock[old_str:old_str + BLEED_ZONE]  # sacrificial bleed (row347's stream over-reads)
    while len(out) % 4:
        out.append(0)
    new_off = {}
    for row in sorted(chunks):
        new_off[row] = len(out)
        out += chunks[row]
        while len(out) % 4:
            out.append(0)
    new_str = len(out)
    strtab = stock[old_str:]
    newkeys = b''.join(k.encode() + b'\x00\x00' for _, k in sorted(
        ((r, keyreq[r]) for r in keyreq), key=lambda t: t[1]))
    # the game reads a row's key at third + KEY_AFTER_THIRD
    ordered = sorted(keyreq)
    newkeys = b''.join(keyreq[r].encode() + b'\x00\x00' for r in ordered)
    out += strtab + newkeys
    delta = new_str - old_str
    base = new_str + len(strtab)
    keyoff, at = {}, 0
    for r in ordered:
        keyoff[r] = base + at - KEY_AFTER_THIRD
        at += len(keyreq[r].encode()) + 2
    for i in range(sn):
        oo, ll, ss = srows[i]
        if i in chunks:
            ko = keyoff.get(i, ss + delta)
            struct.pack_into('<III', out, 24 + i * 12, new_off[i], len(chunks[i]), ko)
        else:
            struct.pack_into('<III', out, 24 + i * 12, oo, ll, (ss + delta) if ss != 0 else 0)
    open(outpath, 'wb').write(bytes(out))
    # verify every row decodes
    b = bytes(out)
    bad = [i for i in range(sn) if _row_broken(b, i)]
    print('teams -> %s (%d bytes), bad rows: %s' % (outpath, len(b), bad if bad else 'none'))


def _row_broken(b, i):
    import struct as S
    o, l, _ = S.unpack_from('<III', b, 24 + i * 12)
    if l == 0:
        return False
    try:
        cl = S.unpack_from('<I', b, o + 24)[0]
        return len(zlib.decompress(b[o + 32:o + 32 + cl + 64])) != 17424
    except Exception:
        return True


def _parse_spec(arg):
    if arg.startswith('swap:'):
        _, row, png = arg.split(':', 2)
        return (None, png, int(row))
    tid, png, suffix = arg.split(':')
    assert suffix in ('r', 'f'), suffix
    return (int(tid), png, suffix)


if __name__ == '__main__':
    if sys.argv[1] == 'league':
        build_league(sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5])
    elif sys.argv[1] == 'teams':
        build_teams(sys.argv[2], sys.argv[3], [_parse_spec(a) for a in sys.argv[4:]])

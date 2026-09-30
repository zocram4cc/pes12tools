"""PES2012 EDIT.bin access: load/save, player / roster / team rows, and the
stock-club renamer below (tools/pes12_import_team.py writes teams through it).

The game's EDIT save shadows the base DB: any team row whose second id word
has the high bit set (0x8000+) is an edited override. The 4cc 2012 saves used
this to rename stock clubs (/fa/ over CELUVARIS 2348 etc.). Our base rewrite
renames the clubs underneath, so the EDIT must agree or the old names win.

Usage:
    python3 pes12crypt.py dec EDIT.bin EDIT.plain
    python3 pes12edit.py EDIT.plain <game dir> <team_list> EDIT.out.plain
    python3 pes12crypt.py enc EDIT.out.plain EDIT.bin

The writer matches EDIT rows to base teams by victim id: plan[i] overwrote
victims[i] (same LEAGUE_CLUBS order as pes12db.cmd_overwrite), and the EDIT
row carrying the victim's stock id in its first id word gets the /name/,
the abbr, and the flag word keeps the row live (second id = tid|0x8000).
Rows with no EDIT row (109 REAL MADRID, 208 WOLVES in the 4cc save) are
reported, not invented. Round-trip check: decrypt -> write -> encrypt ->
decrypt == write output.
"""
import os
import re
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pes12crypt  # noqa: E402

TEAM_OFF = 0x42F5C0
TEAM_REC = 0x5B4
TEAM_ID_OFF = 0x5A4
TEAM_ABBR_OFF = 0x5A0
TEAM_NAMES = 20              # 0x48-byte name fields before the abbr (as dt04 entry 30)
NAME_LEN = 0x48
EDIT_FLAG = 0x8000
PLAYER_OFF = 0x328           # players, 0xE4 each, pid-sorted (07-edit-and-database.md)
PLAYER_REC = 0xE4
ROSTER_REC = 0xA4            # rosters follow the players: u32 team, 32 u32 pids, 32 u8 numbers


def load(path):
    """EDIT.bin -> editable plaintext."""
    return bytearray(pes12crypt.decrypt(open(path, 'rb').read()))


def save(path, plain):
    """Plaintext -> EDIT.bin; the first write keeps the original as
    <path>.pre-import (it is the user's save)."""
    if not os.path.exists(path + '.pre-import'):
        shutil.copy2(path, path + '.pre-import')
    open(path, 'wb').write(pes12crypt.encrypt(bytes(plain)))


def players(plain):
    """{pid: offset} of the player table; returns (map, table end)."""
    at, off, last = {}, PLAYER_OFF, -1
    while off + PLAYER_REC <= TEAM_OFF:
        pid = struct.unpack_from('<I', plain, off)[0]
        if pid <= last:
            break
        at[pid] = off
        last = pid
        off += PLAYER_REC
    return at, off


def roster(plain, tid):
    """Offset of team tid's roster row (the first with that team word)."""
    off = players(plain)[1]
    while off + ROSTER_REC <= TEAM_OFF:
        if struct.unpack_from('<I', plain, off)[0] == tid:
            return off
        off += ROSTER_REC
    raise KeyError('no EDIT roster for team %d' % tid)


def team_row(plain, tid):
    """Offset of team tid's row in the EDIT team table."""
    for off in range(TEAM_OFF, len(plain) - TEAM_REC + 1, TEAM_REC):
        a, b = struct.unpack_from('<HH', plain, off + TEAM_ID_OFF)
        if a == tid or b & ~EDIT_FLAG == tid:
            return off
    raise KeyError('no EDIT team row for team %d' % tid)


def rename(plain, tid, name, abbr):
    """Name every language field + shorthand of team tid; the flag word marks
    the row edited so it wins over the base."""
    o = team_row(plain, tid)
    field = name.encode('utf-8')[:NAME_LEN - 1].ljust(NAME_LEN, b'\0')
    for j in range(TEAM_NAMES):
        plain[o + j * NAME_LEN:o + (j + 1) * NAME_LEN] = field
    plain[o + TEAM_ABBR_OFF:o + TEAM_ABBR_OFF + 4] = abbr.encode()[:3].ljust(4, b'\0')
    struct.pack_into('<H', plain, o + TEAM_ID_OFF + 2, tid | EDIT_FLAG)


def load_plan(team_list):
    pat = re.compile(r'(\d+)\s+(\S+)\s+(.*)')
    out = []
    for line in open(team_list, encoding='utf-8'):
        if not line.strip():
            continue
        m = pat.match(line.strip())
        out.append((int(m.group(1)), m.group(2), m.group(3).strip()))
    return out


def victim_map(game, plan):
    """tid -> victim stock id, same order as pes12db.cmd_overwrite."""
    import pes12db as D
    _, t = D.teams(game)
    _, lists, _ = D.leagues(game)
    club_ids = set()
    for k in D.LEAGUE_CLUBS:
        club_ids.update(x for x in lists[k] if x)
    victims = [k for k, x in enumerate(t) if x['id'] in club_ids]
    by_id = {x['id'] for x in t}
    fresh = [e for e in plan if e[0] not in by_id]
    return dict(zip([e[0] for e in fresh], [t[k]['id'] for k in victims]))


def write_names(plain, game, plan):
    """Rewrite matching EDIT team rows in place. Returns (matched, missing)."""
    vmap = victim_map(game, plan)
    data = bytearray(plain)
    rows = {}
    off = TEAM_OFF
    while off + TEAM_REC <= len(data):
        r = bytes(data[off:off + TEAM_REC])
        if r[:NAME_LEN].split(b'\0')[0]:
            i1 = struct.unpack_from('<H', r, TEAM_ID_OFF)[0]
            if i1 not in rows:  # first live row per stock id (edited or not)
                rows[i1] = off
        off += TEAM_REC
    matched, missing = 0, []
    for tid, abbr, name in plan:
        v = vmap.get(tid)
        if v is None or v not in rows:
            missing.append((tid, name))
            continue
        o = rows[v]
        field = name.encode('utf-8')[:NAME_LEN - 1].ljust(NAME_LEN, b'\0')
        data[o:o + NAME_LEN] = field
        data[o + TEAM_ABBR_OFF:o + TEAM_ABBR_OFF + 4] = abbr.encode()[:3].ljust(4, b'\0')
        struct.pack_into('<HH', data, o + TEAM_ID_OFF, v, tid | EDIT_FLAG)
        matched += 1
    return bytes(data), matched, missing


def main(plain_path, game, team_list, out_path):
    plain = open(plain_path, 'rb').read()
    out, matched, missing = write_names(plain, game, load_plan(team_list))
    open(out_path, 'wb').write(out)
    print('pes12edit: matched %d rows, missing %s' % (matched, missing))
    return 0


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:5]))

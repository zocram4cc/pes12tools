"""PES2012 base database (img/dt04.img) reader/writer.

    python3 pes12db.py dump <game dir> <out dir>     # teams/rosters/leagues json
    python3 pes12db.py test701 <game dir> <out dir>  # add team 701 "/a/", write bins
    python3 pes12db.py overwrite <game dir> <team_list> <out> [pid_base]

`test701` writes dt04_30.bin / dt04_31.bin / dt04_33.bin (WESYS-wrapped,
ready to be served by kitserver afsio in place of afs 0x04 bins 30/31/33).
`overwrite` builds the full 4cc base (ov15): plan rows overwrite stock club
rows in place; 210 teams max (Edit Player sorts the player table and faults
past ~12146 rows: 4830 appends fit, 4876 do not). pid_base shifts placeholder
pids to (base+tid)*100+n; ov15 uses 2000 (no stock collision, no renumber).
"""
import json
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import afs  # noqa: E402

DT04 = 'img/dt04.img'
BIN_PLAYERS, BIN_TEAMS, BIN_ROSTERS, BIN_LEAGUES = 26, 30, 31, 33
PLAYER_REC = 0xE4
PLAYER_STATS = 0x7A          # stat block start (kept from template: Edit Player walks it)
HDR = 0x20
TEAM_REC = 0x5B4
TEAM_NAMES = 20              # 0x48-byte name fields before the id block
NAME_LEN = 0x48
TEAM_ABBR_OFF = 0x5A0
TEAM_ID_OFF = 0x5A4
TEAM_KEY_OFF = 0x5B0   # binary-search lookup key (stock: == id)
ROSTER_REC = 0xA4
ROSTER_SLOTS = 32
LEAGUE_ALIGN = 16
LEAGUE_PREMIER = 7           # English league list (block numbering of the IMG Explorer tutorial minus 1)
BIN_NAMES = 32               # entry 32: per-team display rows + multilingual names
NAMES_B0, NAMES_B1, NAMES_B2, NAMES_TAIL = 6960, 4176, 2784, 52896  # stock sizes: 348 x (20,12,8,152)
NAMES_B0_PER_TEAM = 20      # display row per team-table slot
NAMES_TAIL_PER_TEAM = 152   # multilingual name row per team-table slot
LEAGUE_CLUBS = range(7, 19)  # device-team league blocks; ONLY these rows are victims
LEAGUE_BOARDS, LEAGUE_BACKUP, LEAGUE_VGL, LEAGUE_INV = 7, 12, 13, 14  # 4cc group slots
BIN_FORMATIONS = 29        # entry 29: 316 x 132B formation rows, team u16 @0, 3 pids @28
PLAYER_TOP = 92024          # first free pid: above every tid*100+n, keeps the table sorted
PLAYER_ID_PER_TEAM = 100   # placeholder n of team tid gets pid tid*100+n (1..23)
FORMATION_REC = 132
FORMATION_PID_OFFS = (28, 32, 36)  # captain + set-piece takers (u32 pids)
WESYS_TAG = b'\x00\x01\x01WESYS'
PLACEHOLDER_STAT = 40  # flat placeholder ability, owner decision 25-09
# 0x7A attack, 0x7B defence, 0x8F teamwork, 0x92 tenacity + Technique/Speed/Physical
PLACEHOLDER_FLAT_40 = (0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85,
                       0x86, 0x87, 0x88, 0x89, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90,
                       0x92, 0x93)  # 0x9A is development type, not a stat (edit_experiment 25-09)
POSITION_FLAG_BIT = 0x80  # stat bytes: low 7 bits = value, bit 7 = playable flag
REGISTERED_CB_79, REGISTERED_CB_A1 = 0x20, 0x28  # game-written registered CB
# player record name fields (stock Messi/Xavi/C. Ronaldo rows, 25-09): UTF-8, NUL-padded
PLAYER_JP_NAME_OFF, PLAYER_NAME_OFF, PLAYER_SHIRT_OFF = 0x04, 0x32, 0x60
PLAYER_NAME_LEN, PLAYER_SHIRT_LEN = 0x2E, 0x10


def unwesys(raw):
    return zlib.decompress(raw[16:])


def wesys(body):
    comp = zlib.compress(body, 9)
    return WESYS_TAG + struct.pack('<II', len(comp), len(body)) + comp


def container(body_len, blocks=1):
    return struct.pack('<8I', blocks, 8, HDR, body_len, 0xFFFFFFF0, 0, 0, 0)


def read_table(game, binid):
    b = unwesys(afs.read(os.path.join(game, DT04), binid))
    return b[HDR:]


def teams(game):
    body = read_table(game, BIN_TEAMS)
    out = []
    for k in range(len(body) // TEAM_REC):
        r = body[k * TEAM_REC:(k + 1) * TEAM_REC]
        out.append(dict(name=r[:NAME_LEN].split(b'\0')[0].decode('utf-8', 'replace'),
                        abbr=r[TEAM_ABBR_OFF:TEAM_ABBR_OFF + 4].split(b'\0')[0].decode(),
                        id=struct.unpack_from('<H', r, TEAM_ID_OFF)[0],
                        tail=r[TEAM_ID_OFF + 2:].hex(), raw=r))
    return body, out


def rosters(game):
    body = read_table(game, BIN_ROSTERS)
    out = []
    for k in range(len(body) // ROSTER_REC):
        r = body[k * ROSTER_REC:(k + 1) * ROSTER_REC]
        tid = struct.unpack_from('<I', r)[0]
        pids = struct.unpack_from('<%dI' % ROSTER_SLOTS, r, 4)
        nums = list(r[4 + 4 * ROSTER_SLOTS:])
        out.append(dict(team=tid, players=[p for p in pids if p], numbers=nums))
    return body, out


def leagues(game):
    """Entry 33. Header: n, 8, hs=12+12n, then n x (byteLen, 0xFFFFFFF0, end).
    Lists 0..n-2 follow the header back to back, each zero-padded to 16 bytes
    (end = absolute end of the padded list). The last header entry (end = 0)
    is a 16-byte block that closes the body. Returns (raw, lists, tail)."""
    b = unwesys(afs.read(os.path.join(game, DT04), BIN_LEAGUES))
    n, _, hs = struct.unpack_from('<III', b)
    ent = [struct.unpack_from('<III', b, 12 + 12 * k) for k in range(n)]
    start, out = hs, []
    for size, _, end in ent[:-1]:
        out.append(list(struct.unpack_from('<%dH' % (size // 2), b, start)))
        start = end
    tail = b[start:start + ent[-1][0]]
    return b, out, tail


def write_leagues(lists, tail):
    n = len(lists) + 1
    hs = 12 + 12 * n
    bodies, ends, o = [], [], hs
    for lst in lists:
        raw = struct.pack('<%dH' % len(lst), *lst)
        raw += b'\0' * ((-len(raw)) % LEAGUE_ALIGN)
        bodies.append(raw)
        o += len(raw)
        ends.append(o)
    hdr = struct.pack('<III', n, 8, hs)
    for k, lst in enumerate(lists):
        hdr += struct.pack('<III', len(lst) * 2, 0xFFFFFFF0, ends[k])
    hdr += struct.pack('<III', len(tail), 0xFFFFFFF0, 0)
    return hdr + b''.join(bodies) + tail


def names_blocks(game):
    b = unwesys(afs.read(os.path.join(game, DT04), BIN_NAMES))
    n, _, hs = struct.unpack_from('<III', b)
    ent = [struct.unpack_from('<III', b, 12 + 12 * k) for k in range(n)]
    s, out = hs, []
    for k, (size, _, end) in enumerate(ent[:-1]):
        out.append(b[s:end]); s = end
    out.append(b[s:s + ent[-1][0]])
    return b, out

def write_names(blocks):
    hs = 12 + 12 * len(blocks) + 4  # hs=0x40: header + 4 pad bytes (unknown dwords)
    hdr = struct.pack('<III', len(blocks), 8, hs)
    o = hs; ents = []
    for k, blk in enumerate(blocks):
        o += len(blk)
        ents.append((len(blk), 0xFFFFFFF0, o if k < len(blocks) - 1 else 0))
    for e in ents:
        hdr += struct.pack('<III', *e)
    hdr += b'\0\0\0\0'
    return hdr + b''.join(blocks)


def make_team(template, tid, name, abbr):
    r = bytearray(template)
    field = name.encode('utf-8')[:NAME_LEN - 1].ljust(NAME_LEN, b'\0')
    for k in range(TEAM_NAMES):
        r[k * NAME_LEN:(k + 1) * NAME_LEN] = field
    r[TEAM_ABBR_OFF:TEAM_ABBR_OFF + 4] = abbr.encode()[:3].ljust(4, b'\0')
    struct.pack_into('<HH', r, TEAM_ID_OFF, tid, tid)
    struct.pack_into('<H', r, TEAM_KEY_OFF, tid)  # lookup key @0x5B0 (binary search); 0x5B2 stays template
    return bytes(r)


def cmd_dump(game, out):
    os.makedirs(out, exist_ok=True)
    _, t = teams(game); _, r = rosters(game); _, l, _ = leagues(game)
    t = [{k: v for k, v in x.items() if k != 'raw'} for x in t]
    json.dump(t, open(os.path.join(out, 'teams.json'), 'w'), indent=0)
    json.dump(r, open(os.path.join(out, 'rosters.json'), 'w'), indent=0)
    json.dump(l, open(os.path.join(out, 'leagues.json'), 'w'), indent=0)
    print('teams %d rosters %d leagues %d' % (len(t), len(r), len(l)))


def cmd_test701(game, out, tid=701, name='/a/', abbr='A', template=100):
    """Proven single-team control: append ONE Man Utd clone (+id key fix) to
    bins 26/30/31/32, add tid to the English league list."""
    os.makedirs(out, exist_ok=True)
    tbody, t = teams(game)
    pbody = read_table(game, BIN_PLAYERS)
    rbody, r = rosters(game)
    _, lists, tail = leagues(game)
    _, nblks = names_blocks(game)
    ti = next(k for k, x in enumerate(t) if x['id'] == template)
    ri = next(k for k, x in enumerate(r) if x['team'] == template)
    tbody2 = tbody + make_team(tbody[ti * TEAM_REC:(ti + 1) * TEAM_REC], tid, name, abbr)
    rec = bytearray(rbody[ri * ROSTER_REC:(ri + 1) * ROSTER_REC])
    struct.pack_into('<I', rec, 0, tid)
    rbody2 = rbody + bytes(rec)
    row0 = bytearray(nblks[0][ti * 20:(ti + 1) * 20])
    struct.pack_into('<HH', row0, 0, tid, tid)
    nrow = bytearray(nblks[3][ti * 152:(ti + 1) * 152])
    field = name.encode()[:71]
    nrow[0:72] = field.ljust(72, b'\0')
    nrow[72:76] = abbr.encode()[:3].ljust(4, b'\0')
    nrow[76:152] = field.ljust(76, b'\0')
    names = write_names([nblks[0] + bytes(row0),
                         nblks[1] + nblks[1][ti * 12:(ti + 1) * 12],
                         nblks[2] + nblks[2][ti * 8:(ti + 1) * 8],
                         nblks[3] + bytes(nrow)])
    lists2 = [list(x) for x in lists]
    lists2[LEAGUE_PREMIER].append(tid)
    for binid, data in ((BIN_TEAMS, container(len(tbody2)) + tbody2),
                        (BIN_ROSTERS, container(len(rbody2)) + rbody2),
                        (BIN_NAMES, names),
                        (BIN_LEAGUES, write_leagues(lists2, tail))):
        open(os.path.join(out, 'dt04_%d.bin' % binid), 'wb').write(wesys(data))
    print('wrote test701: teams %d rosters %d premier %d' % (
        len(tbody2) // TEAM_REC, len(rbody2) // ROSTER_REC, len(lists2[LEAGUE_PREMIER])))


def _splice(body, rec_len, slot, rec):
    if slot is None:
        return body + rec
    return body[:slot * rec_len] + rec + body[(slot + 1) * rec_len:]


def _player(pid, shirt, template):
    """Placeholder player: pid twice, shirt/truename overwritten, flat-40
    abilities, CB-only position.
    Position (25-09, game-written CB-only edit of pid 270101 vs template 4618):
    playable flags are bit 7 of the stat bytes - GK = 0x7A.7, CB = 0x7C.7 -
    registered position is 0x79 (0x00 GK -> 0x20 CB) plus 0xA1 (0x00 -> 0x28).
    Stats are the LOW 7 bits: flattening must keep bit 7, or the playable
    flags are wiped and the game falls back to GK.
    Weak foot: 0x94 bits 3-5 = usage-1, 0x95 bits 3-5 = accuracy-1
    ((4,4) -> 0x5c/0x1b, (6,5) -> 0x6c/0x23); kept at template (4/4).
    Byte map (25-09, in-game batch-edit experiment on pid 270101: each stat
    set to a unique value, save, decrypt EDIT.bin, diff vs base):
    Attack 0x7A (low 7 bits; high bit set on template), Defence 0x7B,
    Header 0x8D, ShortPassAcc/Spd 0x84/0x85, LongPassAcc/Spd 0x86/0x87,
    ShotAcc 0x88, PlaceKick 0x8B, Swerve 0x8C, BallControl 0x90,
    Teamwork 0x8F, Tenacity 0x92, Form 0x72, GK-side bytes 0x93/0x9A.
    Kept at template: 0xB3 (attack mirror), 0x72/0x74 (form, stars),
    0x7F/0x8A/0x91/0x99/0x9B/0xA0+
    (flags/cards/unknown), height/weight/injury (unmapped). 40 (0x28) is a
    safe in-range stat: no fault risk (only zeroed binary fields fault)."""
    r = bytearray(template)
    struct.pack_into('<I', r, 0, pid)
    struct.pack_into('<I', r, 0x70, pid)
    nm = shirt.encode()
    r[PLAYER_NAME_OFF:PLAYER_NAME_OFF + PLAYER_NAME_LEN] = nm[:PLAYER_NAME_LEN - 1].ljust(PLAYER_NAME_LEN, b'\0')
    r[PLAYER_SHIRT_OFF:PLAYER_SHIRT_OFF + PLAYER_SHIRT_LEN] = nm.upper()[:PLAYER_SHIRT_LEN - 1].ljust(PLAYER_SHIRT_LEN, b'\0')
    for o in PLACEHOLDER_FLAT_40:
        r[o] = (r[o] & POSITION_FLAG_BIT) | PLACEHOLDER_STAT
    r[0x7A] &= ~POSITION_FLAG_BIT & 0xFF  # drop playable GK
    r[0x7C] |= POSITION_FLAG_BIT          # playable CB
    r[0x79] = REGISTERED_CB_79
    r[0xA1] = REGISTERED_CB_A1
    return bytes(r)

def _roster(tid, pids):
    rec = bytearray(ROSTER_REC)
    struct.pack_into('<I', rec, 0, tid)
    struct.pack_into('<%di' % len(pids), rec, 4, *pids)
    rec[4 + 4 * ROSTER_SLOTS:] = bytes(((k % 11) + 1 for k in range(ROSTER_SLOTS)))
    return bytes(rec)


def cmd_gen(game, team_list, out, template_team=100):
    """Full 4cc base: every team_list row (id abbr /name/) gets a Man Utd
    clone + 23 all-40 placeholder players (ids tid*100+1..23), appended to
    bins 26/30/31/32; boards -> English league, VGL/Invitational ->
    blocks 13/14. Colliding stock ids (869, 902-908) keep their stock rows
    and are skipped in the league lists."""
    rows = [l.split() for l in open(team_list, encoding='utf-8') if l.strip()]
    os.makedirs(out, exist_ok=True)
    tbody, t = teams(game)
    pbody = read_table(game, BIN_PLAYERS)
    rbody, r = rosters(game)
    _, lists, tail = leagues(game)
    _, nblks = names_blocks(game)
    ti = next(k for k, x in enumerate(t) if x['id'] == template_team)
    ri = next(k for k, x in enumerate(r) if x['team'] == template_team)
    have = {x['id'] for x in t}
    boards, vgl, inv = [], [], []
    import re as _re
    pat = _re.compile(r'(\d+)\s+(\S+)\s+(.*)')
    for m in (pat.match(l.strip()) for l in open(team_list, encoding='utf-8') if l.strip()):
        tid = int(m.group(1)); abbr, name = m.group(2), m.group(3)
        if tid in have:
            continue  # stock id (869, 902-908 classics): keep stock row, skip league
        (inv if 'nvitational' in name else
         vgl if 'VGL' in name else boards if 'Backup' not in name else []).append(tid)
        tbody += make_team(tbody[ti * TEAM_REC:(ti + 1) * TEAM_REC], tid, name.strip('/'), abbr)
        p0 = 120000 + (tid - 701) * 23
        for k in range(23):
            pbody += _player(p0 + k, 'P%02d' % (k + 1), pbody[:PLAYER_REC])
        rbody += _roster(tid, [p0 + k for k in range(23)] + [0] * (ROSTER_SLOTS - 23))
        row0 = bytearray(nblks[0][ti * 20:(ti + 1) * 20])
        struct.pack_into('<HH', row0, 0, tid, tid)
        nrow = bytearray(nblks[3][ti * 152:(ti + 1) * 152])
        field = name.strip('/').encode()[:71]
        nrow[0:72] = field.ljust(72, b'\0')
        nrow[72:76] = abbr.encode()[:3].ljust(4, b'\0')
        nrow[76:152] = field.ljust(76, b'\0')
        nblks = [nblks[0] + bytes(row0),
                 nblks[1] + nblks[1][ti * 12:(ti + 1) * 12],
                 nblks[2] + nblks[2][ti * 8:(ti + 1) * 8],
                 nblks[3] + bytes(nrow)]
    lists2 = [list(x) for x in lists]
    lists2[LEAGUE_PREMIER] = boards
    lists2[13] = vgl[:20]
    lists2[14] = inv[:20]
    for binid, data in ((BIN_PLAYERS, container(len(pbody)) + pbody),
                        (BIN_TEAMS, container(len(tbody)) + tbody),
                        (BIN_ROSTERS, container(len(rbody)) + rbody),
                        (BIN_NAMES, write_names(nblks)),
                        (BIN_LEAGUES, write_leagues(lists2, tail))):
        open(os.path.join(out, 'dt04_%d.bin' % binid), 'wb').write(wesys(data))
    print('wrote: teams %d players %d rosters %d boards %d vgl %d inv %d' % (
        len(tbody) // TEAM_REC, len(pbody) // PLAYER_REC, len(rbody) // ROSTER_REC, len(boards), len(vgl), len(inv)))


def cmd_overwrite(game, team_list, out, pid_base=0):
    """4cc base at stock sizes: the 4cc rows (ids 701+) OVERWRITE stock
    CLUB rows in place (league blocks 7-18, nothing else). Special rows are
    never touched: nationals/classics (0-6), ML hide/training/created
    (19-21), and the 19 rows in no league at all (ML DEFAULT, FREE AGENTS,
    ML MY TEAM, CREATED PLAYERS, staff pseudo-teams AGENT/COACH/...).
    Overwriting those rosters page-faults the exe at 0119E9EB (B12: classic
    902; B14: all non-classic victims incl. the no-league specials), while a
    club repurpose boots (B13). team_list ids that collide with a stock row
    (869 ML HIDE3, 902-908 classics) are skipped, not overwritten.
    Bin 31 keeps its stock row count: each new team repurposes its victim
    club's roster row. Players (bin 26): stock + appended tid*100+n
    PLACEHOLDERs; colliding stock pids renumbered with roster + formation
    refs patched."""
    import re as _re
    pat = _re.compile(r'(\d+)\s+(\S+)\s+(.*)')
    entries = [(int(m.group(1)), m.group(2), m.group(3).strip())
               for m in (pat.match(l.strip()) for l in open(team_list, encoding='utf-8') if l.strip())]
    tbody, t = teams(game)
    pbody = bytearray(read_table(game, BIN_PLAYERS))
    rbody = bytearray(read_table(game, BIN_ROSTERS))
    fbody = bytearray(read_table(game, BIN_FORMATIONS))
    r = rosters(game)[1]
    _, lists, tail = leagues(game)
    _, nblks = names_blocks(game)
    nrows = len(tbody) // TEAM_REC
    np0 = len(pbody) // PLAYER_REC

    club_ids = set()
    for k in LEAGUE_CLUBS:
        club_ids.update(x for x in lists[k] if x)
    victims = [k for k, x in enumerate(t) if x['id'] in club_ids]
    assert all(t[k]['id'] in club_ids for k in victims)
    by_id = {x['id']: k for k, x in enumerate(t)}
    rslot = {}
    for k, x in enumerate(r):
        rslot.setdefault(x['team'], k)
    groups = {'board': [], 'backup': [], 'vgl': [], 'inv': []}
    plan = []
    skipped = []
    for tid, abbr, name in entries:
        if tid in by_id:
            skipped.append(tid)  # collides with a stock/special row (869, 902-908): keep stock
            continue
        key = ('inv' if 'nvitational' in name else 'vgl' if 'VGL' in name
               else 'backup' if 'Backup' in name else 'board')
        groups[key].append(tid)
        plan.append((tid, abbr, name))
    if len(plan) > len(victims):
        raise SystemExit('4cc needs %d slots, only %d club rows are disposable'
                         % (len(plan), len(victims)))
    slot_of = dict(zip([e[0] for e in plan], victims))
    gone_ids = {t[k]['id'] for k in victims[:len(plan)]}
    want = {(pid_base + tid) * PLAYER_ID_PER_TEAM + k for tid, _, _ in plan for k in range(1, 24)}
    hit = {}  # stock pid -> PLAYER_TOP+n: the only free pid gap in the table
    top = PLAYER_TOP
    for k in range(np0):
        pid = struct.unpack_from('<I', pbody, k * PLAYER_REC)[0]
        if pid in want and pid not in hit:
            hit[pid] = top
            top += 1
    for k in range(np0):
        pid = struct.unpack_from('<I', pbody, k * PLAYER_REC)[0]
        if pid in hit:
            struct.pack_into('<I', pbody, k * PLAYER_REC, hit[pid])
            struct.pack_into('<I', pbody, k * PLAYER_REC + 0x70, hit[pid])
    for k in range(len(rbody) // ROSTER_REC):
        for j in range(ROSTER_SLOTS):
            o = k * ROSTER_REC + 4 + 4 * j
            pid = struct.unpack_from('<I', rbody, o)[0]
            if pid in hit:
                struct.pack_into('<I', rbody, o, hit[pid])
    fslot = {}
    for k in range(len(fbody) // FORMATION_REC):
        fslot.setdefault(struct.unpack_from('<H', fbody, k * FORMATION_REC)[0], k)
    for k in range(len(fbody) // FORMATION_REC):
        for o in (k * FORMATION_REC + o for o in FORMATION_PID_OFFS):
            pid = struct.unpack_from('<I', fbody, o)[0]
            if pid in hit:
                struct.pack_into('<I', fbody, o, hit[pid])
    # placeholder template: a real national-team player row (Austria 4618).
    # DUMMY/row-0 is a degenerate record the Edit Player screen rejects.
    pix = {struct.unpack_from('<I', pbody, k * PLAYER_REC)[0]: k for k in range(np0)}
    tpl = bytes(pbody[pix[4618] * PLAYER_REC:(pix[4618] + 1) * PLAYER_REC])
    tb = bytearray(tbody)
    n0, n3 = bytearray(nblks[0]), bytearray(nblks[3])
    for tid, abbr, name in plan:
        slot = slot_of[tid]
        tb[slot * TEAM_REC:(slot + 1) * TEAM_REC] = make_team(
            tbody[slot * TEAM_REC:(slot + 1) * TEAM_REC], tid, name, abbr)
        struct.pack_into('<HH', n0, slot * NAMES_B0_PER_TEAM, tid, tid)
        field = name.encode()[:71]
        n3[slot * NAMES_TAIL_PER_TEAM:slot * NAMES_TAIL_PER_TEAM + 72] = field.ljust(72, b'\0')
        n3[slot * NAMES_TAIL_PER_TEAM + 72:slot * NAMES_TAIL_PER_TEAM + 76] = abbr.encode()[:3].ljust(4, b'\0')
        newpids = [(pid_base + tid) * PLAYER_ID_PER_TEAM + k for k in range(1, 24)]
        for k, pid in enumerate(newpids, 1):
            pbody += _player(pid, 'PLACEHOLDER', tpl)
        old = rslot.get(t[slot]['id'])
        assert old is not None, (tid, t[slot]['id'])  # club rows always have one (verified)
        rec = bytearray(rbody[old * ROSTER_REC:(old + 1) * ROSTER_REC])
        struct.pack_into('<I', rec, 0, tid)
        struct.pack_into('<%di' % len(newpids), rec, 4, *newpids)  # numbers tail stays victim's
        rec[4 + 4 * 23:4 + 4 * ROSTER_SLOTS] = b'\0' * 4 * (ROSTER_SLOTS - 23)
        rec[4 + 4 * ROSTER_SLOTS + 23:] = b'\xff' * (ROSTER_SLOTS - 23)
        rbody[old * ROSTER_REC:(old + 1) * ROSTER_REC] = rec
        fold = fslot.get(t[slot]['id'])
        assert fold is not None, (tid, t[slot]['id'])  # every club has a formation row (verified)
        fo = fold * FORMATION_REC
        struct.pack_into('<H', fbody, fo, tid)
        for j, o in enumerate(FORMATION_PID_OFFS):
            if struct.unpack_from('<I', fbody, fo + o)[0]:  # captain / set-piece takers -> first placeholders
                struct.pack_into('<I', fbody, fo + o, newpids[j])
    # the game binary-searches the player table: stock is sorted, so the full
    # table (stock + renumbered + placeholders) must be sorted too. Appends
    # land after 110117, so one final sort fixes the boundary.
    order = sorted(range(len(pbody) // PLAYER_REC),
                   key=lambda k: struct.unpack_from('<I', pbody, k * PLAYER_REC)[0])
    pbody = bytearray(b''.join(pbody[k * PLAYER_REC:(k + 1) * PLAYER_REC] for k in order))
    lists2 = [list(x) for x in lists]
    lists2[LEAGUE_BOARDS] = groups['board']
    lists2[LEAGUE_BACKUP] = groups['backup']
    lists2[LEAGUE_VGL] = groups['vgl']
    lists2[LEAGUE_INV] = groups['inv']
    for k in range(len(lists2)):
        if k in (LEAGUE_BOARDS, LEAGUE_BACKUP, LEAGUE_VGL, LEAGUE_INV):
            continue
        lists2[k] = [x for x in lists2[k] if x and x not in gone_ids]

    os.makedirs(out, exist_ok=True)
    for binid, data in ((BIN_PLAYERS, container(len(pbody)) + bytes(pbody)),
                        (BIN_TEAMS, container(len(tb)) + bytes(tb)),
                        (BIN_ROSTERS, container(len(rbody)) + bytes(rbody)),
                        (BIN_FORMATIONS, container(len(fbody)) + bytes(fbody)),
                        (BIN_NAMES, write_names([bytes(n0), nblks[1], nblks[2], bytes(n3)])),
                        (BIN_LEAGUES, write_leagues(lists2, tail))):
        open(os.path.join(out, 'dt04_%d.bin' % binid), 'wb').write(wesys(data))
    print('overwrote: team rows %d fresh %d skipped-stock %s pids moved %d players %d rosters %d | boards %d backup %d vgl %d inv %d' % (
        nrows, len(plan), skipped, len(hit), len(pbody) // PLAYER_REC,
        len(rbody) // ROSTER_REC, len(groups['board']), len(groups['backup']),
        len(groups['vgl']), len(groups['inv'])))
    return plan, slot_of  # tid -> team-table slot (the victim's)


if __name__ == '__main__':
    if sys.argv[1] == 'test701':
        cmd_test701(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == 'overwrite':
        cmd_overwrite(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]) if len(sys.argv) > 5 else 0)
    elif sys.argv[1] == 'gen':
        cmd_gen(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        cmd_dump(sys.argv[2], sys.argv[3])

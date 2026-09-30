"""Import a 4cc team (aesthetics export + optional tactical export) into the
PES2012 4cc base: squad, lineup, formation, and a custom full body for each
starter.

    python3 pes12_import_team.py <aesthetics> <EDIT.bin> <custom dir> <GDB dir>
        [--export=<tactical export>] [--tid=N] [--pes17] [--tactics=<dt04 dir>]
        [--rename=<name>] [--all] [--pes21=<PES2021 Data dir>] [--pes15=<PES2015 Data dir>]

  aesthetics  the aesthetics export: a folder or a .7z; its Faces/ and Kit
              Textures/ are found inside (the kit is the u0???p1.dds there)
  EDIT.bin    the game's save/EDIT.bin: it shadows the base DB, so squads,
              stats and names go there (the 4cc DLC base keeps its 40-rated
              CB placeholders untouched; game closed while this runs)
  custom dir  drawlogic's model folder (one sub-folder p<pid> per player)
  GDB dir     kitserver/GDB (faces/map.txt + marked face.bin per player)
  --export    PES2015 tactical export *.bin, a PES2017 TEXPORT (--pes17), or a
              4ccEditor squad save *.4ccs. Without one the face folders are
              the roster (names only; numbers, stats, positions stay the DLC's).
  --tid       the team slot; required without an export or with a .4ccs,
              otherwise it overrides the export's own id.

Squad: the team's 23 PLACEHOLDER rows ((2000 + tid) * 100 + slot + 1) take
the export's players in LINEUP order (slot 0 GK, 1-10 the outfield eleven in
formation order, then the bench), so roster slot k = formation slot k.
Formation: each outfield slot's export position + (depth, lateral) becomes a
PES2012 role code (entry 29 +0x50, one per outfield slot) + coordinates
(+0x5A depth, +0x64 lateral) - both games use the same 0-100 pitch scale
with lateral 0 = left touchline. Captain = the export's captain.
--tactics=<dt04 dir> writes the formation row, captain and set-piece takers
into that base dt04 entry 29 (the EDIT formation layout is not mapped yet);
without it, and whenever the export has no lineup (.4ccs, none), no tactics.
Medals (PES17 / .4ccs): gold players (4ccEditor aatf.cpp goldRate 99) take 99
in every ability stat, silver players (silverRate 88) take 88; regulars keep
the export's values mapped below.
Bodies: tools/pes15_to_pes12.py per starter (all with --all); identity rides
fserv (the player's face.bin carries the player id as drawlogic's marker,
tools/mark_face.py; drawlogic hot-loads custom/p<pid>/, any number of them).
The team's GDB map.txt lines are rewritten here and its bodies no longer
imported are deleted, so rerunning an import is safe.
Kits: every u0XXX{p,g}N.dds beside the kit -> <custom>/kits/<tid> (tools/pes15_kits.py;
the layout tables are built once from a PES2015 install, --pes15; --pes21 supplies
the engine textures 4cc faces name).
--rename=<name> renames the team (EDIT team row, all languages + shorthand,
marked edited); league lists key off the tid, so the grouping is untouched.
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pes12db as D  # noqa: E402
import pes12player as P  # noqa: E402
import pes15export as X  # noqa: E402
import pes17export as X17  # noqa: E402
import pes15_kits as K  # noqa: E402
import pes12edit as E  # noqa: E402

PID_BASE = 2000                 # the DLC's placeholder pid base
STARTERS = 11
FACE_TEMPLATE = 'faces/anyukov/face_orig_backup.bin'   # any stock GDB face: only its mips carry the marker
ROLE_OFF, DEPTH_OFF, LATERAL_OFF = 0x50, 0x5A, 0x64    # entry 29, per outfield slot (10 each)
LEFT_MAX, RIGHT_MIN = 40, 60    # lateral thresholds for left / centre / right roles
# PES15 position -> PES2012 formation role codes (left, centre, right), from the
# stock formations' role vs coordinate distribution (25-09)
ROLE = {'CB': (1, 3, 7), 'LB': (8, 8, 8), 'RB': (9, 9, 9), 'DMF': (10, 12, 14),
        'CMF': (17, 19, 21), 'LMF': (22, 22, 22), 'RMF': (23, 23, 23), 'AMF': (24, 26, 28),
        'LWF': (29, 29, 29), 'RWF': (30, 30, 30), 'SS': (31, 33, 35), 'CF': (36, 38, 40)}
WEAK_FOOT_SCALE = 2             # PES15 weak foot 0-3 -> PES2012 1-8: (v + 1) * 2
# Medal ability floors (4ccEditor aatf.cpp goldRate/silverRate): every PES12
# ability stat of a gold player reads 99, of a silver player 88.
MEDAL_STAT = {'gold': 99, 'silver': 88}
def stats12(p):
    """PES player entry -> pes12player field dict."""
    reg = X.POSITIONS[p['reg_pos']] if p['reg_pos'] < len(X.POSITIONS) else 'CB'
    d = {
        'name': p['name'], 'shirt': (p['shirt'] or p['name'])[:P.SHIRT_LEN - 1],
        'attack': p['atk'], 'defence': p['def'], 'header': p['header'],
        'dribble_acc': p['drib'], 'sp_acc': p['lowpass'], 'sp_spd': p['lowpass'],
        'lp_acc': p['loftpass'], 'lp_spd': p['loftpass'], 'shot_acc': p['finish'],
        'place_kick': p['place_kick'], 'swerve': p['swerve'], 'ball_control': p['ball_ctrl'],
        'gk': max(p['gk'], p['catching']),
        'weak_acc': (p['weak_acc'] + 1) * WEAK_FOOT_SCALE, 'weak_use': (p['weak_use'] + 1) * WEAK_FOOT_SCALE,
        'responsiveness': p['exp_pwr'], 'explosive': p['exp_pwr'], 'dribble_spd': p['speed'],
        'top_speed': p['speed'], 'body_balance': p['body_ctrl'], 'stamina': p['stamina'],
        'kick_power': p['kick_pwr'], 'jump': p['jump'], 'teamwork': p['lowpass'], 'tenacity': p['ball_win'],
        'height': p['height'], 'weight': p['weight'], 'age': p['age'],
        'left_foot': p['strong_foot'], 'registered': P.POSITIONS.index(reg),
    }
    for pos in P.POSITIONS:
        d['play_' + pos.lower()] = int(pos == reg or p['play_pos'].get(pos, 0) > 0)
    medal = p.get('medal') # PES17: gold/silver take every ability stat at the medal rate
    if medal in MEDAL_STAT:
        for f in P.ABILITIES:
            if f.key not in ('weak_acc', 'weak_use'):
                d[f.key] = MEDAL_STAT[medal]
    return d


CB_ROLES = ROLE['CB']
DEFENCE_MAX_ROLE, MIDFIELD_MAX_ROLE = 9, 28   # role code bands (stock formations)


def band(r):
    return 0 if r <= DEFENCE_MAX_ROLE else 1 if r <= MIDFIELD_MAX_ROLE else 2


def role(pos, lateral):
    trio = ROLE.get(pos, ROLE['CMF'])
    return trio[0] if lateral < LEFT_MAX else trio[2] if lateral > RIGHT_MIN else trio[1]


def load_bin(path):
    raw = zlib.decompress(open(path, 'rb').read()[16:])
    return raw[:0x20], bytearray(raw[0x20:])


def save_bin(path, head, body):
    open(path, 'wb').write(D.wesys(head + bytes(body)))


def aes_roster(faces, tid):
    """Aesthetics-only roster (teams with no tactics export at all): the face
    folders ARE the registered roster, slot nn = placeholder (2000+tid)*100+nn.
    Numbers/positions/stats stay the DLC's; only names come from the folders."""
    by_nn = {}
    for f in sorted(os.listdir(faces)):
        m = re.match(r'^(?:XXX|\d{3}|[A-Za-z]{3})([0-9]{2}) - (.*)$', f)
        if m and os.path.isdir(os.path.join(faces, f)):
            by_nn[int(m.group(1))] = m.group(2).strip()
    roster = [((PID_BASE + tid) * 100 + nn, 0) for nn in sorted(by_nn)]
    players = {pid: {'name': by_nn[pid % 100], 'shirt': by_nn[pid % 100],
                     'appearance': None} for pid, _ in roster}
    return {'captain_pid': roster[0][0], 'team': {'id': tid},
            'roster': roster, 'lineup': None, 'players': players}


def face_folder(faces, pid):
    """Export source pid -> its face folder. PES15 packs number folders with
    the pid (72101 - Name); PES17 packs use a 3-letter prefix instead
    (XXX01 - Name, MLP01 - Name), so match the index nn = pid % 100 either way."""
    n = pid % 100
    for f in os.listdir(faces):
        if ((f[:5].isdigit() and int(f[3:5]) == n)
                or (len(f) >= 5 and f[:3].isalpha() and f[:3].isupper()
                    and f[3:5] == '%02d' % n)):
            return os.path.join(faces, f)
    return None


def role_ranges(game_db):
    """{role: (dmin, dmax, lmin, lmax)} over the stock formations: coordinates
    outside a role's stock range crash the exe on team select (25-09: forwards
    at depth 45/46, stock max 44)."""
    _, fb = load_bin(game_db)
    out = {}
    for k in range(len(fb) // D.FORMATION_REC):
        o = k * D.FORMATION_REC
        for j in range(STARTERS - 1):
            r, dp, lt = fb[o + ROLE_OFF + j], fb[o + DEPTH_OFF + j], fb[o + LATERAL_OFF + j]
            a = out.setdefault(r, [dp, dp, lt, lt])
            a[0], a[1], a[2], a[3] = min(a[0], dp), max(a[1], dp), min(a[2], lt), max(a[3], lt)
    return out


def pack_dirs(aes):
    """Aesthetics export folder -> (Faces dir, kit-1 dds). The kit is the
    u0???p1.dds in Kit Textures (u0768p1, u0XXXp1, u0000p1 ...)."""
    faces = kit_dir = None
    for root, dirs, _ in os.walk(aes):
        for d in dirs:
            if d.lower() == 'faces' and faces is None:
                faces = os.path.join(root, d)
            if d.lower() == 'kit textures' and kit_dir is None:
                kit_dir = os.path.join(root, d)
    if faces is None or kit_dir is None:
        sys.exit('%s: no Faces/ or Kit Textures/ inside' % aes)
    kits = sorted(f for f in os.listdir(kit_dir) if re.fullmatch(r'u0\w{3}p1\.dds', f, re.I))
    if not kits:
        sys.exit('%s: no u0???p1.dds kit' % kit_dir)
    return faces, os.path.join(kit_dir, kits[0])


def register(custom, gdb, tid, pids):
    """Serve each pid's marked face (GDB faces/map.txt) and drop this team's
    lines, bodies and faces that are no longer imported."""
    team = lambda pid: pid // D.PLAYER_ID_PER_TEAM == PID_BASE + tid
    for d in os.listdir(custom):
        if d[:1] == 'p' and d[1:].isdigit() and team(int(d[1:])) and int(d[1:]) not in pids:
            shutil.rmtree(os.path.join(custom, d))
            face = os.path.join(gdb, 'faces', '4cc', '%s.bin' % d[1:])
            if os.path.exists(face):
                os.remove(face)
    map_path = os.path.join(gdb, 'faces', 'map.txt')
    keep = []
    for line in open(map_path).read().splitlines():
        head = line.split(',', 1)[0].strip()
        if not (head.isdigit() and team(int(head))):
            keep.append(line)
    keep += ['%d, "4cc/%d.bin"' % (p, p) for p in pids]
    open(map_path, 'w').write('\n'.join(keep) + '\n')


ARCHIVES = ('.7z', '.zip', '.rar')   # 4cc packs ship all three; 7z reads each
EXPORT_EXTS = ('', '.bin')           # tactical exports: TEXPORT00000000, <team>.bin


def unpack(path, tmp, single_file=False):
    """An archive -> where it extracted to (with single_file: the export in
    it - the largest .bin or extensionless file: /his/ zips a 405 KB COACH .bin
    beside its 5.6 MB export, /sci/ a textbook PDF); anything else as is."""
    if not path.lower().endswith(ARCHIVES):
        return path
    out = tempfile.mkdtemp(dir=tmp)
    subprocess.run(['7z', 'x', '-y', '-o' + out, path], check=True, stdout=subprocess.DEVNULL)
    if not single_file:
        return out
    files = [os.path.join(r, f) for r, _, fs in os.walk(out) for f in fs
             if os.path.splitext(f)[1].lower() in EXPORT_EXTS]
    if not files:
        sys.exit('%s: empty archive' % path)
    return max(files, key=os.path.getsize)


def main(aes, edit, custom, gdb, export=None, tid=None, starters_only=True, pes21=None,
         pes17=False, tactics=None, rename=None, pes15=None):
    faces, kit = pack_dirs(aes)
    # export kinds: PES15 bin / PES17 TEXPORT (a real lineup), a 4ccEditor
    # .4ccs (stats + medals, no lineup), none (the face folders are the roster)
    kind = 'aes' if export is None else '4ccs' if export.lower().endswith('.4ccs') else 'texport'
    if kind != 'texport' and tid is None:
        sys.exit('--tid is required with a .4ccs or without an export')
    if kind == '4ccs':
        x = X17.read_4ccs(export, tid)
    elif kind == 'aes':
        x = aes_roster(faces, tid)
    else:
        x = (X17 if pes17 else X).read(export)
        tid = x['team']['id'] if tid is None else tid
    K.install(pes21, os.path.dirname(kit), os.path.join(custom, 'kits'), tid, pes15)
    plain = E.load(edit)
    if rename:
        abbr = x['team'].get('abbr') if pes17 else None
        E.rename(plain, tid, rename, abbr or ''.join(w[0] for w in rename.split()).upper()[:3] or rename[:3].upper())
    if x['lineup'] is not None:
        # PES2012 formations list outfield slots defence -> midfield -> attack
        # (all 316 stock rows; slot 0 is always a centre back): sort the export's
        # eleven into that order, then roster slot k = formation slot k.
        gk, outfield = x['lineup'][0], x['lineup'][1:STARTERS]
        for s in outfield:
            s['role'] = role(s['position'], s['lateral'])
        outfield.sort(key=lambda s: (band(s['role']), s['role'] not in CB_ROLES, s['depth'], s['lateral']))
        x['lineup'] = [gk] + outfield + x['lineup'][STARTERS:]
        order = [s['pid'] for s in x['lineup']] + [p for p, _ in x['roster'] if p not in {s['pid'] for s in x['lineup']}]
    else:
        order = [p for p, _ in x['roster']]
        x['lineup'] = [{'pid': p} for p in order[:STARTERS]]
    number = dict(x['roster'])
    new_pid = {src: (PID_BASE + tid) * 100 + k + 1 for k, src in enumerate(order)}

    at = E.players(plain)[0]
    for src, dst in new_pid.items():
        o = at[dst]
        rec = bytearray(plain[o:o + P.REC])
        # 'aes' has no stats source: names (and nothing else) into the rows.
        P.from_dict(rec, stats12(x['players'][src]) if kind != 'aes' else
                    {'name': x['players'][src]['name'], 'shirt': x['players'][src]['shirt']})
        plain[o:o + P.REC] = rec
    o = E.roster(plain, tid)
    for j, src in enumerate(order[:D.ROSTER_SLOTS]):
        struct.pack_into('<I', plain, o + 4 + 4 * j, new_pid[src])
        if number[src]:  # no export: the DLC's own numbers stay
            plain[o + 4 + 4 * D.ROSTER_SLOTS + j] = number[src]
    E.save(edit, plain)

    # Without real lineup data ('4ccs'/'aes' synthesize one: role/depth/
    # lateral unknown) there is nothing to write.
    if tactics and all('role' in s for s in x['lineup'][1:STARTERS]):
        ranges = role_ranges(os.path.join(tactics, 'dt04_29.bin'))
        fh, fb = load_bin(os.path.join(tactics, 'dt04_29.bin'))
        for k in range(len(fb) // D.FORMATION_REC):
            o = k * D.FORMATION_REC
            if struct.unpack_from('<H', fb, o)[0] == tid:
                for j, s in enumerate(x['lineup'][1:STARTERS]):
                    dmin, dmax, lmin, lmax = ranges[s['role']]
                    fb[o + ROLE_OFF + j] = s['role']
                    fb[o + DEPTH_OFF + j] = min(dmax, max(dmin, s['depth']))
                    fb[o + LATERAL_OFF + j] = min(lmax, max(lmin, s['lateral']))
                struct.pack_into('<I', fb, o + D.FORMATION_PID_OFFS[0], new_pid[x['captain_pid']])
        save_bin(os.path.join(tactics, 'dt04_29.bin'), fh, fb)

    bodies = sorted((x['lineup'] if starters_only else [{'pid': p} for p in order]),
                    key=lambda s: s['pid'] % 100)  # marker order = face folder XXXnn
    found = []
    for s in bodies:
        folder = face_folder(faces, s['pid'])
        if folder is None or not any(f.lower().endswith('.model') for f in os.listdir(folder)):
            print('no face models for', s['pid'])  # e.g. /pw/ Inoki: a portrait only
            continue
        found.append((s, folder))
    register(custom, gdb, tid, [new_pid[s['pid']] for s, _ in found])
    os.makedirs(os.path.join(gdb, 'faces', '4cc'), exist_ok=True)
    for s, folder in found:
        pid = new_pid[s['pid']]
        app = x['players'][s['pid']]['appearance']
        # the export's word on PES hiding its body (FPC); none: judge from the geometry
        hint = [] if app is None else ['hide' if X.hides_body(app) else 'keep']
        subprocess.run([sys.executable, os.path.join(HERE, 'pes15_to_pes12.py'), folder, kit,
                        os.path.join(custom, 'p%d' % pid)] + hint, check=True)
        subprocess.run([sys.executable, os.path.join(HERE, 'mark_face.py'), os.path.join(gdb, FACE_TEMPLATE),
                        str(pid), os.path.join(gdb, 'faces', '4cc', '%d.bin' % pid)],
                       check=True, stdout=subprocess.DEVNULL)
        print('p%d %s' % (pid, os.path.basename(folder)))


if __name__ == '__main__':
    a = [v for v in sys.argv[1:] if not v.startswith('--')]
    if len(a) != 4:
        sys.exit(__doc__)
    opt = dict(v[2:].split('=', 1) for v in sys.argv[1:] if v.startswith('--') and '=' in v)
    with tempfile.TemporaryDirectory() as tmp:
        export = opt.get('export')
        main(unpack(a[0], tmp), *a[1:], export and unpack(export, tmp, single_file=True),
             int(opt['tid']) if 'tid' in opt else None, '--all' not in sys.argv, opt.get('pes21'),
             '--pes17' in sys.argv, opt.get('tactics'), opt.get('rename'), opt.get('pes15'))

"""Import a 4cc PES2015 team (tactical export + aesthetics export) into the
PES2012 4cc base: squad, lineup, formation, and a custom full body for each
starter.

    python3 pes12_import_team.py <export.bin> <Faces dir> <kit.dds> <db dir> \
        <custom dir> <GDB dir> <first marker> [--all] [--pes21=<PES2021 Data dir>]

  db dir      the afs2fs folder holding dt04_26/29/31 (kitserver/4cc-dlc/img/dt04.img)
  custom dir  drawlogic's model folder (one sub-folder per player)
  GDB dir     kitserver/GDB (faces/map.txt + marked face.bin per player)

Squad: the team's 23 PLACEHOLDER rows ((2000 + tid) * 100 + slot + 1) take
the export's players in LINEUP order (slot 0 GK, 1-10 the outfield eleven in
formation order, then the bench), so roster slot k = formation slot k.
Formation: each outfield slot's PES15 position + (depth, lateral) becomes a
PES2012 role code (entry 29 +0x50, one per outfield slot) + coordinates
(+0x5A depth, +0x64 lateral) - both games use the same 0-100 pitch scale
with lateral 0 = left touchline. Captain = the export's captain.
Bodies: tools/pes15_to_pes12.py per starter; identity rides fserv (the
player's face.bin carries the drawlogic marker, tools/mark_face.py).
Kits: every u0XXX{p,g}N.dds beside <kit.dds> -> <custom>/kits/<tid> (tools/pes15_kits.py;
the layout tables are built once from a PES2021 install, --pes21).
"""
import os
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pes12db as D  # noqa: E402
import pes12player as P  # noqa: E402
import pes15export as X  # noqa: E402
import pes15_kits as K  # noqa: E402

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


def stats12(p):
    """PES15 player entry -> pes12player field dict."""
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


def face_folder(faces, pid):
    n = pid % 100
    for f in os.listdir(faces):
        if f[:5].isdigit() and int(f[3:5]) == n:
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


def main(export, faces, kit, db, custom, gdb, first_marker, starters_only=True, pes21=None):
    x = X.read(export)
    tid = x['team']['id']
    K.install(pes21, os.path.dirname(kit), os.path.join(custom, 'kits'), tid)
    # PES2012 formations list outfield slots defence -> midfield -> attack
    # (all 316 stock rows; slot 0 is always a centre back): sort the export's
    # eleven into that order, then roster slot k = formation slot k.
    gk, outfield = x['lineup'][0], x['lineup'][1:STARTERS]
    for s in outfield:
        s['role'] = role(s['position'], s['lateral'])
    outfield.sort(key=lambda s: (band(s['role']), s['role'] not in CB_ROLES, s['depth'], s['lateral']))
    x['lineup'] = [gk] + outfield + x['lineup'][STARTERS:]
    order = [s['pid'] for s in x['lineup']] + [p for p, _ in x['roster'] if p not in {s['pid'] for s in x['lineup']}]
    number = dict(x['roster'])
    new_pid = {src: (PID_BASE + tid) * 100 + k + 1 for k, src in enumerate(order)}

    ph, pb = load_bin(os.path.join(db, 'dt04_26.bin'))
    at = {struct.unpack_from('<I', pb, k * P.REC)[0]: k * P.REC for k in range(len(pb) // P.REC)}
    for src, dst in new_pid.items():
        o = at[dst]
        rec = bytearray(pb[o:o + P.REC])
        P.from_dict(rec, stats12(x['players'][src]))
        pb[o:o + P.REC] = rec
    save_bin(os.path.join(db, 'dt04_26.bin'), ph, pb)

    rh, rb = load_bin(os.path.join(db, 'dt04_31.bin'))
    for k in range(len(rb) // D.ROSTER_REC):
        o = k * D.ROSTER_REC
        if struct.unpack_from('<I', rb, o)[0] == tid:
            for j, src in enumerate(order[:D.ROSTER_SLOTS]):
                struct.pack_into('<I', rb, o + 4 + 4 * j, new_pid[src])
                rb[o + 4 + 4 * D.ROSTER_SLOTS + j] = number[src]
    save_bin(os.path.join(db, 'dt04_31.bin'), rh, rb)

    ranges = role_ranges(os.path.join(db, 'dt04_29.bin'))
    fh, fb = load_bin(os.path.join(db, 'dt04_29.bin'))
    for k in range(len(fb) // D.FORMATION_REC):
        o = k * D.FORMATION_REC
        if struct.unpack_from('<H', fb, o)[0] == tid:
            for j, s in enumerate(x['lineup'][1:STARTERS]):
                dmin, dmax, lmin, lmax = ranges[s['role']]
                fb[o + ROLE_OFF + j] = s['role']
                fb[o + DEPTH_OFF + j] = min(dmax, max(dmin, s['depth']))
                fb[o + LATERAL_OFF + j] = min(lmax, max(lmin, s['lateral']))
            struct.pack_into('<I', fb, o + D.FORMATION_PID_OFFS[0], new_pid[x['captain_pid']])
    save_bin(os.path.join(db, 'dt04_29.bin'), fh, fb)

    picks, maps = [], []
    for k, s in enumerate(x['lineup'] if starters_only else [{'pid': p} for p in order]):
        folder = face_folder(faces, s['pid'])
        if folder is None:
            print('no face folder for', s['pid'])
            continue
        name = 'p%d' % new_pid[s['pid']]
        hint = 'hide' if X.hides_body(x['players'][s['pid']]['appearance']) else 'keep'
        subprocess.run([sys.executable, os.path.join(HERE, 'pes15_to_pes12.py'), folder, kit,
                        os.path.join(custom, name), hint], check=True)
        marker = first_marker + k
        face = 'faces/4cc/%d.bin' % new_pid[s['pid']]
        os.makedirs(os.path.join(gdb, 'faces', '4cc'), exist_ok=True)
        subprocess.run([sys.executable, os.path.join(HERE, 'mark_face.py'), os.path.join(gdb, FACE_TEMPLATE),
                        str(marker), os.path.join(gdb, face)], check=True, stdout=subprocess.DEVNULL)
        picks.append('%d %s' % (marker, name))
        maps.append('%d, "%s"' % (new_pid[s['pid']], face[len('faces/'):]))
    print('\n'.join(['# picks'] + picks + ['# map.txt'] + maps))
    return picks, maps


if __name__ == '__main__':
    a = [v for v in sys.argv[1:] if not v.startswith('--')]
    opt = dict(v[2:].split('=', 1) for v in sys.argv[1:] if v.startswith('--') and '=' in v)
    main(*a[:6], int(a[6]), '--all' not in sys.argv, opt.get('pes21'))

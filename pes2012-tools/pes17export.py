"""Reads a 4cc PES2017 team export (TEEXPORT file, decrypted payload).

Decrypt with pes17crypt (payload = data chunk, 5,578,704 bytes). Layout of
the payload, mapped against the4chancup/4ccEditor's PES17 EDIT reader
(pes17.cpp, whose record formats it shares): 23 player records at 0x510840
(player entry 0x74 + appearance 0x48 = 0xBC stride, names at +0x34), one
team entry at 0x10054 (u32 team id, name at +0x98, abbr at +0xDE), one
roster block at 0x1028c (u32 tid + 32 x u32 pid + 32 numbers), one tactics
block at 0x10330 (u32 tid, 11 plan codes at +4, coords at +15, 11 lineup
roster indices at +0x1E4, captain at +0x20D).
"""
import json
import re
import struct
import sys

import pes15export as _X15
import pes17crypt
POSITIONS = _X15.POSITIONS
Bits = _X15.Bits
STARTERS = 11
PLAYER_BASE = 0x510840 # TEXPORT payload: single-team files keep EDIT positions
PLAYER_REC = 0xBC
PLAYER_NAME_OFF, PLAYER_NAME_LEN = 0x34, 0x2E
PLAYER_SHIRT_OFF, PLAYER_SHIRT_LEN = 0x62, 0x12
APP_OFF, APP_REC = 0x74, 0x48
TEAM_BASE = 0x10054
TEAM_NAME_OFF, TEAM_NAME_LEN = 0x98, 0x46
TEAM_ABBR_OFF, TEAM_ABBR_LEN = 0xDE, 0x4
ROSTER_BASE = 0x1028c
PLAN_POS_OFF = 4 # tactics: u32 team id, then 11 codes (slot 0 = GK)
PLAN_COORD_OFF = PLAN_POS_OFF + STARTERS
LINEUP_OFF = 0x1E4 # tactics: 11 x u8 roster indices, formation slot order
CAPTAIN_OFF = 0x20D # tactics: captain = roster index

# aatf.cpp:186-210 for PES17: rating = max of the PES15 set plus clearing,
# reflex, cover (PES > 15) and phys_cont (PES > 16). Rule: pes15export.medal.
ABILITY_STATS = _X15.ABILITY_STATS + ('clearing', 'reflex', 'cover', 'phys_cont')


def medal(p):
    return _X15.medal(p, ABILITY_STATS)


def read_player(d, at):
    b = Bits(d, at)
    p = {'id': b.read(0, 32)}
    b.skip(6)
    p['nation'] = b.read(0, 16)
    p['height'] = d[b.at]; b.skip(1)
    p['weight'] = d[b.at]; b.skip(1)
    b.skip(2)                                   # goal celebrations
    p['atk'] = b.read(0, 7); p['def'] = b.read(7, 7); p['gk'] = b.read(6, 7)
    p['drib'] = b.read(5, 7)
    p['mo_fk'] = b.read(4, 4)
    p['finish'] = b.read(0, 7); p['lowpass'] = b.read(7, 7)
    p['loftpass'] = b.read(6, 7); p['header'] = b.read(5, 7)
    p['form'] = b.read(4, 3)
    b.read(7, 1)
    p['swerve'] = b.read(0, 7); p['catching'] = b.read(7, 7)
    p['clearing'] = b.read(6, 7); p['reflex'] = b.read(5, 7)
    p['injury'] = b.read(4, 2)
    b.read(7, 1)
    p['body_ctrl'] = b.read(0, 7); p['phys_cont'] = b.read(7, 7)
    p['kick_pwr'] = b.read(6, 7); p['exp_pwr'] = b.read(5, 7)
    p['mo_armd'] = b.read(4, 3)
    b.read(7, 1)
    p['age'] = b.read(0, 6)
    p['reg_pos'] = b.read(6, 4)
    p['play_style'] = b.read(3, 5)
    p['ball_ctrl'] = b.read(0, 7); p['ball_win'] = b.read(7, 7)
    p['weak_acc'] = b.read(6, 2)
    p['jump'] = b.read(0, 7)
    p['mo_armr'] = b.read(7, 3)
    p['mo_ck'] = b.read(2, 3)
    p['cover'] = b.read(5, 7)
    p['weak_use'] = b.read(4, 2)
    pos = []
    for k in range(13): # pes17.cpp: bits 6,0,2,4,6,0.. (PES15 uses 0,2,4,6,0..)
        pos.append(b.read((6 + 2 * k) % 8, 2))
    p['play_pos'] = dict(zip(POSITIONS, pos))
    p['mo_hunchd'] = b.read(0, 2); p['mo_hunchr'] = b.read(2, 2); p['mo_pk'] = b.read(4, 2)
    p['place_kick'] = b.read(6, 7)
    b.read(5, 1); b.read(6, 1); b.read(7, 1)    # edit playpos/ability/skill
    p['stamina'] = b.read(0, 7); p['speed'] = b.read(7, 7)
    b.read(6, 1); b.read(7, 1); b.read(0, 1); b.read(1, 1)  # edit style/com/motion/base copy
    p['strong_foot'] = b.read(3, 1)
    raw = d[at + PLAYER_NAME_OFF:at + PLAYER_NAME_OFF + PLAYER_NAME_LEN].split(b'\0')[0].decode('utf-8', 'replace')
    p['name'], p['name_colour'] = _X15.split_colour(raw)
    p['medal'] = medal(p)
    p['shirt'] = d[at + PLAYER_SHIRT_OFF:at + PLAYER_SHIRT_OFF + PLAYER_SHIRT_LEN].split(b'\0')[0].decode('utf-8', 'replace')
    return p


def read_appearance(d, at):
    """Appearance entry (0x48, at +0x74): same field order as the PES15 one,
    boots/gloves ids 14 bits, then the strip style."""
    b = Bits(d, at + 4)
    b.read(0, 4)                                 # edit face/hair/phys/strip flags
    a = {'boot_id': b.read(4, 14), 'glove_id': b.read(2, 14)}
    b.read(0, 32)                                # copy id
    for k in range(7):                           # neck .. head depth (4 bits each)
        b.read(0, 4); b.read(4, 4)
    b.read(0, 3); b.read(3, 3); b.read(6, 2)     # wristband colours, tape
    b.read(0, 3); b.read(3, 3)                   # spectacles colour, style
    a['sleeve'] = b.read(6, 2)
    a['inners'] = b.read(0, 2); a['socks'] = b.read(2, 2); a['undershorts'] = b.read(4, 2)
    a['untucked'] = b.read(6, 1); a['ankle_tape'] = b.read(7, 1)
    a['gloves'] = b.read(0, 1)
    return a


def hides_body(appearance):
    return _X15.hides_body(appearance)


def read(path):
    _, _, d = pes17crypt.decrypt(open(path, 'rb').read())
    t = TEAM_BASE
    team = {'id': struct.unpack_from('<I', d, t)[0],
            'name': d[t + TEAM_NAME_OFF:t + TEAM_NAME_OFF + TEAM_NAME_LEN].split(b'\0')[0].decode('utf-8', 'replace'),
            'abbr': d[t + TEAM_ABBR_OFF:t + TEAM_ABBR_OFF + TEAM_ABBR_LEN].split(b'\0')[0].decode('utf-8', 'replace')}
    pids = list(struct.unpack_from('<32I', d, ROSTER_BASE + 4))
    nums = list(d[ROSTER_BASE + 4 + 128:ROSTER_BASE + 4 + 160])
    k = TEAM_BASE + (0x10330 - 0x10054)
    codes = list(d[k + PLAN_POS_OFF:k + PLAN_POS_OFF + STARTERS])
    coords = [tuple(d[k + PLAN_COORD_OFF + 2 * i:k + PLAN_COORD_OFF + 2 * i + 2]) for i in range(STARTERS)]
    players = {}
    base = {struct.unpack_from('<I', d, PLAYER_BASE + i * PLAYER_REC)[0]: PLAYER_BASE + i * PLAYER_REC
            for i in range(32)}
    for i in range(32):
        if not pids[i]:
            continue
        at = base[pids[i]]
        players[pids[i]] = read_player(d, at)
        players[pids[i]]['appearance'] = read_appearance(d, at + APP_OFF)
    order = list(d[k + LINEUP_OFF:k + LINEUP_OFF + STARTERS])
    lineup = [{'slot': i, 'pid': pids[order[i]], 'number': nums[order[i]], 'position': POSITIONS[codes[i]],
               'depth': coords[i][0], 'lateral': coords[i][1]} for i in range(STARTERS)]
    cap = d[k + CAPTAIN_OFF]
    return {'captain_pid': pids[cap] if cap < len(pids) else pids[0], 'team': team, 'roster': [(p, n) for p, n in zip(pids, nums) if p], 'lineup': lineup, 'players': players}


# 4ccEditor "save squad settings": 3 version bytes + 2 PES-version bytes, one
# player_export (356, MSVC layout) per team player in roster order, 40 u16
# shirt numbers, then whatever the editor appends (ignored). Names are stored
# as 61 wchars. No lineup/formation/captain: takes the tid as an argument and
# synthesizes pids (tid*100+nn) so face folders match by nn like a TEXPORT.
_4CCS_VER, _4CCS_PESVER, _4CCS_REC = 3, 2, 356
_4CCS_NAME_OFF, _4CCS_NAME_LEN = 124, 61 * 2
_4CCS_SHIRT_OFF, _4CCS_SHIRT_LEN = 246, 21
_4CCS_NUMS = 40
# MSVC offset of each stat byte; play_pos is in game order, NOT the POSITIONS
# order (pes20.cpp:61-76): CF SS LWF RWF AMF DMF CMF LMF RMF CB LB RB GK.
_4CCS_PLAY_POS = ['CF', 'SS', 'LWF', 'RWF', 'AMF', 'DMF', 'CMF', 'LMF', 'RMF',
                  'CB', 'LB', 'RB', 'GK']
_4CCS_PLAY_POS_OFF = 42
_4CCS_OFF = {'height': 4, 'weight': 5, 'atk': 8, 'def': 9, 'gk': 10, 'drib': 11, 'finish': 13,
             'lowpass': 14, 'loftpass': 15, 'header': 16, 'swerve': 19,
             'catching': 20, 'clearing': 21, 'reflex': 22, 'body_ctrl': 25,
             'phys_cont': 26, 'kick_pwr': 27, 'exp_pwr': 28, 'age': 31,
             'reg_pos': 32, 'play_style': 33, 'ball_ctrl': 34, 'ball_win': 35,
             'weak_acc': 36, 'jump': 37, 'cover': 40, 'weak_use': 41,
             'place_kick': 58, 'stamina': 67, 'speed': 68, 'strong_foot': 73}


def read_4ccs_player(rec):
    o = _4CCS_OFF
    p = {k: rec[o[k]] for k in o}
    p['play_pos'] = dict(zip(_4CCS_PLAY_POS, rec[_4CCS_PLAY_POS_OFF:_4CCS_PLAY_POS_OFF + 13]))
    raw = rec[_4CCS_NAME_OFF:_4CCS_NAME_OFF + _4CCS_NAME_LEN].decode('utf-16-le').split('\0')[0]
    p['name'], p['name_colour'] = _X15.split_colour(raw)
    p['shirt'] = rec[_4CCS_SHIRT_OFF:_4CCS_SHIRT_OFF + _4CCS_SHIRT_LEN].split(b'\0')[0].decode('utf-8', 'replace')
    p['medal'] = medal(p)
    p['appearance'] = {'socks': rec[347], 'untucked': rec[349]}
    return p


def _4ccs_count(raw, head):
    """Player count: the format stores none, so take the n whose records all
    have names and whose 40-u16 numbers block reads shirt-plausible (<= 99).
    (jp.4ccs has a 405-byte tail past the numbers that a bare division would
    mistake for a 24th player.)"""
    k = 0
    while head + (k + 1) * _4CCS_REC + 2 * _4CCS_NUMS <= len(raw):
        rec = raw[head + k * _4CCS_REC:head + (k + 1) * _4CCS_REC]
        if not rec[_4CCS_NAME_OFF:_4CCS_NAME_OFF + _4CCS_NAME_LEN].decode('utf-16-le').split('\0')[0]:
            break
        k += 1
    ns = [n for n in range(1, k + 1)
          if max(struct.unpack_from('<%dH' % _4CCS_NUMS, raw, head + n * _4CCS_REC)) <= 99]
    assert len(ns) == 1, 'ambiguous .4ccs player count: %r' % (ns,)
    return ns[0]


def read_4ccs(path, tid):
    raw = open(path, 'rb').read()
    head = _4CCS_VER + _4CCS_PESVER
    n = _4ccs_count(raw, head)
    pids, players, nums = [], {}, list(struct.unpack_from('<%dH' % _4CCS_NUMS, raw, head + n * _4CCS_REC))
    for k in range(n):
        pid = tid * 100 + k + 1
        pids.append(pid)
        players[pid] = read_4ccs_player(raw[head + k * _4CCS_REC:head + (k + 1) * _4CCS_REC])
    return {'captain_pid': pids[0], 'team': {'id': tid},
            'roster': [(p, nums[k] if k < len(nums) else 0) for k, p in enumerate(pids)],
            'lineup': None, 'players': players}


if __name__ == '__main__':
    x = read(sys.argv[1])
    print(x['team'])
    for s in x['lineup']:
        p = x['players'][s['pid']]
        print('%2d #%-3d %-4s (%2d,%2d) %-28s reg %-3s atk %2d def %2d spd %2d h %3d foot %d age %d%s' % (
            s['slot'], s['number'], s['position'], s['depth'], s['lateral'], p['name'],
            POSITIONS[p['reg_pos']] if p['reg_pos'] < 13 else p['reg_pos'], p['atk'], p['def'], p['speed'],
            p['height'], p['strong_foot'], p['age'], ' ' + p['medal'].upper() if p['medal'] else ''))
    if '--json' in sys.argv:
        print(json.dumps(x, indent=1))

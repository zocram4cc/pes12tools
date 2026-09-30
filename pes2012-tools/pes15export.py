"""Reads a 4cc PES2015 team export (the tactical export *.bin).

Decrypt with pes15crypt (payload = chunk 2, 5,577,964 bytes). Layout of the
payload, mapped 25-09 against the4chancup/4ccEditor's PES15 EDIT reader
(pes15.cpp), whose record formats it shares:

  0x00      u32 0x1f, u32 players (40), u32 size, ...
  0x1002C   team entry (0x1C8): u32 team id, name at +0x94 (0x46), abbr +0xDA
  +0x1C8    team-player table (0xA4): u32 team id, 32 x u32 pid, 32 x u8 number
            (roster order, not the lineup)
  +0xA4     team tactics (0x204): u32 team id, then per game plan a 1-byte
            11 position codes (PES15 order, see POSITIONS) and 11
            (depth, lateral) coordinate pairs - depth 0 = own goal line,
            lateral 0 = left touchline, both on PES2012's own formation scale;
            the lineup at +0x174 (32 x u32 roster indices in formation-slot
            order: slot 0 = GK, 1-10 outfield, then the bench); captain
            index at +0x1FA
  (first name) - 0x30
            40 player records, 0xB4 each: the EDIT "player entry" (0x70) then
            its "appearance entry" (0x44)

Player entry fields: bit-packed, LSB first, read exactly like 4ccEditor's
read_data15 (a field continues into the next byte when it overflows).

    python3 pes15export.py <export.bin>        # prints team, lineup, players
"""
import json
import re
import struct
import sys

import pes15crypt

TEAM_OFF = 0x1002C
TEAM_REC, ROSTER_REC = 0x1C8, 0xA4
TEAM_NAME_OFF, TEAM_NAME_LEN = 0x94, 0x46
PLAYER_REC = 0x70 + 0x44
PLAYER_NAME_OFF = 0x30
POSITIONS = ['GK', 'CB', 'LB', 'RB', 'DMF', 'CMF', 'LMF', 'RMF', 'AMF', 'LWF', 'RWF', 'SS', 'CF']
STARTERS = 11
COLOUR_TAG = re.compile(r'\x11c([0-9a-fA-F]{6})[0-9a-fA-F]{2}')  # name colour escape: 0x11 'c' RRGGBBAA (4cc medal colours)
COLOUR_ESC = re.compile(r'\x11(?:c[0-9a-fA-F]{8}|d)')  # every colour escape; 0x11 'd' ends a colour run


def split_colour(raw):
    """Name -> (name without colour escapes, first colour RRGGBB or None).
    Escapes may sit mid-name (/vst/: '"<0x11 c..>99% Chance to hit.<0x11 d>"')."""
    m = COLOUR_TAG.search(raw)
    return COLOUR_ESC.sub('', raw), (m.group(1) if m else None)


# 4ccEditor aatf.cpp: player rating = max of the ability stats; goldRate 99
# marks gold medals, silverRate 88 silver medals (giant penalties default 0,
# so the thresholds are exactly 99 and 88).
GOLD_RATE_AATF = 99
SILVER_RATE_AATF = 88
# aatf.cpp:186-210 with pesVersion 15: drib, gk, finish, lowpass, loftpass,
# header, swerve, catching, body_ctrl, kick_pwr, exp_pwr, ball_ctrl, ball_win,
# jump, place_kick, stamina, speed. atk/def are derived, never inputs.
ABILITY_STATS = ('drib', 'gk', 'finish', 'lowpass', 'loftpass', 'header',
                 'swerve', 'catching', 'body_ctrl', 'kick_pwr', 'exp_pwr',
                 'ball_ctrl', 'ball_win', 'jump', 'place_kick', 'stamina', 'speed')


def medal(p, stats=ABILITY_STATS):
    """'gold' / 'silver' / None per 4ccEditor aatf.cpp."""
    rating = max(p[s] for s in stats)
    if rating >= GOLD_RATE_AATF:
        return 'gold'
    if rating >= SILVER_RATE_AATF:
        return 'silver'
    return None


PLAN_POS_OFF = 4            # tactics: u32 team id, then 11 codes (slot 0 = GK)
PLAN_COORD_OFF = PLAN_POS_OFF + STARTERS
CAPTAIN_OFF = 0x1FA          # tactics: captain = roster index
LINEUP_OFF = 0x174           # tactics: 32 x u32 roster indices, formation slot order (0xFF = none)


class Bits:
    """4ccEditor read_data15: bits LSB first from (byte, start_bit), cursor kept."""
    def __init__(self, data, at):
        self.d, self.at = data, at

    def read(self, start_bit, n):
        out, bit = 0, start_bit
        for i in range(n):
            if bit == 8:
                bit = 0
                self.at += 1
            out |= ((self.d[self.at] >> bit) & 1) << i
            bit += 1
        if bit == 8:
            self.at += 1
        return out

    def skip(self, n):
        self.at += n


def read_player(d, at):
    b = Bits(d, at)
    p = {'id': b.read(0, 32)}
    b.skip(6)
    p['nation'] = b.read(0, 16)
    p['height'] = d[b.at]; b.skip(1)
    p['weight'] = d[b.at]; b.skip(1)
    b.skip(2)                                   # goal celebrations
    p['atk'] = b.read(0, 7); p['def'] = b.read(7, 7); p['gk'] = b.read(6, 7)
    b.skip(1)
    p['mo_fk'] = b.read(4, 4)
    p['ball_ctrl'] = b.read(0, 7); p['finish'] = b.read(7, 7)
    p['lowpass'] = b.read(6, 7); p['loftpass'] = b.read(5, 7)
    p['weak_acc'] = b.read(4, 2); b.read(6, 1); b.read(7, 1)
    p['place_kick'] = b.read(0, 7); p['swerve'] = b.read(7, 7)
    p['catching'] = b.read(6, 7); p['speed'] = b.read(5, 7)
    p['form'] = b.read(4, 3)
    b.skip(1)
    p['exp_pwr'] = b.read(0, 7); p['jump'] = b.read(7, 7)
    p['stamina'] = b.read(6, 7); p['age'] = b.read(5, 6)
    p['reg_pos'] = b.read(3, 4)
    b.skip(1)
    p['play_style'] = b.read(0, 5); p['drib'] = b.read(5, 7)
    p['header'] = b.read(4, 7); p['body_ctrl'] = b.read(3, 7)
    p['injury'] = b.read(2, 2)
    b.read(4, 1)
    p['mo_armd'] = b.read(5, 3); p['mo_armr'] = b.read(0, 3)
    p['mo_ck'] = b.read(3, 3); p['weak_use'] = b.read(6, 2)
    pos = []
    for k in range(13):
        pos.append(b.read((2 * k) % 8, 2))
    p['play_pos'] = dict(zip(POSITIONS, pos))
    p['mo_hunchd'] = b.read(2, 2); p['mo_hunchr'] = b.read(4, 2); p['mo_pk'] = b.read(6, 2)
    p['ball_win'] = b.read(0, 7); p['kick_pwr'] = b.read(7, 7)
    p['strong_foot'] = (d[at + 0x2C] >> 2) & 1
    raw = d[at + PLAYER_NAME_OFF:at + PLAYER_NAME_OFF + 0x2E].split(b'\0')[0].decode('utf-8', 'replace')
    p['name'], p['name_colour'] = split_colour(raw)
    p['medal'] = medal(p)
    p['shirt'] = d[at + 0x5E:at + 0x5E + 0x12].split(b'\0')[0].decode('utf-8', 'replace')
    return p


def read_appearance(d, at):
    """Appearance entry (0x44, right after the 0x70 player entry), fields in
    4ccEditor read_appearance_entry15 order: u32 pid, 4 edit flags, boots id,
    gloves id, copy id, 14 body-shape nibbles, then the strip style."""
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

# A 4cc pack tells PES to hide its body under the custom model with an
# impossible strip combo (wiki: Full Player Customization). In the /g/ and
# /sci/ exports every whole-body model, and no face-only one, carries short
# socks + tucked shirt (with long sleeves or an undershirt beside it).
SOCKS_SHORT = 2


def hides_body(appearance):
    return appearance['socks'] == SOCKS_SHORT and not appearance['untucked']



def read(path):
    _, _, d = pes15crypt.decrypt(open(path, 'rb').read())
    t = TEAM_OFF
    team = {'id': struct.unpack_from('<I', d, t)[0],
            'name': d[t + TEAM_NAME_OFF:t + TEAM_NAME_OFF + TEAM_NAME_LEN].split(b'\0')[0].decode('utf-8', 'replace')}
    r = t + TEAM_REC
    pids = list(struct.unpack_from('<32I', d, r + 4))
    nums = list(d[r + 4 + 128:r + 4 + 160])
    k = r + ROSTER_REC
    codes = list(d[k + PLAN_POS_OFF:k + PLAN_POS_OFF + STARTERS])
    coords = [tuple(d[k + PLAN_COORD_OFF + 2 * i:k + PLAN_COORD_OFF + 2 * i + 2]) for i in range(STARTERS)]
    # player records: find the first roster pid followed by a name 0x30 on
    first = struct.pack('<I', pids[0])
    at = d.find(first, 0x100000)
    while at >= 0 and not d[at + PLAYER_NAME_OFF:at + PLAYER_NAME_OFF + 1].isalnum() and d[at + PLAYER_NAME_OFF] < 0x20:
        at = d.find(first, at + 1)
    players = {}
    while at + PLAYER_REC <= len(d):
        pid = struct.unpack_from('<I', d, at)[0]
        if pid not in pids or pid in players:
            break
        players[pid] = read_player(d, at)
        players[pid]['appearance'] = read_appearance(d, at + 0x70)
        at += PLAYER_REC
    order = struct.unpack_from('<%dI' % STARTERS, d, k + LINEUP_OFF)
    lineup = [{'slot': i, 'pid': pids[order[i]], 'number': nums[order[i]], 'position': POSITIONS[codes[i]],
               'depth': coords[i][0], 'lateral': coords[i][1]} for i in range(STARTERS)]
    cap = d[k + CAPTAIN_OFF]
    return {'captain_pid': pids[cap] if cap < len(pids) else pids[0], 'team': team, 'roster': [(p, n) for p, n in zip(pids, nums) if p], 'lineup': lineup, 'players': players}


if __name__ == '__main__':
    x = read(sys.argv[1])
    print(x['team'])
    for s in x['lineup']:
        p = x['players'][s['pid']]
        print('%2d #%-3d %-4s (%2d,%2d) %-24s reg %-3s atk %2d def %2d spd %2d h %3d foot %d age %d' % (
            s['slot'], s['number'], s['position'], s['depth'], s['lateral'], p['name'],
            POSITIONS[p['reg_pos']] if p['reg_pos'] < 13 else p['reg_pos'], p['atk'], p['def'], p['speed'],
            p['height'], p['strong_foot'], p['age']))
    if '--json' in sys.argv:
        print(json.dumps(x, indent=1))

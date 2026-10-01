"""EDIT.bin model: decrypt, index players / rosters / teams, edit, encrypt.

Decrypted layout (PES2012 PC v1.06, 25-09):
  0x328                players, 0xE4 each, pid-sorted: the whole base table
                       (stock + 4cc placeholders) then the created-player
                       slots (pid 0x1000xx); an EDIT shadows the base DB.
  right after players  rosters, 0xA4 each: u32 team, 32 x u32 pid, 32 x u8 number
  0x42F5C0             teams, 0x5B4 each: name[0x48], ..., abbr @0x5A0,
                       u16 id @0x5A4 (+0x8000 = edited row)
"""
import os
import shutil
import struct
import sys

# pes12crypt / pes12edit / pes12player live in pes2012-tools, next to this
# folder in the pes12tools repo (or tools/ of a full PES12 checkout)
_HERE = os.path.dirname(os.path.abspath(__file__))
for _tools in (os.path.join(_HERE, '..', '..', 'pes2012-tools'), os.path.join(_HERE, '..', '..', '..', 'tools')):
    if os.path.isfile(os.path.join(_tools, 'pes12crypt.py')):
        sys.path.insert(0, os.path.abspath(_tools))
        break
import pes12crypt  # noqa: E402
import pes12edit  # noqa: E402
import pes12player as P  # noqa: E402

PLAYERS_OFF = 0x328
ROSTER_REC, ROSTER_SLOTS = 0xA4, 32
ROSTER_PIDS_OFF, ROSTER_NUMS_OFF = 4, 4 + 4 * ROSTER_SLOTS
EMPTY_PID = 0


class EditFile:
    def __init__(self, path):
        self.path = path
        self.plain = None

    def load(self):
        self.plain = bytearray(pes12crypt.decrypt(open(self.path, 'rb').read()))
        self._index()
        return self

    # --- indexing -------------------------------------------------------
    def _index(self):
        d = self.plain
        self.player_off = {}
        k, last = 0, -1
        while True:
            o = PLAYERS_OFF + k * P.REC
            pid = struct.unpack_from('<I', d, o)[0]
            created = pid >> 20 == P.CREATED_ID_TOP
            if k and not created and (pid <= last or pid != struct.unpack_from('<I', d, o + P.ID2_OFF)[0]):
                break
            self.player_off.setdefault(pid, o)
            if not created:
                last = pid
            k += 1
        self.players_end = PLAYERS_OFF + k * P.REC
        self.roster_off = {}
        o = self.players_end
        while struct.unpack_from('<I', d, o)[0]:
            self.roster_off.setdefault(struct.unpack_from('<I', d, o)[0], o)
            o += ROSTER_REC

    # --- teams ----------------------------------------------------------
    def team_rows(self):
        """[{slot, stock_id, name, abbr, edited, off}] for every live team row."""
        out = []
        off = pes12edit.TEAM_OFF
        d = self.plain
        while off + pes12edit.TEAM_REC <= len(d):
            r = d[off:off + pes12edit.TEAM_REC]
            if not r[:pes12edit.NAME_LEN].split(b'\0')[0]:
                break
            i1, i2 = struct.unpack_from('<HH', r, pes12edit.TEAM_ID_OFF)
            out.append(dict(slot=len(out), stock_id=i1,
                            name=bytes(r[:pes12edit.NAME_LEN]).split(b'\0')[0].decode('utf-8', 'replace'),
                            abbr=bytes(r[pes12edit.TEAM_ABBR_OFF:pes12edit.TEAM_ABBR_OFF + 4]).split(b'\0')[0].decode(),
                            edited=bool(i2 & pes12edit.EDIT_FLAG), off=off))
            off += pes12edit.TEAM_REC
        return out

    def rename(self, off, name, abbr):
        d = self.plain
        d[off:off + pes12edit.NAME_LEN] = name.encode('utf-8')[:pes12edit.NAME_LEN - 1].ljust(pes12edit.NAME_LEN, b'\0')
        d[off + pes12edit.TEAM_ABBR_OFF:off + pes12edit.TEAM_ABBR_OFF + 4] = abbr.encode()[:3].ljust(4, b'\0')
        i1, i2 = struct.unpack_from('<HH', d, off + pes12edit.TEAM_ID_OFF)
        struct.pack_into('<HH', d, off + pes12edit.TEAM_ID_OFF, i1, i2 | pes12edit.EDIT_FLAG)

    def apply_plan(self, game, team_list):
        plan = pes12edit.load_plan(team_list)
        out, matched, missing = pes12edit.write_names(bytes(self.plain), game, plan)
        self.plain = bytearray(out)
        return matched, missing

    # --- rosters ----------------------------------------------------------
    def squad(self, team_id):
        """[(slot, pid, number)] of the team's roster, empty slots skipped."""
        o = self.roster_off.get(team_id)
        if o is None:
            return []
        out = []
        for j in range(ROSTER_SLOTS):
            pid = struct.unpack_from('<I', self.plain, o + ROSTER_PIDS_OFF + 4 * j)[0]
            if pid != EMPTY_PID:
                out.append((j, pid, self.plain[o + ROSTER_NUMS_OFF + j]))
        return out

    def set_number(self, team_id, slot, number):
        self.plain[self.roster_off[team_id] + ROSTER_NUMS_OFF + slot] = number

    # --- players ----------------------------------------------------------
    def player(self, pid):
        o = self.player_off[pid]
        return bytearray(self.plain[o:o + P.REC])

    def put_player(self, pid, rec):
        """Write a record back; the ids are kept whatever the record says."""
        o = self.player_off[pid]
        rec = bytearray(rec)
        struct.pack_into('<I', rec, P.ID_OFF, pid)
        struct.pack_into('<I', rec, P.ID2_OFF, struct.unpack_from('<I', self.plain, o + P.ID2_OFF)[0])
        self.plain[o:o + P.REC] = rec

    def save(self, path=None):
        """Encrypt and write. The first save over an existing file keeps the
        original as <name>.bak; the write goes to a temp file renamed over
        the target, so a crash mid-write never leaves a half EDIT.bin."""
        path = path or self.path
        data = pes12crypt.encrypt(bytes(self.plain))
        backup = path + '.bak'
        if os.path.exists(path) and not os.path.exists(backup):
            shutil.copy2(path, backup)
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)

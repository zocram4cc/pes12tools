"""PES2012 player record (0xE4 bytes): every editable field as data.

Same record in the base DB (img/dt04.img entry 26) and in EDIT.bin (players
table at 0x328). Bit fields are read little-endian from a 16-bit window at
`off`: value = ((u16 >> shift) & (2**bits - 1)) + bias.

Provenance (all verified on PES2012 PC v1.06):
- Abilities, playable flags, registered position, weak foot: in-game batch
  edits diffed against the base (25-09), cross-checked against the SER[G]ANT
  PES2012 Cheat Engine table (runtime offset = file offset - 0x50, 26/26).
- BASIC / PHYSIQUE / FACE / HAIR / ACCESSORIES / MOTION / CARDS: see the notes
  above those tables.
"""
import struct
from dataclasses import dataclass, field

REC = 0xE4
ID_OFF, ID2_OFF = 0x00, 0x70
NAME_OFF, NAME_LEN = 0x32, 0x2E       # UTF-8 player name
SHIRT_OFF, SHIRT_LEN = 0x60, 0x10     # printed shirt name
JP_NAME_OFF, JP_NAME_LEN = 0x04, 0x2E  # Japanese (katakana) name, UTF-8
STAT_BITS = 7                          # abilities are 1..99 in the low 7 bits
FLAG_SHIFT = 7                         # bit 7 of an ability byte = playable position flag
CREATED_ID_TOP = 1                     # created-player ids are 0x1000xx (pid >> 20 == 1)
WINDOW = 4                             # bit fields are read from a little-endian u32 at `off`


@dataclass
class Field:
    key: str
    label: str
    group: str
    off: int
    shift: int = 0
    bits: int = 8
    bias: int = 0
    lo: int = None
    hi: int = None
    choices: list = field(default_factory=list)  # index -> label, value = index + bias
    signed: bool = False  # two's-complement field (physique sliders)

    def get(self, rec):
        w = int.from_bytes(bytes(rec[self.off:self.off + WINDOW]).ljust(WINDOW, b'\0'), 'little')
        v = (w >> self.shift) & ((1 << self.bits) - 1)
        if self.signed and v >= 1 << (self.bits - 1):
            v -= 1 << self.bits
        return v + self.bias

    def set(self, rec, value):
        n = min(WINDOW, REC - self.off)
        mask = ((1 << self.bits) - 1) << self.shift
        w = int.from_bytes(bytes(rec[self.off:self.off + n]), 'little')
        w = (w & ~mask) | (((value - self.bias) << self.shift) & mask)
        rec[self.off:self.off + n] = w.to_bytes(n, 'little')

    @property
    def range(self):
        top = (1 << (self.bits - 1)) - 1 if self.signed else (1 << self.bits) - 1
        bottom = -(1 << (self.bits - 1)) if self.signed else 0
        lo = self.bias + bottom if self.lo is None else self.lo
        hi = self.bias + top if self.hi is None else self.hi
        return lo, hi


def _stat(key, label, group, off):
    return Field(key, label, group, off, 0, STAT_BITS, 0, 1, 99)


ABILITIES = [
    _stat('attack', 'Attack', 'Technique', 0x7A),
    _stat('defence', 'Defence', 'Technique', 0x7B),
    _stat('header', 'Header accuracy', 'Technique', 0x8D),
    _stat('dribble_acc', 'Dribble accuracy', 'Technique', 0x82),
    _stat('sp_acc', 'Short pass accuracy', 'Technique', 0x84),
    _stat('sp_spd', 'Short pass speed', 'Technique', 0x85),
    _stat('lp_acc', 'Long pass accuracy', 'Technique', 0x86),
    _stat('lp_spd', 'Long pass speed', 'Technique', 0x87),
    _stat('shot_acc', 'Shot accuracy', 'Technique', 0x88),
    _stat('place_kick', 'Place kicking', 'Technique', 0x8B),
    _stat('swerve', 'Swerve', 'Technique', 0x8C),
    _stat('ball_control', 'Ball control', 'Technique', 0x90),
    _stat('gk', 'Goalkeeping skills', 'Technique', 0x93),
    Field('weak_acc', 'Weak foot accuracy', 'Technique', 0x95, 3, 3, 1),
    Field('weak_use', 'Weak foot usage', 'Technique', 0x94, 3, 3, 1),
    _stat('responsiveness', 'Responsiveness', 'Speed', 0x80),
    _stat('explosive', 'Explosive power', 'Speed', 0x81),
    _stat('dribble_spd', 'Dribble speed', 'Speed', 0x83),
    _stat('top_speed', 'Top speed', 'Speed', 0x7E),
    _stat('body_balance', 'Body balance', 'Physical', 0x7C),
    _stat('stamina', 'Stamina', 'Physical', 0x7D),
    _stat('kick_power', 'Kicking power', 'Physical', 0x89),
    _stat('jump', 'Jump', 'Physical', 0x8E),
    _stat('teamwork', 'Teamwork', 'Resistance', 0x8F),
    _stat('tenacity', 'Tenacity', 'Resistance', 0x92),
]

POSITIONS = ['GK', 'SW', 'CB', 'LB', 'RB', 'DMF', 'CMF', 'LMF', 'RMF', 'AMF', 'LWF', 'RWF', 'SS', 'CF']
# playable-position flag = bit 7 of this ability byte (Cheat Engine table + in-game CB/GK edits)
PLAYABLE_FLAG_OFF = {'GK': 0x7A, 'SW': 0x7B, 'CB': 0x7C, 'DMF': 0x7E, 'CMF': 0x80, 'AMF': 0x82,
                     'SS': 0x84, 'CF': 0x85, 'LB': 0x87, 'RB': 0x88, 'LMF': 0x89, 'RMF': 0x8A,
                     'LWF': 0x8C, 'RWF': 0x8E}
PLAYABLE = [Field('play_' + p.lower(), p, 'Position', off, FLAG_SHIFT, 1)
            for p, off in PLAYABLE_FLAG_OFF.items()]
REGISTERED = Field('registered', 'Registered position', 'Position', 0x79, 4, 4, 0, 0, 13, POSITIONS)

# Located 25-09 by tools/edit_experiment.py (one field changed per placeholder,
# saved, diffed) and cross-checked on a player with every field set to a known
# value; ranges sanity-checked over the 7308 stock players (age 16-46,
# height 155-202, weight 50-108).
BASIC = [
    Field('nationality', 'Nationality (id)', 'Basic', 0xD8, 0, 8),
    Field('age', 'Age', 'Basic', 0xDA, 0, 5, 15),
    Field('left_foot', 'Stronger foot is left', 'Basic', 0x78, 0, 1),
]
PHYSIQUE = [
    Field('height', 'Height (cm)', 'Physique', 0xE0, 0, 6, 148),
    Field('weight', 'Weight (kg)', 'Physique', 0xA4, 7, 7),
    Field('head_width', 'Head size (width)', 'Physique', 0xA6, 4, 4, signed=True),
    Field('head_length', 'Head size (length)', 'Physique', 0xD3, 4, 4, signed=True),
    Field('head_depth', 'Head depth', 'Physique', 0xD4, 0, 4, signed=True),
    Field('neck_length', 'Neck length', 'Physique', 0xCF, 0, 4, signed=True),
    Field('neck_size', 'Neck size', 'Physique', 0xCE, 4, 4, signed=True),
    Field('shoulder_height', 'Shoulder height', 'Physique', 0xD2, 4, 4, signed=True),
    Field('shoulder_width', 'Shoulder width', 'Physique', 0xD3, 0, 4, signed=True),
    Field('chest', 'Chest measurement', 'Physique', 0xCF, 4, 4, signed=True),
    Field('waist', 'Waist size', 'Physique', 0xD0, 4, 4, signed=True),
    Field('arm', 'Arm size', 'Physique', 0xD0, 0, 4, signed=True),
    Field('thigh', 'Thigh size', 'Physique', 0xD1, 0, 4, signed=True),
    Field('calf', 'Calf size', 'Physique', 0xD1, 4, 4, signed=True),
    Field('leg_length', 'Leg length', 'Physique', 0xD2, 0, 4, signed=True),
]
FACE = [
    # ids of the whole face/hair combination (kitserver player.h faceHairBits)
    Field('face_id', 'Face id', 'Face', 0xA8, 15, 11),
]
HAIR = [
    Field('hair_id', 'Hairstyle id', 'Hair', 0xA8, 0, 11),
]
ACCESSORIES = [
    Field('boots', 'Boots (id)', 'Accessories', 0xCD, 2, 5),
    Field('tape', 'Tape', 'Accessories', 0xCC, 1, 1),
    Field('inners', 'Long-sleeved inners', 'Strip style', 0xCC, 0, 1),
    Field('sleeves', 'Sleeves (0 auto, 1 short, 2 long)', 'Strip style', 0xAF, 6, 2, 0, 0, 2),
    Field('shirttail', 'Shirt tucked in', 'Strip style', 0xA5, 7, 1),
]
MOTION = [
    Field('dribbling', 'Dribbling', 'Motion', 0x79, 0, 2, 1),
    Field('drop_kick', 'Drop kick', 'Motion', 0x79, 2, 2, 1),
    Field('free_kick', 'Free kick', 'Motion', 0x78, 1, 4, 1),
    Field('penalty', 'Penalty kick', 'Motion', 0x78, 5, 3, 1),
    Field('celebration1', 'Goal celebration 1 (0 off)', 'Motion', 0x97, 0, 7),
    Field('celebration2', 'Goal celebration 2 (0 off)', 'Motion', 0x98, 0, 7),
]
# Player Index cards. Confirmed in game (25-09): S01 S04 S06 P01 P03 P06 P12 P16.
# The rest follow the SER[G]ANT Cheat Engine table's bits for the same bytes.
_CARD_BITS = {
    'P01': (0x9D, 7), 'P02': (0x9E, 0), 'P03': (0x9E, 1), 'P04': (0x9E, 2), 'P05': (0x9E, 3),
    'P06': (0x9E, 4), 'P07': (0x9E, 5), 'P08': (0x9E, 6), 'P09': (0x9F, 0), 'P10': (0x9F, 1),
    'P11': (0x9F, 2), 'P12': (0x9F, 3), 'P15': (0xA1, 0), 'P16': (0xA1, 1),
    'S01': (0x96, 0), 'S02': (0x96, 1), 'S03': (0x9B, 4), 'S04': (0x97, 7), 'S05': (0xA2, 0),
    'S06': (0x9B, 1), 'S09': (0x9B, 2), 'S11': (0x9C, 1), 'S12': (0x9C, 0), 'S14': (0xA2, 4),
}
CARD_NAMES = {
    'P01': 'Classic No.10', 'P02': 'Anchor Man', 'P03': 'Trickster', 'P04': 'Darting Run',
    'P05': 'Mazing Run', 'P06': 'Pinpoint Pass', 'P07': 'Early Cross', 'P08': 'Box to Box',
    'P09': 'Incisive Run', 'P10': 'Long Ranger', 'P11': 'Enforcer', 'P12': 'Goal Poacher',
    'P15': 'Talisman', 'P16': 'Fox in the Box', 'S01': '1-touch play', 'S02': 'Outside curve',
    'S03': 'Long throw', 'S04': 'Super-sub', 'S05': 'Speed merchant', 'S06': 'Long range drive',
    'S09': 'Roulette', 'S11': 'Flicking', 'S12': 'Scissors', 'S14': 'Side-stepping',
}
CARDS = [Field('card_' + c.lower(), '%s %s' % (c, CARD_NAMES[c]), 'Cards', o, b, 1)
         for c, (o, b) in _CARD_BITS.items()]
# Everything not labelled above (face-build sliders, eyes, brows, nose, mouth,
# jaw, skin, facial hair, bracelet/wristband/undershorts, development type):
# edited as raw bytes, or copied whole from another player.
APPEARANCE_RAW = range(0x99, REC)

FIELDS = ABILITIES + PLAYABLE + [REGISTERED] + BASIC + PHYSIQUE + FACE + HAIR + ACCESSORIES + MOTION + CARDS
APPEARANCE_BYTES = range(0xA4, REC)  # copy-appearance block: physique, face, hair, accessories
BY_KEY = {f.key: f for f in FIELDS}


def pid(rec):
    return struct.unpack_from('<I', rec, ID_OFF)[0]


def text(rec, off, n):
    return bytes(rec[off:off + n]).split(b'\0')[0].decode('utf-8', 'replace')


def set_text(rec, off, n, s):
    rec[off:off + n] = s.encode('utf-8')[:n - 1].ljust(n, b'\0')


def name(rec):
    return text(rec, NAME_OFF, NAME_LEN)


def shirt(rec):
    return text(rec, SHIRT_OFF, SHIRT_LEN)


def to_dict(rec):
    d = {'name': name(rec), 'shirt': shirt(rec)}
    d.update({f.key: f.get(bytes(rec)) for f in FIELDS})
    return d


def from_dict(rec, d):
    """Apply a dict (e.g. a CSV row) onto a record; unknown/blank keys are skipped."""
    if d.get('name'):
        set_text(rec, NAME_OFF, NAME_LEN, d['name'])
    if d.get('shirt'):
        set_text(rec, SHIRT_OFF, SHIRT_LEN, d['shirt'].upper())
    for k, v in d.items():
        f = BY_KEY.get(k)
        if f is not None and v not in ('', None):
            lo, hi = f.range
            f.set(rec, max(lo, min(hi, int(v))))


if __name__ == '__main__':
    # self-check: set/get round-trips at every field's bounds without touching neighbours
    import random
    base = bytearray(random.Random(7).randbytes(REC))
    for f in FIELDS:
        for v in f.range:
            r = bytearray(base)
            f.set(r, v)
            assert f.get(bytes(r)) == v, (f.key, v)
            for g in FIELDS:
                if g is not f and (g.off, g.shift) != (f.off, f.shift) and not (
                        g.off <= f.off + 1 and f.off <= g.off + 1 and
                        set(range(g.off * 8 + g.shift, g.off * 8 + g.shift + g.bits)) &
                        set(range(f.off * 8 + f.shift, f.off * 8 + f.shift + f.bits))):
                    assert g.get(bytes(r)) == g.get(bytes(base)), (f.key, 'clobbered', g.key)
    print('pes12player: %d fields ok' % len(FIELDS))

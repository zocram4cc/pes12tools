"""4cc referees (PES2015 4cc_35_referees.cpk) -> PES2012 custom officials.

    python3 tools/pes12_import_referees.py <4cc_35_referees.cpk> <game dir>

The pack's referees are PES15 face packs (face/real/refereeNNN.cpk: face.xml,
oral_*.model, .mtl) whose materials point at textures the pack ships under
uniform/common/; its referee kits are uniform/texture/referee_<n>.dds
(WESYS-wrapped). Each referee is converted like a 4cc player face
(pes15_to_pes12.convert) into <game>/kitserver/4cc-players/custom/p<pid>,
pid = official_pid(NNN); the kits go to custom/kits/<OFFICIALS_TID>/r<n>_hi.dds.
drawlogic.dll draws a random referee from that pool on each official, per
match (dllprobe/drawlogic.cpp OFFICIAL_*). Needs the runtime's lodmixer pins
(lod.ref.*, dist README) so the officials draw their full-detail model.
"""
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cpk  # noqa: E402
import ktmdl  # noqa: E402
import pes15_to_pes12  # noqa: E402
from pes15_kits import HI_KIT_SUFFIX  # noqa: E402

OFFICIALS_TID = 999        # no 4cc team uses it (teams 705..784); drawlogic OFFICIALS_TID
PLACEHOLDER_TEAM_BASE, PLAYERS_PER_TEAM = 2000, 100   # drawlogic modelTeam: pid / 100 - 2000
REFEREE_CPK = re.compile(r'referee(\d{3})\.cpk$')
REFEREE_KIT = re.compile(r'referee_(\d+)\.dds$')
COMMON_DIR = re.compile(r'/uniform/common/[^/]+/[^/]+$')
CUSTOM_REL = os.path.join('kitserver', '4cc-players', 'custom')


def official_pid(n):
    if not 0 < n < PLAYERS_PER_TEAM:
        raise SystemExit('referee %d outside the officials pid range' % n)
    return (OFFICIALS_TID + PLACEHOLDER_TEAM_BASE) * PLAYERS_PER_TEAM + n


def files_under(root):
    for d, _, fs in os.walk(root):
        for f in fs:
            yield os.path.join(d, f).replace(os.sep, '/')


def stage(pack_cpk, tmp):
    """Extract the pack; build Faces/refereeNNN/ + Common/ as a 4cc face pack
    lays them out (pes15_to_pes12.common_dir). -> ({n: face dir}, {n: kit dds})."""
    raw = os.path.join(tmp, 'pack')
    cpk.extract(pack_cpk, raw)
    common = os.path.join(tmp, 'Common')
    os.makedirs(common)
    faces, kits = {}, {}
    for p in files_under(raw):
        if COMMON_DIR.search(p):
            shutil.copy(p, os.path.join(common, os.path.basename(p)))
        elif (m := REFEREE_KIT.search(p)):
            dst = os.path.join(tmp, 'referee_%s.dds' % m.group(1))
            open(dst, 'wb').write(ktmdl.unwesys(open(p, 'rb').read()))
            kits[int(m.group(1))] = dst
        elif (m := REFEREE_CPK.search(p)):
            n = int(m.group(1))
            out = os.path.join(tmp, 'face%03d' % n)
            cpk.extract(p, out)
            face = os.path.join(tmp, 'Faces', 'referee%03d' % n)
            os.makedirs(face)
            for q in files_under(out):
                shutil.copy(q, os.path.join(face, os.path.basename(q)))
            faces[n] = face
    return faces, kits


def main(pack_cpk, game):
    custom = os.path.join(game, CUSTOM_REL)
    if not os.path.isdir(custom):
        raise SystemExit('no %s: install the player runtime first' % custom)
    with tempfile.TemporaryDirectory() as tmp:
        faces, kits = stage(pack_cpk, tmp)
        if not faces or not kits:
            raise SystemExit('%s: %d referees, %d kits - not a referees pack' % (pack_cpk, len(faces), len(kits)))
        kit_dir = os.path.join(custom, 'kits', str(OFFICIALS_TID))
        os.makedirs(kit_dir, exist_ok=True)
        for n, k in sorted(kits.items()):
            shutil.copy(k, os.path.join(kit_dir, 'r%d%s' % (n, HI_KIT_SUFFIX)))
        stand_in = kits[min(kits)]   # kit-slot meshes sample the worn kit at runtime
        done, failed = 0, []
        for n, face in sorted(faces.items()):
            out = os.path.join(custom, 'p%d' % official_pid(n))
            try:
                if os.path.isdir(out):
                    shutil.rmtree(out)
                pes15_to_pes12.convert(face, stand_in, out)
                done += 1
            except Exception as e:   # one bad face must not cost the other 34
                failed.append((n, '%s: %s' % (type(e).__name__, e)))
        print('officials: %d referees -> custom/p%d..p%d, %d kits -> custom/kits/%d'
              % (done, official_pid(min(faces)), official_pid(max(faces)), len(kits), OFFICIALS_TID))
        for n, why in failed:
            print('  referee%03d not converted: %s' % (n, why))


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])

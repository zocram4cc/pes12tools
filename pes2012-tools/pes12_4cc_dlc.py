"""4CC DLC for PES2012: turns a stock PES2012 + kitserver 12 install into the 4cc base.

    python3 tools/pes12_4cc_dlc.py install <game dir> --crests <cpk> --leagues <cpk> --clover <png>
    python3 tools/pes12_4cc_dlc.py uninstall <game dir>

PES2014+ keep DLC in download/ so it overrides data/ without touching it.
PES2008-2013 have no such folder: the exe reads img/dt*.img only, and
growing an img crashes the game at boot (exit 5 - any entry relocated past
the stock end of dt04.img, even a lone league badge, 25-09). The override
layer for these games is kitserver's afs2fs: every file
`<root>/img/<dtXX>.img/<anything>_<entry>.bin` replaces that AFS entry, and
roots listed later in [afs2fs] win. The DLC is such a root,
`kitserver/4cc-dlc`, registered in kitserver/config.txt; the stock img files
are never written. Uninstall = delete the folder and the config line.

Inputs are the user's own copies; nothing PES- or 4cc-derived ships here:
  --crests   a PES 4cc pack holding common/render/symbol/flag/emblem_<tid>_r.png
             (e.g. PES2017 4cc_01_db.cpk)
  --leagues  a PES 4cc pack holding common/render/symbol/emblemLc/emb_0008.png
             (the /vg/ League swirl; e.g. PES2017 4cc_15_seasonal.cpk)
  --clover   the 4cc cloverleaf PNG (Rigged Wiki File:4cc-cloverleaf-logo.png)
  --team-list  4cc team list, default tools/team_list_4cc.txt (210 teams)

Root contents:
  dt04.img  26 players (23 flat-40 CB PLACEHOLDERs per team), 29 formations,
            30 teams, 31 rosters, 32 team names, 33 league lists (4chan Cup =
            boards, Backup Teams, /vg/ League, Invitational Teams), 36 + 41
            league badges, 65 team crests
  dt06.img  4, 17: menu league names (pes12strings.LEAGUE_RENAMES)
A save/EDIT.bin made before installing shadows the base player/team tables
(the game prefers EDIT rows): delete it or rebuild it with the editor.
"""
import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cpk  # noqa: E402
import pes12db  # noqa: E402
import pes12emblem  # noqa: E402
import pes12strings  # noqa: E402

ROOT_NAME = '4cc-dlc'                 # afs2fs root folder, relative to kitserver/
KITSERVER_CONFIG = 'kitserver/config.txt'
ROOT_LINE = 'img.dir = "%s"' % ROOT_NAME
PLACEHOLDER_PID_BASE = 2000   # placeholder pid = (2000 + tid) * 100 + n: collides with no stock pid
LEAGUE_BADGE_4CHAN_CUP = 36   # dt04 entry of the English League badge (renamed 4chan Cup)
LEAGUE_BADGE_VGL = 41         # dt04 entry of the PES League badge (renamed /vg/ League)
CREST_DIR = 'common/render/symbol/flag'
VGL_BADGE = 'common/render/symbol/emblemLc/emb_0008.png'
DB_BINS = (pes12db.BIN_PLAYERS, pes12db.BIN_FORMATIONS, pes12db.BIN_TEAMS,
           pes12db.BIN_ROSTERS, pes12db.BIN_NAMES, pes12db.BIN_LEAGUES)


def _root(game):
    return os.path.join(game, 'kitserver', ROOT_NAME)


def _register(game):
    """Add the root as the LAST img.dir of [afs2fs] (last root wins)."""
    path = os.path.join(game, KITSERVER_CONFIG)
    lines = open(path, encoding='utf-8-sig').read().splitlines()
    if ROOT_LINE in lines:
        return
    if '[afs2fs]' not in lines:
        lines = ['[afs2fs]', ROOT_LINE, ''] + lines
    else:
        at = lines.index('[afs2fs]') + 1
        while at < len(lines) and lines[at].strip().startswith('img.dir'):
            at += 1
        lines.insert(at, ROOT_LINE)
    if not any(l.strip() == 'dll = afs2fs' for l in lines):
        raise SystemExit('%s: afs2fs is not enabled ([kload] dll = afs2fs)' % path)
    open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')


def install(game, crests_cpk, leagues_cpk, clover, team_list):
    if not os.path.exists(os.path.join(game, KITSERVER_CONFIG)):
        raise SystemExit('%s: kitserver 12 not installed (needs afsio + afs2fs)' % game)
    root = _root(game)
    shutil.rmtree(root, ignore_errors=True)
    work = tempfile.mkdtemp(prefix='pes12_4cc_')
    try:
        cpk.extract(crests_cpk, work, False, CREST_DIR + '/emblem_')
        cpk.extract(leagues_cpk, work, False, VGL_BADGE)
        vgl = os.path.join(work, VGL_BADGE)
        if not os.path.exists(vgl):
            raise SystemExit('%s: no %s' % (leagues_cpk, VGL_BADGE))
        dt04 = os.path.join(root, pes12db.DT04)
        os.makedirs(dt04)
        plan, slots = pes12db.cmd_overwrite(game, team_list, dt04, PLACEHOLDER_PID_BASE)
        for entry, png in ((LEAGUE_BADGE_4CHAN_CUP, clover), (LEAGUE_BADGE_VGL, vgl)):
            pes12emblem.build_league(game, entry, png, os.path.join(dt04, 'dt04_%d.bin' % entry))
        specs, missing = [], []
        for tid, _, name in plan:
            png = os.path.join(work, CREST_DIR, 'emblem_%04d_r.png' % tid)
            if os.path.exists(png):
                specs.append((tid, png, 'f'))  # suffix replaced by the victim row's
            else:
                missing.append(tid)
        pes12emblem.build_teams(game, os.path.join(dt04, 'dt04_%d.bin' % pes12emblem.BIN_TEAMS),
                                specs, slots)
        for img, blobs in pes12strings.rename_blobs(game).items():
            d = os.path.join(root, img)
            os.makedirs(d, exist_ok=True)
            for i, raw in blobs.items():
                open(os.path.join(d, '%s_%d.bin' % (os.path.basename(img)[:4], i)), 'wb').write(raw)
        _register(game)
        print('4CC DLC -> %s: %d teams, %d crests (no crest in the pack for %d tids)'
              % (root, len(plan), len(specs), len(missing)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def uninstall(game):
    shutil.rmtree(_root(game), ignore_errors=True)
    path = os.path.join(game, KITSERVER_CONFIG)
    lines = open(path, encoding='utf-8-sig').read().splitlines()
    open(path, 'w', encoding='utf-8').write('\n'.join(l for l in lines if l != ROOT_LINE) + '\n')
    print('4CC DLC removed')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('cmd', choices=('install', 'uninstall'))
    ap.add_argument('game')
    ap.add_argument('--crests')
    ap.add_argument('--leagues')
    ap.add_argument('--clover')
    ap.add_argument('--team-list', default=os.path.join(HERE, 'team_list_4cc.txt'))
    a = ap.parse_args()
    if a.cmd == 'uninstall':
        return uninstall(a.game)
    if not (a.crests and a.leagues and a.clover):
        ap.error('install needs --crests, --leagues and --clover')
    install(a.game, a.crests, a.leagues, a.clover, a.team_list)


if __name__ == '__main__':
    main()

"""Install the custom-body runtime into a PES2012 + kitserver 12 game folder.

    python3 pes12_runtime.py install <game dir>

What custom players, referees and adboards need on top of the 4cc DLC base
(which loads only afsio + afs2fs), all idempotent:

  kitserver/fserv.dll, lodmixer.dll   stock kitserver 12 modules, copied from
                                      pes2012-4cc/kitserver when missing
  kitserver/drawhook.dll              the runtime shim (runtime/)
  kitserver/4cc-players/drawlogic.dll the runtime itself (hot-reloaded)
  kitserver/4cc-players/custom/, custom/kits/, flags/, kitmap/
  kitserver/config.txt                [kload] dll = fserv, lodmixer, drawhook;
                                      [lodmixer] LOD pins (LOD_PINS)
  pes2012.exe                         large-address-aware (backup
                                      pes2012.exe.pre-laa.bak)

fserv serves each custom player's marked face.bin (kitserver/GDB/faces),
which is how drawlogic knows who it is drawing. lodmixer pins players and
officials to their full-detail model: lower LODs are other meshes drawlogic
does not replace. Pinning the entrance LOD needs the 4 GB address space;
without the LAA flag the game exits (code 5) entering a match (24-09).
"""
import os
import re
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(HERE, 'runtime')
KITSERVER_SRC = os.path.join(HERE, '..', 'pes2012-4cc', 'kitserver')
STOCK_MODULES = ('fserv.dll', 'lodmixer.dll')
# [kload] lines this adds; fserv goes right after afsio (the order the
# working install has), the rest at the end of the section
KLOAD_AFTER_AFSIO = ('fserv',)
KLOAD_APPEND = ('lodmixer', 'drawhook')
# lodmixer: values at or below 0.0001 are ignored (FLOAT_ZERO, strict); 0.001
# never steps down from LOD0 (06-custom-body.md)
LOD_PIN = '0.001'
LOD_PINS = tuple('lod.players.%s.s%d' % (w, s) for w in ('entrance', 'inplay', 'misc', 'replay') for s in (1, 2, 3)) \
    + tuple('lod.active.player.%s.s%d' % (w, s) for w in ('ck', 'fk') for s in (1, 2, 3)) \
    + ('lod.ref.inplay', 'lod.ref.replay')
PLAYER_DIRS = ('custom', os.path.join('custom', 'kits'), 'flags', 'kitmap')
PE_OFFSET_FIELD = 0x3C              # DOS header: offset of the PE signature
COFF_CHARACTERISTICS = 4 + 18       # PE signature + COFF fields before Characteristics
LARGE_ADDRESS_AWARE = 0x0020


def set_config(text):
    """config.txt text -> with the runtime's [kload] modules and LOD pins."""
    lines = text.splitlines()

    def section(name):
        """(start, end) line range of [name]'s body, adding the section if absent."""
        head = next((i for i, l in enumerate(lines) if l.strip().lower() == '[%s]' % name), None)
        if head is None:
            lines.extend(['', '[%s]' % name])
            head = len(lines) - 1
        end = next((i for i in range(head + 1, len(lines)) if lines[i].strip().startswith('[')), len(lines))
        while end > head + 1 and not lines[end - 1].strip():
            end -= 1
        return head + 1, end

    def has_dll(name):
        a, b = section('kload')
        return any(re.match(r'\s*dll\s*=\s*%s\s*$' % name, l, re.I) for l in lines[a:b])

    for name in KLOAD_AFTER_AFSIO:
        if not has_dll(name):
            a, b = section('kload')
            at = next((i + 1 for i in range(a, b) if re.match(r'\s*dll\s*=\s*afsio\s*$', lines[i], re.I)), b)
            lines.insert(at, 'dll = %s' % name)
    for name in KLOAD_APPEND:
        if not has_dll(name):
            lines.insert(section('kload')[1], 'dll = %s' % name)
    for key in LOD_PINS:
        a, b = section('lodmixer')
        at = next((i for i in range(a, b) if lines[i].split('=')[0].strip() == key), None)
        if at is None:
            lines.insert(b, '%s = %s' % (key, LOD_PIN))
        else:
            lines[at] = '%s = %s' % (key, LOD_PIN)
    return '\n'.join(lines) + '\n'


def set_laa(exe):
    """Large-address-aware flag on; -> True if it had to be set."""
    data = bytearray(open(exe, 'rb').read())
    at = struct.unpack_from('<I', data, PE_OFFSET_FIELD)[0] + COFF_CHARACTERISTICS
    flags = struct.unpack_from('<H', data, at)[0]
    if flags & LARGE_ADDRESS_AWARE:
        return False
    backup = exe + '.pre-laa.bak'
    if not os.path.exists(backup):
        shutil.copy2(exe, backup)
    struct.pack_into('<H', data, at, flags | LARGE_ADDRESS_AWARE)
    open(exe, 'wb').write(data)
    return True


def install(game):
    ks = os.path.join(game, 'kitserver')
    exe = os.path.join(game, 'pes2012.exe')
    if not os.path.exists(os.path.join(ks, 'kload.dll')) or not os.path.exists(exe):
        sys.exit('%s: no pes2012.exe + kitserver/kload.dll (install the 4cc DLC base first)' % game)
    for m in STOCK_MODULES:
        dst = os.path.join(ks, m)
        if not os.path.exists(dst):
            src = os.path.join(KITSERVER_SRC, m)
            if not os.path.exists(src):
                sys.exit('%s missing: copy it from kitserver 12 into %s' % (m, ks))
            shutil.copy2(src, dst)
            print('copied', m)
    players = os.path.join(ks, '4cc-players')
    for d in PLAYER_DIRS:
        os.makedirs(os.path.join(players, d), exist_ok=True)
    shutil.copy2(os.path.join(RUNTIME, 'drawhook.dll'), os.path.join(ks, 'drawhook.dll'))
    shutil.copy2(os.path.join(RUNTIME, 'drawlogic.dll'), os.path.join(players, 'drawlogic.dll'))
    cfg = os.path.join(ks, 'config.txt')
    old = open(cfg, encoding='utf-8', errors='surrogateescape').read() if os.path.exists(cfg) else ''
    new = set_config(old)
    if new != old:
        open(cfg, 'w', encoding='utf-8', errors='surrogateescape').write(new)
        print('config.txt: runtime modules + %d LOD pins' % len(LOD_PINS))
    if set_laa(exe):
        print('pes2012.exe: large-address-aware (backup pes2012.exe.pre-laa.bak)')
    print('runtime installed: %s' % players)


def _selfcheck():
    base = '[afs2fs]\nimg.dir = "4cc-dlc"\n\n[kload]\ndll = afsio\ndll = afs2fs\n'
    out = set_config(base)
    assert out.split('[kload]')[1].split()[:12] == ['dll', '=', 'afsio', 'dll', '=', 'fserv', 'dll', '=',
                                                    'afs2fs', 'dll', '=', 'lodmixer'], out
    assert 'dll = drawhook' in out and out.count('= 0.001') == len(LOD_PINS)
    assert set_config(out) == out                                 # idempotent
    stale = out.replace('lod.ref.inplay = 0.001', 'lod.ref.inplay = 0.5')
    assert set_config(stale) == out                               # wrong pin value corrected


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'selfcheck':
        _selfcheck()
        print('ok')
    elif len(sys.argv) == 3 and sys.argv[1] == 'install':
        install(sys.argv[2])
    else:
        sys.exit(__doc__)

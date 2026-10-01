"""AFS (.img) archive reader: 'AFS\\0', u32 count, then count x (u32 offset,
u32 size). Entries are usually WESYS-zlib BINs.

  python3 afs.py list <file.img>                 # index, offset, size, magic
  python3 afs.py scan <file.img> [...]           # KTMDL bbox per entry
  python3 afs.py extract <file.img> <index> <out>  # decompressed
  python3 afs.py raw <file.img> <index> <out>      # as stored (WESYS kept): what afs2fs serves
"""
import struct
import sys

import ktmdl


def entries(path):
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] != b"AFS\0":
            raise ValueError("%s: not AFS" % path)
        count = struct.unpack_from("<I", head, 4)[0]
        table = f.read(8 * count)
    return [struct.unpack_from("<II", table, 8 * i) for i in range(count)]


def read(path, index):
    off, size = entries(path)[index]
    with open(path, "rb") as f:
        f.seek(off)
        return f.read(size)


AFS_ALIGN = 2048  # AFS sector: stock offsets and EOF are 2048-aligned; the game reads whole sectors


def replace(path, blobs):
    """In-place: rewrite entries {index: raw bytes}. A blob that fits the
    entry's current slot (up to the next entry) is written there; a larger
    one is appended at EOF and its table row repointed. Every write is
    zero-padded to the next sector and the file ends on a sector boundary,
    as in stock archives."""
    ents = entries(path)
    starts = sorted(o for o, s in ents if s)
    with open(path, "r+b") as f:
        f.seek(0, 2)
        end = -(-f.tell() // AFS_ALIGN) * AFS_ALIGN
        for i, raw in sorted(blobs.items()):
            off, size = ents[i]
            nxt = next((o for o in starts if o > off), end)
            padded = raw + b"\0" * (-len(raw) % AFS_ALIGN)
            if len(padded) > nxt - off:
                off = end
            f.seek(off)
            f.write(padded)
            end = max(end, off + len(padded))
            f.seek(8 + 8 * i)
            f.write(struct.pack("<II", off, len(raw)))
        f.truncate(end)


def model_extent(blob):
    """-> (#KTMDL blocks, total verts, bbox height) over the entry."""
    blocks = verts = 0
    lo, hi = [1e9] * 3, [-1e9] * 3
    at = 0
    while True:
        base = blob.find(ktmdl.MAGIC, at)
        if base < 0:
            break
        blocks += 1
        try:
            for sec in ktmdl.sections(blob, base):
                if not (0 < sec["stride"] <= 128) or sec["verts"] > 200000:
                    continue
                for p in ktmdl.positions(blob, sec):
                    for k in range(3):
                        lo[k] = min(lo[k], p[k]); hi[k] = max(hi[k], p[k])
                verts += sec["verts"]
        except (struct.error, IndexError):
            pass
        at = base + 1
    return blocks, verts, (hi[1] - lo[1]) if verts else 0.0, lo, hi


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("list", "scan", "extract", "raw"):
        sys.exit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    if cmd == "list":
        with open(path, "rb") as f:
            for i, (off, size) in enumerate(entries(path)):
                f.seek(off)
                print(i, off, size, f.read(8)[3:8])
    elif cmd == "scan":
        for p in sys.argv[2:]:
            with open(p, "rb") as f:
                for i, (off, size) in enumerate(entries(p)):
                    if size == 0:
                        continue
                    f.seek(off)
                    raw = f.read(size)
                    try:
                        blob = ktmdl.unwesys(raw)
                    except Exception:
                        blob = raw
                    b, v, h, lo, hi = model_extent(blob)
                    if b:
                        print("%s %d size %d ktmdl %d verts %d height %.2f x %.2f..%.2f z %.2f..%.2f"
                              % (p.split("/")[-1], i, size, b, v, h, lo[0], hi[0], lo[2], hi[2]))
    elif cmd == "extract":
        raw = read(path, int(sys.argv[3]))
        open(sys.argv[4], "wb").write(ktmdl.unwesys(raw))
    elif cmd == "raw":
        open(sys.argv[4], "wb").write(read(path, int(sys.argv[3])))


if __name__ == "__main__":
    main()

"""PES 2010-2013 KTMDL probe reader.

Layout follows liberostelios/PES-Tools readKTMDL (Tools.cs:1019), offsets
relative to the start of the KTMDL block:
  +40 section count, +44 material table offset (-52), +52 section table,
  +80 material count, +128 texture table offset (+8).
Section record (64 bytes): vert offset, vert count, byte, stride byte, ...,
index offset, index count; offsets relative to the record.

  python3 ktmdl.py <decompressed.bin.raw>        # scans every KTMDL block
"""
import struct
import sys
import zlib

MAGIC = b"KTMDL\0"


def unwesys(data):
    """WESYS container: 16-byte header, zlib body."""
    if data[3:8] == b"WESYS":
        return zlib.decompress(data[16:])
    return data


def sections(blob, base):
    u32 = lambda o: struct.unpack_from("<I", blob, base + o)[0]
    count = u32(40)
    table = u32(52)
    out = []
    for i in range(count):
        rec = base + table + 64 * i
        voff, vcnt = struct.unpack_from("<II", blob, rec)
        stride = blob[rec + 9]
        ioff, icnt = struct.unpack_from("<II", blob, rec + 32)
        out.append(dict(index=i, vert_offset=base + table + 64 * i + voff,
                        verts=vcnt, stride=stride, flags=blob[rec + 8],
                        index_offset=base + table + 64 * i + 32 + ioff,
                        indices=icnt, raw=blob[rec:rec + 64]))
    return out


def positions(blob, sec):
    return [struct.unpack_from("<3f", blob, sec["vert_offset"] + k * sec["stride"])
            for k in range(sec["verts"])]


def main(path):
    blob = unwesys(open(path, "rb").read())
    at = 0
    while True:
        base = blob.find(MAGIC, at)
        if base < 0:
            break
        print("KTMDL @0x%x" % base)
        for sec in sections(blob, base):
            pts = positions(blob, sec)
            lo = [min(p[k] for p in pts) for k in range(3)]
            hi = [max(p[k] for p in pts) for k in range(3)]
            first = blob[sec["vert_offset"]:sec["vert_offset"] + sec["stride"]]
            print("  sec %d verts %d stride %d flags %d idx %d  bbox %s..%s"
                  % (sec["index"], sec["verts"], sec["stride"], sec["flags"],
                     sec["indices"], ["%.3f" % v for v in lo], ["%.3f" % v for v in hi]))
            print("    v0 %s" % first.hex())
        at = base + 1


if __name__ == "__main__":
    main(sys.argv[1])

"""Fox Engine .fpk / .fpkd pack extractor (PES 2021 face/boots/stadium packs).

Layout (GzsTool-compatible):
  0x00  "foxfpk" (or "foxfpkd") + "win" magic
  0x0A  u32 file size, 18 bytes zero, u32 2, u32 fileCount, u32 refCount, pad
  0x30  fileCount x 48-byte entries:
          u32 dataOffset, pad4, i32 dataSize, pad4,
          { i32 strOffset, pad4, i32 strLength, pad4 }, md5[16]
  then reference entries (16 bytes: same string struct), path strings, data.
"""

import os
import struct
import sys


class Entry:
    def __init__(self, path, offset, size):
        self.path = path
        self.offset = offset
        self.size = size


def parse(blob: bytes):
    if blob[:6] != b"foxfpk":
        raise ValueError("not an fpk")
    file_count, ref_count = struct.unpack_from("<II", blob, 0x24)
    entries = []
    at = 0x30
    for _ in range(file_count):
        offset, size = struct.unpack_from("<IxxxxIxxxx", blob, at)
        str_off, str_len = struct.unpack_from("<IxxxxIxxxx", blob, at + 16)
        path = blob[str_off:str_off + str_len].decode("utf-8", "replace")
        entries.append(Entry(path, offset, size))
        at += 48
    return entries


def extract(fpk_path, dest):
    blob = open(fpk_path, "rb").read()
    entries = parse(blob)
    for e in entries:
        name = e.path.replace("\\", "/")
        if ":" in name:                      # drop the "Z:" style drive prefix
            name = name.split(":", 1)[1]
        name = name.lstrip("/")
        out = os.path.join(dest, name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as o:
            o.write(blob[e.offset:e.offset + e.size])
    return entries


if __name__ == "__main__":
    entries = extract(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "fpk_out")
    for e in entries:
        print("%9d  %s" % (e.size, e.path))

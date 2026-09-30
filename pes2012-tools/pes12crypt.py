"""PES2012 PC save crypto (EDIT.bin, OPTION.bin, ...), transcribed from the
v1.06 exe at runtime (dllprobe codedump, 24-09):

  0x4AEAF0  header cipher      0x4AEB30/0x4AEB50  body cipher
  0x4AEB90  CRC-16/CCITT (poly 0x1021, init 0, table, result NOT'ed)
  0x4AE9F0  magic check: u16 0x0000, u16 0xF573

Layout: magic(4) | header(HEADER_BYTES - 4) | body.
  header word at SEED_OFF  = CRC16(header[0:SEED_OFF])        (plaintext)
  header word at BODYCRC_OFF = CRC16(body)                    (encrypted with header)
Both ciphers XOR each byte with (x >> ((x >> 16) & 15)) & 0xFF and step the
32-bit LCG x = x * 0x41C64E6D + 0x3039, x starting at the 16-bit seed:
  header seed = word[SEED_OFF], covering bytes 4 .. SEED_OFF-1
  body seed   = word[SEED_OFF] ^ word[BODYCRC_OFF] (decrypted)

    python3 pes12crypt.py dec EDIT.bin EDIT.plain
    python3 pes12crypt.py enc EDIT.plain EDIT.bin
"""
import struct
import sys

import numpy as np

MAGIC = b'\x00\x00\x73\xf5'
LCG_MUL = 0x41C64E6D
LCG_ADD = 0x3039
CRC_POLY = 0x1021
HEADER_BYTES = 0x308          # magic + 0x304 read by 0x4AEA10
BODYCRC_OFF = 0x304
SEED_OFF = 0x306


def keystream(seed, n):
    out = np.empty(n, np.uint8)
    x = seed & 0xFFFFFFFF
    for i in range(n):
        out[i] = (x >> ((x >> 16) & 15)) & 0xFF
        x = (x * LCG_MUL + LCG_ADD) & 0xFFFFFFFF
    return out


def _crc_table():
    t = []
    for c in range(256):
        v = c << 8
        for _ in range(8):
            v = ((v << 1) ^ CRC_POLY) if v & 0x8000 else (v << 1)
            v &= 0xFFFF
        t.append(v)
    return t


_CRC = _crc_table()


def crc16(data):
    v = 0
    for b in data:
        v = ((v << 8) & 0xFFFF) ^ _CRC[((v >> 8) ^ b) & 0xFF]
    return (~v) & 0xFFFF


def decrypt(blob):
    if blob[:4] != MAGIC:
        raise ValueError('not a PES2012 save (magic %s)' % blob[:4].hex())
    b = np.frombuffer(blob, np.uint8).copy()
    seed = struct.unpack_from('<H', blob, SEED_OFF)[0]
    b[4:SEED_OFF] ^= keystream(seed, SEED_OFF - 4)
    if crc16(bytes(b[:SEED_OFF])) != seed:
        raise ValueError('header CRC mismatch')
    body_crc = struct.unpack_from('<H', bytes(b), BODYCRC_OFF)[0]
    b[HEADER_BYTES:] ^= keystream(seed ^ body_crc, len(b) - HEADER_BYTES)
    if crc16(bytes(b[HEADER_BYTES:])) != body_crc:
        raise ValueError('body CRC mismatch')
    return bytes(b)


def encrypt(plain):
    b = np.frombuffer(plain, np.uint8).copy()
    body_crc = crc16(bytes(b[HEADER_BYTES:]))
    struct.pack_into('<H', b, BODYCRC_OFF, body_crc)
    seed = crc16(bytes(b[:SEED_OFF]))
    struct.pack_into('<H', b, SEED_OFF, seed)
    b[HEADER_BYTES:] ^= keystream(seed ^ body_crc, len(b) - HEADER_BYTES)
    b[4:SEED_OFF] ^= keystream(seed, SEED_OFF - 4)
    return bytes(b)


if __name__ == '__main__':
    mode, src, dst = sys.argv[1:4]
    data = open(src, 'rb').read()
    open(dst, 'wb').write(decrypt(data) if mode == 'dec' else encrypt(data))

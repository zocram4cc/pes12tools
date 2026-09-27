"""PES2015 save / team export crypto (EDIT, 4cc tactical exports *.bin).

Port of the4chancup/libpes15crypter (via the jasonjk192/pesXdecrypter fork):

  0..48          49-byte header (byte 0 = cipher seed, then md5 hashes)
  49..           chunk0 (384 B description) | u32 len1 | chunk1 (PNG) | u32 len2 | chunk2 (data)

Each chunk is XORed byte by byte with a stream restarted from the seed for
every chunk: x = (x * 21 + 7) % 32768, key byte = x % 255. The two length
words are plaintext. Payload = chunk2.

    python3 pes15crypt.py dec <in.bin> <out.plain>
"""
import struct
import sys

HEADER_BYTES = 49
DESC_BYTES = 384
LCG_MUL, LCG_ADD, LCG_MOD, KEY_MOD = 21, 7, 32768, 255


def _xor(chunk, seed):
    out = bytearray(chunk)
    x = seed
    for i in range(len(out)):
        x = (x * LCG_MUL + LCG_ADD) % LCG_MOD
        out[i] ^= x % KEY_MOD
    return bytes(out)


def decrypt(raw):
    """-> (description, png, data)."""
    seed = raw[0]
    at = HEADER_BYTES
    desc = _xor(raw[at:at + DESC_BYTES], seed)
    at += DESC_BYTES
    n1 = struct.unpack_from('<I', raw, at)[0]
    png = _xor(raw[at + 4:at + 4 + n1], seed)
    at += 4 + n1
    n2 = struct.unpack_from('<I', raw, at)[0]
    data = _xor(raw[at + 4:at + 4 + n2], seed)
    return desc, png, data


if __name__ == '__main__':
    if sys.argv[1] == 'dec':
        d, p, data = decrypt(open(sys.argv[2], 'rb').read())
        open(sys.argv[3], 'wb').write(data)
        print('description %r, png %d B, data %d B' % (d.split(b'\0')[0][:60], len(p), len(data)))

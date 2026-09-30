"""PES2016-2017 save / team export crypto (TEEXPORT).

Port of jasonjk192/pesXdecrypter src/crypt.c (the "Old" variant used by
PES16/17, itself from the4chancup/pesXdecrypter) to Python:

  0..319      320-byte encryption header (MT19937-keyed stream)
  320..       FileHeaderOld (176 B): 64 mystery | u32 dataSize | u32 logoSize
              | u32 descSize | u32 serialLength | 64 hash | 32 fileTypeString
  then description | logo | data (payload) | serial*2

The stream (crypt.c cryptStream) is MT19937 init_by_array(64-byte key)
mixed through a 4-register LFSR:
    out[i] = c4 ^ c3 ^ c2 ^ c1 ^ c0 ^ in[i]
    c0 = ror(c1,15)  c1 = rol(c2,11)  c2 = rol(c3,7)  c3 = ror(c4,13)
c4 is always a fresh MT word, so the offsets folded into the four held
registers never accumulate: from i >= 4 the key part is always
    s[i+4] ^ rot(s[i+3],-13) ^ rot(s[i+2],-6) ^ rot(s[i+1],5) ^ rot(s[i],-10)
The first four words (which also feed the NUL-length tail case) use the
exact offsets from the scalar derivation. The whole stream is therefore
one vectorized pass over the MT output (numpy); only the MT19937 refill's
dependent tail (227 words) stays scalar. Verified against the scalar C
reference in `check`.

    python3 pes17crypt.py check
    python3 pes17crypt.py dec <in> <out.plain>
"""
import struct
import sys

HEADER_SIZE = 320          # crypt.c ENCRYPTION_HEADER_SIZE
HEADER_OLD = 176           # sizeof FileHeaderOld (crypt.h)

# src/masterkey.c MasterKeyPes17 (PES 2017 regular)
MASTER_KEY_PES17 = bytes([
    0x9B, 0xC7, 0x13, 0x28, 0x2D, 0xE8, 0x47, 0x75,
    0x4D, 0x52, 0x9E, 0x35, 0x90, 0xAA, 0x6A, 0x7A,
    0x5C, 0xFA, 0x60, 0x9F, 0x6A, 0x32, 0x04, 0x57,
    0xB8, 0x9F, 0x59, 0xA5, 0x5F, 0xAC, 0x7D, 0x62,
    0xFE, 0x10, 0x2A, 0xD6, 0x95, 0xFA, 0xDF, 0xA0,
    0x68, 0xBD, 0x40, 0x95, 0x47, 0x9C, 0xBB, 0x40,
    0xF2, 0x94, 0x49, 0x3C, 0xC8, 0xE0, 0x94, 0x9D,
    0x7B, 0x01, 0x6F, 0xF2, 0xC5, 0x3A, 0x2C, 0xE5,
])

_N, _M = 624, 397
_MATRIX_A = 0x9908B0DF
_UPPER, _LOWER = 0x80000000, 0x7FFFFFFF
# (r0, r1, r2, r3) = rotations of (s[i], s[i+1], s[i+2], s[i+3]) in the four
# registers when out[i] is computed; s[i+4] is always unrotated. Scalar
# derivation: c0 = ror(c1,15), c1 = rol(c2,11), c2 = rol(c3,7),
# c3 = ror(c4,13), c4 fresh; so from i >= 4 the offsets stop changing
# at the fixed point (-10, 5, -6, -13); first four words as derived.
_ROT = ((0, 0, 0, 0), (-15, 11, 7, -13), (-4, 18, -6, -13),
        (3, 5, -6, -13), (-10, 5, -6, -13))


def _rot(v, r):
    r %= 32
    if r == 0:
        return v
    return ((v << r) | (v >> (32 - r))) & 0xFFFFFFFF


def _init_mt(key_words):
    """mt19937ar.c init_by_array (C: i=1; j=0; k=N for a 16-word key)."""
    mt = [0] * _N
    mt[0] = 19650218
    for i in range(1, _N):
        mt[i] = (1812433253 * (mt[i - 1] ^ (mt[i - 1] >> 30)) + i) & 0xFFFFFFFF
    i, j = 1, 0
    k = _N if _N > len(key_words) else len(key_words)
    for _ in range(k):
        mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1664525)) + key_words[j] + j
        mt[i] &= 0xFFFFFFFF
        i += 1; j += 1
        if i >= _N:
            mt[0] = mt[_N - 1]; i = 1
        if j >= len(key_words):
            j = 0
    for k in range(_N - 1, 0, -1):
        mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1566083941)) - i
        mt[i] &= 0xFFFFFFFF
        i += 1
        if i >= _N:
            mt[0] = mt[_N - 1]; i = 1
    mt[0] = 0x80000000
    return mt


def _mt_words(key, count):
    import numpy as np
    mt = np.array(_init_mt(struct.unpack('<16I', key)), np.uint32)
    out = np.empty(count, np.uint32)
    pos = 0
    while pos < count:
        y = (mt[:_N - _M] & _UPPER) | (mt[1:_N - _M + 1] & _LOWER)
        m = mt.copy()
        m[:_N - _M] = mt[_M:] ^ (y >> 1) ^ np.where(y & 1, _MATRIX_A, 0)
        for kk in range(_N - _M, _N - 1):
            yv = (m[kk] & _UPPER) | (m[kk + 1] & _LOWER)
            m[kk] = m[kk + _M - _N] ^ (yv >> 1) ^ (_MATRIX_A if yv & 1 else 0)
        yv = (m[_N - 1] & _UPPER) | (m[0] & _LOWER)
        m[_N - 1] = m[_M - 1] ^ (yv >> 1) ^ (_MATRIX_A if yv & 1 else 0)
        t = m ^ (m >> 11)
        t ^= (t << 7) & np.uint32(0x9D2C5680)
        t ^= (t << 15) & np.uint32(0xEFC60000)
        t ^= t >> 18
        n = min(_N, count - pos)
        out[pos:pos + n] = t[:n]
        pos += n
        mt = m
    return out


def _stream_word(s, i):
    """Word i of the keystream (fresh c4 xorred, four held registers)."""
    r0, r1, r2, r3 = _ROT[i if i < 4 else 4]
    return (_rot(s[i], r0) ^ _rot(s[i + 1], r1) ^ _rot(s[i + 2], r2)
            ^ _rot(s[i + 3], r3) ^ s[i + 4])


def _crypt_stream(key, chunk):
    """crypt.c cryptStream: LFSR over MT19937(key) xor input, 32-bit words."""
    import numpy as np
    n = len(chunk) // 4
    s = _mt_words(key, n + 5)
    out = np.empty(max(n, 1), np.uint32)
    out[:n] = np.frombuffer(chunk[:n * 4], np.uint32)
    if n > 4:
        a, b, c, d = 22, 5, 26, 19 # _ROT[4] as rotl amounts
        out[4:] ^= (np.bitwise_xor.reduce([
            _rotv(s[4:n], a), _rotv(s[5:n + 1], b),
            _rotv(s[6:n + 2], c), _rotv(s[7:n + 3], d), s[8:n + 4]]))
    for i in range(min(n, 4)):
        out[i] ^= _stream_word(s, i)
    r = len(chunk) & 3
    if r:
        part = int(_stream_word(s, n)) & ((1 << 8 * r) - 1)
        tail = int.from_bytes(chunk[n * 4:], 'little') ^ part
        return out[:n].tobytes() + tail.to_bytes(r, 'little')
    return out[:n].tobytes()


def _rotv(v, r):
    import numpy as np
    if r == 0:
        return v
    return ((v << np.uint32(r)) | (v >> np.uint32(32 - r))).astype(np.uint32)


def _reverse_longs(key):
    return b''.join(bytes(reversed(key[i * 8:(i + 1) * 8])) for i in range(8))


def _xor_repeating(dst, key, n):
    # crypt.c xorRepeatingBlocks: output[i & 63] ^= key[i] for i in 0..n
    for i in range(n):
        dst[i & 63] ^= key[i % len(key)]


def _xor_longs(chunk, param):
    w = struct.pack('<Q', param)
    out = bytearray(chunk)
    for i in range(0, len(chunk), 8):
        for j in range(8):
            out[i + j] ^= w[j]
    return bytes(out)


def _crypt_header(inp, master_key):
    """crypt.c cryptHeader: bytes 256..319 are key material, masked by the
    reversed master key; the stream covers only the 320-byte header, and
    bytes 256..319 copy through unchanged."""
    hk = bytearray(inp[256:320])
    _xor_repeating(hk, _reverse_longs(master_key), 64)
    out = _crypt_stream(bytes(hk), inp[:HEADER_SIZE])
    return out[:256] + inp[256:HEADER_SIZE]


def decrypt(raw, master_key=MASTER_KEY_PES17):
    """-> (description, logo, data)."""
    enc_hdr = _crypt_header(raw, master_key)
    at = HEADER_SIZE
    rolling = bytearray(64)
    _xor_repeating(rolling, enc_hdr[:64], 64)
    _xor_repeating(rolling, enc_hdr[64:HEADER_SIZE], 256)
    rolling = bytes(rolling)
    inter = _xor_longs(rolling, HEADER_OLD)
    fh = _crypt_stream(inter, raw[at:at + HEADER_OLD])
    at += HEADER_OLD
    data_size, logo_size, desc_size, serial_len = struct.unpack_from('<4I', fh, 64)
    desc = _crypt_stream(_xor_longs(rolling, 0), raw[at:at + desc_size]); at += desc_size
    logo = _crypt_stream(_xor_longs(rolling, 1), raw[at:at + logo_size]); at += logo_size
    data = _crypt_stream(_xor_longs(rolling, 2), raw[at:at + data_size])
    return desc, logo, data


def check():
    # self-check: vectorized stream == scalar crypt.c line by line
    import numpy as np
    key = bytes(range(64))
    for words, tail in ((3, 0), (4, 0), (5, 0), (40, 0), (200, 0), (9, 3), (40, 1), (5, 2)):
        s = _mt_words(key, words + 7)
        cin = os.urandom(words * 4 + tail)
        ref = bytearray(len(cin))
        c0, c1, c2, c3 = s[0], s[1], s[2], s[3]
        for i in range(words):
            c4 = s[i + 4]
            struct.pack_into('<I', ref, i * 4,
                             c4 ^ c3 ^ c2 ^ c1 ^ c0 ^ struct.unpack_from('<I', cin, i * 4)[0])
            c0 = _rot(c1, -15); c1 = _rot(c2, 11)
            c2 = _rot(c3, 7); c3 = _rot(c4, -13)
        if tail:
            rest = int(int.from_bytes(cin[words * 4:], 'little'))
            rest ^= int((s[words + 4] ^ c3 ^ c2 ^ c1 ^ c0)) & ((1 << 8 * tail) - 1)
            ref[words * 4:] = rest.to_bytes(tail, 'little')
        got = _crypt_stream(key, cin)
        assert got == bytes(ref), (words, tail)
        assert _stream_word(s, words) == (
            s[words + 4] ^ c3 ^ c2 ^ c1 ^ c0), (words, tail)
    print('pes17crypt: stream ok')


if __name__ == '__main__':
    import os
    if sys.argv[1] == 'check':
        check()
    elif sys.argv[1] == 'dec':
        d, l, data = decrypt(open(sys.argv[2], 'rb').read())
        open(sys.argv[3], 'wb').write(data)
        print('description %r, logo %d B, data %d B' % (
            d.split(b'\0')[0][:60], len(l), len(data)))

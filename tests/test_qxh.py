"""The numpy QuickXorHash against a byte-by-byte reference implementation.

Agreement with OneDrive itself was checked on real files (2026-11-03: three files of the sample album,
including a 4 MB JPEG, matched the manifest's `quickxor`).
"""

import os

from omp.qxh import CHUNK, quickxorhash_bytes, quickxorhash_file


def reference(data: bytes) -> bytes:
    acc, mask = 0, (1 << 160) - 1
    for i, b in enumerate(data):
        s = (i * 11) % 160
        acc ^= ((b << s) | (b >> (160 - s))) & mask
    digest = bytearray(acc.to_bytes(20, "little"))
    for i, b in enumerate(len(data).to_bytes(8, "little")):
        digest[12 + i] ^= b
    return bytes(digest)


def test_empty():
    assert quickxorhash_bytes(b"") == bytes(20)


def test_matches_reference():
    for n in (1, 7, 159, 160, 161, 1000, 4099):
        data = os.urandom(n)
        assert quickxorhash_bytes(data) == reference(data), n


def test_file_chunks(tmp_path):
    data = os.urandom(CHUNK + 12345)  # crosses a chunk boundary
    p = tmp_path / "x.bin"
    p.write_bytes(data)
    assert quickxorhash_file(p) == quickxorhash_bytes(data)

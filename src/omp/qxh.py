"""QuickXorHash, the content hash OneDrive reports for every file.

Each byte at offset i is XORed into a 160-bit circular register at bit (i * 11) mod 160. Offsets that
are equal mod 160 land on the same bit position, so the bytes are first XOR-folded into 160 columns
with numpy, then placed into the register once. The file length is XORed into the last 8 bytes.
"""

import base64
from pathlib import Path

import numpy as np

WIDTH = 160
SHIFT = 11
_MASK = (1 << WIDTH) - 1
CHUNK = WIDTH * 65536  # a multiple of 160, so every chunk starts at column 0


def _fold(buf: bytes, columns: np.ndarray) -> None:
    data = np.frombuffer(buf, dtype=np.uint8)
    full = len(data) - len(data) % WIDTH
    if full:
        columns ^= np.bitwise_xor.reduce(data[:full].reshape(-1, WIDTH), axis=0)
    if full < len(data):
        columns[: len(data) - full] ^= data[full:]


def _finish(columns: np.ndarray, length: int) -> bytes:
    acc = 0
    for j, value in enumerate(columns.tolist()):
        if value:
            s = (j * SHIFT) % WIDTH
            acc ^= ((value << s) | (value >> (WIDTH - s))) & _MASK
    digest = bytearray(acc.to_bytes(WIDTH // 8, "little"))
    for i, b in enumerate(length.to_bytes(8, "little")):
        digest[WIDTH // 8 - 8 + i] ^= b
    return bytes(digest)


def quickxorhash_bytes(data: bytes) -> bytes:
    columns = np.zeros(WIDTH, dtype=np.uint8)
    _fold(data, columns)
    return _finish(columns, len(data))


def quickxorhash_file(path: Path) -> bytes:
    columns = np.zeros(WIDTH, dtype=np.uint8)
    length = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            _fold(chunk, columns)
            length += len(chunk)
    return _finish(columns, length)


def to_b64(digest: bytes) -> str:
    """The form the Graph API returns. rclone's lsjson prints hex (`Hashes.quickxor`), our IDs too."""
    return base64.b64encode(digest).decode()


def from_b64(text: str) -> bytes:
    return base64.b64decode(text)

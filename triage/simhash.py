"""Charikar 64-bit SimHash over shingles. Stdlib only."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Iterable

SIMHASH_MAX_HAMMING = 3
SIMHASH_BITS = 64


def _feature_hash(feature: str) -> int:
    """Map a feature string to a stable 64-bit int."""
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def simhash(shingles: Iterable[str], weighted: bool = True) -> int:
    """
    64-bit Charikar SimHash.
    If weighted=True, weight by TF (count); else presence (weight=1).
    """
    counts = Counter(shingles)
    if not counts:
        return 0
    acc = [0] * SIMHASH_BITS
    for feat, cnt in counts.items():
        h = _feature_hash(feat)
        w = cnt if weighted else 1
        for bit in range(SIMHASH_BITS):
            if (h >> bit) & 1:
                acc[bit] += w
            else:
                acc[bit] -= w
    result = 0
    for bit in range(SIMHASH_BITS):
        if acc[bit] > 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit (or smaller) integers."""
    return (a ^ b).bit_count()


def simhash_hex(value: int) -> str:
    return f"{value:016x}"


def simhash_from_hex(hex_str: str) -> int:
    return int(hex_str, 16)

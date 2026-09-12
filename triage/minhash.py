"""MinHash + LSH (Broder) for near-duplicate candidate generation. Stdlib only."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from triage.models import PullRequest

_HUNK_RE = re.compile(r"^@@[^@]+@@", re.MULTILINE)


def _extract_hunk_headers(patch: str) -> list[str]:
    if not patch:
        return []
    return [m.group(0).strip() for m in _HUNK_RE.finditer(patch)]

_TOKEN_RE = re.compile(r"[a-z0-9_/.\-]+", re.IGNORECASE)

DEFAULT_NUM_PERM = 128
DEFAULT_NUM_BANDS = 16  # 16 bands × 8 rows for 128 perms
DEFAULT_ROWS_PER_BAND = 8


def _stable_u32(seed: int, shingle: str) -> int:
    """Deterministic 32-bit hash from seed + shingle (no numpy)."""
    h = hashlib.sha256(f"{seed}:{shingle}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def shingles_for_pr(pr: PullRequest, n: int = 3) -> set[str]:
    """
    Character 3-grams of text_for_embed PLUS explicit path tokens and hunk headers.
    """
    text = pr.text_for_embed.lower()
    cleaned = " ".join(_TOKEN_RE.findall(text))
    out: set[str] = set()
    if len(cleaned) < n:
        if cleaned:
            out.add(f"c:{cleaned}")
    else:
        for i in range(len(cleaned) - n + 1):
            out.add(f"c:{cleaned[i : i + n]}")

    # Explicit path tokens (whole path + path segments)
    for path in pr.paths:
        pl = path.lower()
        out.add(f"p:{pl}")
        for seg in pl.replace("\\", "/").split("/"):
            if seg:
                out.add(f"p:{seg}")

    # Hunk headers from patches
    for cf in pr.changed_files:
        for hunk in _extract_hunk_headers(cf.patch):
            out.add(f"h:{hunk.lower()}")

    return out


def shingles_from_text(
    text: str,
    paths: Iterable[str] | None = None,
    hunks: Iterable[str] | None = None,
    n: int = 3,
) -> set[str]:
    """Build shingles from raw text (+ optional paths/hunks) for SimHash/tests."""
    cleaned = " ".join(_TOKEN_RE.findall(text.lower()))
    out: set[str] = set()
    if len(cleaned) < n:
        if cleaned:
            out.add(f"c:{cleaned}")
    else:
        for i in range(len(cleaned) - n + 1):
            out.add(f"c:{cleaned[i : i + n]}")
    for path in paths or []:
        pl = path.lower()
        out.add(f"p:{pl}")
        for seg in pl.replace("\\", "/").split("/"):
            if seg:
                out.add(f"p:{seg}")
    for hunk in hunks or []:
        out.add(f"h:{hunk.lower()}")
    return out


def minhash_signature(
    shingles: set[str],
    num_perm: int = DEFAULT_NUM_PERM,
) -> tuple[int, ...]:
    """
    MinHash signature: for each of num_perm permutations, min hash of shingles.
    Deterministic via seeded hashlib; no numpy.
    """
    if not shingles:
        return tuple(0 for _ in range(num_perm))
    sig: list[int] = []
    for seed in range(num_perm):
        m = min(_stable_u32(seed, s) for s in shingles)
        sig.append(m)
    return tuple(sig)


def estimated_jaccard(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    """Estimate Jaccard similarity from MinHash signatures."""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return 0.0
    matches = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    return matches / float(len(sig_a))


def lsh_bands(
    signature: tuple[int, ...],
    num_bands: int = DEFAULT_NUM_BANDS,
    rows_per_band: int = DEFAULT_ROWS_PER_BAND,
) -> list[tuple[int, tuple[int, ...]]]:
    """
    Split signature into bands. Returns list of (band_index, band_tuple).
    For 128 perms: 16×8; for 64: 8×8.
    """
    expected = num_bands * rows_per_band
    if len(signature) < expected:
        # Pad with zeros if short
        signature = signature + tuple(0 for _ in range(expected - len(signature)))
    bands: list[tuple[int, tuple[int, ...]]] = []
    for b in range(num_bands):
        start = b * rows_per_band
        band = tuple(signature[start : start + rows_per_band])
        bands.append((b, band))
    return bands


def lsh_candidate_pairs(
    signatures: dict[int, tuple[int, ...]],
    num_bands: int = DEFAULT_NUM_BANDS,
    rows_per_band: int = DEFAULT_ROWS_PER_BAND,
) -> set[tuple[int, int]]:
    """
    Emit candidate pairs that share at least one LSH band.
    Keys of signatures are opaque ids (e.g. PR indices).
    """
    buckets: dict[tuple[int, tuple[int, ...]], list[int]] = {}
    for idx, sig in signatures.items():
        for band_key in lsh_bands(sig, num_bands=num_bands, rows_per_band=rows_per_band):
            buckets.setdefault(band_key, []).append(idx)

    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(set(members))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pairs.add((a, b) if a < b else (b, a))
    return pairs

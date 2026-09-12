"""Character n-gram TF-IDF + cosine similarity. No torch, no API keys."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def char_ngrams(text: str, n: int = 3) -> list[str]:
    """Lowercased character n-grams over alphanumeric+space collapsed text."""
    cleaned = " ".join(_TOKEN_RE.findall(text.lower()))
    if len(cleaned) < n:
        return [cleaned] if cleaned else []
    return [cleaned[i : i + n] for i in range(len(cleaned) - n + 1)]


def build_vocabulary(documents: Iterable[str], n: int = 3) -> dict[str, int]:
    vocab: dict[str, int] = {}
    for doc in documents:
        for gram in set(char_ngrams(doc, n=n)):
            if gram not in vocab:
                vocab[gram] = len(vocab)
    return vocab


def _tf(grams: list[str]) -> dict[str, float]:
    counts = Counter(grams)
    total = float(sum(counts.values())) or 1.0
    return {g: c / total for g, c in counts.items()}


def compute_idf(documents: list[str], vocab: dict[str, int], n: int = 3) -> list[float]:
    n_docs = len(documents) or 1
    df = [0] * len(vocab)
    for doc in documents:
        seen = set(char_ngrams(doc, n=n))
        for gram in seen:
            idx = vocab.get(gram)
            if idx is not None:
                df[idx] += 1
    return [math.log((1 + n_docs) / (1 + d)) + 1.0 for d in df]


def vectorize(
    text: str,
    vocab: dict[str, int],
    idf: list[float],
    n: int = 3,
) -> list[float]:
    grams = char_ngrams(text, n=n)
    tf = _tf(grams)
    vec = [0.0] * len(vocab)
    for gram, weight in tf.items():
        idx = vocab.get(gram)
        if idx is not None:
            vec[idx] = weight * idf[idx]
    return _l2_normalize(vec)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def mean_centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, val in enumerate(v):
            acc[i] += val
    n = float(len(vectors))
    return _l2_normalize([x / n for x in acc])


class Embedder:
    """Fit TF-IDF over a corpus; transform new texts deterministically."""

    def __init__(self, n: int = 3) -> None:
        self.n = n
        self.vocab: dict[str, int] = {}
        self.idf: list[float] = []

    def fit(self, documents: list[str]) -> Embedder:
        self.vocab = build_vocabulary(documents, n=self.n)
        self.idf = compute_idf(documents, self.vocab, n=self.n)
        return self

    def transform(self, text: str) -> list[float]:
        if not self.vocab:
            return []
        return vectorize(text, self.vocab, self.idf, n=self.n)

    def fit_transform(self, documents: list[str]) -> list[list[float]]:
        self.fit(documents)
        return [self.transform(d) for d in documents]

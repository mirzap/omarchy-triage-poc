"""Classic disjoint-set (union-find) with path compression and union by rank."""

from __future__ import annotations

from typing import Hashable, Iterable


class UnionFind:
    """Disjoint-set forest for clustering components."""

    def __init__(self) -> None:
        self._parent: dict[Hashable, Hashable] = {}
        self._rank: dict[Hashable, int] = {}

    def add(self, x: Hashable) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0

    def find(self, x: Hashable) -> Hashable:
        if x not in self._parent:
            self.add(x)
        # Path compression
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: Hashable, b: Hashable) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Union by rank
        if self._rank[ra] < self._rank[rb]:
            self._parent[ra] = rb
        elif self._rank[ra] > self._rank[rb]:
            self._parent[rb] = ra
        else:
            self._parent[rb] = ra
            self._rank[ra] += 1

    def components(self) -> list[list[Hashable]]:
        """Return connected components as lists of members (stable by first-seen order)."""
        buckets: dict[Hashable, list[Hashable]] = {}
        order: list[Hashable] = []
        for x in self._parent:
            root = self.find(x)
            if root not in buckets:
                buckets[root] = []
                order.append(root)
            buckets[root].append(x)
        return [buckets[r] for r in order]

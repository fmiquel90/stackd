"""Capped exponential backoff with jitter, for the agent's retry loops (registration, claim): a
fleet of workers that lost the API together does not come back in lockstep, nor wait a fixed
interval once it is back."""

from __future__ import annotations

import random


class Backoff:
    def __init__(self, base: float = 1.0, cap: float = 60.0) -> None:
        self._base = base
        self._cap = cap
        self._failures = 0

    def next(self) -> float:
        """The delay before the next try: half the capped exponential step, plus a random half."""
        step = min(self._cap, self._base * 2 ** min(self._failures, 32))
        self._failures += 1
        return step / 2 + random.uniform(0, step / 2)

    def reset(self) -> None:
        self._failures = 0

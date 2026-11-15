"""A small circuit breaker: after repeated failures a service route is skipped for a bounded cooldown.

It exists so that an unattended run does not spend its whole time budget retrying a route that is known to be down, and so
that the failure state is explicit (``CIRCUIT_OPEN``) rather than an endless loop. It never converts a failure into a result.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class CircuitBreaker:
    """closed -> (``threshold`` consecutive failures) -> open for ``cooldown_s`` -> half-open (one trial) -> closed on success."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 120.0, clock: Callable[[], float] = time.monotonic) -> None:
        if threshold < 1 or cooldown_s < 0:
            raise ValueError("threshold must be >= 1 and cooldown_s >= 0")
        self.threshold, self.cooldown_s, self.clock = threshold, cooldown_s, clock
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.clock() - self.opened_at >= self.cooldown_s:
            return True  # half-open: allow one trial; a failure re-opens immediately because failures stays at threshold
        return False

    def retry_in_s(self) -> float:
        return 0.0 if self.opened_at is None else max(0.0, self.cooldown_s - (self.clock() - self.opened_at))

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if self.allow() else "OPEN"

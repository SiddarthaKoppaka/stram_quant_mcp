"""Bounded per-caller throttle that stops a caller who keeps being denied."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable


class DenialThrottle:
    """Blocks a caller after too many denials inside a sliding window.

    Every denied attempt is recorded, including attempts made while blocked, so a caller that keeps
    probing stays blocked. Memory is bounded: at most ``max_principals`` callers are tracked and the
    least recently denied caller is forgotten first, which can never lock out anyone else.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        max_principals: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a throttle.

        Args:
            limit: Denials inside the window that trigger a block.
            window_seconds: Length of the sliding window.
            max_principals: Maximum number of callers tracked at once.
            clock: Monotonic time source, replaceable in tests.
        """
        self._limit = limit
        self._window = window_seconds
        self._max_principals = max_principals
        self._clock = clock
        self._denials: OrderedDict[str, deque[float]] = OrderedDict()

    def is_blocked(self, principal: str) -> bool:
        """Return whether ``principal`` has reached the limit inside the window."""
        denials = self._denials.get(principal)
        if denials is None:
            return False
        return len(denials) >= self._limit and denials[0] > self._clock() - self._window

    def record_denial(self, principal: str) -> bool:
        """Record one denied attempt and return whether the caller is now blocked."""
        denials = self._denials.get(principal)
        if denials is None:
            if len(self._denials) >= self._max_principals:
                self._denials.popitem(last=False)
            denials = deque(maxlen=self._limit)
            self._denials[principal] = denials
        self._denials.move_to_end(principal)
        denials.append(self._clock())
        return self.is_blocked(principal)

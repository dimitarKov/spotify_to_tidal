import asyncio
import time
from typing import Awaitable, Callable


class RateLimiter:
    """ Token bucket rate limiter for asyncio tasks.
        Allows bursts of up to `capacity` calls, then a sustained `rate` calls per second.
        Unused tokens accumulate only up to `capacity`, so a quiet period never allows a bigger burst """

    _EPSILON = 1e-9

    def __init__(self, rate: float, capacity: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be positive and capacity at least 1")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._last_refill = clock()

    def _refill(self):
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last_refill) * self.rate)
        self._last_refill = now

    async def acquire(self):
        """ Waits until a call is allowed. Safe to call from many tasks on one event loop:
            there is no await between checking and taking a token """
        while True:
            self._refill()
            # tolerance for float rounding: after sleeping exactly long enough, tokens can come out as 0.9999...
            if self._tokens >= 1 - self._EPSILON:
                self._tokens = max(0.0, self._tokens - 1)
                return
            await self._sleep((1 - self._tokens) / self.rate)

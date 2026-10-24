# tests/unit/test_rate_limiter.py

import asyncio
import pytest
from typing import List

from spotify_to_tidal.rate_limiter import RateLimiter


class FakeClock:
    """ Time only advances when the limiter sleeps (or the test says so) """

    def __init__(self):
        self.now = 0.0
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(rate: float, capacity: int) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(rate=rate, capacity=capacity, clock=clock, sleep=clock.sleep), clock


def acquire_times(limiter: RateLimiter, clock: FakeClock, count: int) -> List[float]:
    """ Times at which each of `count` concurrent tasks was allowed through """
    times: List[float] = []

    async def task():
        await limiter.acquire()
        times.append(clock.now)

    async def main():
        await asyncio.gather(*[task() for _ in range(count)])

    asyncio.run(main())
    return sorted(times)


def test_allows_a_burst_up_to_capacity():
    limiter, clock = make_limiter(rate=2, capacity=3)
    assert acquire_times(limiter, clock, 3) == [0, 0, 0]
    assert clock.sleeps == []


def test_sustained_rate_after_the_burst():
    limiter, clock = make_limiter(rate=2, capacity=3)
    times = acquire_times(limiter, clock, 7)
    assert times[:3] == [0, 0, 0]
    # then one call every 1/rate seconds
    assert all(abs(actual - expected) < 1e-6 for actual, expected in zip(times[3:], [0.5, 1.0, 1.5, 2.0]))
    assert len(times) == 7


def test_idle_time_does_not_allow_a_bigger_burst():
    limiter, clock = make_limiter(rate=10, capacity=3)
    acquire_times(limiter, clock, 3)
    clock.now += 1000  # long quiet period: would be 10000 tokens without a cap
    start = clock.now
    times = acquire_times(limiter, clock, 6)
    assert [t - start for t in times[:3]] == [0, 0, 0]
    assert all(t > start for t in times[3:])


@pytest.mark.parametrize("rate, capacity", [(0, 1), (-1, 1), (1, 0)])
def test_rejects_invalid_settings(rate: float, capacity: int):
    with pytest.raises(ValueError):
        RateLimiter(rate=rate, capacity=capacity)

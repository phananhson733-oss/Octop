"""Pacing and 429 backoff, ported from OpenSEO crawl-throttle.test.ts.

Upstream persists the throttle across workflow chunks; here one in-memory throttle
lives for the whole crawl, so the chunk-boundary cases have no counterpart.
"""

from collections.abc import Callable

import pytest

from octop.infra.seo.audit.throttle import CrawlThrottle, parse_retry_after


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: Callable[[], None] | None = None

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            hook, self.on_sleep = self.on_sleep, None
            hook()


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


def _throttle(fake: FakeTime) -> CrawlThrottle:
    return CrawlThrottle(fake.clock, fake.sleep)


async def _starts(fake: FakeTime, throttle: CrawlThrottle, count: int) -> list[float]:
    starts = []
    for _ in range(count):
        if await throttle.ready():
            starts.append(fake.now)
    return starts


async def test_spaces_request_starts_one_second_apart(fake: FakeTime) -> None:
    assert await _starts(fake, _throttle(fake), 5) == [0, 1, 2, 3, 4]


async def test_first_429_pauses_thirty_seconds_and_spaces_retries(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    assert await throttle.ready()
    assert throttle.backoff(1, None)
    assert await _starts(fake, throttle, 3) == [30, 32, 34]


async def test_rechecks_a_cooldown_extended_while_waiting(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    throttle.backoff(1, "5")

    def extend() -> None:
        fake.now = 4
        throttle.backoff(1, "10")

    fake.on_sleep = extend
    assert await _starts(fake, throttle, 1) == [14]


@pytest.mark.parametrize("header", ["60", "Thu, 01 Jan 1970 00:01:00 GMT"])
def test_parses_retry_after(header: str) -> None:
    assert parse_retry_after(header, now=0) == 60


async def test_honors_retry_after_seconds(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    throttle.backoff(1, "60")
    assert await _starts(fake, throttle, 1) == [60]


async def test_keeps_oversized_retry_after_finite_and_stops(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    assert not throttle.backoff(1, "9" * 310)
    assert throttle.stopped
    assert throttle.paused_until < float("inf")
    assert not await throttle.ready()


async def test_backs_off_and_stops_after_four_consecutive_refusals(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    for index, delay in enumerate([30, 60, 120, 240]):
        assert throttle.backoff(index + 1 if index < 3 else 1, None) is (index < 3)
        assert throttle.paused_until - fake.now == delay
        fake.now += delay
    assert throttle.stopped
    assert not await throttle.ready()


async def test_retains_slower_pacing_after_recovery_and_caps_it(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    for _ in range(8):
        throttle.backoff(1, "1")
        fake.now += 30
        throttle.recovered()
    assert throttle.interval == 30
    assert not throttle.stopped


async def test_stops_when_cooldowns_exceed_thirty_minutes(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    for _ in range(3):
        assert throttle.backoff(1, "600")
        fake.now += 600
        throttle.recovered()
    assert not throttle.backoff(1, "600")
    assert not await throttle.ready()


def test_a_url_is_retried_at_most_three_times(fake: FakeTime) -> None:
    throttle = _throttle(fake)
    assert throttle.backoff(3, "1")
    throttle.recovered()
    assert not throttle.backoff(4, "1")

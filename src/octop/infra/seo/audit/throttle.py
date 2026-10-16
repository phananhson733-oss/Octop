"""Per-audit request pacing and 429 backoff, ported from OpenSEO crawl-throttle.ts.

One request start per interval (1 s to begin with). Every 429 doubles the
interval (capped at 30 s) and pauses the site for Retry-After, or 30 s doubling
per consecutive refusal. A URL is retried up to three times; four refusals in a
row, or more than 30 minutes of total cooldown, stop the crawl. The state lives
in memory and starts fresh when an audit resumes after a restart.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime

MAX_RETRIES = 3
FIRST_DELAY_S = 30.0
MAX_INTERVAL_S = 30.0
MAX_COOLDOWN_S = 30 * 60.0


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from ``Retry-After``: delay-seconds or an HTTP-date."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        # Keep over-budget values finite.
        return min(float(text), MAX_COOLDOWN_S + 1)
    try:
        at = parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, at - (time.time() if now is None else now))


class CrawlThrottle:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self.interval = 1.0
        self.next_request_at = 0.0
        self.paused_until = 0.0
        self.consecutive_rate_limits = 0
        self.cooldown = 0.0

    @property
    def stopped(self) -> bool:
        return self.consecutive_rate_limits > MAX_RETRIES or self.cooldown > MAX_COOLDOWN_S

    async def ready(self) -> bool:
        """Wait for the next request slot; False once the site has stopped the crawl."""
        while not self.stopped:
            now = self._clock()
            ready_at = max(self.paused_until, self.next_request_at)
            if ready_at <= now:
                self.next_request_at = now + self.interval
                return True
            await self._sleep(ready_at - now)
        return False

    def backoff(self, attempt: int, retry_after: str | None) -> bool:
        """Pause the site on a 429; True when this URL may be retried."""
        self.consecutive_rate_limits += 1
        self.interval = min(MAX_INTERVAL_S, self.interval * 2)
        requested = parse_retry_after(retry_after)
        delay = max(
            self.interval,
            requested
            if requested is not None
            else FIRST_DELAY_S * 2 ** (self.consecutive_rate_limits - 1),
        )
        now = self._clock()
        paused_until = max(self.paused_until, now + delay)
        self.cooldown += paused_until - max(now, self.paused_until)
        self.paused_until = paused_until
        return not self.stopped and attempt <= MAX_RETRIES

    def recovered(self) -> None:
        """A non-429 answer ends a run of consecutive refusals; the slower pace stays."""
        self.consecutive_rate_limits = 0

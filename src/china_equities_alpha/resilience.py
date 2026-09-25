"""Rate limiting, retries with backoff, and per-job failure isolation."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)
T = TypeVar("T")


class RateLimiter:
    """Enforce a minimum interval between calls (per source)."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class PermanentError(Exception):
    """An error that retrying won't fix (e.g. endpoint removed, access blocked)."""


def with_retries(fn: Callable[..., T], *, attempts: int = 4, max_wait: float = 30.0,
                 on_retry: Callable[[], None] | None = None) -> Callable[..., T]:
    def _before_sleep(state):
        before_sleep_log(log, logging.WARNING)(state)
        if on_retry:
            on_retry()

    return retry(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=max_wait),
        retry=retry_if_not_exception_type(PermanentError),
        before_sleep=_before_sleep,
    )(fn)


@dataclass
class JobResult:
    job: str
    status: str = "ok"  # ok | partial | failed | skipped
    rows_fetched: int = 0
    rows_inserted: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def fail(self, key: str, exc: BaseException) -> None:
        self.failures.append({"key": key, "error": f"{type(exc).__name__}: {exc}"})


def run_isolated(name: str, fn: Callable[[], JobResult]) -> JobResult:
    """Run one job; an exception is logged and returned as a failed result, never raised."""
    t0 = time.monotonic()
    try:
        result = fn()
        if result.failures and result.status == "ok":
            result.status = "partial"
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        log.error("job %s failed: %s\n%s", name, exc, traceback.format_exc())
        result = JobResult(job=name, status="failed")
        result.fail("<job>", exc)
    result.seconds = round(time.monotonic() - t0, 1)
    return result

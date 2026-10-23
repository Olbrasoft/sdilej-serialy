"""Bounded, opt-in retries with diagnostics that never expose request data."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from typing import Callable

import requests


TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 120


def retry_delay(response: requests.Response | None, attempt: int) -> float | None:
    delay = 5 * 2 ** (attempt - 1)
    if response is None:
        return delay
    value = response.headers.get("Retry-After", "")
    if value:
        try:
            seconds = int(value) if value.isdigit() else (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
            # Do not retry earlier than requested by the server. A longer
            # cooldown belongs to a later scheduled run, not this batch.
            if seconds > MAX_RETRY_DELAY:
                return None
            delay = max(delay, seconds)
        except (TypeError, ValueError, OverflowError):
            pass
    return delay


def log_failure(phase: str, error: requests.RequestException) -> None:
    """Phase must be a fixed label from code, never a URL or exception text."""
    response = error.response
    status = response.status_code if response is not None else "none"
    print(f"http_failure phase={phase} status={status} error={type(error).__name__}", flush=True)


def request(operation: Callable[[], requests.Response], *, phase: str,
            retry_safe: bool = False) -> requests.Response:
    """Retry only explicitly approved reads; preserve caller status handling.

    Some site mutations use GET, so the HTTP verb alone is not sufficient.
    Login POSTs and subtitle writes must always keep retry_safe=False.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = None
        failure = None
        try:
            response = operation()
        except requests.RequestException as error:
            failure = error
            response = error.response
        status = response.status_code if response is not None else None
        if failure is None and (not isinstance(status, int) or status < 300):
            return response
        transient = status in TRANSIENT_STATUSES if status is not None else (
            isinstance(failure, (requests.Timeout, requests.ConnectionError))
            and not isinstance(failure, requests.exceptions.SSLError)
        )
        delay = retry_delay(response, attempt) if retry_safe and transient and attempt < MAX_ATTEMPTS else None
        print(
            f"http_failure phase={phase} status={status if status is not None else 'none'} "
            f"error={type(failure).__name__ if failure is not None else 'HTTPStatus'} "
            f"attempt={attempt}/{MAX_ATTEMPTS if retry_safe else 1} "
            f"retry_in={delay if delay is not None else 'none'}",
            flush=True,
        )
        if delay is None:
            if failure is not None:
                raise failure
            return response
        if response is not None:
            response.close()
        time.sleep(delay)
    raise AssertionError("Unreachable retry state")

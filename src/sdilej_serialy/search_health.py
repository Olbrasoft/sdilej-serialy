"""Distinguish an empty query from a degraded authenticated search session."""
import time
import math
import threading
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests
from sdilej_to_prehrajto.sdilej import BASE_URL, SdilejError, parse_search_html

CONTROL_QUERY = 'Teorie velkého třesku'
CONTROL_URL = BASE_URL + '/teorie-velkeho-tresku/s/-6'
FALLBACK_QUERY = 'Doktor Martin'
FALLBACK_URL = BASE_URL + '/doktor-martin/s/-6'


def retry_seconds(remaining):
    # Adding/subtracting a large monotonic timestamp can produce 300.0000000005.
    # Do not turn that rounding noise into a spurious additional retry second.
    return max(0, math.ceil(round(remaining, 6)))


class SearchUnavailable(SdilejError):
    """The shared search backend has not passed its positive control."""


class SearchCircuit:
    """One quiet recovery window shared by every discovery session."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, wall_clock=time.time,
                 restored=None, max_failed_cycles=0):
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock
        self.lock = threading.Lock()
        self.probe_lock = threading.Lock()
        self.healthy_until = 0
        self.probe_count = 0
        self.failures = max(0, int((restored or {}).get('consecutive_failures', 0)))
        self.request_gap_seconds = min(8, max(2, float((restored or {}).get(
            'request_gap_seconds', 2 * 2 ** min(self.failures, 2)))))
        self.stable_since = None
        self.retry_at = 0
        self.verified = False
        self.controls = {}
        self.max_failed_cycles = max_failed_cycles
        self.failed_cycles_this_run = 0
        resume = (restored or {}).get('resume_after')
        if resume:
            remaining = datetime.fromisoformat(resume).timestamp() - self.wall_clock()
            if remaining > 0:
                self.retry_at = self.clock() + min(remaining, 900)

    def wait(self):
        waited = False
        while True:
            with self.lock:
                if self._restart_required():
                    raise SearchUnavailable('Source worker must yield after repeated failed recovery')
                delay = self.retry_at - self.clock()
            if delay <= 0:
                return waited
            # Release the shared lock while waiting so peers and checkpoints
            # can observe the same pause. No source request starts here.
            self.sleep(min(delay, 30))
            waited = True

    def failure(self):
        with self.lock:
            now = self.clock()
            if now >= self.retry_at:
                self.failures += 1
                self.failed_cycles_this_run += 1
                self.retry_at = now + min(300 * 2 ** min(self.failures - 1, 2), 900)
                self.request_gap_seconds = min(8, self.request_gap_seconds * 2)
            self.verified = False
            self.healthy_until = 0
            self.stable_since = None
            return retry_seconds(self.retry_at - now)

    def success(self):
        with self.lock:
            # A positive request already in flight cannot cancel a quiet
            # period opened by another worker's failed control.
            if self.clock() < self.retry_at:
                return
            self.failures = self.retry_at = 0
            self.failed_cycles_this_run = 0
            self.verified = True
            self.healthy_until = self.clock() + 60
            if self.stable_since is None:
                self.stable_since = self.clock()

    def _request_gap(self):
        # After a real outage, resume gently rather than immediately recreate
        # the same request pressure. Healthy operation gradually restores the
        # normal rate; one successful probe alone is not sustained recovery.
        if (self.verified and self.stable_since is not None
                and self.clock() - self.stable_since >= 600):
            self.request_gap_seconds = max(2, self.request_gap_seconds / 2)
            self.stable_since = self.clock()
        return self.request_gap_seconds

    def request_gap(self):
        with self.lock:
            return self._request_gap()

    def _restart_required(self):
        return bool(self.max_failed_cycles and self.failed_cycles_this_run >= self.max_failed_cycles)

    def restart_required(self):
        with self.lock:
            return self._restart_required()

    def record_control(self, url, response, count):
        with self.lock:
            self.probe_count += 1
            response_url = urlsplit(getattr(response, 'url', url))
            self.controls[url.removeprefix(BASE_URL)] = dict(
                status=getattr(response, 'status_code', None),
                response_bytes=len(response.text.encode('utf-8')), parsed_candidates=count,
                response_path=response_url.path if response_url.netloc == urlsplit(BASE_URL).netloc else None,
                unexpected_origin=response_url.netloc != urlsplit(BASE_URL).netloc,
                checked_at=datetime.fromtimestamp(self.wall_clock(), UTC).isoformat())

    def record_control_error(self, url, error):
        # Keep diagnostic types/status only, never authenticated URLs or the
        # exception message. _get wraps the original requests exception.
        kinds, statuses, seen = [], [], set()
        while error is not None and id(error) not in seen and len(kinds) < 8:
            seen.add(id(error))
            kinds.append(type(error).__name__)
            status = getattr(getattr(error, 'response', None), 'status_code', None)
            if isinstance(status, int):
                statuses.append(status)
            error = error.__cause__ or error.__context__
        with self.lock:
            self.probe_count += 1
            self.controls[url.removeprefix(BASE_URL)] = dict(
                status=statuses[-1] if statuses else None, error_types=kinds,
                response_bytes=None, parsed_candidates=0,
                checked_at=datetime.fromtimestamp(self.wall_clock(), UTC).isoformat())
        return statuses

    def snapshot(self):
        with self.lock:
            remaining = max(0, self.retry_at - self.clock())
            return dict(status='paused' if remaining else 'probe_due' if self.failures
                        else 'healthy' if self.verified else 'unverified',
                consecutive_failures=self.failures, retry_seconds=retry_seconds(remaining),
                failed_cycles_this_run=self.failed_cycles_this_run,
                control_requests=self.probe_count,
                request_gap_seconds=self._request_gap(),
                restart_required=self._restart_required(),
                resume_after=(datetime.fromtimestamp(self.wall_clock() + remaining, UTC).isoformat()
                              if remaining else None),
                controls=[dict(path=path, **value) for path,value in self.controls.items()])


class SearchHealth:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, circuit=None):
        self.clock, self.sleep = clock, sleep
        self.circuit = circuit if circuit is not None else SearchCircuit(clock=clock, sleep=sleep)
        self.healthy_until = 0
        self.last_refresh = None

    def wait(self):
        self.circuit.wait()

    def control(self, provider):
        previous = getattr(provider, '_fresh_search', False)
        provider._fresh_search = True
        try:
            for url,query in ((CONTROL_URL,CONTROL_QUERY),(FALLBACK_URL,FALLBACK_QUERY)):
                try:
                    response = provider._get(url)
                except (SdilejError, requests.RequestException) as error:
                    statuses = self.circuit.record_control_error(url, error)
                    if 429 in statuses:
                        # Explicit throttling starts the shared quiet window;
                        # do not send an alternative probe or another login.
                        raise
                    # A failed request alone does not establish a backend-wide
                    # outage. Use the existing second control before pausing
                    # every worker or renewing the authenticated session.
                    continue
                count = len(parse_search_html(response.text, query=query))
                self.circuit.record_control(url, response, count)
                if count:
                    return True
            return False
        finally:
            provider._fresh_search = previous

    def verify_empty(self, provider):
        """Return True only if a renewed session requires replaying the query."""
        # Only one worker diagnoses or renews the shared backend at a time.
        # Peers reuse the fresh positive control instead of sending four copies.
        with self.circuit.probe_lock:
            return self._verify_empty(provider)

    def _verify_empty(self, provider):
        waited = self.circuit.wait()
        if self.clock() < max(self.healthy_until, self.circuit.healthy_until):
            return waited
        refreshed = False
        try:
            healthy = self.control(provider)
            if not healthy and (self.last_refresh is None or self.clock() - self.last_refresh >= 900):
                self.last_refresh = self.clock()
                self.circuit.wait()
                replacement = provider.session_factory()
                previous, provider.session = provider.session, replacement
                previous.close()
                if hasattr(provider, 'search_pages'):
                    provider.search_pages.clear()
                refreshed = True
                healthy = self.control(provider)
            if not healthy:
                raise SdilejError('Known-positive search control returned no sources')
        except (SdilejError, requests.RequestException) as error:
            self.healthy_until = 0
            delay = self.circuit.failure()
            print(f'source_search_unavailable retry_seconds={delay}', flush=True)
            raise SearchUnavailable('Source search health is unconfirmed') from error
        self.healthy_until = self.clock() + 60
        self.circuit.success()
        self.last_refresh = None
        if refreshed:
            print('source_search_session_renewed', flush=True)
        return refreshed or waited

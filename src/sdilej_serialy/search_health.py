"""Distinguish an empty query from a degraded authenticated search session."""
import time
import math
import threading
from datetime import UTC, datetime

import requests
from sdilej_to_prehrajto.sdilej import BASE_URL, SdilejError, parse_search_html

CONTROL_QUERY = 'Teorie velkého třesku'
CONTROL_URL = BASE_URL + '/teorie-velkeho-tresku/s/-6'
FALLBACK_QUERY = 'Doktor Martin'
FALLBACK_URL = BASE_URL + '/doktor-martin/s/-6'


class SearchUnavailable(SdilejError):
    """The shared search backend has not passed its positive control."""


class SearchCircuit:
    """One quiet recovery window shared by every discovery session."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, wall_clock=time.time,
                 restored=None):
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock
        self.lock = threading.Lock()
        self.failures = max(0, int((restored or {}).get('consecutive_failures', 0)))
        self.retry_at = 0
        self.verified = False
        self.controls = {}
        resume = (restored or {}).get('resume_after')
        if resume:
            remaining = datetime.fromisoformat(resume).timestamp() - self.wall_clock()
            if remaining > 0:
                self.retry_at = self.clock() + min(remaining, 900)

    def wait(self):
        waited = False
        while True:
            with self.lock:
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
                self.retry_at = now + min(300 * 2 ** min(self.failures - 1, 2), 900)
            self.verified = False
            return max(0, math.ceil(self.retry_at - now))

    def success(self):
        with self.lock:
            # A positive request already in flight cannot cancel a quiet
            # period opened by another worker's failed control.
            if self.clock() < self.retry_at:
                return
            self.failures = self.retry_at = 0
            self.verified = True

    def record_control(self, url, response, count):
        with self.lock:
            self.controls[url.removeprefix(BASE_URL)] = dict(
                status=getattr(response, 'status_code', None),
                response_bytes=len(response.text.encode('utf-8')), parsed_candidates=count,
                checked_at=datetime.fromtimestamp(self.wall_clock(), UTC).isoformat())

    def snapshot(self):
        with self.lock:
            remaining = max(0, self.retry_at - self.clock())
            return dict(status='paused' if remaining else 'probe_due' if self.failures
                        else 'healthy' if self.verified else 'unverified',
                consecutive_failures=self.failures, retry_seconds=math.ceil(remaining),
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
                response = provider._get(url)
                count = len(parse_search_html(response.text, query=query))
                self.circuit.record_control(url, response, count)
                if count:
                    return True
            return False
        finally:
            provider._fresh_search = previous

    def verify_empty(self, provider):
        """Return True only if a renewed session requires replaying the query."""
        waited = self.circuit.wait()
        if self.clock() < self.healthy_until:
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

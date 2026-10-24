"""Distinguish an empty query from a degraded authenticated search session."""
import time

import requests
from sdilej_to_prehrajto.sdilej import BASE_URL, SdilejError, parse_search_html

CONTROL_QUERY = 'Teorie velkého třesku'
CONTROL_URL = BASE_URL + '/teorie-velkeho-tresku/s/-6'


class SearchHealth:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.healthy_until = self.retry_at = 0
        self.last_refresh = None

    def wait(self):
        delay = self.retry_at - self.clock()
        if delay > 0:
            self.sleep(min(delay, 60))

    @staticmethod
    def control(provider):
        previous = getattr(provider, '_fresh_search', False)
        provider._fresh_search = True
        try:
            response = provider._get(CONTROL_URL)
            return bool(parse_search_html(response.text, query=CONTROL_QUERY))
        finally:
            provider._fresh_search = previous

    def verify_empty(self, provider):
        """Return True only if a renewed session requires replaying the query."""
        if self.clock() < self.healthy_until:
            return False
        refreshed = False
        try:
            healthy = self.control(provider)
            if not healthy and (self.last_refresh is None or self.clock() - self.last_refresh >= 900):
                self.last_refresh = self.clock()
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
            self.retry_at = self.clock() + 60
            print('source_search_unavailable retry_seconds=60', flush=True)
            raise SdilejError('Source search health is unconfirmed') from error
        self.healthy_until = self.clock() + 60
        self.retry_at = 0
        self.last_refresh = None
        if refreshed:
            print('source_search_session_renewed', flush=True)
        return refreshed

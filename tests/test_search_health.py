from types import SimpleNamespace

import pytest
import requests
from sdilej_to_prehrajto.sdilej import SdilejError

from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.search_health import CONTROL_URL, SearchHealth
from sdilej_serialy.source_audit import AuditProvider
from sdilej_serialy.source_cache import SourceCache
from test_episodes import episode

POSITIVE = '<div class="videobox"><a href="/123/source.mp4">Source</a></div>'
EMPTY = '<h1>Empty search</h1>'
QUERY_URL = 'https://sdilej.cz/series/s/-6'


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False
        self.headers, self.cookies = {}, {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(text=self.response(url), status_code=200, raise_for_status=lambda: None)

    def close(self):
        self.closed = True


def test_empty_old_session_is_renewed_and_original_query_replayed_without_cached_control():
    old, fresh = Session(lambda _: EMPTY), Session(lambda _: POSITIVE)
    p = AuditProvider(old, detector=object(), request_gap_seconds=0, session_factory=lambda: fresh)
    # Even an old in-memory empty page must not establish control health.
    p.search_pages[CONTROL_URL] = SimpleNamespace(text=EMPTY)
    page = p._search_page(QUERY_URL, 'Series')
    assert [r['source_id'] for r in page['candidates']] == ['123']
    assert old.closed and p.session is fresh
    assert old.calls == [QUERY_URL, CONTROL_URL]
    assert fresh.calls == [CONTROL_URL, QUERY_URL]


def test_genuine_empty_query_is_allowed_only_after_positive_control_and_probe_is_bounded():
    session = Session(lambda url: POSITIVE if url == CONTROL_URL else EMPTY)
    p = EpisodeSourceProvider(session, detector=object(), request_gap_seconds=0,
        session_factory=lambda: pytest.fail('Healthy search must not log in again'))
    assert not p._search_page(QUERY_URL, 'Series')['candidates']
    assert not p._search_page(QUERY_URL, 'Series')['candidates']
    assert session.calls.count(CONTROL_URL) == 1


def test_persistent_empty_control_is_transient_and_backs_off_without_repeated_logins(tmp_path):
    old, fresh = Session(lambda _: EMPTY), Session(lambda _: EMPTY)
    renewed, delays, now = [], [], [100]
    def factory():
        renewed.append(True)
        return fresh
    def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds
    cache = SourceCache(tmp_path / 'cache.json')
    p = EpisodeSourceProvider(old, detector=object(), request_gap_seconds=0,
                              session_factory=factory, cache=cache)
    p.search_health = SearchHealth(clock=lambda: now[0], sleep=sleep)
    assert p.discover(episode()) is None
    assert p.last_outcome == 'transient_search'
    assert len(renewed) == 1 and delays == [60]
    assert not cache.entries


def test_failed_reauthentication_keeps_old_client_and_never_logs_exception_text(capsys):
    old = Session(lambda _: EMPTY)
    def factory():
        raise requests.Timeout('private-password signed-url')
    p = EpisodeSourceProvider(old, detector=object(), request_gap_seconds=0, session_factory=factory)
    with pytest.raises(SdilejError):
        p._search_page(QUERY_URL, 'Series')
    assert p.session is old and not old.closed
    output = capsys.readouterr().out
    assert 'retry_seconds=60' in output and 'private-password' not in output and 'signed-url' not in output


def test_worker_copies_refresh_capability_but_uses_independent_cookies_and_health():
    original = Session(lambda _: POSITIVE)
    factory = lambda: Session(lambda _: POSITIVE)
    p = EpisodeSourceProvider(original, detector=object(), session_factory=factory)
    worker = p.fork_worker()
    try:
        assert worker.session_factory is factory
        assert worker.session is not p.session and worker.search_health is not p.search_health
    finally:
        worker.session.close()


def test_audit_provider_never_retains_empty_broad_search_html():
    session = Session(lambda _: EMPTY)
    p = AuditProvider(session, detector=object(), request_gap_seconds=0)
    p._get(QUERY_URL)
    p._get(QUERY_URL)
    assert session.calls == [QUERY_URL, QUERY_URL] and not p.search_pages

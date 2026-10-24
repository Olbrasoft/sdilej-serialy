from types import SimpleNamespace

import pytest
import requests
from sdilej_to_prehrajto.sdilej import SdilejError

from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.search_health import CONTROL_URL, FALLBACK_URL, SearchHealth, SearchCircuit
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
    assert old.calls == [QUERY_URL, CONTROL_URL, FALLBACK_URL]
    assert fresh.calls == [CONTROL_URL, QUERY_URL]


def test_genuine_empty_query_is_allowed_only_after_positive_control_and_probe_is_bounded():
    session = Session(lambda url: POSITIVE if url == CONTROL_URL else EMPTY)
    p = EpisodeSourceProvider(session, detector=object(), request_gap_seconds=0,
        session_factory=lambda: pytest.fail('Healthy search must not log in again'))
    assert not p._search_page(QUERY_URL, 'Series')['candidates']
    assert not p._search_page(QUERY_URL, 'Series')['candidates']
    assert session.calls.count(CONTROL_URL) == 1


def test_one_empty_control_series_cannot_block_a_healthy_search_backend():
    session=Session(lambda url: POSITIVE if url==FALLBACK_URL else EMPTY)
    p=EpisodeSourceProvider(session,detector=object(),request_gap_seconds=0,
        session_factory=lambda: pytest.fail('A healthy alternative control must prevent reauthentication'))
    assert not p._search_page(QUERY_URL,'Series')['candidates']
    report=p.search_health.circuit.snapshot()
    assert report['status']=='healthy'
    assert [r['parsed_candidates'] for r in report['controls']]==[0,1]
    assert session.calls==[QUERY_URL,CONTROL_URL,FALLBACK_URL]


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
    assert p.last_outcome == 'source_search_unavailable'
    assert len(renewed) == 1 and sum(delays) == 300 and max(delays) <= 30
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
    assert 'retry_seconds=300' in output and 'private-password' not in output and 'signed-url' not in output


def test_worker_copies_refresh_capability_but_uses_independent_cookies_and_health():
    original = Session(lambda _: POSITIVE)
    factory = lambda: Session(lambda _: POSITIVE)
    p = EpisodeSourceProvider(original, detector=object(), session_factory=factory)
    worker = p.fork_worker()
    try:
        assert worker.session_factory is factory
        assert worker.session is not p.session and worker.search_health is not p.search_health
        assert worker.search_health.circuit is p.search_health.circuit
    finally:
        worker.session.close()


def test_audit_provider_never_retains_empty_broad_search_html():
    session = Session(lambda _: EMPTY)
    p = AuditProvider(session, detector=object(), request_gap_seconds=0)
    p._get(QUERY_URL)
    p._get(QUERY_URL)
    assert session.calls == [QUERY_URL, QUERY_URL] and not p.search_pages


def test_workers_share_one_quiet_window_that_escalates_and_recovers():
    now, delays = [100], []
    def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds
    circuit = SearchCircuit(clock=lambda: now[0], sleep=sleep)
    assert [circuit.failure() for _ in range(4)] == [300]*4
    assert circuit.snapshot()['consecutive_failures'] == 1
    circuit.success()  # An earlier in-flight success cannot reopen requests.
    assert circuit.snapshot()['status'] == 'paused'
    first = EpisodeSourceProvider(Session(lambda _: EMPTY), detector=object(), session_factory=lambda: None,
                                  search_circuit=circuit)
    times = []
    second = EpisodeSourceProvider(Session(lambda _: times.append(now[0]) or POSITIVE),
        detector=object(), session_factory=lambda: None, request_gap_seconds=0,
        search_circuit=circuit)
    second._get(QUERY_URL)
    assert times == [400] and sum(delays) == 300
    assert first.search_health.circuit is second.search_health.circuit
    assert circuit.failure() == 600
    circuit.wait()
    assert circuit.failure() == 900
    circuit.wait()
    assert circuit.failure() == 900
    circuit.wait()
    circuit.success()
    assert circuit.snapshot()['status'] == 'healthy'
    assert circuit.failure() == 300


def test_shared_pause_forces_replay_of_an_empty_response_that_was_already_in_flight():
    now = [100]
    def sleep(seconds): now[0] += seconds
    circuit = SearchCircuit(clock=lambda: now[0], sleep=sleep)
    session = Session(lambda _: POSITIVE)
    p = EpisodeSourceProvider(session, detector=object(), session_factory=lambda: None,
                              search_circuit=circuit, request_gap_seconds=0)
    p.search_health = SearchHealth(clock=lambda: now[0], sleep=sleep, circuit=circuit)
    p.search_health.healthy_until = 160
    circuit.failure()
    assert p.search_health.verify_empty(p) is True
    assert now[0] == 400 and session.calls == [CONTROL_URL]


def test_search_pause_checkpoint_survives_job_restart_and_expires_by_wall_clock():
    now, wall = [100], [1800000000]
    circuit = SearchCircuit(clock=lambda: now[0], wall_clock=lambda: wall[0])
    circuit.failure()
    snapshot = circuit.snapshot()
    now[0], wall[0] = 9000, wall[0]+120
    resumed = SearchCircuit(clock=lambda: now[0], wall_clock=lambda: wall[0], restored=snapshot)
    assert resumed.snapshot()['retry_seconds'] == 180
    now[0] += 180
    assert resumed.failure() == 600
    wall[0] += 1000
    expired = SearchCircuit(clock=lambda: now[0], wall_clock=lambda: wall[0], restored=snapshot)
    assert expired.snapshot()['retry_seconds'] == 0


def test_publisher_checkpoints_shared_search_pause_without_changing_target_history(tmp_path, monkeypatch):
    import json
    from sdilej_serialy import replenish
    from test_replenish import setup
    directory, _, _ = setup(tmp_path, monkeypatch)
    target = (directory/'state.json').read_bytes()
    circuit = SearchCircuit()
    circuit.failure()
    provider = SimpleNamespace(discover=lambda _: None, search_health=SimpleNamespace(circuit=circuit))
    replenish.prepare(tmp_path,'test',provider,runtime_minutes=0)
    report = json.loads((tmp_path/'reports/reserve-preparation.json').read_text())
    assert report['search_health']['status'] == 'paused'
    assert report['search_health']['resume_after']
    assert (directory/'state.json').read_bytes() == target


def test_restored_pause_is_observed_before_source_login(monkeypatch):
    from sdilej_serialy import episodes
    now, calls = [100], []
    def sleep(seconds): now[0] += seconds
    circuit = SearchCircuit(clock=lambda: now[0], sleep=sleep)
    circuit.failure()
    monkeypatch.setattr(episodes,'login_with_retry',lambda *a: calls.append(now[0]) or Session(lambda _: POSITIVE))
    p = EpisodeSourceProvider.authenticated('source@example.test','test',detector=object(),search_circuit=circuit)
    assert calls == [400]
    p.session.close()


def test_shared_search_outage_does_not_increment_episode_or_series_failure_counts(tmp_path, monkeypatch):
    import json
    from sdilej_serialy import replenish
    from test_replenish import setup
    setup(tmp_path,monkeypatch)
    path=tmp_path/'state/reserve-preparation.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(schema_version=1,episodes={
        '1:1:5':dict(status='deferred',consecutive_failures=2)},series_discovery={})))
    circuit = SearchCircuit()
    circuit.failure()
    provider=SimpleNamespace(discover=lambda _:None,last_outcome='source_search_unavailable',
                             search_health=SimpleNamespace(circuit=circuit))
    replenish.prepare(tmp_path,'test',provider,runtime_minutes=0)
    state=json.loads(path.read_text())
    assert state['episodes']['1:1:5']['consecutive_failures']==2
    assert state['episodes']['1:1:6']['consecutive_failures']==0
    assert not state['series_discovery']
    from datetime import datetime
    deadlines=[datetime.fromisoformat(state['episodes'][i]['retry_after']) for i in ('1:1:5','1:1:6')]
    assert abs((deadlines[0]-deadlines[1]).total_seconds()) < 1


def test_backend_outage_is_not_a_checked_season_sample_or_a_used_extra_hint_budget(tmp_path):
    from sdilej_serialy.requested_series import RequestedSeries
    from test_requested_series import request_plan
    from test_dual import source
    path=tmp_path/'requests.json'
    request_plan(path)
    records={f'1:1:{n}':dict(request_probe_revision=1,request_hint_probe_revision=1,
                            reason='source_search_unavailable') for n in range(1,7)}
    gate=RequestedSeries(path,'test',records,{})
    assert not gate.checked('1:1:1')
    assert not gate.available(dict(source()['episode'],season=2))
    assert gate.available(source()['episode'])
    assert not gate.extra_samples[1,1]
    assert gate.report()[0]['seasons'][0]['status']=='sampling'

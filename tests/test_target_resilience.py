from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import pytest
import requests

from sdilej_serialy import continuous, dual, pipeline, resilience
from sdilej_serialy.models import Episode
from test_dual import setup_plan, stub_live, source
from test_source_retry import stub_transfer


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError('Private URL must not be logged', response=response)


@pytest.mark.parametrize('status,expected', [(504, True), (503, True), (429, True), (401, False), (403, False), (404, False)])
def test_transient_classifier_preserves_account_and_permission_failures(status, expected):
    assert resilience.transient_http(http_error(status)) == expected
    assert resilience.error_evidence(http_error(status)) == {'type': 'HTTPError', 'http_status': status}


def test_read_adapter_retries_get_but_never_post():
    calls = {'GET': 0, 'POST': 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls['GET'] += 1
            self.send_response(504 if calls['GET'] < 3 else 200)
            self.end_headers()
        def do_POST(self):
            calls['POST'] += 1
            self.send_response(504)
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    session = resilience.retry_target_reads(requests.Session())
    adapter = session.get_adapter('https://prehraj.to/')
    adapter.max_retries.backoff_factor = 0
    assert adapter.max_retries.connect == 0
    session.mount('http://127.0.0.1:', adapter)
    url = f'http://127.0.0.1:{server.server_port}/'
    try:
        assert session.get(url, timeout=2).status_code == 200
        assert session.post(url, timeout=2).status_code == 504
        assert calls == {'GET': 3, 'POST': 1}
    finally:
        session.close()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize('status,position,expected', [(200, 100, True), (201, 100, True), (200, 99, False), (504, 100, False)])
def test_receipt_requires_accepted_complete_body(tmp_path, monkeypatch, status, position, expected):
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    episode = Episode.from_dict(source()['episode'])
    state.row(episode)['prepared_target'] = {'target_video_id': '555', 'size_bytes': 100}
    reader = SimpleNamespace(position=position, total=100)
    encoder = SimpleNamespace(fields=[('files', ('file.mkv', reader, 'video/mp4'))])
    monkeypatch.setattr(resilience.requests, 'post', lambda *a, **k: SimpleNamespace(status_code=status))
    resilience.receipt_requester(state, episode)('https://upload.invalid/', data=encoder)
    assert resilience.receipt_matches(state.row(episode)) == expected
    if expected:
        assert resilience.receipt_matches(pipeline.EpisodeState(state.path).row(episode))


def run_transfer(rows, state, stop, pause):
    return continuous.upload_continuously(rows, state, workers=1, source_email='source',
        source_password='x', target_email='target', target_password='x', require_original_size=True,
        recover_source_errors=True, recover_target_errors=True, stop_event=stop, transient_pause=pause)


def test_allocated_504_pauses_without_halt_or_replay(tmp_path, monkeypatch):
    stub_transfer(monkeypatch, lambda c, **kw: c)
    rows = [source(number=1), source(number=2)]
    def relay(*args, on_prepared, upload_requester):
        on_prepared('555', 100)
        raise http_error(504)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    stop, pause = Event(), Event()
    run_transfer(rows, state, stop, pause)
    assert pause.is_set() and not stop.is_set()
    pending = state.data['episodes'][rows[0]['identity']]
    assert pending['prepared_target']['target_video_id'] == '555'
    assert not pending.get('upload')
    pending['attempts'][-1]['at'] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', lambda *a, **k: pytest.fail('Uncertain target replay'))
    run_transfer(rows[:1], state, Event(), Event())
    assert state.data['episodes'][rows[0]['identity']]['prepared_target']['target_video_id'] == '555'


def test_uncertain_target_does_not_block_other_episodes(tmp_path, monkeypatch):
    stub_transfer(monkeypatch, lambda c, **kw: c)
    rows = [source(number=1), source(number=2)]
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    state.row(Episode.from_dict(rows[0]['episode']))['prepared_target'] = {'creation_intent': True}
    def relay(target, provider, candidate, name, description, on_prepared, upload_requester):
        assert candidate.source_id == rows[1]['selected']['source_id']
        on_prepared('666', 100)
        return SimpleNamespace(video_id='666')
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    stop, pause = Event(), Event()
    run_transfer(rows, state, stop, pause)
    assert not stop.is_set() and not pause.is_set()
    assert state.data['episodes'][rows[1]['identity']]['upload']['target_video_id'] == '666'
    assert state.data['episodes'][rows[0]['identity']]['prepared_target'] == {'creation_intent': True}


def test_complete_receipt_reconciles_same_target_without_post(tmp_path, monkeypatch):
    stub_transfer(monkeypatch, lambda c, **kw: c)
    row = source()
    episode = Episode.from_dict(row['episode'])
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    state.row(episode).update(prepared_target={'target_video_id': '555', 'size_bytes': 100},
        transfer_receipt={'target_video_id': '555', 'size_bytes': 100, 'source_bytes_read': 100, 'http_status': 200})
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', lambda *a, **k: pytest.fail('Duplicate upload'))
    run_transfer([row], state, Event(), Event())
    assert state.row(episode)['upload']['target_video_id'] == '555'
    assert 'prepared_target' not in state.row(episode)
    assert 'transfer_receipt' not in state.row(episode)


def test_statistics_504_after_success_does_not_lose_completed_upload(tmp_path, monkeypatch):
    stub_transfer(monkeypatch, lambda c, **kw: c)
    calls = []
    def count(session):
        calls.append(1)
        if len(calls) > 1:
            raise http_error(504)
        return 0
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', count)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', lambda *a, **k: SimpleNamespace(video_id='555'))
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    stop, pause = Event(), Event()
    report = run_transfer([source()], state, stop, pause)
    assert report['target_video_count_after'] is None
    assert state.row(Episode.from_dict(source()['episode']))['upload']['target_video_id'] == '555'
    assert pause.is_set() and not stop.is_set()


def test_preflight_504_is_retried_next_batch_not_persistently_halted(tmp_path, monkeypatch):
    directory, _, _ = setup_plan(tmp_path, monkeypatch)
    stub_live(monkeypatch)
    def failed_count(session):
        raise http_error(504)
    monkeypatch.setattr(dual.prehrajto, 'uploaded_video_count', failed_count)
    for _ in range(2):
        report = dual.run(tmp_path, 'test', 'pilot')
        assert not report['halted'] and report['transient_pause']
    saved = pipeline.EpisodeState(directory / 'state.json').data
    assert saved['last_transient_error']['http_status'] == 504
    assert not saved.get('halted_at')


def test_preflight_permission_failure_still_halts(tmp_path, monkeypatch):
    directory, _, _ = setup_plan(tmp_path, monkeypatch)
    stub_live(monkeypatch)
    def failed_count(session):
        raise http_error(403)
    monkeypatch.setattr(dual.prehrajto, 'uploaded_video_count', failed_count)
    with pytest.raises(requests.HTTPError):
        dual.run(tmp_path, 'test', 'pilot')
    assert pipeline.EpisodeState(directory / 'state.json').data['halted_at']


def test_request_liveness_clears_even_after_transport_failure(tmp_path, monkeypatch):
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    request = resilience.receipt_requester(state, Episode.from_dict(source()['episode']))
    def failed_post(*args, **kwargs):
        assert not request.finished.is_set()
        raise requests.Timeout()
    monkeypatch.setattr(resilience.requests, 'post', failed_post)
    with pytest.raises(requests.Timeout):
        request('https://upload.invalid/')
    assert request.finished.is_set()

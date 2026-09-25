from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import threading

import pytest
import requests
from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto.sdilej import SdilejError

from sdilej_serialy import continuous, dual, pipeline, source_detail
from sdilej_serialy.models import Episode
from test_dual import setup_plan, source, stub_live


def stub_transfer(monkeypatch, refresh):
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: SimpleNamespace(
        session=object(), refresh=refresh))
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous, 'target_confirmed', lambda *a: True)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 0)
    monkeypatch.setattr(continuous.time, 'sleep', lambda _: None)
    monkeypatch.setattr(source_detail, 'resolve_original', lambda _, c: c)


def transfer(rows, state, stop):
    return continuous.upload_continuously(rows, state, workers=1, source_email='source',
        source_password='x', target_email='target', target_password='x',
        require_original_size=True, recover_source_errors=True, stop_event=stop)


def test_source_failure_defers_only_failed_episode_and_retries_after_restart(tmp_path, monkeypatch):
    rows = [source(number=1), source(number=2)]
    bad_id = rows[0]['selected']['source_id']
    unavailable = [True]
    calls = []
    def refresh(c, **kwargs):
        calls.append(c.source_id)
        if c.source_id == bad_id and unavailable[0]:
            raise SdilejError('Temporary source failure')
        return c
    stub_transfer(monkeypatch, refresh)
    uploads = []
    def relay(target, source_session, candidate, name, description, on_prepared):
        uploads.append(candidate.source_id)
        on_prepared(candidate.source_id, candidate.size_bytes)
        return SimpleNamespace(video_id=candidate.source_id)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    stop = threading.Event()
    transfer(rows, state, stop)
    assert calls.count(bad_id) == 3
    assert not stop.is_set()
    assert uploads == [rows[1]['selected']['source_id']]
    failed = state.data['episodes'][rows[0]['identity']]
    assert 'prepared_target' not in failed and 'claim' not in failed
    restored = pipeline.EpisodeState(state.path)
    assert rows[0]['identity'] in restored.retry_deferred_identities()
    transfer(rows, restored, stop)
    assert len(uploads) == 1
    restored.data['episodes'][rows[0]['identity']]['attempts'][-1]['at'] = (
        datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    unavailable[0] = False
    transfer(rows, restored, stop)
    assert sorted(uploads) == sorted(r['selected']['source_id'] for r in rows)
    assert not stop.is_set()


def test_source_recovers_during_bounded_retry(monkeypatch):
    candidate = Candidate.from_dict(source()['selected'])
    calls = []
    def refresh(c, **kwargs):
        calls.append(c)
        if len(calls) < 3:
            raise requests.Timeout()
        return c
    monkeypatch.setattr(continuous.time, 'sleep', lambda _: None)
    provider = SimpleNamespace(session=object(), refresh=refresh)
    assert continuous.prepare_source(provider, candidate, False) == candidate
    assert len(calls) == 3


@pytest.mark.parametrize('error', [SdilejError('connection lost'), requests.Timeout()])
def test_failure_after_target_allocation_still_stops_without_duplicate(tmp_path, monkeypatch, error):
    rows = [source(number=1), source(number=2)]
    stub_transfer(monkeypatch, lambda c, **kwargs: c)
    calls = []
    def relay(*args, on_prepared):
        calls.append(1)
        on_prepared('555', 100)
        raise error
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    stop = threading.Event()
    transfer(rows, state, stop)
    assert stop.is_set() and len(calls) == 1
    assert state.data['episodes'][rows[0]['identity']]['prepared_target']['target_video_id'] == '555'
    assert not state.data['episodes'][rows[0]['identity']].get('upload')


def test_source_size_change_is_not_treated_as_transient(tmp_path, monkeypatch):
    stub_transfer(monkeypatch, lambda c, **kwargs: c)
    monkeypatch.setattr(source_detail, 'resolve_original', lambda _, c: SimpleNamespace(size_bytes=101))
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', lambda *a, **k: pytest.fail('Unsafe upload'))
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    stop = threading.Event()
    transfer([source()], state, stop)
    assert stop.is_set()


def test_source_login_outage_does_not_persist_global_halt(tmp_path, monkeypatch):
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    videos, _ = stub_live(monkeypatch)
    state = pipeline.EpisodeState(directory / 'state.json')
    state.data['initialized_at'] = pipeline.now_iso()
    state.data['pilot_verified_at'] = pipeline.now_iso()
    state.save()
    def upload(batch, state, **kwargs):
        assert kwargs['recover_source_errors']
        raise continuous.SourceUnavailable('Source login unavailable')
    monkeypatch.setattr(dual, 'upload_continuously', upload)
    report = dual.run(tmp_path, 'test', 'full')
    assert not report['halted']
    assert report['accounts']['a']['source_unavailable']
    assert report['accounts']['b']['source_unavailable']
    assert 'halted_at' not in pipeline.EpisodeState(state.path).data
    assert not dual.run(tmp_path, 'test', 'full')['halted']


def test_source_deferred_pilot_can_be_resumed_without_permanent_halt(tmp_path, monkeypatch):
    directory, _, _ = setup_plan(tmp_path, monkeypatch)
    stub_live(monkeypatch)
    monkeypatch.setattr(dual, 'upload_continuously', lambda *a, **k: {})
    report = dual.run(tmp_path, 'test', 'pilot')
    assert not report['halted']
    assert 'pilot_verified_at' not in pipeline.EpisodeState(directory / 'state.json').data


def test_workflow_reinstalls_pulled_code_before_each_batch():
    from pathlib import Path
    workflow = Path('.github/workflows/dual.yml').read_text()
    loop = workflow.split('while [', 1)[1]
    assert loop.index('git pull') < loop.index('pip install --no-deps .') < loop.index('python -m sdilej_serialy.dual')

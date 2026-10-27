import threading
import time
from types import SimpleNamespace

import pytest

from sdilej_serialy import continuous
from sdilej_serialy.pipeline import EpisodeState
from test_dual import source


def test_remote_queue_read_does_not_hold_the_upload_state_lock(tmp_path, monkeypatch):
    from sdilej_serialy.dual import LiveReserve, SharedState
    from test_dual import setup_plan
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    state = SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    reading, release, saved = threading.Event(), threading.Event(), threading.Event()
    def read(*args, **kwargs):
        reading.set()
        assert release.wait(3)
        return ''
    reserve = LiveReserve(tmp_path, 'test', rows, state, {'a': [], 'b': []}, 25,
                          SimpleNamespace(read_remote_file=read))
    errors = []
    def poll():
        try:
            reserve.take('a')
        except Exception as error:
            errors.append(error)
    reader = threading.Thread(target=poll)
    reader.start()
    assert reading.wait(3)
    def checkpoint():
        with state._lock:
            state.save()
            saved.set()
    writer = threading.Thread(target=checkpoint)
    writer.start()
    try:
        assert saved.wait(1), 'Remote read blocked upload checkpoints'
    finally:
        release.set()
        reader.join(3)
        writer.join(3)
    assert not errors and not reader.is_alive() and not writer.is_alive()


def test_temporary_queue_read_failure_pauses_without_permanent_stop(tmp_path, monkeypatch):
    from sdilej_serialy.git_state import CheckpointError
    options = stub(monkeypatch)
    stop, pause = threading.Event(), threading.Event()
    def unavailable():
        raise CheckpointError('Temporary read failure')
    report = continuous.upload_continuously([], EpisodeState(tmp_path / 'state.json'),
        refill_rows=unavailable, stop_event=stop, transient_pause=pause, recover_target_errors=True, **options)
    assert not stop.is_set() and pause.is_set() and report['uploaded_or_reconciled'] == 0


def stub(monkeypatch):
    provider = SimpleNamespace(session=object(), refresh=lambda c, **kw: c)
    monkeypatch.setattr(continuous, 'EpisodeSourceProvider', SimpleNamespace(authenticated=lambda *a: provider))
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 0)
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous, 'target_confirmed', lambda *a: True)
    return dict(workers=2, source_email='source', source_password='test',
                target_email='target', target_password='test',
                refill_interval_seconds=.001, idle_refill_seconds=.03)


def test_initially_empty_account_accepts_later_work_once_without_extra_workers(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    row = source()
    polls, transferred = [], []
    def refill():
        polls.append(1)
        return [] if len(polls) == 1 else [row] if len(polls) == 2 else None
    def relay(_target, _source, candidate, name, description, *, on_prepared):
        on_prepared(candidate.source_id, candidate.size_bytes)
        transferred.append(name)
        return SimpleNamespace(video_id=candidate.source_id)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    result = continuous.upload_continuously([], EpisodeState(tmp_path / 'state.json'),
                                            refill_rows=refill, **options)
    assert result['uploaded_or_reconciled'] == 1
    assert result['queued'] == 1 and transferred == [row['display_name']]


def test_empty_account_polling_is_bounded(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    polls = []
    result = continuous.upload_continuously([], EpisodeState(tmp_path / 'state.json'),
        refill_rows=lambda: polls.append(1) or [], **options)
    assert polls and result['queued'] == 0


def test_unvalidated_refill_token_cannot_requeue_a_known_identity(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    row = source()
    state = EpisodeState(tmp_path / 'state.json')
    claims = []
    original_claim = state.claim
    def claim(*args, **kwargs):
        claims.append(1)
        return original_claim(*args, **kwargs)
    monkeypatch.setattr(state, 'claim', claim)
    def unavailable(*args):
        raise continuous.SourceUnavailable('Unavailable original')
    monkeypatch.setattr(continuous, 'prepare_source', unavailable)
    continuous.upload_continuously([row], state, recover_source_errors=True,
        refill_rows=lambda: [dict(row, _dispatch_repair_token='untrusted')], **options)
    assert claims == [1]


def test_idle_account_keeps_refilling_while_peer_transfers_without_extra_workers(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    row = source()
    polls = []
    started = time.monotonic()
    def refill():
        polls.append(1)
        if time.monotonic() - started < .07:
            return []
        return [row] if len(polls) else None
    def relay(_target, _source, candidate, name, description, *, on_prepared):
        on_prepared(candidate.source_id, candidate.size_bytes)
        return SimpleNamespace(video_id=candidate.source_id)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    result = continuous.upload_continuously([], EpisodeState(tmp_path / 'state.json'),
        refill_rows=refill, keep_waiting=lambda: time.monotonic() - started < .1, **options)
    assert result['uploaded_or_reconciled'] == 1
    assert result['queued'] == 1


def test_idle_wait_is_still_bounded_after_peer_stops(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    result = continuous.upload_continuously([], EpisodeState(tmp_path / 'state.json'),
        refill_rows=lambda: [], keep_waiting=lambda: False, **options)
    assert result['queued'] == 0


def test_uncertain_allocations_are_not_active_peers(tmp_path):
    from sdilej_serialy.dual import SharedState
    state = SharedState(tmp_path / 'state.json', {})
    state.data['episodes']['old'] = dict(target_account='b', prepared_target=dict(target_video_id='123'))
    assert not state.other_account_active('a')
    state.data['episodes']['active'] = dict(target_account='b', claim=dict(worker_id='active'))
    assert state.other_account_active('a')
    assert not state.other_account_active('b')


def test_invalid_live_queue_stops_idle_peers_instead_of_silently_dropping_error(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    stop = threading.Event()
    def invalid():
        raise ValueError('Invalid queue')
    with pytest.raises(ValueError, match='Invalid queue'):
        continuous.upload_continuously([], EpisodeState(tmp_path / 'state.json'),
                                       refill_rows=invalid, stop_event=stop, **options)
    assert stop.is_set()

import io
import json
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
import requests

from sdilej_serialy import continuous, pipeline, source_transfer
from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.models import Episode
from sdilej_serialy.source_transfer import FreshSourceSession, SourceUnavailable, stream_size
from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto.sdilej import SdilejError
from test_dual import source
from test_target_resilience import run_transfer

SIZE = 2 * 1024 * 1024


def response(status=200, *, size=SIZE, content_type='video/x-matroska', content_range=None):
    result = requests.Response()
    result.status_code = status
    result.url = 'https://cdn.example/current?private=do-not-persist'
    result.headers.update({'Content-Length': str(size), 'Content-Type': content_type})
    if content_range is not None:
        result.headers['Content-Range'] = content_range
    result.raw = io.BytesIO(b'x' * min(size, SIZE))
    return result


@pytest.mark.parametrize('status,length,content_range,valid', [
    (200, SIZE, None, True),
    (206, SIZE, f'bytes 0-{SIZE - 1}/{SIZE}', True),
    (206, 1, f'bytes 0-0/{SIZE}', False),
    (206, SIZE, f'bytes 1-{SIZE}/{SIZE + 1}', False),
    (206, SIZE, f'bytes 0-{SIZE - 2}/{SIZE}', False),
    (206, SIZE - 1, f'bytes 0-{SIZE - 1}/{SIZE}', False),
    (206, SIZE, None, False),
    (200, SIZE, f'bytes 0-{SIZE - 1}/{SIZE}', False),
    (200, 18, None, False),
    (204, SIZE, None, False),
])
def test_stream_must_include_the_complete_original(status, length, content_range, valid):
    stream = response(status, size=length, content_range=content_range)
    try:
        if valid:
            assert stream_size(stream) == SIZE
        else:
            with pytest.raises(SdilejError):
                stream_size(stream)
    finally:
        stream.close()


@pytest.mark.parametrize('content_type', ['text/html', 'application/json', 'application/xhtml+xml'])
def test_login_and_error_documents_cannot_be_uploaded(content_type):
    stream = response(content_type=content_type)
    try:
        with pytest.raises(SdilejError):
            stream_size(stream)
    finally:
        stream.close()


@pytest.mark.parametrize('mode', ['ok', 'expired', 'unavailable', 'changed_size', 'allocation_timeout'])
def test_pinned_sdk_opens_current_source_after_checkpoint_and_statistics(tmp_path, monkeypatch, mode):
    """Exercise the real SDK relay, including its multipart upload thread."""
    row = source(size=SIZE)
    episode = Episode.from_dict(row['episode'])
    events, streams, snapshots = [], [], []
    revision = [0]
    opens = [0]
    counts = [0]

    def checkpoint(path):
        saved = json.loads(path.read_text())
        snapshots.append(saved)
        record = saved['episodes'].get(episode.identity, {})
        if record.get('prepared_target') == {'creation_intent': True}:
            revision[0] += 1  # Previous URLs expire during a slow checkpoint.
            events.append('intent')

    state = pipeline.EpisodeState(tmp_path / 'state.json', on_save=checkpoint)

    def source_get(url, **kwargs):
        if url == row['selected']['url']:
            events.append('detail')
            return SimpleNamespace(text=(f'<h1>episode.mkv</h1><a href="https://cdn.example/{revision[0]}">'
                                         'Stáhnout rychle</a>'), raise_for_status=lambda: None)
        assert url == f'https://cdn.example/{revision[0]}', 'Used a URL from before the checkpoint/statistics'
        assert kwargs['headers']['Referer'] == row['selected']['url']
        if kwargs['headers']['Range'] == 'bytes=0-0':
            events.append('probe')
            stream = response(206, size=1, content_range=f'bytes 0-0/{SIZE}')
        else:
            assert kwargs['stream'] is True and kwargs['allow_redirects'] is True
            events.append('open')
            opens[0] += 1
            if mode == 'unavailable' or mode == 'expired' and opens[0] == 1:
                revision[0] += 1
                stream = response(403)
            else:
                stream = response(size=SIZE + 1 if mode == 'changed_size' else SIZE)
        stream.url = url
        streams.append(stream)
        return stream

    provider = EpisodeSourceProvider(SimpleNamespace(get=source_get), detector=object())
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: provider)
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous, 'target_confirmed', lambda *a: True)
    monkeypatch.setattr(source_transfer.time, 'sleep', lambda _: None)

    def count(*args):
        counts[0] += 1
        if counts[0] == 2:  # This call is inside the pinned SDK.
            events.append('baseline')
            revision[0] += 1
        return 0

    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', count)

    def allocate(*args, **kwargs):
        events.append('allocate')
        assert streams[-1].raw.tell() == 0
        assert not streams[-1].raw.closed
        if mode == 'allocation_timeout':
            raise requests.Timeout('Target may already have been allocated')
        return '555', {key: 'test' for key in ('response', 'project', 'nonce', 'params', 'signature')}

    monkeypatch.setattr(continuous.prehrajto, 'prepare_video', allocate)
    monkeypatch.setattr(continuous.prehrajto, 'rename_video', lambda *a: None)
    monkeypatch.setattr(continuous.prehrajto, 'upload_confirmed_after_baseline', lambda *a: True)

    def upload(*args, data, **kwargs):
        events.append('post')
        assert state.row(episode)['prepared_target']['target_video_id'] == '555'
        while data.read(65536):
            pass
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(requests, 'post', upload)
    stop, pause = Event(), Event()
    run_transfer([row], state, stop, pause)
    record = state.row(episode)
    assert events[:6] == ['detail', 'probe', 'intent', 'baseline', 'detail', 'open']
    assert all(stream.raw.closed for stream in streams)
    serialized = json.dumps(snapshots)
    assert 'cdn.example' not in serialized and 'download_url' not in serialized
    if mode in ('ok', 'expired'):
        assert state.uploaded(episode) and not stop.is_set() and not pause.is_set()
        assert events.count('allocate') == events.count('post') == 1
        assert opens[0] == (2 if mode == 'expired' else 1)
        receipt = next(s['episodes'][episode.identity] for s in snapshots
                       if s['episodes'].get(episode.identity, {}).get('transfer_receipt'))
        assert receipt['transfer_receipt']['source_bytes_read'] == SIZE
    elif mode == 'allocation_timeout':
        assert record['prepared_target'] == {'creation_intent': True}
        assert pause.is_set() and not stop.is_set()
        assert events.count('allocate') == 1 and 'post' not in events
    else:
        assert 'prepared_target' not in record and 'upload' not in record and 'claim' not in record
        assert 'allocate' not in events and 'post' not in events
        assert stop.is_set() == (mode == 'changed_size')
        if mode == 'unavailable':
            assert opens[0] == 3 and events.count('detail') == 4
            assert record['attempts'][-1]['error'] == 'SourceUnavailable'
            # A known pre-allocation source failure must remain retryable.
            restored = pipeline.EpisodeState(state.path)
            assert 'prepared_target' not in restored.row(episode)


def test_changed_source_identity_never_opens_media():
    candidate = Candidate.from_dict(source(size=SIZE)['selected'])
    provider = SimpleNamespace(session=SimpleNamespace(get=lambda *a, **k: pytest.fail('Wrong source')),
        refresh=lambda c, **kw: replace(c, source_id='999'))
    session = FreshSourceSession(provider, candidate, require_original_size=True)
    with pytest.raises(RuntimeError, match='identity') as caught:
        session.get('https://old.example/expired')
    assert session.failure_before_return is caught.value


def test_failed_source_open_does_not_discard_an_allocated_target(tmp_path, monkeypatch):
    """Even conflicting evidence must retain an ID, never authorize a replay."""
    from test_source_retry import stub_transfer
    stub_transfer(monkeypatch, lambda c, **kw: c)
    row = source(size=SIZE)
    episode = Episode.from_dict(row['episode'])
    state = pipeline.EpisodeState(tmp_path / 'state.json')

    def relay(target, session, candidate, name, description, on_prepared, **kwargs):
        on_prepared('555', SIZE)
        error = SourceUnavailable('Source failed')
        session.failure_before_return = error
        raise error

    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    run_transfer([row], state, Event(), Event())
    assert state.row(episode)['prepared_target']['target_video_id'] == '555'
    assert not state.uploaded(episode)

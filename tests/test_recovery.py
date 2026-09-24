import gzip
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier

from sdilej_serialy import cli, continuous
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import EpisodeState, atomic_json
from sdilej_serialy.quality import QUALITY_POLICY
from sdilej_serialy.recovery import prepare_recovery, load_recovery, recovery_directory


def row(series=1, number=1, *, name='Series', size=100, policy=QUALITY_POLICY, language='czech_audio'):
    episode = Episode(number, series, name, None, 1, number)
    candidate = Candidate(source_id=str(series * 100 + number), url=f'https://sdilej.cz/{series * 100 + number}/video',
                          title=f'{name} {episode.code}', filename='video.mkv', size_bytes=size,
                          duration_sec=100, width=1920, height=1080,
                          match_tier=MatchTier.STRONG, language_tier=LanguageTier.CZECH_AUDIO)
    selected = candidate.to_dict()
    selected['language_tier'] = language
    return dict(identity=episode.identity, episode=episode.to_dict(), selected=selected,
                display_name=f'{name} {episode.code} - Title 1080p CZ Dabing', quality_policy=policy)


def prepare(tmp_path, rows=None, uploaded=None):
    rows = rows if rows is not None else [row(), row(number=2)]
    uploaded = uploaded if uploaded is not None else [r['identity'] for r in rows]
    manifest = SourceManifest(tmp_path / 'original.jsonl')
    for r in rows:
        manifest.add(r)
    manifest.save()
    history = tmp_path / 'history.json'
    state = EpisodeState(history)
    for r in rows:
        if r['identity'] in uploaded:
            state.success(Episode.from_dict(r['episode']), '999', r['display_name'])
    prepare_recovery(tmp_path, 'test', history, manifest.path, 'abc123')
    return load_recovery(tmp_path, 'test')


def test_recovery_deduplicates_and_preserves_history(tmp_path):
    rows = [row(), row(series=2, size=80), row(number=2), row(number=3, policy=None),
            row(number=4, language='foreign_audio'), row(number=5)]
    directory, plan, manifest = prepare(tmp_path, rows, [r['identity'] for r in rows[:-1]])
    assert plan['historical_unique'] == 4
    assert plan['selected_count'] == 2
    assert set(manifest.rows) == {'2:1:1', '1:1:2'}
    assert gzip.decompress((directory / 'history.json.gz').read_bytes()) == (tmp_path / 'history.json').read_bytes()
    assert json.loads((directory / 'state.json').read_text())['episodes'] == {}
    assert {r['reason'] for r in json.loads((directory / 'excluded.json').read_text())} == {'legacy_czech_policy', 'non_czech_audio'}


def test_cannot_reset_existing_generation(tmp_path):
    prepare(tmp_path)
    with pytest.raises(ValueError, match='already exists'):
        prepare_recovery(tmp_path, 'test', tmp_path/'history.json', tmp_path/'original.jsonl', 'abc123')


@pytest.mark.parametrize('generation', ['../state', '/tmp', 'a/b', '', 'UPPER'])
def test_invalid_generation_rejected(tmp_path, generation):
    with pytest.raises(ValueError):
        recovery_directory(tmp_path, generation)


@pytest.mark.parametrize('filename', ['manifest.jsonl', 'history.json.gz'])
def test_modified_snapshot_is_rejected(tmp_path, filename):
    directory, _, _ = prepare(tmp_path)
    if filename.endswith('.gz'):
        (directory/filename).write_bytes(gzip.compress(b'{}'))
    else:
        with (directory/filename).open('ab') as file:
            file.write(b'\n')
    with pytest.raises(ValueError, match='snapshot changed'):
        load_recovery(tmp_path, 'test')


def args(mode='pilot'):
    return Namespace(generation='test', mode=mode, workers=1, limit=2, persist_git_state=False)


def stub_restore(monkeypatch, tmp_path, count=0):
    from sdilej_serialy import pipeline
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setenv('RECOVERY_ENABLED', 'true')
    monkeypatch.setattr(cli, 'require_env', lambda _: 'test')
    monkeypatch.setattr(pipeline, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: count)


def test_full_recovery_requires_verified_pilot(tmp_path, monkeypatch):
    prepare(tmp_path)
    stub_restore(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='pilot'):
        cli.restore(args('full'))


def test_first_recovery_requires_empty_account(tmp_path, monkeypatch):
    prepare(tmp_path)
    stub_restore(monkeypatch, tmp_path, count=1)
    with pytest.raises(RuntimeError, match='empty account'):
        cli.restore(args())


def test_recovery_never_refills_from_ordinary_manifest(tmp_path, monkeypatch):
    directory, _, manifest = prepare(tmp_path)
    original = (tmp_path/'history.json').read_bytes()
    stub_restore(monkeypatch, tmp_path)
    calls = []

    def upload(rows, state, **kwargs):
        assert 'refill_rows' not in kwargs
        assert kwargs['require_original_size'] is True
        assert state.path == directory / 'state.json'
        calls.extend(r['identity'] for r in rows)
        for r in rows:
            state.success(Episode.from_dict(r['episode']), str(100 + len(calls)), r['display_name'])
        return {'queued': len(rows), 'uploaded_or_reconciled': len(rows)}

    monkeypatch.setattr(cli, 'upload_continuously', upload)
    cli.restore(args())
    assert set(calls) == set(manifest.rows)
    assert (tmp_path/'history.json').read_bytes() == original
    assert json.loads((directory/'report.json').read_text())['remaining'] == 0
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 2)
    cli.restore(args())
    assert len(calls) == 2


def test_recreated_target_cannot_reuse_recovery_state(tmp_path, monkeypatch):
    directory, _, manifest = prepare(tmp_path)
    state = EpisodeState(directory/'state.json')
    state.data['target_initialized_at'] = 'now'
    r = next(iter(manifest.rows.values()))
    state.success(Episode.from_dict(r['episode']), '100', r['display_name'])
    stub_restore(monkeypatch, tmp_path, count=0)
    with pytest.raises(RuntimeError, match='empty again'):
        cli.restore(args())


def test_recovery_changed_original_never_creates_target(tmp_path, monkeypatch):
    from sdilej_serialy import source_detail
    r = row()
    candidate = Candidate.from_dict(r['selected'])
    provider = SimpleNamespace(session=object(), refresh=lambda *a, **k: candidate)
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: provider)
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 0)
    monkeypatch.setattr(source_detail, 'resolve_original', lambda *a: SimpleNamespace(size_bytes=999))
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', lambda *a, **k: pytest.fail('Unexpected upload'))
    state = EpisodeState(tmp_path/'state.json')
    continuous.upload_continuously([r], state, workers=1, source_email='s', source_password='x',
                                   target_email='t', target_password='x', require_original_size=True)
    assert state.data['episodes'][r['identity']]['attempts']
    assert not state.data['episodes'][r['identity']].get('prepared_target')


def test_verify_pilot_requires_unique_matching_targets(tmp_path, monkeypatch):
    directory, _, manifest = prepare(tmp_path)
    state = EpisodeState(directory/'state.json')
    for i, r in enumerate(manifest.rows.values(), 1):
        state.success(Episode.from_dict(r['episode']), str(i), r['display_name'])
    stub_restore(monkeypatch, tmp_path, count=2)
    from sdilej_serialy import pipeline
    monkeypatch.setattr(pipeline, 'target_session', lambda *a: SimpleNamespace(
        get=lambda *a, **k: SimpleNamespace(text='<div></div>', raise_for_status=lambda: None)))
    with pytest.raises(RuntimeError, match='missing or duplicated'):
        cli.verify_recovery(args())
    assert 'pilot_verified_at' not in EpisodeState(state.path).data


def test_verify_completed_pilot_unlocks_full_mode(tmp_path, monkeypatch):
    directory, _, manifest = prepare(tmp_path)
    state = EpisodeState(directory/'state.json')
    state.data['target_initialized_at'] = 'now'
    rows = list(manifest.rows.values())
    for i, r in enumerate(rows, 1):
        state.success(Episode.from_dict(r['episode']), str(i), r['display_name'])
    stub_restore(monkeypatch, tmp_path, count=2)
    from sdilej_serialy import pipeline

    def get(url, params, **kwargs):
        i, r = next((i, r) for i, r in enumerate(rows, 1)
                    if r['display_name'].startswith(params['searchPhrase']))
        html = (f'<section><h3>{r["display_name"]}</h3><a href="?'
                f'uploadedVideoListing-videoId={i}&amp;do=uploadedVideoListing-deleteVideo">Delete</a></section>')
        return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(pipeline, 'target_session', lambda *a: SimpleNamespace(get=get))
    cli.verify_recovery(args())
    assert EpisodeState(state.path).data['pilot_verified_at']
    cli.restore(args('full'))
    assert json.loads((directory/'report.json').read_text())['remaining'] == 0


def test_partial_recovery_transfer_is_not_reconciled_as_complete(tmp_path, monkeypatch):
    from sdilej_serialy import source_detail
    r = row()
    candidate = Candidate.from_dict(r['selected'])
    provider = SimpleNamespace(session=object(), refresh=lambda *a, **k: candidate)
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: provider)
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    lookups = []

    def lookup(*a):
        lookups.append(1)
        return None if len(lookups) == 1 else '123'

    monkeypatch.setattr(continuous, 'existing_episode', lookup)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 0)
    monkeypatch.setattr(source_detail, 'resolve_original', lambda *a: candidate)

    def relay(*a, on_prepared):
        on_prepared('123', candidate.size_bytes)
        raise TimeoutError('Transfer interrupted')

    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    state = EpisodeState(tmp_path/'state.json')
    continuous.upload_continuously([r], state, workers=1, source_email='s', source_password='x',
                                   target_email='t', target_password='x', require_original_size=True)
    saved = state.data['episodes'][r['identity']]
    assert saved['prepared_target']['target_video_id'] == '123'
    assert not saved.get('upload')

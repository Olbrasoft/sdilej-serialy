from datetime import UTC, datetime, timedelta

from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import EpisodeState


def test_failure_backoff_survives_restart_without_losing_target(tmp_path):
    episode = Episode(1, 1, 'Series', None, 1, 1)
    other = Episode(2, 1, 'Series', None, 1, 2)
    path = tmp_path / 'state.json'
    state = EpisodeState(path)
    state.row(episode)['prepared_target'] = {'target_video_id': '123'}
    state.failure(episode, TimeoutError())
    restored = EpisodeState(path)
    assert restored.retry_deferred_identities() == {episode.identity}
    assert not restored.claim(episode, 'retry')
    assert restored.claim(other, 'other')
    assert restored.row(episode)['prepared_target'] == {'target_video_id': '123'}
    restored.row(episode)['attempts'][-1]['at'] = (
        datetime.now(UTC) - timedelta(minutes=16)
    ).isoformat()
    assert restored.retry_deferred_identities() == set()
    assert restored.claim(episode, 'later')
    assert restored.row(episode)['prepared_target'] == {'target_video_id': '123'}


def test_success_is_not_deferred_by_old_failure(tmp_path):
    episode = Episode(1, 1, 'Series', None, 1, 1)
    state = EpisodeState(tmp_path / 'state.json')
    state.failure(episode, TimeoutError())
    state.success(episode, '123', 'Series S01E01')
    assert state.retry_deferred_identities() == set()


def test_repeated_source_outage_backs_off_to_one_hour_across_restarts(tmp_path):
    from sdilej_serialy.continuous import SourceUnavailable
    episode = Episode(1, 1, 'Series', None, 1, 1)
    state = EpisodeState(tmp_path / 'state.json')
    for _ in range(3):
        state.failure(episode, SourceUnavailable())
    state.row(episode)['attempts'][-1]['at'] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    state.save()
    restored = EpisodeState(state.path)
    assert episode.identity in restored.retry_deferred_identities()
    restored.row(episode)['attempts'][-1]['at'] = (datetime.now(UTC) - timedelta(minutes=61)).isoformat()
    assert not restored.retry_deferred_identities()


def test_different_failure_does_not_inherit_source_backoff(tmp_path):
    from sdilej_serialy.continuous import SourceUnavailable
    episode = Episode(1, 1, 'Series', None, 1, 1)
    state = EpisodeState(tmp_path / 'state.json')
    for _ in range(3):
        state.failure(episode, SourceUnavailable())
    state.failure(episode, TimeoutError())
    state.row(episode)['attempts'][-1]['at'] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    assert not state.retry_deferred_identities()


def test_deferred_episode_does_not_consume_queue_limit(tmp_path, monkeypatch):
    from argparse import Namespace
    from sdilej_serialy import cli
    from sdilej_serialy.manifest import SourceManifest
    from sdilej_serialy.quality import QUALITY_POLICY

    failed = Episode(1, 1, 'Series', None, 1, 1)
    ready = Episode(2, 1, 'Series', None, 1, 2)
    state_path = tmp_path / 'state.json'
    EpisodeState(state_path).failure(failed, TimeoutError())
    manifest_path = tmp_path / 'manifest.jsonl'
    manifest = SourceManifest(manifest_path)
    for episode in (failed, ready):
        manifest.add(dict(identity=episode.identity, episode=episode.to_dict(),
                          quality_policy=QUALITY_POLICY,
                          selected=dict(url='https://sdilej.cz/1/video', language_tier='czech_audio')))
    manifest.save()
    monkeypatch.setenv('CONTINUOUS_ENABLED', 'true')
    monkeypatch.setattr(cli, 'require_env', lambda name: 'test')

    def upload(rows, state, **kwargs):
        assert [r['identity'] for r in rows] == [ready.identity]
        assert [r['identity'] for r in kwargs['refill_rows']()] == [ready.identity]
        return {'queued': 1}

    monkeypatch.setattr(cli, 'upload_continuously', upload)
    cli.continuous(Namespace(state=state_path, manifest=manifest_path, limit=1,
                            workers=1, persist_git_state=False, report=tmp_path / 'report.json'))

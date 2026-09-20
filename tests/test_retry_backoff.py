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

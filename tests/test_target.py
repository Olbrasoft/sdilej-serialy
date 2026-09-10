from sdilej_serialy.target import episode_key, listing_rows, existing_episode
from sdilej_serialy.pipeline import EpisodeState
from sdilej_serialy.models import Episode
from types import SimpleNamespace
from sdilej_serialy import continuous
from sdilej_to_prehrajto.models import Candidate


def test_target_sanitized_subtitle_does_not_change_identity():
    wanted = 'Zoufalé manželky S03E06 - Miláčku, musím se ti přiznat... 1080p CZ Dabing'
    actual = 'Zoufalé manželky S03E06 - Miláčku, musím se ti přiznat 1080p CZ Dabing.mkv (Zpracovává se)'
    html = f'<section><h3>{actual}</h3><a href="?uploadedVideoListing-videoId=123&do=uploadedVideoListing-deleteVideo">Delete</a></section>'
    response = SimpleNamespace(text=html, raise_for_status=lambda: None)
    session = SimpleNamespace(get=lambda *a, **kw: response)
    assert existing_episode(session, wanted) == '123'
    assert listing_rows(html)[0]['processing']
    assert episode_key(wanted) != episode_key(wanted.replace('S03E06', 'S03E060'))


def test_uncertain_target_survives_failure_restart_and_orphan_release(tmp_path, monkeypatch):
    item = Episode(1, 2, 'Series', None, 3, 6)
    path = tmp_path / 'state.json'
    state = EpisodeState(path)
    state.row(item)['prepared_target'] = {'target_video_id': '123'}
    state.failure(item, TimeoutError())
    restarted = EpisodeState(path)
    monkeypatch.setenv('GITHUB_RUN_ID', 'next-run')
    restarted.release_orphaned_claims()
    assert restarted.row(item)['prepared_target']['target_video_id'] == '123'


def test_retry_never_creates_second_target_when_listing_is_delayed(tmp_path, monkeypatch):
    item = Episode(1, 2, 'Series', None, 3, 6)
    candidate = Candidate(source_id='1', url='https://sdilej.cz/1/video', title='Series S03E06')
    row = dict(identity=item.identity, episode=item.to_dict(), selected=candidate.to_dict(),
               display_name='Series S03E06 - Title')
    provider = SimpleNamespace(session=object(), refresh=lambda c, **kw: c)
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: provider)
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda *a: 0)
    created = []

    def relay(*a, on_prepared):
        created.append('123')
        on_prepared('123', 100)
        raise TimeoutError('Target response lost')

    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    path = tmp_path / 'state.json'
    for _ in range(3):
        continuous.upload_continuously([row], EpisodeState(path), workers=1,
            source_email='s', source_password='x', target_email='t', target_password='x')
    assert created == ['123']


def test_different_database_ids_cannot_upload_same_named_episode(tmp_path):
    a = Episode(1, 2, 'Series', None, 3, 6)
    b = Episode(2, 99, 'Series', None, 3, 6)
    state = EpisodeState(tmp_path / 'state.json')
    assert state.claim(a, 'one')
    assert not state.claim(b, 'two')
    state.success(a, '123', 'Series S03E06 - Subtitle')
    assert not state.claim(b, 'two')
    assert state.row(b)['upload']['target_video_id'] == '123'

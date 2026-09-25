import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from sdilej_serialy import dual, pipeline, continuous
from sdilej_serialy.models import Episode
from sdilej_serialy.quality import QUALITY_POLICY
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier


def source(series=1, number=1, size=100, height=1080):
    ep = Episode(series * 100 + number, series, f'Series {series}', None, 1, number)
    candidate = Candidate(source_id=str(series * 100 + number), url=f'https://sdilej.cz/{series * 100 + number}/video',
        title=ep.code, filename='episode.mkv', size_bytes=size, width=1280 if height == 720 else 1920, height=height,
        duration_sec=100, language_tier=LanguageTier.CZECH_AUDIO, match_tier=MatchTier.STRONG,
        audio_language='cs', language_probability=.99)
    return dict(episode=ep.to_dict(), identity=ep.identity, selected=candidate.to_dict(),
                display_name=f'{ep.series_title} {ep.code} 1080p CZ Dabing', quality_policy=QUALITY_POLICY)


def setup_plan(tmp_path, monkeypatch, count=8):
    rows = [source(number=i) for i in range(1, count + 1)]
    manifest = tmp_path / 'sources.jsonl'
    manifest.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    catalog = tmp_path / 'catalog.jsonl'
    catalog.write_text(json.dumps(dict(series_id=1, imdb_rating=9.4, imdb_votes=100)) + '\n')
    dual.prepare(tmp_path, 'test', manifest, catalog, ['a@test.cz', 'b@test.cz'])
    monkeypatch.setenv('DUAL_ENABLED', 'true')
    for alias in dual.ACCOUNTS:
        monkeypatch.setenv(f'PREHRAJTO_{alias.upper()}_EMAIL', f'{alias}@test.cz')
        monkeypatch.setenv(f'PREHRAJTO_{alias.upper()}_PASSWORD', 'password')
    monkeypatch.setenv('SDILEJ_EMAIL', 'source')
    monkeypatch.setenv('SDILEJ_PASSWORD', 'password')
    return dual.load(tmp_path, 'test')


def test_imdb_order_keeps_series_together_and_owners_alternate():
    rows = [source(2, 2), source(1, 2), source(2, 1), source(1, 1), source(3)]
    catalog = [dict(series_id=1, imdb_rating=8, imdb_votes=999999),
               dict(series_id=2, imdb_rating=9, imdb_votes=10)]
    result = dual.ranked_sources(rows, catalog)
    assert [r['identity'] for r in result] == ['2:1:1', '2:1:2', '1:1:1', '1:1:2', '3:1:1']
    assert [r['target_account'] for r in result] == ['a', 'b', 'a', 'b', 'a']
    assert result[-1]['imdb_rating'] is None


def test_only_current_verified_czech_sources_and_no_history_filter():
    rows = [source(number=i) for i in range(1, 6)]
    rows[1]['selected']['audio_language'] = 'sk'
    rows[2]['quality_policy'] = 'legacy'
    rows[3]['selected']['language_probability'] = .4
    rows[4]['selected']['language_tier'] = 'foreign_audio'
    assert len(dual.ranked_sources(rows, [])) == 1


def test_aliases_deduplicate_using_quality_then_smallest_size():
    rows = [source(1, size=20, height=720), source(2, size=200), source(3, size=100)]
    for row in rows:
        row['display_name'] = 'Same Series S01E01 1080p CZ Dabing'
    result = dual.ranked_sources(rows, [])
    assert len(result) == 1 and result[0]['identity'] == '3:1:1'


def test_one_source_cannot_feed_different_episodes():
    rows = [source(number=1), source(number=2)]
    rows[1]['selected']['source_id'] = rows[0]['selected']['source_id']
    with pytest.raises(ValueError, match='multiple episodes'):
        dual.ranked_sources(rows, [])


def test_authenticated_download_urls_never_enter_frozen_queue():
    row = source()
    row['selected']['download_url'] = 'https://example.test/private-download'
    with pytest.raises(ValueError, match='Authenticated source URLs'):
        dual.ranked_sources([row], [])


@pytest.mark.parametrize('tamper', ['manifest', 'account', 'state_owner'])
def test_frozen_manifest_and_accounts_cannot_change(tmp_path, monkeypatch, tamper):
    directory, plan, rows = setup_plan(tmp_path, monkeypatch)
    if tamper == 'manifest':
        with (directory / 'manifest.jsonl').open('a') as output:
            output.write('\n')
    elif tamper == 'account':
        plan['account_hashes']['a'] = dual.account_digest('wrong@test.cz')
        pipeline.atomic_json(directory / 'plan.json', plan)
    else:
        state = pipeline.EpisodeState(directory / 'state.json')
        state.data['episodes'][rows[0]['identity']] = dict(target_account='b')
        state.save()
    with pytest.raises(ValueError):
        dual.load(tmp_path, 'test')


def test_old_account_credentials_are_rejected(tmp_path, monkeypatch):
    _, plan, _ = setup_plan(tmp_path, monkeypatch)
    monkeypatch.setenv('PREHRAJTO_A_EMAIL', 'share.series@email.cz')
    with pytest.raises(RuntimeError, match='frozen plan'):
        dual.credentials(plan)


def test_generation_cannot_be_reset(tmp_path, monkeypatch):
    setup_plan(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='never reset'):
        dual.prepare(tmp_path, 'test', tmp_path / 'sources.jsonl', tmp_path / 'catalog.jsonl',
                     ['a@test.cz', 'b@test.cz'])


def test_full_mode_cannot_skip_pilot(tmp_path, monkeypatch):
    setup_plan(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match='pilot'):
        dual.run(tmp_path, 'test', 'full')


def test_shared_claim_is_durable_and_exclusive(tmp_path, monkeypatch):
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    state = dual.SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    episode = Episode.from_dict(rows[0]['episode'])
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda i: state.claim(episode, str(i)), range(4)))
    assert sum(results) == 1
    assert json.loads(state.path.read_text())['episodes'][episode.identity]['target_account'] == 'a'


def test_target_login_guard_is_serialized_and_restored(monkeypatch):
    original = pipeline.prehrajto.EXPECTED_EMAIL
    def login(email, password):
        time.sleep(.005)
        assert pipeline.prehrajto.EXPECTED_EMAIL == email
        return email
    monkeypatch.setattr(pipeline.prehrajto, 'login', login)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda e: pipeline.target_session(e, 'x', expected_email=e),
                                    ['a@test.cz', 'b@test.cz'] * 2))
    assert results == ['a@test.cz', 'b@test.cz'] * 2
    assert pipeline.prehrajto.EXPECTED_EMAIL == original


def stub_live(monkeypatch):
    videos = {a: {} for a in dual.ACCOUNTS}
    sessions = {}
    for alias in dual.ACCOUNTS:
        def get(url, params, alias=alias, **kwargs):
            html = '<div id="uploadedVideoListing">'
            for video_id, name in videos[alias].items():
                if name.startswith(params['searchPhrase']):
                    html += f'<section><h3>{name}</h3><a href="?uploadedVideoListing-videoId={video_id}&amp;do=uploadedVideoListing-deleteVideo">Delete</a></section>'
            return SimpleNamespace(text=html + '</div>', raise_for_status=lambda: None)
        sessions[alias] = SimpleNamespace(alias=alias, get=get, close=lambda: None)
    monkeypatch.setattr(dual, 'target_session', lambda email, *a, **k: sessions[email[0]])
    monkeypatch.setattr(dual.prehrajto, 'uploaded_video_count', lambda s: len(videos[s.alias]))
    return videos, sessions


def test_pilot_then_restart_uploads_only_remaining_rows(tmp_path, monkeypatch):
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    videos, sessions = stub_live(monkeypatch)
    calls = []
    def upload(batch, state, **kwargs):
        alias = kwargs['target_login']().alias
        assert kwargs['workers'] == 2 and kwargs['require_original_size']
        for row in batch:
            assert row['target_account'] == alias
            episode = Episode.from_dict(row['episode'])
            assert state.claim(episode, alias)
            video_id = str(row['queue_rank'])
            videos[alias][video_id] = row['display_name']
            state.success(episode, video_id, row['display_name'])
            calls.append(row['identity'])
        return {'uploaded_or_reconciled': len(batch)}
    monkeypatch.setattr(dual, 'upload_continuously', upload)
    pilot = dual.run(tmp_path, 'test', 'pilot')
    assert pilot['completed_by_account'] == {'a': 2, 'b': 2}
    assert pipeline.EpisodeState(directory / 'state.json').data['pilot_verified_at']
    result = dual.run(tmp_path, 'test', 'full')
    assert result['completed_by_account'] == {'a': 4, 'b': 4}
    dual.run(tmp_path, 'test', 'full')
    assert len(calls) == len(set(calls)) == 8
    videos['a'].clear()
    with pytest.raises(RuntimeError, match='lost confirmed'):
        dual.run(tmp_path, 'test', 'full')
    assert len(calls) == 8


def test_pilot_rejects_nonempty_accounts(tmp_path, monkeypatch):
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    videos, _ = stub_live(monkeypatch)
    videos['b']['123'] = 'existing'
    monkeypatch.setattr(dual, 'upload_continuously', lambda *a, **k: pytest.fail('Unexpected transfer'))
    with pytest.raises(RuntimeError, match='empty accounts'):
        dual.run(tmp_path, 'test', 'pilot')
    assert pipeline.EpisodeState(directory / 'state.json').data['halted_at']


def test_failed_transfer_halts_both_accounts_and_all_future_runs(tmp_path, monkeypatch):
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    stub_live(monkeypatch)
    def upload(batch, state, **kwargs):
        kwargs['stop_event'].set()
        return {}
    monkeypatch.setattr(dual, 'upload_continuously', upload)
    with pytest.raises(RuntimeError, match='both account queues'):
        dual.run(tmp_path, 'test', 'pilot')
    with pytest.raises(RuntimeError, match='manual review'):
        dual.run(tmp_path, 'test', 'pilot')


def test_cross_account_duplicate_rejects_pilot(tmp_path, monkeypatch):
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    videos, sessions = stub_live(monkeypatch)
    state = dual.SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    for row in rows[:4]:
        episode = Episode.from_dict(row['episode'])
        state.claim(episode, 'test')
        state.success(episode, str(row['queue_rank']), row['display_name'])
        videos[row['target_account']][str(row['queue_rank'])] = row['display_name']
    videos['b']['999'] = rows[0]['display_name']
    with pytest.raises(RuntimeError, match='wrong account'):
        dual.verify_pilot(rows, state, sessions)


def test_local_process_lock(tmp_path, monkeypatch):
    import fcntl
    directory, _, _ = setup_plan(tmp_path, monkeypatch)
    with (directory / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='Another process'):
            dual.run(tmp_path, 'test', 'pilot')


def test_real_workers_never_exceed_two_per_account(tmp_path, monkeypatch):
    from sdilej_serialy import source_detail
    directory, _, rows = setup_plan(tmp_path, monkeypatch)
    videos, sessions = stub_live(monkeypatch)
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: SimpleNamespace(
        session=object(), refresh=lambda c, **k: c))
    monkeypatch.setattr(source_detail, 'resolve_original', lambda s, c: c)
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous, 'target_confirmed', lambda *a: True)
    active = dict(a=0, b=0)
    peak = dict(a=0, b=0)
    lock = threading.Lock()
    barrier = threading.Barrier(4)
    calls = []
    def relay(target, source, candidate, name, description, on_prepared):
        alias = target.alias
        with lock:
            active[alias] += 1
            peak[alias] = max(peak[alias], active[alias])
            calls.append((alias, candidate.source_id))
        on_prepared(candidate.source_id, candidate.size_bytes)
        barrier.wait(timeout=5)
        with lock:
            videos[alias][candidate.source_id] = name
            active[alias] -= 1
        return SimpleNamespace(video_id=candidate.source_id)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    result = dual.run(tmp_path, 'test', 'pilot')
    assert result['completed_by_account'] == {'a': 2, 'b': 2}
    assert peak == {'a': 2, 'b': 2}
    assert len({c[1] for c in calls}) == 4
    assert {c[1] for c in calls if c[0] == 'a'} == {'101', '103'}
    assert {c[1] for c in calls if c[0] == 'b'} == {'102', '104'}

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier

from sdilej_serialy import source_audit, continuous, source_detail
from sdilej_serialy.dual import SharedState
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import atomic_json
from sdilej_serialy.quality import QUALITY_POLICY


def row(number=1, height=720, language='cs'):
    episode = Episode(number, 10, 'Test', 'Test', 1, number, title='Episode title', runtime_min=25)
    candidate = Candidate(source_id=str(number), url=f'https://sdilej.cz/{number}/video',
        title=f'Test S01E{number:02d}', filename='episode.mkv', size_bytes=100, duration_sec=1500,
        width=1920 if height >= 1080 else 1280, height=height,
        language_tier=source_audit.language_tier(language), audio_language=language,
        language_probability=.99, match_tier=MatchTier.STRONG)
    return dict(identity=episode.identity, episode=episode.to_dict(), selected=candidate.to_dict(),
                display_name=f'Test S01E{number:02d} - Episode title 720p CZ Dabing', quality_policy=QUALITY_POLICY)


def upgraded(base, **kwargs):
    candidate = replace(Candidate.from_dict(base['selected']), source_id='100', url='https://sdilej.cz/100/video',
                        width=1920, height=1080, size_bytes=200, **kwargs)
    return dict(base, selected=candidate.to_dict())


def setup(tmp_path, rows):
    manifest = SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl')
    for item in rows:
        manifest.add(item)
    manifest.save()
    return manifest.path


def test_audit_includes_uploaded_episodes_but_leaves_uploads_and_frozen_plan_untouched(tmp_path):
    base = row()
    path = setup(tmp_path, [base, row(2, 1080)])
    frozen = tmp_path / 'dual/test/manifest.jsonl'
    frozen.parent.mkdir(parents=True)
    frozen.write_text('frozen source snapshot')
    state = frozen.parent / 'state.json'
    atomic_json(state, dict(episodes={base['identity']: dict(upload=dict(target_video_id='999'))}))
    previous_state = state.read_bytes()
    calls = []
    def discover(episode):
        calls.append(episode.identity)
        return Candidate.from_dict(upgraded(base)['selected'])
    result = source_audit.audit(tmp_path, SimpleNamespace(discover=discover))
    assert calls == [base['identity']]
    assert result['upgraded_this_run'] == 1
    selected = SourceManifest(path).rows[base['identity']]
    assert selected['selected']['height'] == 1080
    assert 'Episode title 1080p' in selected['display_name']
    assert SourceManifest(tmp_path / source_audit.UPGRADES_PATH).rows[base['identity']] == selected
    assert state.read_bytes() == previous_state
    assert frozen.read_text() == 'frozen source snapshot'


def test_new_source_preserves_account_order_and_episode_metadata():
    base = dict(row(), target_account='b', queue_rank=123, imdb_rating=9.0)
    replacement = dict(upgraded(base), target_account='a', queue_rank=1)
    result = source_audit.upgraded_row(base, replacement)
    assert result['target_account'] == 'b' and result['queue_rank'] == 123
    assert result['episode'] == base['episode']
    assert result['selected']['height'] == 1080


def test_foreign_1080p_never_replaces_czech_720p():
    base = row()
    replacement = upgraded(base, language_tier=LanguageTier.FOREIGN_AUDIO,
                           audio_language='en')
    assert source_audit.upgraded_row(base, replacement) == base


def test_verified_foreign_fallback_can_improve_existing_foreign_source():
    base = row(language='en')
    result = source_audit.upgraded_row(base, upgraded(base))
    assert result['selected']['height'] == 1080
    assert source_audit.upgraded_row(base, upgraded(base), czech_only=True) == base


@pytest.mark.parametrize('corruption', ['remake', 'wrong_episode', 'low_confidence', 'private_url'])
def test_invalid_upgrades_are_not_published(tmp_path, corruption):
    base = row()
    path = setup(tmp_path, [base])
    candidate = Candidate.from_dict(upgraded(base)['selected'])
    if corruption == 'remake':
        candidate.duration_sec = 3101
    elif corruption == 'wrong_episode':
        candidate.title = 'Test S01E02'
    elif corruption == 'low_confidence':
        candidate.language_probability = .5
    else:
        replacement = upgraded(base)
        replacement['selected']['download_url'] = 'https://example.test/private'
        with pytest.raises(ValueError, match='Authenticated source'):
            source_audit.upgraded_row(base, replacement)
        return
    result = source_audit.audit(tmp_path, SimpleNamespace(discover=lambda _: candidate))
    assert result['upgraded_this_run'] == 0
    assert SourceManifest(path).rows[base['identity']] == base
    audit_state = json.loads((tmp_path / 'audit/low-resolution/state.json').read_text())
    assert audit_state['episodes'][base['identity']]['status'] == 'deferred'


def test_1080p_is_never_researched_or_replaced():
    base = row(height=1080)
    assert not source_audit.low_resolution(base)
    assert source_audit.upgraded_row(base, {}) == base


def test_audit_resumes_without_rechecking_confirmed_best(tmp_path):
    base = row()
    setup(tmp_path, [base])
    calls = []
    def discover(episode):
        calls.append(episode.identity)
        return Candidate.from_dict(base['selected'])
    source_audit.audit(tmp_path, SimpleNamespace(discover=discover))
    result = source_audit.audit(tmp_path, SimpleNamespace(discover=discover))
    assert result['reviewed_this_run'] == 0 and calls == [base['identity']]
    result = source_audit.audit(tmp_path, SimpleNamespace(discover=discover), force=True)
    assert result['reviewed_this_run'] == 1 and len(calls) == 2


def test_inconclusive_search_retains_source_and_defers_retry(tmp_path):
    base = row()
    path = setup(tmp_path, [base])
    source_audit.audit(tmp_path, SimpleNamespace(discover=lambda _: None))
    state = json.loads((tmp_path / 'audit/low-resolution/state.json').read_text())
    record = state['episodes'][base['identity']]
    assert record['status'] == 'deferred'
    assert not source_audit.due(base, record, datetime.now(UTC))
    assert SourceManifest(path).rows[base['identity']] == base


def test_live_feed_refreshes_before_each_low_resolution_transfer():
    base = row()
    payload = ['']
    feed = source_audit.UpgradeFeed(lambda: payload[0])
    assert feed.select(base) == base
    payload[0] = json.dumps(upgraded(base)) + '\n'
    assert feed.select(base)['selected']['height'] == 1080
    assert base['selected']['height'] == 720


def test_two_equal_resolution_sources_prefer_smaller_file():
    base = row()
    small = dict(base, selected=replace(Candidate.from_dict(base['selected']), size_bytes=80,
                                       source_id='2', url='https://sdilej.cz/2/video').to_dict())
    assert source_audit.upgraded_row(base, small)['selected']['size_bytes'] == 80
    assert source_audit.upgraded_row(small, base) == small


def test_uploader_uses_upgrade_and_records_actual_source(tmp_path, monkeypatch):
    base = dict(row(), target_account='a')
    upgraded_candidate = Candidate.from_dict(upgraded(base)['selected'])
    state = SharedState(tmp_path / 'state.json', {base['identity']: 'a'})
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: SimpleNamespace(
        session=object(), refresh=lambda c, **kw: c))
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous, 'target_confirmed', lambda *a: True)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 1)
    monkeypatch.setattr(source_detail, 'resolve_original', lambda _, c: c)
    def relay(target, source, candidate, name, description, on_prepared):
        assert candidate.source_id == upgraded_candidate.source_id
        assert '1080p' in name
        on_prepared('555', candidate.size_bytes)
        return SimpleNamespace(video_id='555')
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    continuous.upload_continuously([base], state, workers=1, source_email='s', source_password='x',
        target_email='a', target_password='x', require_original_size=True,
        select_source=source_audit.UpgradeFeed(lambda: json.dumps(upgraded(base))).select)
    saved = state.data['episodes'][base['identity']]
    assert saved['target_account'] == 'a'
    assert saved['source']['height'] == 1080
    assert saved['upload']['target_video_id'] == '555'


def test_uncertain_upload_never_switches_source(tmp_path, monkeypatch):
    base = dict(row(), target_account='a')
    state = SharedState(tmp_path / 'state.json', {base['identity']: 'a'})
    episode = Episode.from_dict(base['episode'])
    state.row(episode)['prepared_target'] = {'target_video_id': '123'}
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: object())
    monkeypatch.setattr(continuous, 'target_session', lambda *a: object())
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda _: 1)
    continuous.upload_continuously([base], state, workers=1, source_email='s', source_password='x',
        target_email='a', target_password='x', require_original_size=True,
        select_source=lambda _: pytest.fail('An allocated upload must not be replaced'))
    assert state.row(episode)['prepared_target']['target_video_id'] == '123'


def test_cached_search_pages_do_not_cache_detail_pages_or_exact_episode_queries():
    visited = []
    session = SimpleNamespace(get=lambda url, **kwargs: (
        visited.append(url) or SimpleNamespace(text='results', raise_for_status=lambda: None)))
    provider = source_audit.AuditProvider(session, detector=object(), request_gap_seconds=0)
    for _ in range(2):
        provider._get('https://sdilej.cz/test/s/-6')
        provider._get('https://sdilej.cz/test-s01e01/s/-6')
        provider._get('https://sdilej.cz/123/test.mkv')
    assert visited.count('https://sdilej.cz/test/s/-6') == 1
    assert visited.count('https://sdilej.cz/test-s01e01/s/-6') == 2
    assert visited.count('https://sdilej.cz/123/test.mkv') == 2


def test_force_still_ignores_high_resolution(tmp_path):
    setup(tmp_path, [row(height=1080)])
    result = source_audit.audit(tmp_path, SimpleNamespace(discover=lambda _: pytest.fail('Unexpected search')),
                               force=True)
    assert result['reviewed_this_run'] == 0

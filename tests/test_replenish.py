import json
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier

from sdilej_serialy import dual, replenish, pipeline
from sdilej_serialy.catalog import write_jsonl_gzip, load_jsonl
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from test_dual import setup_plan, source, stub_live


def setup(tmp_path, monkeypatch):
    directory, plan, rows = setup_plan(tmp_path, monkeypatch, count=4)
    catalog = [dict(r['episode'], imdb_rating=9.0, imdb_votes=100) for r in rows]
    for n in (5, 6):
        catalog.append(dict(source(number=n)['episode'], imdb_rating=9.0, imdb_votes=100))
    write_jsonl_gzip(tmp_path / 'backlog/series-episodes.jsonl.gz', catalog)
    manifest = SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl')
    for r in rows:
        manifest.add(r)
    manifest.save()
    return directory, plan, rows


def discover(episode):
    candidate = Candidate.from_dict(source(number=episode.number)['selected'])
    return replace(candidate, title=f'{episode.series_title} {episode.code}')


def test_preparation_appends_unique_owned_rows_preserving_frozen_queue_and_history(tmp_path, monkeypatch):
    directory, plan, rows = setup(tmp_path, monkeypatch)
    snapshot = {name: (directory / name).read_bytes() for name in ('manifest.jsonl', 'plan.json', 'state.json')}
    calls = []
    def provider(episode):
        calls.append(episode.identity)
        return discover(episode)
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert result['prepared_this_run'] == 2
    for name, payload in snapshot.items():
        assert (directory / name).read_bytes() == payload
    _, _, extended = dual.load(tmp_path, 'test')
    assert extended[:4] == rows
    assert [r['queue_rank'] for r in extended[4:]] == [5, 6]
    assert [r['target_account'] for r in extended[4:]] == ['a', 'b']
    assert calls == ['1:1:5', '1:1:6']
    previous = (directory / 'additions.jsonl').read_bytes()
    again = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert again['attempted_this_run'] == 0
    assert (directory / 'additions.jsonl').read_bytes() == previous


@pytest.mark.parametrize('mode', ['foreign', 'inconclusive', 'wrong_episode', 'low_confidence', 'source_reuse'])
def test_unverified_or_reused_sources_are_not_added_and_retry_is_delayed(tmp_path, monkeypatch, mode):
    directory, _, _ = setup(tmp_path, monkeypatch)
    def provider(episode):
        candidate = discover(episode)
        if mode == 'inconclusive': return None
        if mode == 'foreign': return replace(candidate, language_tier=LanguageTier.FOREIGN_AUDIO, audio_language='en')
        if mode == 'wrong_episode': return replace(candidate, title='Series 1 S99E99')
        if mode == 'low_confidence': return replace(candidate, language_probability=.3)
        return replace(candidate, source_id='101')
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert result['prepared_this_run'] == 0
    assert not (directory / 'additions.jsonl').exists()
    assert replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)['attempted_this_run'] == 0


def test_existing_verified_source_is_reused_without_discovery(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    manifest = SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl')
    row = source(number=5)
    row['selected'] = discover(Episode.from_dict(row['episode'])).to_dict()
    manifest.add(row)
    manifest.save()
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=lambda _: pytest.fail('Unexpected search')),
                               limit=1, runtime_minutes=0)
    assert result['prepared_this_run'] == 1


def save_legacy(tmp_path, number=5, height=1080):
    manifest = SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl')
    row = source(number=number, height=height)
    row.pop('quality_policy')
    row['selected']['title'] = f"Series 1 S01E{number:02d}"
    manifest.add(row)
    manifest.save()
    return row


def test_legacy_hd_precedes_new_search_and_preserves_frozen_state(tmp_path, monkeypatch):
    directory, _, _ = setup(tmp_path, monkeypatch)
    saved = save_legacy(tmp_path, number=6)
    before = {name: (directory / name).read_bytes() for name in ('manifest.jsonl', 'plan.json', 'state.json')}
    calls = []
    def verify(ep, candidates):
        calls.append(ep.identity)
        assert candidates[0].source_id == saved['selected']['source_id']
        return replace(candidates[0], size_bytes=123456789)
    p = SimpleNamespace(revalidate_saved=verify, discover=lambda _: pytest.fail('Legacy sources have priority'))
    result = replenish.prepare(tmp_path, 'test', p, limit=1, runtime_minutes=0)
    assert result['prepared_this_run'] == 1 and calls == ['1:1:6']
    added = load_jsonl(directory / 'additions.jsonl')[0]
    assert added['selected']['size_bytes'] == 123456789
    assert added['quality_policy'] == replenish.QUALITY_POLICY
    assert added['source_review']['policy'] == 'saved-original-revalidation-v1'
    assert added['queue_rank'] == 5 and added['target_account'] == 'a'
    for name, payload in before.items(): assert (directory / name).read_bytes() == payload
    dual.load(tmp_path, 'test')


@pytest.mark.parametrize('result_kind', ['missing', 'foreign', 'wrong_episode', 'duplicate_source'])
def test_failed_saved_review_never_relables_or_enqueues_unverified_sources(tmp_path, monkeypatch, result_kind):
    directory, _, _ = setup(tmp_path, monkeypatch)
    saved = save_legacy(tmp_path)
    def verify(ep, candidates):
        c = candidates[0]
        if result_kind == 'missing': return None
        if result_kind == 'foreign': return replace(c, audio_language='en', language_tier=LanguageTier.FOREIGN_AUDIO)
        if result_kind == 'wrong_episode': return replace(c, title='Other S99E99')
        return replace(c, source_id='101')
    p = SimpleNamespace(revalidate_saved=verify, discover=lambda _: pytest.fail('No slow fallback in fast pass'))
    result = replenish.prepare(tmp_path, 'test', p, limit=1, runtime_minutes=0)
    assert result['prepared_this_run'] == 0
    assert not (directory / 'additions.jsonl').exists()
    assert SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl').rows[saved['identity']] == saved


def test_saved_review_bypasses_old_search_cooldown_once(tmp_path, monkeypatch):
    from datetime import datetime, UTC, timedelta
    setup(tmp_path, monkeypatch)
    saved = save_legacy(tmp_path)
    path = tmp_path / 'state/reserve-preparation.json'
    pipeline.atomic_json(path, dict(schema_version=1, episodes={saved['identity']: dict(
        status='deferred', retry_after=(datetime.now(UTC) + timedelta(hours=23)).isoformat())}))
    p = SimpleNamespace(revalidate_saved=lambda *a: None, discover=lambda _: pytest.fail('No search'))
    first = replenish.prepare(tmp_path, 'test', p, limit=1, runtime_minutes=0, identities=[saved['identity']])
    second = replenish.prepare(tmp_path, 'test', p, limit=1, runtime_minutes=0, identities=[saved['identity']])
    assert first['attempted_this_run'] == 1 and second['attempted_this_run'] == 0
    assert json.loads(path.read_text())['episodes'][saved['identity']]['saved_review_revision'] == 1


def test_legacy_low_resolution_uses_full_quality_search(tmp_path, monkeypatch):
    directory, _, _ = setup(tmp_path, monkeypatch)
    save_legacy(tmp_path, height=720)
    p = SimpleNamespace(revalidate_saved=lambda *a: pytest.fail('720p requires best-source search'), discover=discover)
    result = replenish.prepare(tmp_path, 'test', p, limit=1, runtime_minutes=0)
    assert result['prepared_this_run'] == 1
    assert load_jsonl(directory / 'additions.jsonl')[0]['selected']['height'] == 1080


def test_saved_episode_missing_from_cached_catalog_is_still_reviewed(tmp_path, monkeypatch):
    directory, _, _ = setup(tmp_path, monkeypatch)
    saved = save_legacy(tmp_path, number=99)
    p = SimpleNamespace(revalidate_saved=lambda ep, candidates: candidates[0],
                        discover=lambda _: pytest.fail('Review saved metadata first'))
    result = replenish.prepare(tmp_path, 'test', p, limit=1, runtime_minutes=0)
    assert result['prepared_this_run'] == 1
    assert load_jsonl(directory / 'additions.jsonl')[0]['identity'] == saved['identity']


def test_publisher_does_not_advance_or_duplicate_sources_during_checkpoint_retry(tmp_path, monkeypatch):
    from sdilej_serialy import git_state
    directory, _, _ = setup(tmp_path, monkeypatch)
    searched, persisted = [], []
    def provider(episode):
        searched.append(episode.identity)
        return discover(episode)
    def persist(path):
        persisted.append(path.read_bytes())
        if len(persisted) <= 2:
            assert searched == ['1:1:5']
        if len(persisted) == 1:
            raise git_state.CheckpointError('Busy remote')
    monkeypatch.setattr(replenish, 'GitCheckpointPersister', lambda *a, **k: persist)
    monkeypatch.setattr(git_state.time, 'sleep', lambda _: None)
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider),
                               runtime_minutes=0, persist=True)
    assert result['prepared_this_run'] == 2
    assert persisted[0] == persisted[1] and len(persisted) == 3
    assert searched == ['1:1:5', '1:1:6']
    assert [r['identity'] for r in load_jsonl(directory / 'additions.jsonl')] == searched


@pytest.mark.parametrize('field,value', [('identity', '1:1:1'), ('target_account', 'b'), ('queue_rank', 2),
    ('generation', 'another'), ('base_manifest_sha256', 'wrong')])
def test_invalid_append_is_rejected_by_uploader(tmp_path, monkeypatch, field, value):
    directory, _, _ = setup(tmp_path, monkeypatch)
    replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=discover), limit=1, runtime_minutes=0)
    path = directory / 'additions.jsonl'
    row = load_jsonl(path)[0]
    row[field] = value
    path.write_text(json.dumps(row) + '\n')
    with pytest.raises(ValueError):
        dual.load(tmp_path, 'test')


def test_catalog_prefers_imdb_then_season_episode():
    common = dict(imdb_votes=100, season=1)
    rows = [dict(common, series_id=1, episode=2, imdb_rating=8),
            dict(common, series_id=2, episode=1, imdb_rating=9),
            dict(common, series_id=1, episode=1, imdb_rating=8)]
    assert [(r['series_id'],r['episode']) for r in sorted(rows,key=replenish.catalog_order)] == [(2,1),(1,1),(1,2)]


def test_due_retries_cannot_monopolize_fresh_searches():
    rows = [source(number=n)['episode'] for n in range(1, 25)]
    records = {Episode.from_dict(r).identity: dict(status='deferred') for r in rows[:4]}
    ordered = list(replenish.preparation_order(rows, records))
    numbers = [r['episode'] for r in ordered]
    assert numbers[:10] == list(range(5, 13)) + [1, 2]
    assert numbers[10:20] == list(range(13, 21)) + [3, 4]
    assert sorted(numbers) == list(range(1, 25))


def test_paused_series_do_not_consume_the_fresh_lane_budget():
    first = [source(number=n)['episode'] for n in range(1, 25)]
    next_series = dict(source(number=25)['episode'], series_id=2)
    paused = set()
    ordered = replenish.preparation_order(first + [next_series], {}, paused)
    assert next(ordered) == first[0]
    paused.add(1)
    assert list(ordered) == [next_series]


def test_unavailable_series_yields_to_another_series_without_marking_unsearched_episodes(tmp_path, monkeypatch):
    directory, _, rows = setup(tmp_path, monkeypatch)
    catalog = [dict(source(number=n)['episode'], imdb_rating=9, imdb_votes=100) for n in range(1, 15)]
    catalog.append(dict(source(number=15)['episode'], series_id=2, series_title='Other',
                        series_original_title='Other', imdb_rating=8, imdb_votes=100))
    write_jsonl_gzip(tmp_path / 'backlog/series-episodes.jsonl.gz', catalog)
    calls = []
    def provider(episode):
        calls.append(episode.identity)
        return None if episode.series_id == 1 else discover(episode)
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert result['prepared_this_run'] == 1
    assert calls == ['1:1:5', '1:1:6', '1:1:7', '2:1:15']
    state = json.loads((tmp_path / 'state/reserve-preparation.json').read_text())
    assert '1:1:8' not in state['episodes']
    assert load_jsonl(directory / 'additions.jsonl')[0]['identity'] == '2:1:15'
    # The next run tries the next unsearched episodes, not a permanent series ban.
    calls.clear()
    replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert calls == ['1:1:8', '1:1:9', '1:1:10']


def test_success_resets_series_miss_budget(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    catalog = [dict(source(number=n)['episode'], imdb_rating=9, imdb_votes=100) for n in range(1, 11)]
    write_jsonl_gzip(tmp_path / 'backlog/series-episodes.jsonl.gz', catalog)
    calls = []
    def provider(episode):
        calls.append(episode.number)
        return discover(episode) if episode.number in (7, 10) else None
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert result['prepared_this_run'] == 2 and calls == list(range(5, 11))


def test_uploader_reads_additions_and_does_not_replay_existing_episodes(tmp_path, monkeypatch):
    directory, _, rows = setup(tmp_path, monkeypatch)
    videos, sessions = stub_live(monkeypatch)
    state = pipeline.EpisodeState(directory / 'state.json')
    state.data.update(initialized_at=pipeline.now_iso(), pilot_verified_at=pipeline.now_iso())
    for r in rows:
        ep = Episode.from_dict(r['episode'])
        state.row(ep)['target_account'] = r['target_account']
        state.success(ep, r['selected']['source_id'], r['display_name'])
        videos[r['target_account']][r['selected']['source_id']] = r['display_name']
    replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=discover), runtime_minutes=0)
    seen=[]
    def upload(batch, state, **kwargs):
        for r in batch:
            assert r['identity'] in ('1:1:5','1:1:6')
            ep=Episode.from_dict(r['episode'])
            assert state.claim(ep, 'test')
            state.success(ep,r['selected']['source_id'],r['display_name'])
            videos[r['target_account']][r['selected']['source_id']]=r['display_name']
            seen.append(r['identity'])
        return {}
    monkeypatch.setattr(dual,'upload_continuously',upload)
    report=dual.run(tmp_path,'test','full')
    assert report['completed']==6 and report['remaining']==0
    dual.run(tmp_path,'test','full')
    assert sorted(seen)==['1:1:5','1:1:6']


def test_preparation_workflow_has_no_production_db_or_target_credentials():
    text = Path('.github/workflows/prepare-reserve.yml').read_text()
    assert 'DATABASE_URL' not in text and 'CR_VPS' not in text and 'PREHRAJTO_' not in text
    assert 'sdilej-serialy-source-preparation' in text


def test_dependency_bug_records_are_retried_once_without_waiting_a_day(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta
    setup(tmp_path, monkeypatch)
    retry_after = (datetime.now(UTC) + timedelta(hours=23)).isoformat()
    pipeline.atomic_json(tmp_path / 'state/reserve-preparation.json', {
        'schema_version': 1, 'episodes': {
            '1:1:5': dict(status='deferred', reason='TypeError', retry_after=retry_after),
            '1:1:6': dict(status='deferred', reason='no_verified_czech_match', retry_after=retry_after)}})
    calls = []
    def provider(episode):
        calls.append(episode.identity)
        return discover(episode)
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)
    assert result['prepared_this_run'] == 1
    assert calls == ['1:1:5']
    assert replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), runtime_minutes=0)['attempted_this_run'] == 0


def test_targeted_acceptance_uses_normal_quality_and_duplicate_guards(tmp_path, monkeypatch):
    directory, _, _ = setup(tmp_path, monkeypatch)
    calls = []
    def provider(episode):
        calls.append(episode.identity)
        return discover(episode)
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider),
                               identities=['1:1:6'], runtime_minutes=0)
    assert calls == ['1:1:6'] and result['prepared_this_run'] == 1
    assert load_jsonl(directory / 'additions.jsonl')[0]['queue_rank'] == 5
    assert replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider),
                             identities=['1:1:6'], runtime_minutes=0)['attempted_this_run'] == 0
    with pytest.raises(ValueError, match='not in the cached catalog'):
        replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=provider), identities=['1:1:999'])


def test_programming_failure_is_durable_and_fails_job_without_false_success(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    def broken(episode):
        raise TypeError('Sensitive error details must not enter the report')
    with pytest.raises(RuntimeError, match='code or dependency failure'):
        replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=broken), runtime_minutes=0)
    payload = (tmp_path / 'state/reserve-preparation.json').read_text()
    assert 'Sensitive' not in payload
    record = json.loads(payload)['episodes']['1:1:5']
    assert record['reason'] == 'TypeError'
    assert record['preparation_revision'] == replenish.PREPARATION_REVISION
    assert record['error']['frames'][-1]['function'] == 'broken'
    assert not (tmp_path / 'dual/test/additions.jsonl').exists()
    # The migration bypass is one-time, not a repeated exception loop.
    assert replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=discover), limit=1,
                             runtime_minutes=0)['prepared_this_run'] == 1
    assert load_jsonl(tmp_path / 'dual/test/additions.jsonl')[0]['identity'] == '1:1:6'

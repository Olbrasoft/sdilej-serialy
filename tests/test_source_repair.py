import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier

from sdilej_serialy import continuous, dual, pipeline, replenish, reserve, source_detail
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.source_repair import SourceRepairFeed, repair_entry, repaired_row
from test_dual import setup_plan, source
from test_replenish import setup


def replacement(base, source_id='999', height=1080):
    ep = Episode.from_dict(base['episode'])
    candidate = replace(Candidate.from_dict(base['selected']), source_id=source_id,
                        url=f'https://sdilej.cz/{source_id}/video',
                        title=f'{ep.series_title} {ep.code}', height=height,
                        width=1280 if height == 720 else 1920)
    return dict(base, selected=candidate.to_dict())


def failed(directory, row, **extra):
    path = directory / 'state.json'
    state = json.loads(path.read_text())
    state['episodes'][row['identity']] = dict(target_account=row['target_account'],
        attempts=[dict(at=pipeline.now_iso(), error='SourceUnavailable')], **extra)
    pipeline.atomic_json(path, state)


def test_failed_high_resolution_source_is_rediscovered_without_changing_queue_or_history(tmp_path, monkeypatch):
    directory, plan, rows = setup(tmp_path, monkeypatch)
    base = rows[0]
    failed(directory, base)
    before = {name: (directory / name).read_bytes() for name in ('manifest.jsonl', 'plan.json', 'state.json')}
    calls = []
    def discover(ep):
        calls.append(ep.identity)
        return Candidate.from_dict(replacement(base)['selected'])
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=lambda _: pytest.fail('Stale search'),
                                                               discover_fresh=discover),
                               identities=[base['identity']], runtime_minutes=0)
    assert calls == [base['identity']]
    assert result['repaired_this_run'] == 1 and result['prepared_this_run'] == 0
    for name, content in before.items():
        assert (directory / name).read_bytes() == content
    assert not (directory / 'additions.jsonl').exists()
    entry = json.loads((directory / 'source-repairs.jsonl').read_text())
    assert repaired_row(base, entry, plan)['target_account'] == base['target_account']
    assert SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl').rows[base['identity']]['selected']['source_id'] == '999'
    again = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=lambda _: pytest.fail('Already ready')),
                               identities=[base['identity']], runtime_minutes=0)
    assert again['attempted_this_run'] == 0


@pytest.mark.parametrize('protected', ['upload', 'prepared_target', 'claim'])
def test_uploaded_allocated_and_active_episodes_are_never_repaired(tmp_path, monkeypatch, protected):
    directory, _, rows = setup(tmp_path, monkeypatch)
    failed(directory, rows[0], **{protected: dict(target_video_id='123', worker_id='active')})
    before = (directory / 'state.json').read_bytes()
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=lambda _: pytest.fail('Protected episode')),
                               identities=[rows[0]['identity']], runtime_minutes=0)
    assert result['repaired_this_run'] == 0
    assert not (directory / 'source-repairs.jsonl').exists()
    assert (directory / 'state.json').read_bytes() == before


def test_claim_during_discovery_prevents_publishing_replacement(tmp_path, monkeypatch):
    directory, _, rows = setup(tmp_path, monkeypatch)
    failed(directory, rows[0])
    def discover(ep):
        failed(directory, rows[0], claim=dict(worker_id='active'))
        return Candidate.from_dict(replacement(rows[0])['selected'])
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=discover),
                               identities=[rows[0]['identity']], runtime_minutes=0)
    assert result['repaired_this_run'] == 0
    assert not (directory / 'source-repairs.jsonl').exists()


@pytest.mark.parametrize('kind', ['inconclusive', 'foreign', 'duplicate_source'])
def test_bad_repair_keeps_original_and_defers_retry(tmp_path, monkeypatch, kind):
    directory, _, rows = setup(tmp_path, monkeypatch)
    failed(directory, rows[0])
    original = SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl').rows[rows[0]['identity']]
    candidate = Candidate.from_dict(replacement(rows[0])['selected'])
    if kind == 'inconclusive': candidate = None
    if kind == 'foreign': candidate = replace(candidate, audio_language='en', language_tier=LanguageTier.FOREIGN_AUDIO)
    if kind == 'duplicate_source': candidate = replace(candidate, source_id=rows[1]['selected']['source_id'])
    result = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=lambda _: candidate),
                               identities=[rows[0]['identity']], runtime_minutes=0)
    assert result['repaired_this_run'] == 0
    assert SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl').rows[rows[0]['identity']] == original
    assert not (directory / 'source-repairs.jsonl').exists()
    again = replenish.prepare(tmp_path, 'test', SimpleNamespace(discover=lambda _: pytest.fail('Backoff')),
                               identities=[rows[0]['identity']], runtime_minutes=0)
    assert again['attempted_this_run'] == 0


@pytest.mark.parametrize('tamper', ['foreign', 'wrong_episode', 'stale', 'generation', 'private_url', 'signed_detail', 'duplicate_source'])
def test_feed_rejects_unverified_mismatched_or_reused_sources(tmp_path, monkeypatch, tamper):
    _, plan, rows = setup_plan(tmp_path, monkeypatch)
    entry = repair_entry(rows[0], replacement(rows[0]), plan)
    if tamper == 'foreign': entry['selected']['audio_language'] = 'en'
    if tamper == 'wrong_episode': entry['selected']['title'] = 'Series 1 S01E02'
    if tamper == 'stale': entry['source_repair']['previous_fingerprint'] = 'wrong'
    if tamper == 'generation': entry['source_repair']['generation'] = 'wrong'
    if tamper == 'private_url': entry['selected']['download_url'] = 'https://example.test/private'
    if tamper == 'signed_detail': entry['selected']['url'] += '?token=private'
    if tamper == 'duplicate_source': entry['selected']['source_id'] = rows[1]['selected']['source_id']
    with pytest.raises(ValueError):
        SourceRepairFeed(plan, rows, lambda: json.dumps(entry)).refresh()


def test_fresh_repair_bypasses_old_backoff_once_without_erasing_attempts(tmp_path, monkeypatch):
    directory, plan, rows = setup_plan(tmp_path, monkeypatch)
    base = rows[0]
    failed(directory, base)
    entry = repair_entry(base, replacement(base, height=720), plan)
    feed = SourceRepairFeed(plan, rows, lambda: json.dumps(entry))
    feed.refresh()
    state = dual.SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    state.source_repairs = feed
    assert base['identity'] not in state.retry_deferred_identities()
    assert reserve.stock(rows, state.data, fresh_sources=feed.ready(state.data))['ready'] == len(rows)
    ep = Episode.from_dict(base['episode'])
    assert state.claim(ep, 'worker')
    assert not state.claim(ep, 'second-worker')
    state.row(ep)['source_repair_token'] = entry['source_repair']['token']
    state.failure(ep, continuous.SourceUnavailable('Still unavailable'))
    assert base['identity'] in state.retry_deferred_identities()
    assert len(state.row(ep)['attempts']) == 2
    assert not feed.ready(state.data)
    assert reserve.stock(rows, state.data, fresh_sources=feed.ready(state.data))['ready'] == len(rows) - 1


def test_uploader_consumes_repair_only_after_claim_and_records_actual_source(tmp_path, monkeypatch):
    directory, plan, rows = setup_plan(tmp_path, monkeypatch, count=4)
    base = rows[0]
    failed(directory, base)
    state = dual.SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    entry = repair_entry(base, replacement(base), plan)
    feed = SourceRepairFeed(plan, rows, lambda: json.dumps(entry))
    feed.refresh()
    state.source_repairs = feed
    monkeypatch.setattr(continuous.EpisodeSourceProvider, 'authenticated', lambda *a: SimpleNamespace(
        session=object(), refresh=lambda c, **k: c))
    monkeypatch.setattr(source_detail, 'resolve_original', lambda s, c: c)
    monkeypatch.setattr(continuous, 'existing_episode', lambda *a: None)
    monkeypatch.setattr(continuous, 'target_confirmed', lambda *a: True)
    monkeypatch.setattr(continuous.prehrajto, 'uploaded_video_count', lambda *a: 1)
    def select(row):
        assert state.row(Episode.from_dict(row['episode']))['claim']
        return feed.select(row)
    def relay(target, session, candidate, name, description, on_prepared):
        record = state.row(Episode.from_dict(base['episode']))
        assert candidate.source_id == record['source']['source_id'] == '999'
        assert record['source_repair_token'] == entry['source_repair']['token']
        on_prepared('123', candidate.size_bytes)
        return SimpleNamespace(video_id='123')
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    continuous.upload_continuously([base], state, workers=2, source_email='x', source_password='x',
        target_email='x', target_password='x', require_original_size=True,
        target_login=lambda: object(), select_source=select)
    assert state.row(Episode.from_dict(base['episode']))['upload']['target_video_id'] == '123'
    assert not feed.ready(state.data)


def test_source_detail_failure_is_not_misreported_as_missing_premium():
    candidate = Candidate.from_dict(replacement(source())['selected'])
    with pytest.raises(source_detail.sdilej.SdilejError, match='temporarily unavailable'):
        source_detail.parse_detail_html('<h1>Video</h1><p>Detail souboru se nepodařilo načíst (dočasně nedostupné).</p>', candidate)


def test_idle_workers_pick_up_new_repairs_without_reassigning_or_repeating_rows(tmp_path, monkeypatch):
    directory, plan, rows = setup_plan(tmp_path, monkeypatch)
    for row in rows:
        failed(directory, row)
    entries = []
    feed = SourceRepairFeed(plan, rows, lambda: '\n'.join(json.dumps(e) for e in entries))
    feed.refresh()
    state = dual.SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    state.source_repairs = feed
    reserve_feed = dual.LiveReserve(tmp_path, 'test', rows, state, dict(a=[], b=[]), 3)
    assert reserve_feed.take('a') == []
    assert reserve_feed.take('b') == []
    for index, row in enumerate(rows[:4]):
        entries.append(repair_entry(row, replacement(row, source_id=str(900 + index)), plan))
    reserve_feed.last_read = float('-inf')
    assert [r['identity'] for r in reserve_feed.take('a')] == [rows[0]['identity'], rows[2]['identity']]
    assert [r['identity'] for r in reserve_feed.take('b')] == [rows[1]['identity'], rows[3]['identity']]
    assert reserve_feed.take('a') == []
    assert reserve_feed.take('b') == []

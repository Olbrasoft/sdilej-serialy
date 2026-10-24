import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sdilej_to_prehrajto.models import LanguageTier

from sdilej_serialy import replenish, dual
from sdilej_serialy.catalog import write_jsonl_gzip
from sdilej_serialy.discovery_index import CandidateIndex, indexed_order, series_probe_allowed
from sdilej_serialy.models import Episode
from sdilej_serialy.source_cache import SourceCache
from test_discovery_index import metadata, candidate
from test_replenish import setup, discover
from test_dual import source


def test_low_stock_can_probe_only_exact_dubbed_hints_inside_a_series_pause():
    rows = [metadata(number=n) for n in range(1, 6)]
    index = CandidateIndex(rows)
    index.observe([candidate('Series 1 S01E01 CZ', '1'),
                   candidate('Series 1 S01E02 SK Dabing', '2'),
                   candidate('Series 1 S01E03 CZ-SK tit', '3'),
                   candidate('Series 1 S01E04', '4'),
                   candidate('Series 1 S01E05 CZ', '5')])
    records = {'1:1:5': dict(consecutive_failures=3)}
    fresh = {Episode.from_dict(r).identity for r in rows}
    assert not list(indexed_order(rows, fresh, {1}, index, lambda: None, {}, records=records))
    result = list(indexed_order(rows, fresh, {1}, index, lambda: None, {}, records=records, urgent=True))
    assert [r['episode'] for r in result] == [1, 2]
    records['1:1:1'] = dict(consecutive_failures=3)
    assert not series_probe_allowed(rows[0], {1}, index, records, urgent=True)


@pytest.mark.parametrize('foreign', [False, True])
def test_live_publisher_keeps_episode_backoff_and_checks_audio_inside_series_pause(tmp_path, monkeypatch, foreign):
    directory, _, _ = setup(tmp_path, monkeypatch)
    frozen = (directory / 'manifest.jsonl').read_bytes()
    target = (directory / 'state.json').read_bytes()
    catalog = [source(number=n)['episode'] for n in range(1, 10)]
    write_jsonl_gzip(tmp_path / 'backlog/series-episodes.jsonl.gz', catalog)
    later = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    before = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    state_path = tmp_path / 'state/reserve-preparation.json'
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(dict(schema_version=1, episodes={
        '1:1:5': dict(status='deferred', reason='no_matches', preparation_revision=5,
                      consecutive_failures=1, retry_after=before),
        '1:1:7': dict(status='deferred', reason='no_matches', preparation_revision=5,
                      consecutive_failures=1, retry_after=later,
                      search_recovery_revision=replenish.SEARCH_RECOVERY_REVISION),
        '1:1:8': dict(status='deferred', reason='no_matches', preparation_revision=5,
                      consecutive_failures=3, retry_after=before)},
        series_discovery={'1': dict(misses=['1:1:1','1:1:2','1:1:3'], retry_after=later)})))
    cache = SourceCache(tmp_path / 'state/source-evidence-cache.json')
    for n in (5, 7, 8, 9):
        row = source(number=n)['selected']
        hint = {k: row[k] for k in ('source_id','url','title','size_bytes','duration_sec','width','height')}
        hint['title'] = f'Series 1 S01E{n:02d} ' + ('CZ-SK tit' if n == 9 else 'CZ Dabing')
        cache.remember('search', str(n), lambda hint=hint: dict(candidates=[hint], next=None))
    calls = []
    def inspect(ep):
        calls.append(ep.number)
        if ep.number != 5:
            return None
        result = discover(ep)
        return replace(result, audio_language='en', language_tier=LanguageTier.FOREIGN_AUDIO) if foreign else result
    provider = SimpleNamespace(cache=cache, discover=inspect, last_outcome='verified_non_czech')
    result = replenish.prepare(tmp_path, 'test', provider, runtime_minutes=0)
    assert calls[0] == 5 and 7 not in calls
    assert result['prepared_this_run'] == (0 if foreign else 1)
    assert (directory / 'manifest.jsonl').read_bytes() == frozen
    assert (directory / 'state.json').read_bytes() == target
    if foreign:
        assert calls == [5] and not (directory / 'additions.jsonl').exists()
    else:
        assert [r['identity'] for r in dual.load(tmp_path, 'test')[2][4:]] == ['1:1:5']

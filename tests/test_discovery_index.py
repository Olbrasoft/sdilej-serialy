from datetime import UTC, datetime, timedelta
from itertools import islice

from sdilej_serialy.discovery_index import CandidateIndex, indexed_order, paused_series, record_series_result
from sdilej_serialy.models import Episode
from test_dual import source


def metadata(series=1, number=1, title=None):
    row = source(series=series, number=number)['episode']
    return dict(row, series_title=title or row['series_title'])


def candidate(title, source_id='123'):
    return dict(source_id=source_id, title=title)


def test_index_requires_exact_series_and_episode_and_does_not_trust_czech_filename():
    rows = [metadata(title='Planet Earth'), metadata(series=2, title='Planet Earth II')]
    index = CandidateIndex(rows)
    index.observe([candidate('Planet_Earth_II_S01E01_CZ.mkv'),
                   candidate('Planet Earth S01E01-02 CZ.mkv', '124'),
                   candidate('Other Planet Earth S01E01 CZ.mkv', '125')])
    assert index.hints == {'2:1:1': 0}
    assert index.rank('1:1:1') == 3
    index.observe([candidate('Planet Earth S01E01.mkv', '126')])
    assert index.rank('1:1:1') == 1


def test_index_accepts_original_aliases_combined_prefixes_and_years():
    row = dict(metadata(title='Český seriál'), series_original_title='Original title')
    index = CandidateIndex([row])
    index.observe([candidate('Original title Český seriál 2020 1x1 CZ dabing')])
    assert index.rank('1:1:1') == 0


def test_indexed_order_prioritizes_seen_sources_without_starving_exploration_or_retries():
    rows = [metadata(series=n) for n in range(1, 21)]
    index = CandidateIndex(rows)
    index.observe([candidate(f'Series {n} S01E01 CZ.mkv', str(n)) for n in range(10, 18)])
    fresh = {Episode.from_dict(r).identity for r in rows if r['series_id'] > 2}
    progress = {}
    result = list(indexed_order(rows, fresh, set(), index, lambda: None, progress))
    assert [r['series_id'] for r in result[:10]] == [10, 11, 12, 13, 14, 15, 3, 4, 1, 2]
    assert len(result) == len(rows)
    assert len({Episode.from_dict(r).identity for r in result}) == len(rows)


def test_exploration_cursor_survives_restart_and_pauses_do_not_record_unsearched_rows():
    rows = [metadata(series=s, number=n) for s in range(1, 5) for n in (1, 2)]
    index = CandidateIndex(rows)
    fresh = {Episode.from_dict(r).identity for r in rows}
    progress = {}
    first = indexed_order(rows, fresh, set(), index, lambda: None, progress)
    assert [r['series_id'] for r in islice(first, 2)] == [1, 2]
    assert progress['last_series'] == 2
    resumed = indexed_order(rows, fresh, {3}, index, lambda: None, progress)
    assert next(resumed)['series_id'] == 4


def test_cursor_continues_when_last_series_has_no_eligible_episodes_after_restart():
    rows = [metadata(series=n) for n in (1, 2, 3)]
    progress = {}
    index = CandidateIndex(rows)
    first = indexed_order(rows, {Episode.from_dict(r).identity for r in rows}, set(), index, lambda: None, progress)
    list(islice(first, 2))
    remaining = [rows[0], rows[2]]
    resumed = indexed_order(remaining, {Episode.from_dict(r).identity for r in remaining}, set(), index,
                            lambda: None, progress)
    assert next(resumed)['series_id'] == 3


def test_index_picks_up_positive_results_found_by_other_workers():
    rows = [metadata(series=n) for n in range(1, 30)]
    index = CandidateIndex(rows)
    now = [0]
    def refresh():
        if now[0]:
            index.observe([candidate('Series 29 S01E01 CZ.mkv')])
    result = indexed_order(rows, {Episode.from_dict(r).identity for r in rows}, set(), index,
                           refresh, {}, clock=lambda: now[0])
    assert [r['series_id'] for r in islice(result, 8)] == list(range(1, 9))
    now[0] = 61
    assert next(result)['series_id'] == 29


def test_series_pause_uses_distinct_misses_survives_restart_and_expires():
    now = datetime(2026, 10, 11, tzinfo=UTC)
    records = {}
    failure = dict(status='deferred', reason='no_matches')
    ep = Episode.from_dict(metadata())
    for _ in range(5):
        record_series_result(records, ep, failure, now)
    assert paused_series(records, now) == set()
    for n in (2, 3):
        record_series_result(records, Episode.from_dict(metadata(number=n)), failure, now)
    assert paused_series(records, now + timedelta(hours=3)) == {1}
    assert paused_series(records, now + timedelta(hours=5)) == set()
    record_series_result(records, ep, failure, now + timedelta(hours=5))
    assert len(records['1']['misses']) == 1 and 'retry_after' not in records['1']
    record_series_result(records, ep, dict(status='prepared'), now + timedelta(hours=5))
    assert records['1']['misses'] == []

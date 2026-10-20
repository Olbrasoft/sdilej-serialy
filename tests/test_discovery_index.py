from datetime import UTC, datetime, timedelta
from itertools import islice

from sdilej_serialy.discovery_index import (CandidateIndex, indexed_order, paused_series,
                                            record_series_result, release_expired_pauses)
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


def test_czech_search_hints_cannot_bypass_the_due_retry_budget():
    rows = [metadata(series=n) for n in range(1, 31)]
    index = CandidateIndex(rows)
    index.observe([candidate(f'Series {n} S01E01 CZ.mkv', str(n)) for n in range(1, 13)])
    fresh = {Episode.from_dict(r).identity for r in rows[12:]}
    records = {Episode.from_dict(r).identity: dict(consecutive_failures=80) for r in rows[:12]}
    result = list(indexed_order(rows, fresh, set(), index, lambda: None, {}, records=records))
    first = [r['series_id'] for r in result[:10]]
    assert first == list(range(13, 21)) + [1, 2]
    assert len({Episode.from_dict(r).identity for r in result}) == len(rows)


def test_productive_fresh_seasons_precede_unproven_hints_without_starving_other_lanes():
    rows = [metadata(series=n) for n in range(1, 31)]
    index = CandidateIndex(rows)
    index.observe([candidate(f'Series {n} S01E01 CZ.mkv', str(n)) for n in range(1, 13)])
    fresh = {Episode.from_dict(r).identity for r in rows[:-2]}
    productive = {(n, 1) for n in range(21, 27)}
    result = list(indexed_order(rows, fresh, set(), index, lambda: None, {}, productive_seasons=productive))
    assert [r['series_id'] for r in result[:10]] == [21, 22, 23, 24, 1, 2, 3, 4, 29, 30]
    assert len({Episode.from_dict(r).identity for r in result}) == len(rows)


def test_new_productive_season_is_used_without_restarting_the_producer():
    rows = [metadata(series=n) for n in range(1, 30)]
    index, productive, now = CandidateIndex(rows), set(), [0]
    result = indexed_order(rows, {Episode.from_dict(r).identity for r in rows}, set(), index,
                           lambda: None, {}, productive_seasons=productive, clock=lambda: now[0])
    assert [r['series_id'] for r in islice(result, 8)] == list(range(1, 9))
    productive.add((29, 1))
    now[0] = 61
    assert next(result)['series_id'] == 29


def test_repeat_failures_do_not_monopolize_the_retry_lane():
    rows = [metadata(series=n) for n in range(1, 6)]
    index = CandidateIndex(rows)
    index.observe([candidate('Series 1 S01E01 CZ.mkv')])
    records = {'1:1:1': dict(consecutive_failures=89), '2:1:1': dict(consecutive_failures=1)}
    result = list(indexed_order(rows, set(), set(), index, lambda: None, {}, records=records,
                               productive_seasons={(2, 1)}))
    assert result[0]['series_id'] == 2
    assert result[-1]['series_id'] == 1
    assert len(result) == 5


def test_productive_lane_respects_pauses_and_does_not_treat_other_seasons_as_proven():
    rows = [metadata(series=n) for n in range(1, 5)]
    index = CandidateIndex(rows)
    result = list(indexed_order(rows, {Episode.from_dict(r).identity for r in rows}, {4}, index,
                               lambda: None, {}, productive_seasons={(3, 2), (4, 1)}))
    assert [r['series_id'] for r in result] == [1, 2, 3]


def test_reserve_production_path_preserves_productive_hints_and_retry_budget():
    from sdilej_serialy.replenish import reserve_order
    rows = [metadata(series=n) for n in range(1, 14)]
    index = CandidateIndex(rows)
    index.observe([candidate('Series 1 S01E01 CZ.mkv')])
    records = {'1:1:1': dict(reason='inconclusive_audio', preparation_revision=4,
                            consecutive_failures=89)}
    result = list(reserve_order(rows, records, {}, set(), {(13, 1)},
                               index=index, refresh=lambda: None, progress={}))
    assert result[0]['series_id'] == 13
    assert result[8]['series_id'] == 1


def test_expired_pauses_are_released_without_restoring_explicit_unpauses():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    records = {
        '1': dict(retry_after=(now - timedelta(seconds=1)).isoformat()),
        '2': dict(retry_after=(now + timedelta(hours=1)).isoformat()),
        '3': dict(retry_after=now.isoformat()),
        '4': dict(retry_after=(now + timedelta(hours=1)).isoformat()),
    }
    paused = {1, 2, 3, 5}
    assert release_expired_pauses(records, paused, now) == {1, 3}
    assert paused == {2, 5}
    assert release_expired_pauses(records, paused, now) == set()


def test_expired_pause_restores_skipped_unhinted_rows_without_repeating_work():
    rows = [metadata(series=n) for n in range(1, 13)]
    fresh = {Episode.from_dict(r).identity for r in rows}
    index, paused, now, progress = CandidateIndex(rows), {1}, [0], {}
    def refresh():
        if now[0] >= 60:
            paused.clear()
    result = indexed_order(rows, fresh, paused, index, refresh, progress, clock=lambda: now[0])
    first = list(islice(result, 8))
    assert [r['series_id'] for r in first] == list(range(2, 10))
    now[0] = 61
    rest = list(result)
    assert [r['series_id'] for r in rest] == [10, 11, 12, 1]
    assert len({Episode.from_dict(r).identity for r in first + rest}) == len(rows)


def test_refresh_keeps_active_pauses_and_restores_hinted_and_productive_lanes():
    rows = [metadata(series=n) for n in range(1, 16)]
    fresh = {Episode.from_dict(r).identity for r in rows}
    index, paused, now = CandidateIndex(rows), {1, 2, 3}, [0]
    index.observe([candidate('Series 1 S01E01 CZ.mkv')])
    def refresh():
        if now[0] >= 60:
            paused.difference_update({1, 2})
    result = indexed_order(rows, fresh, paused, index, refresh, {}, productive_seasons={(2, 1)},
                           clock=lambda: now[0])
    first = list(islice(result, 8))
    assert [r['series_id'] for r in first] == list(range(4, 12))
    now[0] = 61
    rest = list(result)
    assert [r['series_id'] for r in rest[:2]] == [2, 1]
    assert 3 not in [r['series_id'] for r in first + rest]
    assert len({Episode.from_dict(r).identity for r in first + rest}) == 14

from datetime import date

import pytest

from sdilej_serialy.catalog_repair import supplement


def test_catalog_repair_preserves_existing_rows_and_only_appends_aired_missing_episodes():
    existing = dict(series_id=12, series_title='Series', series_original_title='Original',
                    episode_id=42, season=1, episode=1, episode_title='Trusted title',
                    source_policy='must-not-spread', imdb_rating=9.0)
    episodes = [dict(id=n+100, season_number=1, episode_number=n, name=f'Episode {n}', runtime=43,
                     air_date='2020-01-01' if n<3 else '2030-01-01' if n==3 else None)
                for n in range(1,5)]
    details = dict(id=99, name='Series', original_name='Original')
    seasons = [dict(season_number=1, episodes=episodes)]
    result = supplement([existing], 12, details, seasons, today=date(2026,10,29))
    assert len(result) == 2 and result[0] is existing
    added = result[1]
    assert added['episode_id'] == -12001002 and added['tmdb_episode_id'] == 102
    assert added['episode_code'] == 'S01E02' and added['runtime_min'] == 43
    assert added['imdb_rating'] == 9.0 and 'source_policy' not in added
    assert 'selected' not in added and 'quality_policy' not in added
    assert supplement(result, 12, details, seasons, today=date(2026,10,29)) == result


@pytest.mark.parametrize('problem', ['series', 'episode', 'collision'])
def test_catalog_repair_rejects_mismatched_or_ambiguous_inventory(problem):
    catalog = [dict(series_id=12, series_title='Series', episode_id=42, season=1, episode=1)]
    details = dict(id=99, name='Other series' if problem=='series' else 'Series')
    item = dict(id=102, season_number=2 if problem=='episode' else 1, episode_number=2,
                air_date='2020-01-01', runtime=43)
    if problem=='collision':
        catalog.append(dict(series_id=33, season=1, episode=1, episode_id=-12001002))
    with pytest.raises(ValueError):
        supplement(catalog,12,details,[dict(season_number=1,episodes=[item])],today=date(2026,10,29))

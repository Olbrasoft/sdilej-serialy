import json
from datetime import UTC, datetime, timedelta

from sdilej_serialy.catalog import load_jsonl, write_jsonl_gzip
from sdilej_serialy.catalog_refresh import due_series, refresh
from sdilej_serialy.pipeline import atomic_json


def setup(root):
    catalog = [dict(series_id=s, series_title=f'Series {s}', episode_id=s, season=1, episode=1,
                    imdb_rating=10-s) for s in (1,2,3)]
    write_jsonl_gzip(root/'backlog/series-episodes.jsonl.gz', catalog)
    atomic_json(root/'backlog/series-inventory.json',dict(schema_version=1,
        series=[dict(series_id=s,tmdb_id=s+10) for s in (1,2,3)]))
    return catalog


def inventory(mid, **kwargs):
    kwargs['before_request']()
    return (dict(id=mid,name=f'Series {mid-10}'), [dict(season_number=1,episodes=[
        dict(id=mid*10+2,season_number=1,episode_number=2,air_date='2020-01-01',runtime=42)])])


def test_refresh_is_bounded_preserves_existing_metadata_and_skips_checked_series_on_restart(tmp_path):
    old=setup(tmp_path);now=datetime(2026,10,30,tzinfo=UTC)
    protected=tmp_path/'dual/test/state.json';atomic_json(protected,{'uploaded':'must remain unchanged'})
    before=protected.read_bytes()
    report=refresh(tmp_path,inventory,limit=1,now=now)
    assert report['added_episodes']==1 and report['results'][0]['series_id']==1
    catalog=load_jsonl(tmp_path/'backlog/series-episodes.jsonl.gz')
    assert catalog[:len(old)]==old and protected.read_bytes()==before
    again=refresh(tmp_path,inventory,limit=1,now=now)
    assert again['results'][0]['series_id']==2
    assert len(load_jsonl(tmp_path/'backlog/series-episodes.jsonl.gz'))==5
    for r in catalog[len(old):]:
        assert 'selected' not in r and 'source_policy' not in r


def test_metadata_outage_does_not_change_catalog_or_expose_secrets(tmp_path):
    setup(tmp_path);path=tmp_path/'backlog/series-episodes.jsonl.gz';before=path.read_bytes()
    def outage(*args,**kwargs):
        raise RuntimeError('private-key authenticated-url')
    report=refresh(tmp_path,outage,limit=1)
    assert report['added_episodes']==0 and report['results'][0]['status']=='deferred'
    assert path.read_bytes()==before
    assert 'private-key' not in json.dumps(report) and 'authenticated-url' not in json.dumps(report)


def test_deadline_stops_further_series_and_does_not_mark_unvisited_inventory(tmp_path):
    setup(tmp_path);clock=[0]
    def fetch(mid,**kwargs):
        kwargs['before_request']()
        clock[0]=121
        kwargs['before_request']()
    report=refresh(tmp_path,fetch,clock=lambda:clock[0])
    assert report['checked_this_run']==1 and report['results'][0]['error']=='TimeoutError'
    assert len(json.loads((tmp_path/'state/catalog-inventory.json').read_text())['series'])==1


def test_proven_series_precede_broad_inventory_and_daily_cooldown_is_respected():
    now=datetime(2026,10,30,tzinfo=UTC)
    catalog=[dict(series_id=s,imdb_rating=10-s) for s in (1,2,3)]
    registry=[dict(series_id=s,tmdb_id=s+10) for s in (1,2,3)]
    prep=dict(episodes={'2:1:1':dict(status='prepared'),'3:1:1':dict(status='deferred')})
    assert due_series(catalog,registry,prep,{},now)[0]['series_id']==2
    state={'2':dict(checked_at=now.isoformat(),retry_after=(now+timedelta(days=1)).isoformat())}
    assert [r['series_id'] for r in due_series(catalog,registry,prep,state,now)]==[1,3]

import pytest
from sdilej_serialy.catalog_merge import merge


def row(sid, mid, title, number=1):
    return dict(series_id=sid,series_tmdb_id=mid,series_title=title,
                episode_id=sid*100+number,season=1,episode=number,imdb_rating=8.5)


def test_append_only_import_is_idempotent_preserves_history_and_ignores_upload_claims():
    old=[dict(row(1,11,'Original'),trusted='unchanged')]
    registry=dict(schema_version=1,series=[dict(series_id=1,tmdb_id=11)])
    incoming=[dict(row(2,22,'New'),selected={'untrusted':'not proof'},source_policy='not authorized'),
              row(2,22,'New',2),row(2,22,'New',2),dict(row(1,11,'Changed'),episode=3)]
    catalog,mapping,report=merge(old,registry,incoming)
    assert catalog[:len(old)]==old and len(catalog)==3 and report['added_series']==1
    assert 'selected' not in catalog[1] and 'source_policy' not in catalog[1]
    assert merge(catalog,mapping,incoming)[0]==catalog


def test_tmdb_and_title_aliases_are_not_imported_as_new_series():
    old=[row(1,11,'Original')]
    registry=dict(schema_version=1,series=[dict(series_id=1,tmdb_id=11)])
    incoming=[row(2,11,'Translated alias'),row(3,33,'ORIGINAL'),row(4,44,'New'),row(5,44,'New alias')]
    catalog,mapping,report=merge(old,registry,incoming)
    assert [r['series_id'] for r in catalog]==[1,4]
    assert report['skipped_alias_series']==[2,3,5]


def test_episode_id_collision_is_rejected_without_mutating_inputs():
    old=[row(1,11,'Original')];registry=dict(schema_version=1,series=[dict(series_id=1,tmdb_id=11)])
    incoming=[dict(row(2,22,'New'),episode_id=101)]
    with pytest.raises(ValueError,match='conflicts'):
        merge(old,registry,incoming)
    assert len(old)==1 and len(registry['series'])==1

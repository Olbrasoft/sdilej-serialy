import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sdilej_serialy import replenish
from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.quality import SLOVAK_POLICY
from sdilej_serialy.requested_series import RequestedSeries
from sdilej_serialy.source_cache import SourceCache
from test_replenish import setup, discover
from test_dual import source
from test_episodes import episode
from test_requested_series import request_plan


def hint(title):
    return dict(source_id='123',url='https://sdilej.cz/123/video.mkv',title=title,
                size_bytes=100,width=1920,height=1080,duration_sec=100)


@pytest.mark.parametrize('title,expired,expected', [
    ('Teorie velkého třesku S01E01 CZ',False,'transient_search'),
    ('Teorie velkého třesku S01E01 CZ',True,'no_matches'),
    ('Teorie velkého třesku II S01E01 CZ',False,'no_matches'),
    ('Teorie velkého třesku S01E02 CZ',False,'no_matches')])
def test_fresh_empty_search_cannot_disprove_recent_matching_episode(tmp_path,monkeypatch,title,expired,expected):
    now=[100]
    cache=SourceCache(tmp_path/'cache.json',clock=lambda:now[0])
    cache.remember('search','positive',lambda:dict(candidates=[hint(title)],next=None),ttl=10)
    if expired:now[0]=120
    provider=EpisodeSourceProvider(None,detector=object(),cache=cache,request_gap_seconds=0)
    # These strings must never be copied into the persisted diagnostics.
    html='<h1>Teorie velkého třesku</h1><div>secret-password signed-token=private</div>'
    monkeypatch.setattr(provider,'_get',lambda url:SimpleNamespace(text=html,status_code=200))
    assert provider.discover_fresh(episode()) is None
    assert provider.last_outcome==expected
    trace=provider.last_search_evidence
    assert trace and all(r['status']==200 and r['parsed_candidates']==0 for r in trace)
    assert 'secret-password' not in json.dumps(trace) and 'signed-token' not in json.dumps(trace)


def positive_sample(number=1):
    row=source(number=number)
    row['selected']=discover(Episode.from_dict(row['episode'])).to_dict()
    row['requested_audio_check']=dict(revision=1,identity=row['identity'],
        source_id=row['selected']['source_id'],audio_language='cs',checked_at='2026-10-23T12:00:00+00:00')
    return row


def test_verified_season_neighbors_stop_being_initial_samples(tmp_path):
    path=tmp_path/'request.json'
    request_plan(path)
    saved={}
    requested=RequestedSeries(path,'test',{},saved)
    metadata=source(number=2)['episode']
    assert requested.decorate(metadata)['requested_probe'] is True
    saved['1:1:1']=positive_sample()
    assert requested.decorate(metadata)['requested_probe'] is False
    assert requested.decorate(dict(metadata,season=2))['requested_probe'] is True


def test_reused_sample_preserves_its_season_availability_evidence():
    row=positive_sample()
    metadata=dict(row['episode'],requested_audio_policy=SLOVAK_POLICY,requested_probe=False)
    provider=SimpleNamespace(discover=lambda _:pytest.fail('Verified sample rediscovered'))
    record,replacement,fatal=replenish.inspect_preparation(provider,(metadata,row,{},[]))
    assert not fatal and record['method']=='current_saved'
    assert replacement['requested_audio_check']['identity']==row['identity']
    assert replacement['requested_audio_check']['source_id']==row['selected']['source_id']


def test_positive_hint_releases_only_one_old_requested_missing_result():
    now=datetime.now(UTC)
    record=dict(reason='no_matches',preparation_revision=5,audio_policy_revision=1,
                request_probe_revision=1,retry_after=(now+timedelta(hours=23)).isoformat())
    metadata=dict(source(number=2)['episode'],requested_audio_policy=SLOVAK_POLICY,requested_probe=False)
    assert not replenish.preparation_due(record,metadata,None,now,source_hint=False)
    assert replenish.preparation_due(record,metadata,None,now,source_hint=True)
    assert not replenish.preparation_due(record,source(number=2)['episode'],None,now,source_hint=True)
    record['search_recovery_revision']=replenish.SEARCH_RECOVERY_REVISION
    assert not replenish.preparation_due(record,metadata,None,now,source_hint=True)


def test_publisher_recovers_cached_neighbor_without_replaying_uploaded_queue(tmp_path,monkeypatch):
    directory,_,_=setup(tmp_path,monkeypatch)
    original=(directory/'manifest.jsonl').read_bytes()
    path=tmp_path/'backlog/requested-series.json'
    request_plan(path,[dict(season=1,episode=n) for n in (5,6)])
    manifest=SourceManifest(tmp_path/'manifests/selected-episodes.jsonl')
    manifest.add(positive_sample(number=5))
    manifest.save()
    record=dict(status='deferred',reason='no_matches',preparation_revision=5,audio_policy_revision=1,
        request_probe_revision=1,retry_after=(datetime.now(UTC)+timedelta(hours=23)).isoformat())
    (tmp_path/'state').mkdir(exist_ok=True)
    (tmp_path/'state/reserve-preparation.json').write_text(json.dumps(dict(schema_version=1,episodes={'1:1:6':record})))
    cache=SourceCache(tmp_path/'state/source-evidence-cache.json')
    observed=hint('Series 1 S01E06 CZ')
    cache.remember('search','known',lambda:dict(candidates=[observed],next=None))
    calls=[]
    provider=SimpleNamespace(cache=cache,discover=lambda ep:calls.append(ep.identity) or discover(ep),
        discover_fresh=lambda ep:pytest.fail('Known Czech season unnecessarily refreshes shared search pages'))
    result=replenish.prepare(tmp_path,'test',provider,runtime_minutes=0)
    assert result['prepared_this_run']==2 and calls==['1:1:6']
    assert (directory/'manifest.jsonl').read_bytes()==original
    assert SourceManifest(tmp_path/'manifests/selected-episodes.jsonl').rows['1:1:5']['requested_audio_check']
    after=json.loads((tmp_path/'state/reserve-preparation.json').read_text())
    assert after['episodes']['1:1:6']['search_recovery_revision']==1
    assert replenish.prepare(tmp_path,'test',provider,runtime_minutes=0)['attempted_this_run']==0

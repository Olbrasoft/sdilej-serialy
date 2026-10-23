import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier

from sdilej_serialy import dual, replenish
from sdilej_serialy.catalog import write_jsonl_gzip
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.quality import SLOVAK_POLICY, slovak_eligible, upload_eligible
from sdilej_serialy.requested_series import RequestedSeries
from sdilej_serialy.source_workers import DISPATCH_WAIT, inspected_results
from sdilej_serialy.source_repair import repair_entry, repaired_row
from test_dual import setup_plan, source
from test_episodes import episode, candidate, provider_with
from test_replenish import setup, discover, parallel_provider


def slovak_row(number=5):
    row = source(number=number)
    row['selected'].update(title=f'Series 1 S01E{number:02d}', audio_language='sk',
                           language_tier='slovak_audio')
    selected = row['selected']
    row.update(source_policy=SLOVAK_POLICY, language_selection=dict(policy=SLOVAK_POLICY,
        czech_search_complete=True, czech_found=False, checked_source_count=2,
        checked_at='2026-01-01T00:00:00+00:00', checked_sources_sha256='a'*64,
        source_id=selected['source_id'], source_url=selected['url'], source_size_bytes=selected['size_bytes']))
    return row


def request_plan(path, episodes=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(schema_version=1,generation='test',series=[dict(
        series_id=1,title='Series 1',episodes=episodes or [dict(season=s,episode=n)
            for s in (1,2) for n in range(1,6)])])))


def test_language_precedence_and_smallest_slovak_original_after_full_czech_search(monkeypatch):
    sk = replace(candidate('11', height=1080,size_bytes=200,language=LanguageTier.SLOVAK_AUDIO),
                 width=3840,height=2160,audio_language='sk',language_probability=.99)
    cs = replace(sk,source_id='12',width=720,height=480,language_tier=LanguageTier.CZECH_AUDIO,audio_language='cs')
    provider = provider_with(monkeypatch,[sk,cs],lambda ep,c:c)
    assert provider.discover_fresh(episode()) is cs
    small = replace(sk, source_id='13',size_bytes=100)
    provider = provider_with(monkeypatch,[sk,small],lambda ep,c:c)
    selected = provider.discover_fresh(episode())
    assert selected is small
    assert selected._language_selection['checked_source_count'] == 2
    assert selected._language_selection['czech_found'] is False


def test_partial_saved_review_cannot_authorize_slovak_fallback(monkeypatch):
    sk = replace(candidate('11', height=1080,size_bytes=200,language=LanguageTier.SLOVAK_AUDIO),
                 audio_language='sk',language_probability=.99)
    provider = provider_with(monkeypatch,[sk],lambda ep,c:c)
    result = provider.revalidate_saved(episode(),[sk])
    assert result is sk and not hasattr(result,'_language_selection')


def test_unknown_czech_candidate_prevents_slovak_fallback(monkeypatch):
    from sdilej_to_prehrajto.language import LanguageDetectionError
    sk=replace(candidate('11',height=1080,size_bytes=200,language=LanguageTier.SLOVAK_AUDIO),
               audio_language='sk',language_probability=.99)
    unresolved=replace(sk,source_id='12',width=720,height=480,language_tier=LanguageTier.UNKNOWN)
    def verify(ep,c):
        if c is unresolved:raise LanguageDetectionError('Unresolved audio')
        return c
    provider=provider_with(monkeypatch,[sk,unresolved],verify)
    assert provider.discover_fresh(episode()) is None


def test_source_repair_preserves_owner_and_can_replace_slovak_with_czech():
    base=slovak_row()
    base.update(queue_rank=9,target_account='a')
    czech=source(number=5)
    czech['selected']['title']='Series 1 S01E05'
    plan=dict(generation='test',manifest_sha256='b'*64)
    entry=repair_entry(base,czech,plan)
    result=repaired_row(base,entry,plan)
    assert result['target_account']=='a' and result['queue_rank']==9
    assert 'source_policy' not in result and 'language_selection' not in result
    entry=repair_entry(czech,slovak_row(),plan)
    result=repaired_row(czech,entry,plan)
    assert upload_eligible(result) and result['source_policy']==SLOVAK_POLICY


@pytest.mark.parametrize('field,value', [('source_id','999'),('source_size_bytes',999),
    ('czech_search_complete',False),('czech_found',True),('checked_source_count',0),
    ('checked_sources_sha256','bad'),('checked_at','2026-01-01')])
def test_slovak_evidence_binding_and_static_queue_eligibility(field,value):
    row=slovak_row()
    assert slovak_eligible(row) and upload_eligible(row)
    row['language_selection'][field]=value
    assert not upload_eligible(row)


def test_first_three_samples_gate_each_season_and_survive_restart(tmp_path):
    path=tmp_path/'request.json'
    request_plan(path)
    records,sources={},{}
    gate=RequestedSeries(path,'test',records,sources)
    metadata=lambda s,n: dict(source(number=n)['episode'],season=s)
    assert gate.available(metadata(1,1))
    assert not gate.available(metadata(1,2)) and not gate.available(metadata(2,1))
    for n in (1,2,3):
        assert gate.available(metadata(1,n))
        records[f'1:1:{n}']=dict(request_probe_revision=1,observed_audio_language='en')
    assert not gate.available(metadata(1,4)) and gate.available(metadata(2,1))
    assert gate.report()[0]['seasons'][0]['status']=='needs_subtitles'
    row=source(number=2)
    row['requested_audio_check']=dict(revision=1,identity=row['identity'],source_id=row['selected']['source_id'],audio_language='cs')
    sources[row['identity']]=row
    resumed=RequestedSeries(path,'test',records,sources)
    assert resumed.available(metadata(1,5))
    assert not resumed.available(metadata(2,4))
    row['quality_policy']='probe-only'
    assert not resumed.available(metadata(1,5))


def test_slovak_sample_unlocks_only_its_own_season(tmp_path):
    path=tmp_path/'request.json'
    request_plan(path)
    row=slovak_row(number=1)
    row['requested_audio_check']=dict(revision=1,identity=row['identity'],source_id=row['selected']['source_id'],audio_language='sk')
    gate=RequestedSeries(path,'test',{}, {'1:1:1':row})
    assert gate.available(source(number=5)['episode'])
    assert not gate.available(dict(source(number=5)['episode'],season=2))


def test_foreign_series_or_wrong_generation_is_rejected(tmp_path):
    path=tmp_path/'request.json'
    request_plan(path)
    with pytest.raises(ValueError): RequestedSeries(path,'wrong',{}, {})
    payload=json.loads(path.read_text())
    payload['excluded_series']=[dict(series_id=1)]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError): RequestedSeries(path,'test',{}, {})


def test_fresh_probe_bypasses_legacy_source_and_backoff_only_once():
    ep=Episode.from_dict(source(number=5)['episode'])
    saved=source(number=5)
    selected=discover(ep)
    calls=[]
    provider=SimpleNamespace(discover=lambda ep:pytest.fail('Cached discovery used'),
        discover_fresh=lambda ep:calls.append(ep.identity) or selected)
    metadata=dict(ep.to_dict(),requested_probe=True,requested_audio_policy=SLOVAK_POLICY)
    record,row,fatal=replenish.inspect_preparation(provider,(metadata,saved,{},[]))
    assert not fatal and calls==[ep.identity]
    assert row['requested_audio_check']['audio_language']=='cs'
    old=dict(reason='verified_non_czech',retry_after=(datetime.now(UTC)+timedelta(days=1)).isoformat(),
             preparation_revision=5,audio_policy_revision=1)
    assert replenish.preparation_due(old,metadata,None,datetime.now(UTC))
    old['request_probe_revision']=1
    assert not replenish.preparation_due(old,metadata,None,datetime.now(UTC))


def test_slovak_publisher_waits_for_capability_then_dual_loader_accepts(tmp_path,monkeypatch):
    directory,_,_=setup(tmp_path,monkeypatch)
    row=slovak_row()
    selected=Candidate.from_dict(row['selected'])
    selected._language_selection=row['language_selection']
    provider=SimpleNamespace(discover=lambda _: selected)
    result=replenish.prepare(tmp_path,'test',provider,limit=1,runtime_minutes=0,identities=['1:1:5'])
    assert result['prepared_this_run']==0
    assert SourceManifest(tmp_path/'manifests/selected-episodes.jsonl').rows['1:1:5']['source_policy']==SLOVAK_POLICY
    (directory/'uploader-capabilities.json').write_text(json.dumps(dict(schema_version=1,generation='test',
        audio_queue_version=1,source_policies=[SLOVAK_POLICY])))
    state_path=tmp_path/'state/reserve-preparation.json'
    state=json.loads(state_path.read_text())
    state['episodes']['1:1:5'].pop('retry_after')
    state_path.write_text(json.dumps(state))
    result=replenish.prepare(tmp_path,'test',provider,limit=1,runtime_minutes=0,identities=['1:1:5'])
    assert result['prepared_this_run']==1
    _,_,queue=dual.load(tmp_path,'test')
    assert queue[-1]['source_policy']==SLOVAK_POLICY
    assert 'SK Dabing' in queue[-1]['display_name']
    assert queue[-1]['target_account']=='a'


def test_parallel_sampler_resumes_gated_neighbors_without_duplicate_dispatch(tmp_path,monkeypatch):
    directory,_,_=setup_plan(tmp_path,monkeypatch,count=4)
    catalog=[dict(source(series=2,number=n)['episode'],imdb_rating=9) for n in range(1,8)]
    write_jsonl_gzip(tmp_path/'backlog/series-episodes.jsonl.gz',catalog)
    path=tmp_path/'backlog/requested-series.json'
    request_plan(path)
    payload=json.loads(path.read_text())
    payload['series'][0]['series_id']=2
    payload['series'][0]['episodes']=[dict(season=1,episode=n) for n in range(1,8)]
    path.write_text(json.dumps(payload))
    calls=[]
    def inspect(ep):
        calls.append(ep.identity)
        if ep.number==1:time.sleep(.03)
        c=Candidate.from_dict(source(series=ep.series_id,number=ep.number)['selected'])
        return replace(c,title=f'{ep.series_title} {ep.code}')
    provider=parallel_provider(inspect)
    result=replenish.prepare(tmp_path,'test',provider,workers=4,runtime_minutes=0)
    assert result['prepared_this_run']==7
    assert calls[0]=='2:1:1'
    assert len(calls)==len(set(calls))==7
    assert len(dual.load(tmp_path,'test')[2])==11


def test_idle_wait_does_not_spin_or_submit_a_sentinel():
    ticks=[]
    def items():
        yield DISPATCH_WAIT
        pytest.fail('Idle scheduler resumed')
    assert list(inspected_results(items(),SimpleNamespace(),lambda *a:pytest.fail('Sentinel inspected'),
        workers=2,limit=5,deadline=float('inf'),tick=lambda:ticks.append(1)))==[]

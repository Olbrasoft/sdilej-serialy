import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier

from sdilej_serialy import replenish, dual
from sdilej_serialy.catalog import write_jsonl_gzip
from sdilej_serialy.discovery_index import CandidateIndex, indexed_order
from sdilej_serialy.language_hints import audio_language_hint, search_hint_rank
from sdilej_serialy.models import Episode
from sdilej_serialy.quality import SLOVAK_POLICY
from sdilej_serialy.requested_series import RequestedSeries
from sdilej_serialy.source_cache import SourceCache
from test_dual import source, setup_plan
from test_discovery_index import metadata, candidate
from test_requested_series import request_plan
from test_episodes import provider_with, episode


@pytest.mark.parametrize('title,language,rank', [
    ('Series S01E01 (CZ-SK tit)',None,2),
    ('Series S01E01 CZ-SKTit',None,2),
    ('Series S01E01 CZ titulky',None,2),
    ('Series S01E01 SKtit',None,2),
    ('Series S01E01 CZ Dabing','cs',0),
    ('Series S01E01 CZDAB','cs',0),
    ('Series S01E01 SK dabing CZ titulky','sk',1),
    ('Series S01E01 CZ dabing CZ-SK tit','cs',0),
    ('Series S01E01 CZ / SK dabing','cs',0),
    ('Series S01E01 česky','cs',0),
    ('Series S01E01 [ENG1080p]',None,2),
    ('Series S01E01 English dubbed',None,2),
    ('Series S01E01',None,1),
])
def test_subtitle_labels_do_not_claim_dubbed_audio(title,language,rank):
    assert audio_language_hint(title)==language
    assert search_hint_rank(title)==rank


def test_index_retains_only_exact_dubbed_hints_for_extra_samples():
    rows=[metadata(number=n) for n in range(1,6)]
    index=CandidateIndex(rows)
    index.observe([candidate('Series 1 S01E01 (CZ-SK tit)','1'),
        candidate('Series 1 S01E02 SK Dabing','2'),candidate('Series 1 S01E03 CZ','3'),
        candidate('Series 1 S01E04-05 CZ','4'),candidate('Other Series 1 S01E05 CZ','5')])
    assert index.dubbed_hints=={'1:1:2','1:1:3'}
    assert index.rank('1:1:1')==2


def failed_sample():
    return dict(request_probe_revision=1,observed_audio_language='en',status='deferred',
                preparation_revision=5,audio_policy_revision=1,search_recovery_revision=1,
                reason='verified_non_czech',retry_after=(datetime.now(UTC)+timedelta(hours=23)).isoformat())


def test_hint_waits_for_initial_samples_and_has_a_durable_three_probe_budget(tmp_path):
    path=tmp_path/'request.json'
    request_plan(path,[dict(season=1,episode=n) for n in range(1,10)])
    records,sources={},{}
    gate=RequestedSeries(path,'test',records,sources)
    gate.observe_hints({f'1:1:{n}' for n in range(4,10)})
    assert gate.extra_samples[1,1]==['1:1:4','1:1:5','1:1:6']
    assert not gate.available(source(number=4)['episode'])
    for n in (1,2,3):records[f'1:1:{n}']=failed_sample()
    assert gate.available(source(number=4)['episode'])
    assert not gate.available(source(number=5)['episode'])
    for n in (4,5,6):
        records[f'1:1:{n}']=dict(failed_sample(),request_hint_probe_revision=1)
    resumed=RequestedSeries(path,'test',records,sources)
    resumed.observe_hints({'1:1:7','1:1:8','1:1:9'})
    assert resumed.extra_samples[1,1]==['1:1:4','1:1:5','1:1:6']
    assert not resumed.available(source(number=7)['episode'])


def test_verified_later_sample_survives_restart_and_unlocks_only_its_season(tmp_path):
    path=tmp_path/'request.json';request_plan(path)
    row=source(number=4)
    row['requested_audio_check']=dict(revision=1,identity=row['identity'],source_id=row['selected']['source_id'],audio_language='cs')
    gate=RequestedSeries(path,'test',{}, {'1:1:4':row})
    assert gate.available(source(number=5)['episode'])
    assert not gate.available(dict(source(number=5)['episode'],season=2))
    row['quality_policy']='unverified-name-hint'
    assert not gate.available(source(number=5)['episode'])


def test_starved_scheduler_reserves_two_slots_for_dubbed_retries_without_starving_exploration():
    rows=[metadata(series=n) for n in range(1,21)];index=CandidateIndex(rows)
    index.observe([candidate('Series 1 S01E01 CZ-SK tit','1'),candidate('Series 2 S01E01 CZ Dabing','2'),
                   candidate('Series 3 S01E01 SK Dabing','3'),candidate('Series 4 S01E01 ENG','4')])
    fresh={Episode.from_dict(r).identity for r in rows[4:]}
    result=list(indexed_order(rows,fresh,set(),index,lambda:None,{},urgent=True))
    assert [r['series_id'] for r in result[:10]]==[2,3,5,6,7,8,9,10,1,4]
    assert len(result)==len({Episode.from_dict(r).identity for r in result})==20


@pytest.mark.parametrize('foreign',[False,True])
def test_real_publisher_checks_extra_hints_before_unlocking_neighbors(tmp_path,monkeypatch,foreign):
    directory,_,_=setup_plan(tmp_path,monkeypatch,count=4)
    original=(directory/'manifest.jsonl').read_bytes()
    catalog=[source(series=2,number=n)['episode'] for n in range(1,9)]
    write_jsonl_gzip(tmp_path/'backlog/series-episodes.jsonl.gz',catalog)
    path=tmp_path/'backlog/requested-series.json'
    request_plan(path,[dict(season=1,episode=n) for n in range(1,9)])
    plan=json.loads(path.read_text());plan['series'][0]['series_id']=2;path.write_text(json.dumps(plan))
    (tmp_path/'state').mkdir(exist_ok=True)
    state_path=tmp_path/'state/reserve-preparation.json'
    state_path.write_text(json.dumps(dict(schema_version=1,episodes={f'2:1:{n}':failed_sample() for n in (1,2,3)})))
    cache=SourceCache(tmp_path/'state/source-evidence-cache.json')
    for n in range(4,9):
        c=source(series=2,number=n)['selected']
        row={k:c[k] for k in ('source_id','url','title','size_bytes','duration_sec','width','height')}
        row['title']=f'Series 2 S01E{n:02d} CZ Dabing'
        cache.remember('search',str(n),lambda row=row:dict(candidates=[row],next=None))
    calls=[]
    def discover(ep):
        calls.append(ep.number)
        c=Candidate.from_dict(source(series=2,number=ep.number)['selected'])
        return replace(c,title=f'Series 2 {ep.code}',
            audio_language='en' if foreign else 'cs',
            language_tier=LanguageTier.FOREIGN_AUDIO if foreign else LanguageTier.CZECH_AUDIO)
    result=replenish.prepare(tmp_path,'test',SimpleNamespace(cache=cache,discover=discover),runtime_minutes=0)
    assert (directory/'manifest.jsonl').read_bytes()==original
    if foreign:
        assert result['prepared_this_run']==0 and calls==[4,5,6]
        assert not (directory/'additions.jsonl').exists()
    else:
        assert result['prepared_this_run']==5 and calls==[4,5,6,7,8]
        assert len(dual.load(tmp_path,'test')[2])==9
    state=json.loads(state_path.read_text())
    assert state['episodes']['2:1:4']['request_hint_probe_revision']==1


def test_audio_retry_uses_a_different_offset_and_keeps_confidence_gate(monkeypatch):
    from sdilej_to_prehrajto.language import LanguageDetectionError
    from sdilej_serialy.episodes import EpisodeSourceProvider
    c=Candidate.from_dict(source()['selected'])
    c=replace(c,title=f'{episode().series_title} {episode().code}')
    p=provider_with(monkeypatch,[c],lambda ep,c:c)
    p.detector=SimpleNamespace(_detect_at=lambda *a:None)
    offsets=[]
    def verify(ep,candidate,*,sample_offset=None):
        offsets.append(sample_offset)
        if sample_offset is None:raise LanguageDetectionError('Initial sample was inconclusive')
        return candidate
    monkeypatch.setattr(p,'_verify_language',verify)
    assert p.discover(episode()) is c and offsets==[None,60]
    offsets.clear()
    def inconclusive(ep,candidate,*,sample_offset=None):
        offsets.append(sample_offset)
        raise LanguageDetectionError('Both samples were inconclusive')
    monkeypatch.setattr(p,'_verify_language',inconclusive)
    assert p.discover(episode()) is None and offsets==[None,60]


def test_sampling_recovery_is_hint_scoped_and_only_once():
    now=datetime.now(UTC)
    record=dict(reason='inconclusive_audio',preparation_revision=5,
                retry_after=(now+timedelta(hours=1)).isoformat())
    assert not replenish.preparation_due(record,metadata(),None,now)
    assert replenish.preparation_due(record,metadata(),None,now,source_hint=True)
    record['audio_sampling_revision']=1
    assert not replenish.preparation_due(record,metadata(),None,now,source_hint=True)


def test_sampling_recovery_stays_in_the_bounded_retry_lane():
    rows=[metadata(series=n) for n in range(1,31)]
    index=CandidateIndex(rows)
    index.observe([candidate(f'Series {n} S01E01 CZ Dabing',str(n)) for n in range(1,11)])
    records={f'{n}:1:1':dict(reason='inconclusive_audio',preparation_revision=5,consecutive_failures=50)
             for n in range(1,11)}
    result=list(replenish.reserve_order(rows,records,{},set(),index=index,refresh=lambda:None,progress={}))
    assert [r['series_id'] for r in result[:10]]==list(range(11,19))+[1,2]


def test_empty_stock_does_not_let_requested_series_starve_the_rest_of_the_catalog():
    rows=[metadata(series=s,number=n) for s in (1,2) for n in range(1,13)]
    index=CandidateIndex(rows)
    result=list(replenish.reserve_order(rows,{}, {},set(),index=index,refresh=lambda:None,
        progress={},requested=SimpleNamespace(series={1}),urgent=True))
    assert [r['series_id'] for r in result[:4]]==[1]*4
    assert 2 in [r['series_id'] for r in result[:8]]
    assert len(result)==len({Episode.from_dict(r).identity for r in result})==24

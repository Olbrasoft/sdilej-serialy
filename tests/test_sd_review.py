from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace
import pytest
from sdilej_to_prehrajto.models import Candidate,LanguageTier,MatchTier
from sdilej_serialy import cli
from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import EpisodeState
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.quality import QUALITY_POLICY


def test_reviewed_sd_is_eligible_when_no_better_czech_source_exists(tmp_path):
    manifest=SourceManifest(tmp_path/'manifest')
    for key,width,height in [('sd',854,480),('hd',1920,1080)]:
        manifest.add(dict(identity=key,quality_policy=QUALITY_POLICY,selected=dict(url='https://sdilej.cz/1/video',width=width,height=height,language_tier='czech_audio')))
    assert [r['identity'] for r in manifest.pending(set(),limit=1)] == ['sd']


def test_stale_hd_and_foreign_sources_are_not_uploadable(tmp_path):
    manifest = SourceManifest(tmp_path/'manifest')
    for key, policy, language in [('old', None, 'czech_audio'),
                                  ('foreign', QUALITY_POLICY, 'foreign_audio'),
                                  ('ready', QUALITY_POLICY, 'czech_audio')]:
        manifest.add(dict(identity=key, quality_policy=policy, selected=dict(
            url='https://sdilej.cz/1/video', width=1920, height=1080, language_tier=language)))
    assert [r['identity'] for r in manifest.pending(set(), limit=1)] == ['ready']


@pytest.mark.parametrize('already_uploaded',[False,True])
@pytest.mark.parametrize('old_resolution',[(854,480),(1920,1080)])
def test_producer_rechecks_all_pending_but_never_uploaded_episodes(tmp_path,monkeypatch,already_uploaded,old_resolution):
    episode=Episode(1,3,'Test',None,1,1)
    sd=Candidate(source_id='1',url='https://sdilej.cz/1/video',title='Test S01E01',width=854,height=480,
                 size_bytes=100,duration_sec=100,language_tier=LanguageTier.CZECH_AUDIO,
                 match_tier=MatchTier.STRONG,audio_language='cs',language_probability=0.99)
    sd=replace(sd,width=old_resolution[0],height=old_resolution[1])
    hd=replace(sd,source_id='2',url='https://sdilej.cz/2/video',width=3840,height=2160)
    path=tmp_path/'manifest'
    manifest=SourceManifest(path)
    manifest.add(dict(identity=episode.identity,episode=episode.to_dict(),selected=sd.to_dict(),display_name='Test S01E01 SD'))
    manifest.save()
    if already_uploaded:
        uploads=EpisodeState(tmp_path/'state'/'episodes.json')
        uploads.success(episode,'123','Test S01E01 SD')
    called=[]
    def discover(e):
        called.append(e.identity)
        return hd
    monkeypatch.setattr(cli,'ROOT',tmp_path)
    monkeypatch.setattr(cli,'load_jsonl',lambda _: [episode.to_dict()])
    monkeypatch.setattr(cli.EpisodeSourceProvider,'authenticated',lambda *a:SimpleNamespace(discover=discover))
    monkeypatch.setenv('SDILEJ_EMAIL','test')
    monkeypatch.setenv('SDILEJ_PASSWORD','x')
    cli.prepare_queue(Namespace(backlog=tmp_path/'backlog',state=tmp_path/'source-state',manifest=path,
                               limit=1,workers=1,runtime_minutes=0,persist_git_state=False))
    result=SourceManifest(path).rows[episode.identity]
    if already_uploaded:
        assert not called
        assert result['selected']['source_id']=='1'
    else:
        assert called==[episode.identity]
        assert result['selected']['source_id']=='2'
        assert result['quality_policy']==QUALITY_POLICY

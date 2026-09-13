from types import SimpleNamespace
import pytest
import requests
from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.models import Episode
from sdilej_to_prehrajto.models import Candidate, LanguageTier
from sdilej_to_prehrajto.language import LanguageDetectionError


@pytest.mark.parametrize('filename',['Series S01E01.mkv','Series S01E01 CZ.mkv'])
def test_low_confidence_uses_dispersed_samples_even_without_filename_conflict(filename):
    calls=[]
    def consensus(url,duration,**kwargs):
        calls.append((duration,kwargs['initial']))
        return 'cs',0.98
    detector=SimpleNamespace(detect=lambda url:('cs',0.4),detect_consensus=consensus)
    provider=EpisodeSourceProvider(requests.Session(),detector=detector)
    result=provider._verify_language(Episode(1,1,'Series',None,1,1),Candidate(
        source_id='1',url='https://sdilej.cz/1/video',title='Series S01E01',
        filename=filename,duration_sec=2971,sample_url='https://example.test/media'))
    assert result.language_tier==LanguageTier.CZECH_AUDIO
    assert result.language_probability==0.98
    assert calls==[(2971,('cs',0.4))]


def test_weak_consensus_does_not_relax_language_requirement():
    detector=SimpleNamespace(detect=lambda url:('cs',0.4),
                             detect_consensus=lambda *a,**kw:('cs',0.5))
    provider=EpisodeSourceProvider(requests.Session(),detector=detector)
    with pytest.raises(LanguageDetectionError):
        provider._verify_language(Episode(1,1,'Series',None,1,1),Candidate(
            source_id='1',url='x',title='Series S01E01',filename='Series.mkv'))


def test_audio_timeout_defers_episode_without_crashing_producer(monkeypatch):
    import subprocess
    item=Candidate(source_id='1',url='x',title='Series S01E01',width=1920,height=1080,size_bytes=100)
    provider=EpisodeSourceProvider(requests.Session(),detector=object())
    monkeypatch.setattr(provider,'search',lambda e:[item])
    monkeypatch.setattr(provider,'_inspect',lambda e,c:c)
    def verify(*args):
        raise subprocess.TimeoutExpired('ffmpeg',120)
    monkeypatch.setattr(provider,'_verify_language',verify)
    assert provider.discover(Episode(1,1,'Series',None,1,1)) is None

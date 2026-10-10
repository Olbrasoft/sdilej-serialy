import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

from sdilej_serialy import episodes
from sdilej_serialy.source_cache import SourceCache, media_key, safe_value
from sdilej_to_prehrajto.models import Candidate, LanguageTier
from test_episodes import episode


def media(width=1920):
    return dict(video_codec='h264', width=width, height=1080, duration_sec=1400)


def test_cache_survives_restart_expires_and_invalidates_changed_original(tmp_path):
    now = [100]
    path = tmp_path / 'cache.json'
    cache = SourceCache(path, clock=lambda: now[0])
    original = Candidate('123', 'https://sdilej.cz/123/test.mkv', 'test', size_bytes=10000000)
    key = media_key(original)
    assert cache.remember('media', key, media, ttl=50) == media()
    cache.save()
    again = SourceCache(path, clock=lambda: now[0])
    assert again.remember('media', key, lambda: (_ for _ in ()).throw(AssertionError('Reprobe'))) == media()
    changed = replace(original, size_bytes=20000000)
    assert again.remember('media', media_key(changed), lambda: media(1280)) == media(1280)
    now[0] = 151
    assert again.remember('media', key, lambda: media(1440)) == media(1440)


def test_concurrent_cache_single_flight_and_inconclusive_results_are_not_cached(tmp_path):
    cache = SourceCache(tmp_path / 'cache.json')
    calls = []
    def inspect():
        calls.append(1)
        time.sleep(.02)
        return media()
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: cache.remember('media', 'same', inspect), range(4)))
    assert result == [media()] * 4 and len(calls) == 1
    for _ in range(2):
        assert cache.remember('media', 'failed', lambda: {}, cacheable=bool) == {}
    assert cache.metrics()['media_miss'] == 3
    assert cache.metrics()['media_hit'] == 3


def test_cache_schemas_reject_secrets_html_and_signed_urls(tmp_path):
    cache = SourceCache(tmp_path / 'cache.json')
    values = [dict(media(), download_url='https://cdn.invalid/?secret=private'),
              dict(language='cs', probability=.3), dict(html='<a>secret</a>'),
              dict(language='cs', probability=.9, cookie='private')]
    for value in values:
        namespace = 'audio' if 'language' in value else 'media'
        assert not safe_value(namespace, value)
        cache.remember(namespace, 'invalid', lambda: value)
    cache.save()
    assert json.loads(cache.path.read_text())['entries'] == {}
    assert 'private' not in cache.path.read_text()


def test_resume_keeps_probes_and_verified_foreign_audio_but_rechecks_unresolved_candidate(tmp_path, monkeypatch):
    ep = episode()
    originals = [Candidate('123', 'https://sdilej.cz/123/test.mkv', f'{ep.series_title} {ep.code}',
                           width=1920, height=1080, duration_sec=1400, size_bytes=20000000),
                 Candidate('456', 'https://sdilej.cz/456/test.mkv', f'{ep.series_title} {ep.code}',
                           width=1280, height=720, duration_sec=1400, size_bytes=10000000)]
    probes, samples, resolved = [], [], []
    low_confident = [False]
    def parse(_, candidate):
        return replace(candidate, filename=candidate.title + '.mkv',
                       download_url=f'https://cdn.invalid/{candidate.source_id}?secret=private',
                       sample_url=f'https://cdn.invalid/{candidate.source_id}?secret=private')
    def resolve(_, candidate, *, evidence):
        resolved.append(candidate.source_id)
        evidence.update(etag=candidate.source_id)
        return candidate
    def probe(url):
        probes.append(url)
        return media() if '/123?' in url else dict(media(1280), height=720)
    def detect(url):
        samples.append(url)
        return ('en', .99) if '/123?' in url else ('cs', .99 if low_confident[0] else .3)
    monkeypatch.setattr(episodes, 'parse_detail_html', parse)
    monkeypatch.setattr(episodes, 'resolve_original', resolve)
    monkeypatch.setattr(episodes, 'probe_media', probe)
    path = tmp_path / 'cache.json'
    def provider():
        p = episodes.EpisodeSourceProvider(None, cache=SourceCache(path), detector=SimpleNamespace(detect=detect))
        monkeypatch.setattr(p, '_get', lambda _: SimpleNamespace(text='fresh detail'))
        return p
    first = provider()
    assert first._select_originals(ep, originals, float('inf')) is None
    assert first.last_outcome == 'inconclusive_audio'
    first.cache.save()
    low_confident[0] = True
    resumed = provider()
    chosen = resumed._select_originals(ep, originals, float('inf'))
    assert chosen.source_id == '456' and chosen.language_tier == LanguageTier.CZECH_AUDIO
    assert len(probes) == 2 and len(resolved) == 4
    assert sum('/123?' in url for url in samples) == 1
    assert resumed.cache.metrics()['media_hit'] == 2
    assert resumed.cache.metrics()['audio_hit'] == 1
    resumed.cache.save()
    assert 'private' not in path.read_text() and 'cdn.invalid' not in path.read_text()


def test_shared_audio_model_is_serialized_and_sessions_are_independent(tmp_path):
    active, peak = [0], [0]
    def detect(_):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(.02)
        active[0] -= 1
        return 'cs', .99
    import requests
    original = episodes.EpisodeSourceProvider(requests.Session(), detector=SimpleNamespace(detect=detect))
    worker = original.fork_worker()
    assert worker.session is not original.session
    assert worker.audio_lock is original.audio_lock
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda p: p._verify_language(episode(), Candidate('123', 'url', 'title', sample_url='sample')),
                      [original, worker]))
    assert peak[0] == 1
    worker.session.close()
    original.session.close()

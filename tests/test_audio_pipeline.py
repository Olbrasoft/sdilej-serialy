import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdilej_serialy.audio_pipeline import PipelinedLanguageDetector
from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.models import Candidate
from test_episodes import episode


def test_provider_overlaps_sample_downloads_but_serializes_one_model(monkeypatch):
    detector = PipelinedLanguageDetector()
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    paths, offsets = [], []
    active, peak = [0], [0]

    def extract(url, offset, sample):
        with lock:
            paths.append(sample)
            offsets.append(offset)
        sample.write_bytes(b'audio')
        # This fails if the provider still holds the old whole-detect lock.
        barrier.wait(timeout=3)

    def transcribe(path, **kwargs):
        assert Path(path).read_bytes() == b'audio'
        assert kwargs == dict(beam_size=1, vad_filter=True)
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(.02)
        with lock:
            active[0] -= 1
        return iter(()), SimpleNamespace(language='cs', language_probability=.99)

    monkeypatch.setattr(detector, '_extract', extract)
    monkeypatch.setattr(detector, '_load_model', lambda: SimpleNamespace(transcribe=transcribe))
    import requests
    provider = EpisodeSourceProvider(requests.Session(), detector=detector)
    providers = [provider] + [provider.fork_worker() for _ in range(3)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = list(pool.map(lambda p: p._verify_language(episode(), Candidate(
                '123', 'detail', 'title', sample_url='https://signed.invalid/secret')), providers))
        assert all(r.audio_language == 'cs' and r.language_probability == .99 for r in result)
        assert peak[0] == 1 and len(set(paths)) == 4
        assert offsets == [180] * 4 and detector.seconds == 75
        assert not any(path.exists() for path in paths)
        assert detector.metrics()['sample_count'] == 4
        assert detector.metrics()['inference_wait_seconds'] > 0
        assert 'secret' not in str(detector.metrics())
    finally:
        for p in providers:
            p.session.close()


def test_consensus_keeps_sdk_offsets_votes_and_confidence(monkeypatch):
    detector = PipelinedLanguageDetector()
    offsets = []
    monkeypatch.setattr(detector, '_detect_at', lambda url, offset: offsets.append(offset) or ('cs', .9))
    assert detector.detect_consensus('sample', 1600, initial=('en', .8), preferred_language='cs') == ('cs', .9)
    assert offsets == [400, 800, 1200]


@pytest.mark.parametrize('duration,expected', [(420, [105, 210, 315]), (100, [0, 12]), (30, [])])
def test_short_episode_consensus_never_seeks_past_end_or_duplicates_initial(monkeypatch, duration, expected):
    detector = PipelinedLanguageDetector()
    offsets = []
    def detect(url, offset):
        assert 0 <= offset < duration
        assert offset + min(detector.seconds, duration) <= duration
        offsets.append(offset)
        return 'cs', .95
    monkeypatch.setattr(detector, '_detect_at', detect)
    initial = detector.detect_for_duration('sample', duration)
    initial_offset = offsets.pop()
    assert initial_offset == min(180, max(0, duration - 75))
    assert detector.detect_consensus('sample', duration, initial=initial, preferred_language='cs') == ('cs', .95)
    assert offsets == expected
    assert initial_offset not in offsets and len(set(offsets)) == len(offsets)


def test_short_episode_provider_rechecks_disagreement_inside_real_duration(monkeypatch):
    detector = PipelinedLanguageDetector()
    offsets = []
    def detect(url, offset):
        offsets.append(offset)
        assert offset + 75 <= 420
        return ('en', .8) if offset == 180 else ('cs', .9)
    monkeypatch.setattr(detector, '_detect_at', detect)
    provider = EpisodeSourceProvider(None, detector=detector)
    result = provider._verify_language(episode(), Candidate('1', 'url', 'title',
        filename='Episode CZ dabing.mkv', sample_url='sample', duration_sec=420))
    assert result.audio_language == 'cs' and result.language_probability == .9
    assert offsets == [180, 105, 210, 315]


def test_failed_download_never_loads_model_or_leaks_sample(monkeypatch):
    detector = PipelinedLanguageDetector()
    paths = []
    def extract(url, offset, path):
        paths.append(path)
        path.write_bytes(b'partial')
        raise LanguageDetectionError('ffmpeg failed')
    monkeypatch.setattr(detector, '_extract', extract)
    monkeypatch.setattr(detector, '_load_model', lambda: pytest.fail('No complete sample'))
    with pytest.raises(LanguageDetectionError):
        detector.detect('signed-url')
    assert not paths[0].exists()
    assert detector.metrics()['failures'] == 1
    assert detector.metrics()['sample_count'] == 1


def test_extraction_uses_original_and_sdk_decoder_limits(tmp_path, monkeypatch):
    from sdilej_serialy import audio_pipeline
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        Path(command[-1]).write_bytes(b'audio')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(audio_pipeline.subprocess, 'run', run)
    monkeypatch.setenv('WHISPER_FFMPEG_TIMEOUT_SECONDS', '130')
    detector = PipelinedLanguageDetector()
    detector._extract('original', 180, tmp_path / 'sample.wav')
    command, kwargs = commands[0]
    for option, value in [('-ss', '180'), ('-t', '75'), ('-i', 'original'),
                          ('-ac', '1'), ('-ar', '16000'), ('-rw_timeout', '130000000')]:
        assert command[command.index(option) + 1] == value
    assert kwargs['timeout'] == 130 and kwargs['capture_output']

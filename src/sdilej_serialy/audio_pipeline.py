"""Overlap remote sample extraction while keeping one bounded Whisper model."""
import os
import subprocess
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from sdilej_to_prehrajto.language import LanguageDetectionError, WhisperLanguageDetector


class PipelinedLanguageDetector(WhisperLanguageDetector):
    # The provider may overlap detect/consensus calls. Only this implementation
    # owns the narrower lock; unknown/custom detectors retain the outer lock.
    concurrent_samples = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.inference_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.stats = Counter()

    def _add(self, **values):
        with self.metrics_lock:
            self.stats.update(values)

    def metrics(self):
        with self.metrics_lock:
            return {key: round(value, 2) for key, value in self.stats.items()}

    def initial_offset(self, duration_sec):
        offset = max(0, int(os.environ.get('WHISPER_SAMPLE_OFFSET', '180')))
        return min(offset, max(0, int(duration_sec) - self.seconds)) if duration_sec else offset

    def detect_for_duration(self, media_url, duration_sec):
        return self._detect_at(media_url, self.initial_offset(duration_sec))

    def detect_consensus(self, media_url, duration_sec, *, initial=None, preferred_language=None):
        if not duration_sec or duration_sec >= 900:
            return super().detect_consensus(media_url, duration_sec, initial=initial,
                                            preferred_language=preferred_language)
        # The SDK assumes a minimum 15-minute movie. That seeks beyond the end
        # of short episodes, turning a valid Czech sample into a decoder error.
        maximum = max(0, int(duration_sec) - self.seconds)
        offsets = ([0, maximum // 2, maximum] if duration_sec < 2 * self.seconds else
                   [min(int(duration_sec * fraction), maximum) for fraction in (.25, .5, .75)])
        used = {self.initial_offset(duration_sec)} if initial else set()
        samples = [initial] if initial else []
        for offset in offsets:
            if offset not in used:
                samples.append(self._detect_at(media_url, offset))
                used.add(offset)
        # Preserve the SDK's confidence/preferred-language rule; repeated
        # clamped offsets must not count as independent consensus votes.
        grouped = defaultdict(list)
        for language, probability in samples:
            grouped[language.lower()].append(float(probability))
        preferred = (preferred_language or '').lower()
        if preferred in grouped and max(grouped[preferred]) >= .55:
            return preferred, max(grouped[preferred])
        winner, probabilities = max(grouped.items(), key=lambda item: (len(item[1]), sum(item[1]), item[0]))
        return winner, sum(probabilities) / len(probabilities)

    def _extract(self, media_url, offset, sample):
        timeout = max(self.seconds + 30, int(os.environ.get('WHISPER_FFMPEG_TIMEOUT_SECONDS', '120')))
        # Same source, sample length and decoder settings as the SDK.
        # Signed URLs live only in memory/the child process, never in reports.
        result = subprocess.run([
            'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-rw_timeout', str(timeout * 1_000_000), '-ss', str(offset),
            '-t', str(self.seconds), '-i', media_url, '-vn', '-ac', '1',
            '-ar', '16000', str(sample),
        ], capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0 or not sample.exists() or sample.stat().st_size == 0:
            raise LanguageDetectionError('ffmpeg could not create an audio sample')

    def _detect_at(self, media_url, offset):
        try:
            with tempfile.TemporaryDirectory() as directory:
                sample = Path(directory) / 'sample.wav'
                started = time.monotonic()
                try:
                    self._extract(media_url, offset, sample)
                finally:
                    self._add(sample_count=1, download_seconds=time.monotonic() - started)
                waiting = time.monotonic()
                with self.inference_lock:
                    self._add(inference_wait_seconds=time.monotonic() - waiting)
                    started = time.monotonic()
                    try:
                        # Loading and language detection remain serialized.
                        # transcribe computes language eagerly; its unused
                        # segment generator is never iterated, as in the SDK.
                        model = self._load_model()
                        _, info = model.transcribe(str(sample), beam_size=1, vad_filter=True)
                    finally:
                        self._add(inference_seconds=time.monotonic() - started)
                language = (info.language or '').lower()
                if not language:
                    raise LanguageDetectionError('Whisper returned no language')
                return language, float(info.language_probability or 0.0)
        except Exception:
            self._add(failures=1)
            raise

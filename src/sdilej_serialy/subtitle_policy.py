"""Explicit English episode discovery with verified original Czech subtitles."""
from __future__ import annotations

import hashlib
import re
import subprocess
import time

import requests
from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.sdilej import SdilejError

from .quality import resolution_rank
from .subtitle_media import extract_original, normalize_srt, SubtitleUnavailable


def subtitle_evidence(payload, candidate, provenance):
    normalized = normalize_srt(payload)
    timings = re.findall(rb'(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2,}):(\d{2}):(\d{2}),(\d{3})', normalized)
    if len(timings) < 20:
        raise SubtitleUnavailable('source_czech_full_text_missing')
    hours, minutes, seconds, millis = map(int, timings[-1][4:])
    end_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    if end_ms < candidate.duration_sec * 500:
        raise SubtitleUnavailable('source_czech_full_text_missing')
    return dict(language='cs', full=True, source_id=candidate.source_id,
                source_url=candidate.url, source_size_bytes=candidate.size_bytes,
                sha256=hashlib.sha256(normalized).hexdigest(), size_bytes=len(normalized),
                cue_count=len(timings), end_ms=end_ms, provenance=provenance)


def discover_subtitled(provider, episode):
    """Inspect every original, then English/full-Czech-text, resolution, size."""
    deadline = time.monotonic() + provider.discovery_timeout_seconds
    provider.last_outcome = 'transient_search'
    previous_fresh = getattr(provider, '_fresh_search', False)
    provider._fresh_search = True
    try:
        candidates = provider.search(episode)
        if not candidates:
            candidates = provider.search(episode)
    except (SdilejError, requests.RequestException):
        return None
    finally:
        provider._fresh_search = previous_fresh
    if not candidates:
        provider.last_outcome = 'no_matches'
        return None
    originals = []
    provider.last_outcome = 'transient_original'
    for candidate in candidates:
        original = None
        for attempt in range(2):
            if time.monotonic() >= deadline:
                return None
            try:
                original = provider._inspect(episode, candidate)
                break
            except (SdilejError, requests.RequestException):
                if attempt:
                    return None
        if original:
            originals.append(original)
    for original in sorted(originals, key=lambda c: (-resolution_rank(c.width, c.height), c.size_bytes, c.source_id)):
        provider.last_outcome = 'inconclusive_audio'
        checked = None
        for attempt in range(2):
            if time.monotonic() >= deadline:
                return None
            try:
                checked = provider._verify_language(episode, original)
                break
            except (SdilejError, LanguageDetectionError, requests.RequestException, subprocess.TimeoutExpired):
                if attempt:
                    return None
        if checked.audio_language != 'en' or (checked.language_probability or 0) < .65:
            continue
        provider.last_outcome = 'inconclusive_subtitles'
        try:
            payload, provenance = extract_original(provider.session,
                dict(source_id=checked.source_id, source_url=checked.url, source_filename=checked.filename), deadline=deadline)
            checked._subtitle_evidence = subtitle_evidence(payload, checked, provenance)
        except SubtitleUnavailable as error:
            if str(error) in {'source_czech_text_missing', 'source_czech_full_text_unsupported', 'source_czech_full_text_missing'}:
                continue
            return None
        except (requests.RequestException, subprocess.TimeoutExpired):
            return None
        provider.last_outcome = 'verified_english_czech_subtitles'
        return checked
    provider.last_outcome = 'no_english_czech_subtitles'
    return None

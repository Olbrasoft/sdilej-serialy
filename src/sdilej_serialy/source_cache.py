"""Bounded, durable source evidence; never store authenticated media URLs."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

CACHE_REVISION = 1


def stable_url(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.netloc == 'sdilej.cz'
            and not parts.query and not parts.fragment and not parts.username)


def search_url(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.netloc == 'sdilej.cz'
            and '/s/-6' in parts.path and not parts.fragment
            and all(k in ('page', 'p') and v.isdigit()
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)))


def safe_value(namespace, value):
    """Explicit stage schemas prevent signed links or credentials in cache."""
    if not isinstance(value, dict):
        return False
    if namespace == 'audio':
        probability = value.get('probability')
        return (set(value) == {'language', 'probability'}
                and isinstance(value.get('language'), str)
                and re.fullmatch(r'[a-z]{2,3}', value['language']) is not None
                and isinstance(probability, (int, float)) and math.isfinite(probability)
                and .65 <= probability <= 1)
    if namespace == 'media':
        allowed = {'video_codec', 'width', 'height', 'duration_sec'}
        return (set(value) == allowed
                and (value['video_codec'] is None or re.fullmatch(r'[a-z0-9_]+', value['video_codec']) is not None)
                and all(v is None or isinstance(v, int) and v >= 0
                        for k, v in value.items() if k != 'video_codec'))
    if namespace == 'search':
        fields = {'source_id', 'url', 'title', 'size_bytes', 'duration_sec', 'width', 'height'}
        return (set(value) == {'candidates', 'next'}
                and (value['next'] is None or search_url(value['next']))
                and isinstance(value['candidates'], list)
                and all(isinstance(r, dict) and set(r) == fields
                        and isinstance(r['source_id'], str) and r['source_id'].isdigit()
                        and stable_url(r['url']) and isinstance(r['title'], str)
                        and '://' not in r['title'] for r in value['candidates']))
    return False


def media_key(candidate):
    # Re-resolve the authenticated original before looking up evidence. A
    # changed byte size/title/preview metadata invalidates both probe and audio.
    return json.dumps([candidate.source_id, candidate.url, candidate.filename,
                       candidate.size_bytes, candidate.width, candidate.height,
                       candidate.duration_sec], ensure_ascii=False)


class SourceCache:
    def __init__(self, path: Path, *, max_entries=10000, clock=time.time):
        self.path, self.max_entries, self.clock = path, max_entries, clock
        self.lock = threading.RLock()
        self.flights = {}
        self.stats = Counter()
        payload = json.loads(path.read_text()) if path.exists() else {}
        self.entries = payload.get('entries', {}) if payload.get('revision') == CACHE_REVISION else {}

    def remember(self, namespace, key, compute, *, ttl=86400, cacheable=lambda value: True, force=False):
        digest = hashlib.sha256(f'{namespace}:{key}'.encode()).hexdigest()
        # One computation per key even when two episodes share search pages.
        # Failed/inconclusive work is not cached and must be retried next time.
        with self.lock:
            flight = self.flights.setdefault(digest, threading.Lock())
        with flight:
            with self.lock:
                entry = self.entries.get(digest)
                if (not force and entry and entry['expires_at'] > self.clock()
                        and safe_value(namespace, entry['value'])):
                    self.stats[f'{namespace}_hit'] += 1
                    return json.loads(json.dumps(entry['value']))
                self.stats[f'{namespace}_miss'] += 1
            started = time.monotonic()
            try:
                value = compute()
            finally:
                with self.lock:
                    self.stats[f'{namespace}_seconds'] += round(time.monotonic() - started, 2)
            if cacheable(value) and safe_value(namespace, value):
                with self.lock:
                    self.entries[digest] = dict(namespace=namespace, expires_at=self.clock() + ttl, value=value)
            return value

    def save(self):
        from .pipeline import atomic_json
        # Called only by the publisher; workers only update memory. Snapshot
        # under the lock so a checkpoint cannot include a half-written value.
        with self.lock:
            now = self.clock()
            entries = {k: v for k, v in self.entries.items() if v['expires_at'] > now
                       and safe_value(v.get('namespace'), v['value'])}
            if len(entries) > self.max_entries:
                entries = dict(sorted(entries.items(), key=lambda item: item[1]['expires_at'],
                                      reverse=True)[:self.max_entries])
            self.entries = entries
            atomic_json(self.path, dict(revision=CACHE_REVISION, entries=entries))

    def metrics(self):
        with self.lock:
            return {k: round(v, 2) for k, v in self.stats.items()}


class RequestGate:
    """Keep the existing site-wide request spacing across source workers."""
    def __init__(self, gap_seconds=2):
        self.gap_seconds = gap_seconds
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self):
        with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + self.gap_seconds

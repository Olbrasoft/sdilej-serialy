"""Validated source-only recovery for failed rows in an immutable target queue."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from urllib.parse import urlsplit

from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier

from .episodes import display_name, episode_match, runtime_acceptable
from .manifest import SourceManifest
from .models import Episode
from .pipeline import now_iso
from .quality import QUALITY_POLICY, quality_acceptable
from .source_audit import fingerprint
from .target import episode_key

REPAIR_POLICY = 'unavailable-original-discovery-v1'


def repairable(record):
    attempts = record.get('attempts', [])
    return (bool(attempts) and attempts[-1]['error'] == 'SourceUnavailable'
            and not any(record.get(k) for k in ('upload', 'prepared_target', 'claim')))


def repair_entry(base, replacement, plan):
    """Only a complete best-source discovery may replace an unavailable original."""
    reviewed_at = now_iso()
    return dict(replacement, source_repair=dict(
        policy=REPAIR_POLICY, generation=plan['generation'],
        base_manifest_sha256=plan['manifest_sha256'], previous_fingerprint=fingerprint(base),
        reviewed_at=reviewed_at,
        token=hashlib.sha256((fingerprint(replacement) + reviewed_at).encode()).hexdigest()))


def repaired_row(base, entry, plan):
    SourceManifest._validate(entry)
    review = entry.get('source_repair', {})
    episode, other = Episode.from_dict(base['episode']), Episode.from_dict(entry['episode'])
    candidate = Candidate.from_dict(entry['selected'])
    detail = urlsplit(candidate.url)
    tier, _ = episode_match(episode, candidate.title)
    if (review.get('policy') != REPAIR_POLICY or review.get('generation') != plan['generation']
            or review.get('base_manifest_sha256') != plan['manifest_sha256']
            or review.get('previous_fingerprint') != fingerprint(base)
            or not re.fullmatch(r'[0-9a-f]{64}', review.get('token', ''))
            or detail.scheme != 'https' or detail.hostname not in ('sdilej.cz', 'www.sdilej.cz')
            or not detail.path.startswith(f'/{candidate.source_id}/') or detail.query or detail.fragment
            or entry['identity'] != base['identity'] or episode.identity != other.identity
            or episode.episode_id != other.episode_id
            or episode_key(entry['display_name']) != episode_key(base['display_name'])
            or entry.get('quality_policy') != QUALITY_POLICY
            or candidate.language_tier != LanguageTier.CZECH_AUDIO or candidate.audio_language != 'cs'
            or (candidate.language_probability or 0) < .65
            or candidate.match_tier not in (MatchTier.STRONG, MatchTier.SOLID)
            or tier not in (MatchTier.STRONG, MatchTier.SOLID)
            or not runtime_acceptable(episode, candidate.duration_sec) or not quality_acceptable(candidate)):
        raise ValueError('Source repair lacks matching queue, episode or verified Czech original')
    # Ownership, rank and metadata are always taken from the frozen queue, never
    # from an overlay. A lower resolution is allowed only after full discovery.
    return dict(base, selected=entry['selected'], quality_policy=QUALITY_POLICY,
                display_name=display_name(episode, candidate), source_repair=review)


class SourceRepairFeed:
    def __init__(self, plan, rows, read_payload):
        self.plan, self.rows, self.read_payload = plan, rows, read_payload
        self.entries = {}
        self.lock = threading.RLock()
        self.last_read = float('-inf')

    def refresh(self, *, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_read < 15:
                return
            entries = {}
            mapping = {r['identity']: r for r in self.rows}
            used = {r['selected']['source_id']: r['identity'] for r in self.rows}
            for line in self.read_payload().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                identity = entry['identity']
                if identity in entries or identity not in mapping:
                    raise ValueError('Duplicate or unknown source repair identity')
                row = repaired_row(mapping[identity], entry, self.plan)
                source_id = row['selected']['source_id']
                if used.get(source_id, identity) != identity:
                    raise ValueError('One repaired source is assigned to multiple episodes')
                used[source_id] = identity
                entries[identity] = row
            self.entries = entries
            self.last_read = time.monotonic()

    def ready(self, target_state):
        with self.lock:
            return {identity for identity, row in self.entries.items()
                    if repairable(target_state.get('episodes', {}).get(identity, {}))
                    and target_state['episodes'][identity].get('source_repair_token')
                    != row['source_repair']['token']}

    def select(self, row):
        self.refresh()
        with self.lock:
            entry = self.entries.get(row['identity'])
            return repaired_row(row, entry, self.plan) if entry else row

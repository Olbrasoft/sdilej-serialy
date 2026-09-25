"""Resumable SD/720p review and validated upgrades for an immutable upload queue."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import language_tier, resolution_rank

from .catalog import load_jsonl
from .episodes import EpisodeSourceProvider, display_name, episode_match, runtime_acceptable
from .git_state import GitCheckpointPersister
from .manifest import SourceManifest
from .models import Episode
from .pipeline import atomic_json, now_iso
from .quality import QUALITY_POLICY, quality_acceptable
from .target import episode_key

UPGRADES_PATH = 'manifests/quality-upgrades.jsonl'
AUDIT_POLICY = 'sd-720-original-review-v1'


def low_resolution(row):
    candidate = row['selected']
    return resolution_rank(candidate.get('width', 0), candidate.get('height', 0)) <= 2


def fingerprint(row):
    payload = json.dumps({k: row.get(k) for k in ('identity', 'selected', 'quality_policy')}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def preference(candidate):
    return (int(candidate.language_tier), -resolution_rank(candidate.width, candidate.height),
            candidate.size_bytes if candidate.size_bytes and candidate.size_bytes > 0 else float('inf'))


def upgraded_row(base, replacement, *, czech_only=False):
    """Keep ownership/order fixed; reject wrong episodes, remakes and unverified media."""
    if not low_resolution(base):
        return base
    SourceManifest._validate(replacement)
    episode = Episode.from_dict(base['episode'])
    other = Episode.from_dict(replacement['episode'])
    candidate = Candidate.from_dict(replacement['selected'])
    tier, _ = episode_match(episode, candidate.title)
    if (replacement['identity'] != base['identity'] or episode.identity != other.identity
            or episode.episode_id != other.episode_id
            or episode_key(replacement['display_name']) != episode_key(base['display_name'])
            or replacement.get('quality_policy') != QUALITY_POLICY
            or tier not in (MatchTier.STRONG, MatchTier.SOLID)
            or candidate.match_tier not in (MatchTier.STRONG, MatchTier.SOLID)
            or not runtime_acceptable(episode, candidate.duration_sec)
            or not quality_acceptable(candidate)
            or not candidate.audio_language or (candidate.language_probability or 0) < .65
            or candidate.language_tier == LanguageTier.UNKNOWN
            or language_tier(candidate.audio_language) != candidate.language_tier):
        raise ValueError('Upgrade lacks matching episode, original media or verified language evidence')
    if czech_only and candidate.language_tier != LanguageTier.CZECH_AUDIO:
        return base
    if preference(candidate) >= preference(Candidate.from_dict(base['selected'])):
        return base
    return dict(base, selected=replacement['selected'], quality_policy=QUALITY_POLICY,
                display_name=display_name(episode, candidate),
                source_review=replacement.get('source_review', {}))


class UpgradeFeed:
    def __init__(self, read_payload):
        self.read_payload = read_payload
        self.lock = threading.Lock()

    def select(self, row):
        if not low_resolution(row):
            return row
        # Fetch before each low-resolution claim is sent. Git transports only
        # changes; the small overlay is read locally from the fetched commit.
        with self.lock:
            entries = {}
            for line in self.read_payload().splitlines():
                if line.strip():
                    item = json.loads(line)
                    if item['identity'] in entries:
                        raise ValueError('Duplicate upgrade identity')
                    entries[item['identity']] = item
            replacement = entries.get(row['identity'])
            return upgraded_row(row, replacement, czech_only=True) if replacement else row


class AuditProvider(EpisodeSourceProvider):
    """Reuse complete broad-search pages across neighboring episodes in one run."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_pages = OrderedDict()

    def _get(self, url, *, session=None):
        path = urlsplit(url).path
        cacheable = session is None and '/s/-6' in path and not re.search(r's\d+e\d+', path, re.I)
        if cacheable and url in self.search_pages:
            self.search_pages.move_to_end(url)
            return self.search_pages[url]
        response = super()._get(url, session=session)
        if cacheable:
            self.search_pages[url] = response
            if len(self.search_pages) > 512:
                self.search_pages.popitem(last=False)
        return response


def due(row, record, now):
    if not low_resolution(row):
        return False
    if record.get('policy') != AUDIT_POLICY or record.get('fingerprint') != fingerprint(row):
        return True
    if record.get('status') in ('upgraded', 'no_better'):
        return False
    retry = record.get('retry_after')
    return not retry or datetime.fromisoformat(retry) <= now


def audit(root, provider, *, identities=None, limit=20, runtime_minutes=0, persist=False,
          generation=None, force=False):
    if limit < 1 or not 0 <= runtime_minutes <= 300:
        raise ValueError('Invalid audit limit or runtime')
    directory = root / 'audit/low-resolution'
    state_path, report_path = directory / 'state.json', directory / 'report.json'
    manifest_path, upgrades_path = root / 'manifests/selected-episodes.jsonl', root / UPGRADES_PATH
    manifest = SourceManifest(manifest_path)
    state = json.loads(state_path.read_text()) if state_path.exists() else dict(schema_version=1, episodes={})
    if state.get('schema_version') != 1:
        raise ValueError('Unsupported audit state')
    selected_ids = set(identities or ())
    if selected_ids - manifest.identities():
        raise ValueError('Requested audit identity is not in saved sources')
    queue_order = {}
    if generation:
        from .dual import directory_for
        queue_order = {r['identity']: r['queue_rank'] for r in load_jsonl(directory_for(root, generation) / 'manifest.jsonl')}
    now = datetime.now(UTC)
    rows = [r for r in manifest.rows.values() if low_resolution(r)
            and (not selected_ids or r['identity'] in selected_ids)
            and (force or due(r, state['episodes'].get(r['identity'], {}), now))]
    rows.sort(key=lambda r: (queue_order.get(r['identity'], float('inf')),
                             r['episode'].get('priority_rank') or 10**9, r['identity']))
    deadline = time.monotonic() + runtime_minutes * 60 if runtime_minutes else float('inf')
    persister = GitCheckpointPersister(root, (manifest_path, upgrades_path, report_path)) if persist else None
    reviewed = changed = 0
    for row in rows[:limit]:
        if time.monotonic() >= deadline:
            break
        identity = row['identity']
        record = dict(policy=AUDIT_POLICY, fingerprint=fingerprint(row), before=row['selected'], at=now_iso())
        improvement = None
        try:
            candidate = provider.discover(Episode.from_dict(row['episode']))
            if candidate is None:
                # None also means an unresolved candidate or a timeout, not
                # evidence that a lower-resolution source is the best available.
                record['status'] = 'deferred'
                record['reason'] = 'discovery_inconclusive'
            else:
                episode = Episode.from_dict(row['episode'])
                replacement = dict(row, selected=candidate.to_dict(), quality_policy=QUALITY_POLICY,
                                   display_name=display_name(episode, candidate),
                                   source_review=dict(policy=AUDIT_POLICY, reviewed_at=now_iso(),
                                                      previous_source_id=row['selected']['source_id']))
                # Reload after discovery so unrelated local changes survive.
                manifest = SourceManifest(manifest_path)
                current = manifest.rows[identity]
                improved = upgraded_row(current, replacement)
                if improved != current:
                    improvement = improved
                    record.update(status='upgraded', after=improved['selected'], fingerprint=fingerprint(improved))
                else:
                    record.update(status='no_better', fingerprint=fingerprint(current),
                                  best_found=candidate.to_dict())
        except Exception as error:
            record.update(status='deferred', reason=type(error).__name__)
        # Publishing failures must stop the run, not commit a partial update as
        # a deferred review. The complete set is pushed as one Git checkpoint.
        if improvement is not None:
            feed = SourceManifest(upgrades_path)
            feed.add(improvement)
            manifest.add(improvement)
            feed.save()
            manifest.save()
            changed += 1
        if record['status'] == 'deferred':
            record['retry_after'] = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
        state['episodes'][identity] = record
        state['updated_at'] = now_iso()
        atomic_json(state_path, state)
        reviewed += 1
        summary = Counter(r['status'] for r in state['episodes'].values())
        report = dict(policy=AUDIT_POLICY, reviewed_total=len(state['episodes']), statuses=dict(summary),
                      reviewed_this_run=reviewed, upgraded_this_run=changed, updated_at=now_iso())
        atomic_json(report_path, report)
        if persister:
            persister(state_path)
        print(f"quality_audit identity={identity} status={record['status']}", flush=True)
    return dict(reviewed_this_run=reviewed, upgraded_this_run=changed,
                reviewed_total=len(state['episodes']), due_at_start=len(rows))


def main():
    import argparse
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity', action='append')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--runtime-minutes', type=int, default=0)
    parser.add_argument('--generation')
    parser.add_argument('--persist-git-state', action='store_true')
    parser.add_argument('--force', action='store_true', help='Recheck completed low-resolution reviews')
    args = parser.parse_args()
    if os.environ.get('QUALITY_AUDIT_ENABLED') != 'true':
        raise SystemExit('Quality audit requires QUALITY_AUDIT_ENABLED=true')
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path(__file__).resolve().parents[2])).resolve()
    directory = root / 'audit/low-resolution'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provider = AuditProvider.authenticated(os.environ['SDILEJ_EMAIL'], os.environ['SDILEJ_PASSWORD'],
                                              discovery_timeout_seconds=900)
        try:
            result = audit(root, provider, identities=args.identity, limit=args.limit,
                           runtime_minutes=args.runtime_minutes, persist=args.persist_git_state,
                           generation=args.generation, force=args.force)
            print(json.dumps(result))
        finally:
            provider.session.close()


if __name__ == '__main__':
    main()

"""Prepare new Czech originals from the cached catalog and append a queue reserve."""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier

from .catalog import load_jsonl
from .dual import ACCOUNTS, load
from .episodes import display_name, episode_match, runtime_acceptable
from .git_state import GitCheckpointPersister, persist_source_checkpoint
from .manifest import SourceManifest
from .models import Episode
from .pipeline import atomic_json, now_iso
from .quality import QUALITY_POLICY, quality_acceptable
from .source_audit import AuditProvider, low_resolution
from .target import episode_key
from .resilience import error_evidence

PREPARATION_REVISION = 2
SAVED_REVIEW_REVISION = 1
MAX_SERIES_MISSES = 3


def legacy_czech(row):
    selected = (row or {}).get('selected', {})
    return (bool(row) and row.get('quality_policy') != QUALITY_POLICY
            and selected.get('language_tier') == 'czech_audio'
            and selected.get('audio_language') == 'cs'
            and (selected.get('language_probability') or 0) >= .65)


def needs_saved_review(row, record):
    return legacy_czech(row) and record.get('saved_review_revision') != SAVED_REVIEW_REVISION


def reserve_order(catalog, records, saved, paused):
    """Drain the known-source backlog before broad discovery, HD originals first."""
    fast, low, ordinary = [], [], []
    for metadata in catalog:
        identity = Episode.from_dict(metadata).identity
        row = saved.get(identity)
        if needs_saved_review(row, records.get(identity, {})):
            (low if low_resolution(row) else fast).append(metadata)
        else:
            ordinary.append(metadata)
    for metadata in fast + low:
        if metadata['series_id'] not in paused:
            yield metadata
    yield from preparation_order(ordinary, records, paused)


def valid_czech(episode, candidate):
    tier, _ = episode_match(episode, candidate.title)
    return (candidate.language_tier == LanguageTier.CZECH_AUDIO and candidate.audio_language == 'cs'
            and (candidate.language_probability or 0) >= .65
            and candidate.match_tier in (MatchTier.STRONG, MatchTier.SOLID)
            and tier in (MatchTier.STRONG, MatchTier.SOLID)
            and runtime_acceptable(episode, candidate.duration_sec) and quality_acceptable(candidate))


def catalog_order(row):
    return (row.get('imdb_rating') is None, -(row.get('imdb_rating') or 0),
            -(row.get('imdb_votes') or 0), row['series_id'], row['season'], row['episode'])


def preparation_order(catalog, records, paused_series=None):
    """Reserve search time for unseen episodes without abandoning due retries."""
    fresh, retries = deque(), deque()
    paused_series = paused_series if paused_series is not None else set()
    for metadata in catalog:
        record = records.get(Episode.from_dict(metadata).identity, {})
        repaired_error = (record.get('reason') == 'TypeError'
                          and record.get('preparation_revision', 1) < PREPARATION_REVISION)
        (fresh if not record or repaired_error else retries).append(metadata)
    # Inputs retain IMDb/season order within each lane. A daily retry of a long
    # unavailable series must not consume every run before new series are seen.
    while fresh or retries:
        for lane, budget in ((fresh, 8), (retries, 2)):
            emitted = 0
            while lane and emitted < budget:
                metadata = lane.popleft()
                if metadata['series_id'] in paused_series:
                    continue
                emitted += 1
                yield metadata


def prepare(root, generation, provider, *, limit=500, runtime_minutes=110, persist=False, identities=None):
    if limit < 1 or not 0 <= runtime_minutes <= 110:
        raise ValueError('Invalid preparation limits')
    directory, plan, queue = load(root, generation)
    additions_path = directory / 'additions.jsonl'
    manifest_path = root / 'manifests/selected-episodes.jsonl'
    state_path = root / 'state/reserve-preparation.json'
    report_path = root / 'reports/reserve-preparation.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'schema_version': 1, 'episodes': {}}
    if state.get('schema_version') != 1:
        raise ValueError('Unsupported reserve preparation state')
    catalog = sorted(load_jsonl(root / 'backlog/series-episodes.jsonl.gz'), key=catalog_order)
    manifest = SourceManifest(manifest_path)
    # Historical sources can outlive a cached catalog entry. Keep their saved
    # episode metadata instead of silently losing them during queue migration.
    catalog_ids = {Episode.from_dict(r).identity for r in catalog}
    catalog.extend(dict(r['episode'], imdb_rating=r.get('imdb_rating'),
                        imdb_votes=r.get('imdb_votes') or 0)
                   for r in manifest.rows.values()
                   if legacy_czech(r) and r['identity'] not in catalog_ids)
    catalog.sort(key=catalog_order)
    selected_ids = set(identities or ())
    if selected_ids:
        if selected_ids - {Episode.from_dict(r).identity for r in catalog}:
            raise ValueError('Requested preparation identity is not in the cached catalog')
        catalog = [r for r in catalog if Episode.from_dict(r).identity in selected_ids]
    identities = {r['identity'] for r in queue}
    keys = {episode_key(r['display_name']) for r in queue}
    source_ids = {r['selected']['source_id'] for r in queue}
    saved_groups = defaultdict(dict)
    for saved in manifest.rows.values():
        if saved['selected'].get('language_tier') == 'czech_audio':
            saved_groups[episode_key(saved['display_name'])][saved['selected']['source_id']] = saved
    additions = load_jsonl(additions_path) if additions_path.exists() else []
    persister = GitCheckpointPersister(root, (manifest_path, additions_path, report_path),
                                     min_interval_seconds=15) if persist else None
    deadline = time.monotonic() + runtime_minutes * 60 if runtime_minutes else float('inf')
    attempted = published = 0
    saved_reviewed = saved_published = 0
    eligible = []
    for metadata in catalog:
        episode = Episode.from_dict(metadata)
        key = episode_key(f'{episode.series_title} {episode.code}')
        if episode.identity in identities or key in keys:
            continue
        record = state['episodes'].get(episode.identity, {})
        # Retry records affected by the pre-v2 audio dependency bug immediately,
        # once. Other inconclusive sources keep their normal cooldown.
        repaired_error = (record.get('reason') == 'TypeError'
                          and record.get('preparation_revision', 1) < PREPARATION_REVISION)
        fresh_saved_review = needs_saved_review(manifest.rows.get(episode.identity), record)
        if (not repaired_error and not fresh_saved_review and record.get('retry_after')
                and datetime.fromisoformat(record['retry_after']) > datetime.now(UTC)):
            continue
        eligible.append(metadata)
    series_misses = Counter()
    paused_series = set()
    for metadata in reserve_order(eligible, state['episodes'], manifest.rows, paused_series):
        episode = Episode.from_dict(metadata)
        key = episode_key(f'{episode.series_title} {episode.code}')
        if episode.identity in identities or key in keys:
            continue
        if attempted >= limit or time.monotonic() >= deadline:
            break
        record = {'at': now_iso(), 'status': 'deferred', 'preparation_revision': PREPARATION_REVISION}
        saved = manifest.rows.get(episode.identity)
        old_record = state['episodes'].get(episode.identity, {})
        direct_review = needs_saved_review(saved, old_record) and not low_resolution(saved)
        if legacy_czech(saved):
            record['saved_review_revision'] = SAVED_REVIEW_REVISION
        record['method'] = 'saved_original' if direct_review else 'discovery'
        if direct_review:
            saved_reviewed += 1
        replacement = None
        fatal_error = False
        try:
            candidate = None
            if saved and saved.get('quality_policy') == QUALITY_POLICY:
                existing = Candidate.from_dict(saved['selected'])
                if valid_czech(episode, existing):
                    candidate = existing
                    record['method'] = 'current_saved'
            if direct_review:
                known = saved_groups.get(key) or {saved['selected']['source_id']: saved}
                candidate = provider.revalidate_saved(episode, [
                    Candidate.from_dict(r['selected']) for r in known.values()])
            elif candidate is None:
                candidate = provider.discover(episode)
            if candidate is not None and valid_czech(episode, candidate):
                if candidate.source_id in source_ids:
                    record['reason'] = 'source_already_assigned'
                else:
                    replacement = dict(identity=episode.identity, episode=episode.to_dict(),
                        selected=candidate.to_dict(), display_name=display_name(episode, candidate),
                        quality_policy=QUALITY_POLICY, imdb_rating=metadata.get('imdb_rating'),
                        imdb_votes=int(metadata.get('imdb_votes') or 0))
                    if direct_review:
                        replacement['source_review'] = dict(
                            policy='saved-original-revalidation-v1', reviewed_at=now_iso(),
                            previous_policy=saved.get('quality_policy'),
                            previous_source_id=saved['selected']['source_id'])
                    SourceManifest._validate(replacement)
            else:
                record['reason'] = ('saved_original_unverified' if direct_review
                                    else 'no_verified_czech_match')
        except Exception as error:
            record['reason'] = type(error).__name__
            record['error'] = error_evidence(error)
            # Programming/dependency failures are not evidence of missing Czech
            # media. Persist the diagnostic, then fail the job visibly.
            fatal_error = isinstance(error, (TypeError, AttributeError, ImportError))
        # Checkpoint errors must stop publishing. Never swallow a failed push.
        if replacement is not None:
            manifest.add(replacement)
            manifest.save()
            rank = len(queue) + 1
            addition = dict(replacement, queue_rank=rank, target_account=ACCOUNTS[(rank - 1) % 2],
                            generation=generation, base_manifest_sha256=plan['manifest_sha256'])
            additions.append(addition)
            temporary = additions_path.with_suffix('.jsonl.tmp')
            temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in additions), encoding='utf-8')
            temporary.replace(additions_path)
            queue.append(addition)
            identities.add(episode.identity)
            keys.add(key)
            source_ids.add(candidate.source_id)
            record.update(status='prepared', source_id=candidate.source_id, queue_rank=rank)
            published += 1
            saved_published += int(direct_review)
            series_misses[episode.series_id] = 0
        else:
            record['retry_after'] = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
            series_misses[episode.series_id] += 1
            if not selected_ids and series_misses[episode.series_id] >= MAX_SERIES_MISSES:
                paused_series.add(episode.series_id)
        state['episodes'][episode.identity] = record
        state['updated_at'] = now_iso()
        atomic_json(state_path, state)
        attempted += 1
        report = dict(generation=generation, attempted_this_run=attempted, prepared_this_run=published,
                      reserve_total=len(additions), queue_total=len(queue), updated_at=now_iso(),
                      series_paused_this_run=len(paused_series),
                      saved_reviewed_this_run=saved_reviewed,
                      saved_prepared_this_run=saved_published,
                      statuses=dict(Counter(r['status'] for r in state['episodes'].values())))
        atomic_json(report_path, report)
        if persister:
            persist_source_checkpoint(persister, state_path)
        print(f"source_reserve identity={episode.identity} status={record['status']}", flush=True)
        if fatal_error:
            print(f"source_preparation_failed error={record['error']}", flush=True)
            raise RuntimeError('Source preparation code or dependency failure; see safe error evidence')
    return dict(attempted_this_run=attempted, prepared_this_run=published, reserve_total=len(additions),
                saved_reviewed_this_run=saved_reviewed, saved_prepared_this_run=saved_published)


def main():
    import argparse
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', required=True)
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--runtime-minutes', type=int, default=110)
    parser.add_argument('--persist-git-state', action='store_true')
    parser.add_argument('--identity', action='append', help='Optional targeted recovery/acceptance check')
    args = parser.parse_args()
    if os.environ.get('SOURCE_PREPARATION_ENABLED') != 'true':
        raise SystemExit('Source preparation is disabled')
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path(__file__).resolve().parents[2])).resolve()
    (root / 'state').mkdir(exist_ok=True)
    with (root / 'state/.reserve-preparation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provider = AuditProvider.authenticated(os.environ['SDILEJ_EMAIL'], os.environ['SDILEJ_PASSWORD'],
                                              discovery_timeout_seconds=900)
        try:
            print(json.dumps(prepare(root, args.generation, provider, limit=args.limit,
                                    runtime_minutes=args.runtime_minutes, persist=args.persist_git_state,
                                    identities=args.identity)))
        finally:
            provider.session.close()


if __name__ == '__main__':
    main()

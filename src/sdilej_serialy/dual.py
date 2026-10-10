"""One immutable ranked queue, two fixed owners, at most two workers per owner."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sdilej_to_prehrajto import prehrajto
from sdilej_to_prehrajto.models import Candidate

from .catalog import load_jsonl
from .continuous import SourceUnavailable, upload_continuously, uploaded_identities
from .git_state import GitCheckpointPersister
from .manifest import SourceManifest
from .models import Episode
from .pipeline import EpisodeState, atomic_json, now_iso, target_session
from .quality import QUALITY_POLICY, rank_candidates, upload_eligible
from .recovery import digest
from .target import episode_key, listing_rows
from .resilience import error_evidence, receipt_matches, transient_http

ACCOUNTS = ('a', 'b')


def account_batch(rows, state, blocked, alias, limit, fresh_sources=frozenset()):
    eligible = [r for r in rows if r['target_account'] == alias and r['identity'] not in blocked]
    def unavailable_retry(row):
        record = state.data['episodes'].get(row['identity'], {})
        attempts = record.get('attempts', [])
        return (row['identity'] not in fresh_sources and not record.get('prepared_target') and bool(attempts)
                and attempts[-1]['error'] == 'SourceUnavailable')
    retries = [r for r in eligible if unavailable_retry(r)]
    ready = [r for r in eligible if not unavailable_retry(r)]
    if limit <= 2:
        return eligible[:limit]
    # Repeated missing sources must not occupy every slot while fresh uploads
    # or receipted targets await processing. Keep owners and relative ranks.
    selected = {r['identity'] for r in retries[:2] + ready[:limit - min(2, len(retries))]}
    return [r for r in eligible if r['identity'] in selected]


def account_digest(email):
    return digest(email.strip().casefold().encode())


def directory_for(root, generation):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', generation):
        raise ValueError('Invalid dual generation')
    return root / 'dual' / generation


def ranked_sources(sources, catalog):
    """No old upload state or remote discovery participates in this selection."""
    ratings = {int(r['series_id']): r for r in catalog}
    groups = defaultdict(list)
    for row in sources:
        SourceManifest._validate(row)
        if not upload_eligible(row):
            continue
        selected = row['selected']
        if selected.get('audio_language') != 'cs' or (selected.get('language_probability') or 0) < .65:
            continue
        key = episode_key(row['display_name'])
        if not key or row['identity'] != Episode.from_dict(row['episode']).identity:
            raise ValueError('Invalid source episode identity')
        groups[key].append(row)
    chosen = []
    for group in groups.values():
        ranked = rank_candidates([Candidate.from_dict(r['selected']) for r in group])
        if not ranked:
            continue
        row = min((r for r in group if r['selected']['source_id'] == ranked[0].source_id),
                  key=lambda r: r['identity'])
        metadata = ratings.get(int(row['episode']['series_id']), {})
        raw_rating = metadata.get('imdb_rating')
        rating = float(raw_rating) if raw_rating is not None else None
        if rating is not None and not 0 <= rating <= 10:
            raise ValueError('Invalid IMDb rating')
        chosen.append(dict(row, imdb_rating=rating, imdb_votes=int(metadata.get('imdb_votes') or 0)))
    chosen.sort(key=lambda r: (r['imdb_rating'] is None, -(r['imdb_rating'] or 0),
                               -r['imdb_votes'], int(r['episode']['series_id']),
                               int(r['episode']['season']), int(r['episode']['episode'])))
    for index, row in enumerate(chosen):
        row['queue_rank'] = index + 1
        row['target_account'] = ACCOUNTS[index % 2]
    if len({r['selected']['source_id'] for r in chosen}) != len(chosen):
        raise ValueError('One source is assigned to multiple episodes')
    return chosen


def prepare(root, generation, manifest, catalog, emails):
    directory = directory_for(root, generation)
    if directory.exists():
        raise ValueError('Dual generation already exists; never reset progress')
    if len(emails) != 2 or len({account_digest(e) for e in emails}) != 2:
        raise ValueError('Two distinct target accounts are required')
    rows = ranked_sources(load_jsonl(manifest), load_jsonl(catalog))
    if len(rows) < 4:
        raise ValueError('At least four verified sources are required')
    payload = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows).encode()
    plan = dict(schema_version=1, generation=generation, created_at=now_iso(),
                quality_policy=QUALITY_POLICY, selected_count=len(rows),
                manifest_sha256=digest(payload), source_manifest_sha256=digest(manifest.read_bytes()),
                catalog_sha256=digest(catalog.read_bytes()), order='imdb_desc_votes_desc_series_season_episode',
                account_hashes=dict(zip(ACCOUNTS, map(account_digest, emails))),
                workers_per_account=2, total_workers=4)
    directory.mkdir(parents=True)
    (directory / 'manifest.jsonl').write_bytes(payload)
    atomic_json(directory / 'plan.json', plan)
    atomic_json(directory / 'state.json', dict(schema_version=1, generation=generation,
                account_hashes=plan['account_hashes'],
                manifest_sha256=plan['manifest_sha256'], episodes={}))
    return plan


def load(root, generation, *, additions_payload=None):
    directory = directory_for(root, generation)
    plan = json.loads((directory / 'plan.json').read_text())
    state = json.loads((directory / 'state.json').read_text())
    payload = (directory / 'manifest.jsonl').read_bytes()
    if (plan.get('generation') != generation or state.get('generation') != generation
            or plan.get('schema_version') != 1 or plan.get('quality_policy') != QUALITY_POLICY
            or plan.get('workers_per_account') != 2 or plan.get('total_workers') != 4
            or plan.get('account_hashes') != state.get('account_hashes')
            or plan.get('manifest_sha256') != digest(payload)
            or state.get('manifest_sha256') != plan['manifest_sha256']):
        raise ValueError('Dual generation or snapshot mismatch')
    rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
    if len(rows) != plan['selected_count']:
        raise ValueError('Frozen queue length changed')
    additions_path = directory / 'additions.jsonl'
    if additions_payload is not None or additions_path.exists():
        additions = ([json.loads(line) for line in additions_payload.splitlines() if line.strip()]
                     if additions_payload is not None else load_jsonl(additions_path))
        for row in additions:
            SourceManifest._validate(row)
            if (row.get('generation') != generation
                    or row.get('base_manifest_sha256') != plan['manifest_sha256']
                    or row['identity'] != Episode.from_dict(row['episode']).identity
                    or row['selected'].get('audio_language') != 'cs'
                    or (row['selected'].get('language_probability') or 0) < .65):
                raise ValueError('Invalid prepared addition')
        rows.extend(additions)
    mapping = {r['identity']: r for r in rows}
    if (len(mapping) != len(rows)
            or len({episode_key(r['display_name']) for r in rows}) != len(rows)
            or len({r['selected']['source_id'] for r in rows}) != len(rows)
            or any(not upload_eligible(r) or r['target_account'] != ACCOUNTS[i % 2]
                   or r['queue_rank'] != i + 1 for i, r in enumerate(rows))
            or not set(state['episodes']).issubset(mapping)):
        raise ValueError('Invalid dual queue')
    for identity, record in state['episodes'].items():
        if record.get('target_account') != mapping[identity]['target_account']:
            raise ValueError('Persisted episode account changed')
    return directory, plan, rows


class LiveReserve:
    """Admit newly published rows without changing owners or the batch budget."""
    def __init__(self, root, generation, rows, state, batches, limit, persister=None):
        self.root, self.generation, self.rows, self.state = root, generation, rows, state
        self.limit, self.persister = limit, persister
        self.delivered = {a: {r['identity'] for r in batches[a]} for a in ACCOUNTS}
        self.lock = threading.Lock()
        self.last_read = float('-inf')

    def take(self, alias):
        with self.lock, self.state._lock:
            if len(self.delivered[alias]) >= self.limit:
                return None
            if time.monotonic() - self.last_read >= 15:
                path = directory_for(self.root, self.generation) / 'additions.jsonl'
                relative = str(path.relative_to(self.root))
                payload = (self.persister.read_remote_file(relative, missing_ok=True) if self.persister
                           else path.read_text() if path.exists() else '')
                _, _, latest = load(self.root, self.generation, additions_payload=payload)
                if latest[:len(self.rows)] != self.rows:
                    raise ValueError('Live reserve changed existing queue rows')
                self.state.assignments.update({r['identity']: r['target_account'] for r in latest})
                self.rows[:] = latest
                if getattr(self.state, 'source_repairs', None):
                    self.state.source_repairs.refresh(force=True)
                self.last_read = time.monotonic()
            fresh = self.state.source_repairs.ready(self.state.data) if getattr(self.state, 'source_repairs', None) else set()
            blocked = uploaded_identities(self.state) | self.state.retry_deferred_identities() | self.delivered[alias]
            # Retain uncertain allocations, and do not open a new retry lane
            # beyond the two SourceUnavailable retries selected at batch start.
            blocked.update(identity for identity, record in self.state.data['episodes'].items()
                           if (record.get('prepared_target') and not receipt_matches(record))
                           or (identity not in fresh and not record.get('prepared_target') and record.get('attempts')
                               and record['attempts'][-1]['error'] == 'SourceUnavailable'))
            chosen = account_batch(self.rows, self.state, blocked, alias,
                                   self.limit - len(self.delivered[alias]), fresh)
            self.delivered[alias].update(r['identity'] for r in chosen)
            return chosen


class SharedState(EpisodeState):
    def __init__(self, path, assignments, **kwargs):
        super().__init__(path, **kwargs)
        self.assignments = assignments
        self.source_repairs = None

    def retry_deferred_identities(self):
        with self._lock:
            fresh = self.source_repairs.ready(self.data) if self.source_repairs else set()
            return super().retry_deferred_identities() - fresh

    def claim(self, episode, worker_id, **kwargs):
        with self._lock:
            row = self.row(episode)
            owner = self.assignments[episode.identity]
            if row.get('target_account', owner) != owner:
                raise RuntimeError('Episode account changed')
            row['target_account'] = owner
            return super().claim(episode, worker_id, **kwargs)


def credentials(plan):
    result = {}
    for alias in ACCOUNTS:
        email = os.environ.get(f'PREHRAJTO_{alias.upper()}_EMAIL', '')
        password = os.environ.get(f'PREHRAJTO_{alias.upper()}_PASSWORD', '')
        if not password or account_digest(email) != plan['account_hashes'][alias]:
            raise RuntimeError(f'Target {alias} credentials do not match frozen plan')
        result[alias] = (email, password)
    return result


def verify_pilot(rows, state, sessions):
    for row in rows[:4]:
        saved = state.data['episodes'].get(row['identity'], {})
        upload = saved.get('upload')
        if not upload or saved.get('prepared_target') or saved.get('claim'):
            raise RuntimeError('Pilot transfer not confirmed')
        query = re.sub(r'\*+', ' ', re.match(r'^(.*?\s+S\d+E\d+)', row['display_name'])[1])
        for alias, session in sessions.items():
            response = session.get(prehrajto.BASE_URL + '/profil/nahrana-videa',
                                   params={'searchPhrase': query}, timeout=30)
            response.raise_for_status()
            if 'uploadedVideoListing' not in response.text:
                raise RuntimeError('Pilot listing unavailable')
            matches = [r for r in listing_rows(response.text) if r['key'] == episode_key(row['display_name'])]
            expected = [upload['target_video_id']] if alias == row['target_account'] else []
            if [r['id'] for r in matches] != expected:
                raise RuntimeError('Pilot target missing, duplicated or on wrong account')
    counts = {a: prehrajto.uploaded_video_count(s) for a, s in sessions.items()}
    if any(count is None or count < 2 for count in counts.values()):
        raise RuntimeError('Both accounts must confirm pilot statistics')
    state.data['pilot_verified_at'] = now_iso()
    state.data['pilot_counts'] = counts
    state.save()


def _run(root, generation, mode, limit_per_account=25, persist=False, idle_refill_seconds=0):
    if os.environ.get('DUAL_ENABLED') != 'true':
        raise RuntimeError('Dual upload is disabled')
    if (mode not in ('pilot', 'full') or not 1 <= limit_per_account <= 25
            or mode == 'pilot' and limit_per_account < 2 or not 0 <= idle_refill_seconds <= 300):
        raise ValueError('Invalid dual run mode or batch limit')
    directory, plan, rows = load(root, generation)
    report_path = directory / 'report.json'
    persister = GitCheckpointPersister(root, (report_path,)) if persist else None
    state = SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows},
                        on_save=persister)
    from .source_audit import UpgradeFeed, UPGRADES_PATH
    upgrade_path = root / UPGRADES_PATH
    read_upgrades = (lambda: persister.read_remote_file(UPGRADES_PATH)) if persister else (
        lambda: upgrade_path.read_text() if upgrade_path.exists() else '')
    upgrade_feed = UpgradeFeed(read_upgrades)
    from .source_repair import SourceRepairFeed
    repairs_path = directory / 'source-repairs.jsonl'
    repairs_relative = str(repairs_path.relative_to(root))
    read_repairs = (lambda: persister.read_remote_file(repairs_relative, missing_ok=True)) if persister else (
        lambda: repairs_path.read_text() if repairs_path.exists() else '')
    repair_feed = SourceRepairFeed(plan, rows, read_repairs)
    repair_feed.refresh()
    state.source_repairs = repair_feed
    if state.data.get('halted_at'):
        raise RuntimeError('Dual queue halted after an error; manual review is required')
    if mode == 'full' and not state.data.get('pilot_verified_at'):
        raise RuntimeError('A verified four-episode pilot is required')
    creds = credentials(plan)
    source_email, source_password = os.environ.get('SDILEJ_EMAIL'), os.environ.get('SDILEJ_PASSWORD')
    if not source_email or not source_password:
        raise RuntimeError('Source credentials are required')
    sessions = {}
    stop = threading.Event()
    transient_pause = threading.Event()
    results = {}
    try:
        for alias, (email, password) in creds.items():
            sessions[alias] = target_session(email, password, expected_email=email)
        counts = {a: prehrajto.uploaded_video_count(s) for a, s in sessions.items()}
        if any(c is None for c in counts.values()):
            raise RuntimeError('Cannot verify both account statistics')
        if not state.data.get('initialized_at'):
            if any(counts.values()) or state.data['episodes']:
                raise RuntimeError('New generation requires two empty accounts')
            state.data['initialized_at'] = now_iso()
            state.save()
        else:
            for alias, count in counts.items():
                confirmed = sum(bool(r.get('upload')) for r in state.data['episodes'].values()
                                if r['target_account'] == alias)
                if count < confirmed:
                    raise RuntimeError('Target lost confirmed videos; refusing automatic replay')
        blocked = uploaded_identities(state) | state.retry_deferred_identities()
        # These allocations require manual evidence, not another upload attempt.
        # Reclaiming them every batch cannot resolve anything and can consume all
        # 25 slots ahead of fresh episodes. Keep them reserved and reported;
        # valid complete-transfer receipts still enter automatic reconciliation.
        blocked.update(identity for identity, record in state.data['episodes'].items()
                       if record.get('prepared_target') and not receipt_matches(record))
        selected = rows[:4] if mode == 'pilot' else rows
        fresh = repair_feed.ready(state.data)
        batches = {a: account_batch(selected, state, blocked, a, limit_per_account, fresh) for a in ACCOUNTS}
        reserve = LiveReserve(root, generation, rows, state, batches, limit_per_account, persister)

        def select_source(row):
            repaired = repair_feed.select(row)
            return repaired if repaired.get('source_repair') else upgrade_feed.select(row)

        def account_worker(alias):
            email, password = creds[alias]
            try:
                return upload_continuously(batches[alias], state, workers=2,
                    source_email=source_email, source_password=source_password,
                    target_email=email, target_password=password, require_original_size=True,
                    target_login=lambda: target_session(email, password, expected_email=email), stop_event=stop,
                    select_source=select_source, recover_source_errors=True,
                    recover_target_errors=True, transient_pause=transient_pause,
                    **({'refill_rows': lambda: reserve.take(alias), 'idle_refill_seconds': idle_refill_seconds}
                       if mode == 'full' else {}))
            except SourceUnavailable:
                # No target was allocated in this account worker. The other
                # account may continue; the next batch retries source login.
                return {'source_unavailable': True}
            except Exception as error:
                if transient_http(error):
                    transient_pause.set()
                else:
                    stop.set()
                raise

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {a: executor.submit(account_worker, a) for a in ACCOUNTS
                       if batches[a] or mode == 'full' and idle_refill_seconds > 0}
            for alias, future in futures.items():
                results[alias] = future.result()
        if stop.is_set():
            raise RuntimeError('A transfer failed; both account queues have been stopped')
        if mode == 'pilot' and all(state.uploaded(Episode.from_dict(r['episode'])) for r in rows[:4]):
            verify_pilot(rows, state, sessions)
    except Exception as error:
        if transient_http(error) and not stop.is_set():
            transient_pause.set()
            state.data['last_transient_error'] = dict(at=now_iso(), **error_evidence(error))
            state.save()
            print(f'target_batch_deferred error={error_evidence(error)}', flush=True)
        else:
            # Credential/account, integrity and checkpoint failures remain fatal.
            state.data['halted_at'] = now_iso()
            state.data['halt_reason'] = type(error).__name__
            state.save()
            raise
    finally:
        completed = Counter(r['target_account'] for r in state.data['episodes'].values() if r.get('upload'))
        report = dict(generation=generation, total=len(rows), completed=sum(completed.values()),
                      completed_by_account={a: completed[a] for a in ACCOUNTS},
                      remaining=len(rows) - sum(completed.values()), accounts=results,
                      halted=bool(state.data.get('halted_at')), updated_at=now_iso())
        report['retry_deferred'] = len(state.retry_deferred_identities())
        report['transient_pause'] = transient_pause.is_set()
        report['pending_confirmation'] = sum(bool(r.get('prepared_target')) for r in state.data['episodes'].values())
        atomic_json(report_path, report)
        if persister:
            persister(state.path)
        for session in sessions.values():
            session.close()
    return report


def run(root, generation, mode, limit_per_account=25, persist=False, idle_refill_seconds=0):
    # Actions concurrency serializes runners; this lock also rejects two local
    # processes using the same working tree. Never lock a file replaced by save.
    import fcntl
    directory = directory_for(root, generation)
    with (directory / '.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another process owns this dual queue') from None
        return _run(root, generation, mode, limit_per_account, persist, idle_refill_seconds)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'run'))
    parser.add_argument('--generation', required=True)
    parser.add_argument('--mode', choices=('pilot', 'full'), default='pilot')
    parser.add_argument('--limit-per-account', type=int, default=25)
    parser.add_argument('--persist-git-state', action='store_true')
    parser.add_argument('--idle-refill-seconds', type=float, default=300)
    args = parser.parse_args()
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path(__file__).resolve().parents[2])).resolve()
    if args.command == 'prepare':
        result = prepare(root, args.generation, root / 'manifests/selected-episodes.jsonl',
                         root / 'backlog/series-episodes.jsonl.gz',
                         [os.environ[f'PREHRAJTO_{a.upper()}_EMAIL'] for a in ACCOUNTS])
    else:
        result = run(root, args.generation, args.mode, args.limit_per_account, args.persist_git_state,
                     args.idle_refill_seconds)
    print(json.dumps(result))


if __name__ == '__main__':
    main()

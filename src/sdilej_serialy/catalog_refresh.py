"""Bounded catalogue maintenance in the serialized source-preparation lane."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .catalog import load_jsonl, write_jsonl_gzip
from .catalog_repair import fetch_inventory, supplement
from .git_state import GitCheckpointPersister, persist_source_checkpoint
from .pipeline import atomic_json


def due_series(catalog, registry, preparation, state, now):
    metadata = {r['series_id']: r for r in catalog}
    productive = {int(i.split(':')[0]) for lane in ('episodes', 'repairs')
                  for i, r in preparation.get(lane, {}).items() if r.get('status') == 'prepared'}
    due = [r for r in registry if r['series_id'] in metadata
           and (not state.get(str(r['series_id']), {}).get('retry_after')
                or datetime.fromisoformat(state[str(r['series_id'])]['retry_after']) <= now)]
    return sorted(due, key=lambda r: (
        r['series_id'] not in productive,
        bool(state.get(str(r['series_id']), {}).get('checked_at')),
        -(metadata[r['series_id']].get('imdb_rating') or 0),
        -(metadata[r['series_id']].get('imdb_votes') or 0), r['series_id']))


def refresh(root, fetch, *, limit=5, runtime_seconds=120, persist=False, now=None, clock=time.monotonic):
    if not 1 <= limit <= 20 or not 0 < runtime_seconds <= 300:
        raise ValueError('Invalid catalog refresh limits')
    now = now or datetime.now(UTC)
    catalog_path = root / 'backlog/series-episodes.jsonl.gz'
    registry = json.loads((root / 'backlog/series-inventory.json').read_text())
    if registry.get('schema_version') != 1:
        raise ValueError('Unsupported series inventory schema')
    mappings = registry['series']
    if len({r['series_id'] for r in mappings}) != len(mappings):
        raise ValueError('Duplicate series inventory mapping')
    catalog = load_jsonl(catalog_path)
    state_path = root / 'state/catalog-inventory.json'
    report_path = root / 'reports/catalog-inventory.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else dict(schema_version=1, series={})
    if state.get('schema_version') != 1:
        raise ValueError('Unsupported catalog inventory state')
    preparation_path = root / 'state/reserve-preparation.json'
    preparation = json.loads(preparation_path.read_text()) if preparation_path.exists() else {}
    deadline = clock() + runtime_seconds
    def before_request():
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Catalog refresh deadline reached')
        return min(15, remaining)
    results = []
    for item in due_series(catalog, mappings, preparation, state['series'], now)[:limit]:
        if clock() >= deadline:
            break
        sid, mid = item['series_id'], item['tmdb_id']
        record = dict(checked_at=now.isoformat(), tmdb_id=mid)
        try:
            details, seasons = fetch(mid, before_request=before_request)
            if details.get('id') != mid:
                raise ValueError('Unexpected TMDB series ID')
            updated = supplement(catalog, sid, details, seasons, today=now.date())
            record.update(status='checked', added_episodes=len(updated)-len(catalog),
                          retry_after=(now+timedelta(days=1)).isoformat())
            catalog = updated
        except (RuntimeError, ValueError, TimeoutError) as error:
            # A metadata outage must not block already catalogued sources.
            # Do not include exception text: request URLs can contain API keys.
            record.update(status='deferred', error=type(error).__name__,
                          retry_after=(now+timedelta(hours=1)).isoformat())
        state['series'][str(sid)] = record
        results.append(dict(series_id=sid, **record))
    report = dict(updated_at=now.isoformat(), checked_this_run=len(results),
                  added_episodes=sum(r.get('added_episodes', 0) for r in results), results=results)
    if not results:
        return report
    if report['added_episodes']:
        write_jsonl_gzip(catalog_path, catalog)
    atomic_json(state_path, state)
    atomic_json(report_path, report)
    if persist:
        persister = GitCheckpointPersister(root, (catalog_path, report_path))
        persist_source_checkpoint(persister, state_path)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--persist-git-state', action='store_true')
    args = parser.parse_args()
    key = os.environ.get('TMDB_API_KEY')
    if not key:
        print('catalog_refresh_skipped reason=missing_metadata_key')
        return
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path.cwd()))
    with requests.Session() as session:
        def fetch(mid, **kwargs):
            return fetch_inventory(session, key, mid, **kwargs)
        print(json.dumps(refresh(root, fetch, persist=args.persist_git_state)))


if __name__ == '__main__':
    main()

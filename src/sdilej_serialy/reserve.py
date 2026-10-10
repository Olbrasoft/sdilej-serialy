"""Actual unused stock, not the cumulative number of prepared additions."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from .dual import ACCOUNTS, load

LOW_WATER = 1000
TARGET_STOCK = 3000


def stock(rows, target_state, *, now=None, fresh_sources=frozenset()):
    now = now or datetime.now(UTC)
    records = target_state.get('episodes', {})
    categories = Counter()
    ready_by_account, completed_by_account = Counter(), Counter()
    recent = Counter()
    for row in rows:
        record = records.get(row['identity'], {})
        owner = row['target_account']
        if record.get('upload'):
            category = 'completed'
            completed_by_account[owner] += 1
            at = record['upload'].get('uploaded_at')
            if at and datetime.fromisoformat(at) >= now - timedelta(hours=24):
                recent[owner] += 1
        elif record.get('prepared_target'):
            category = 'allocated'
        elif record.get('claim'):
            category = 'in_flight'
        elif record.get('attempts') and row['identity'] not in fresh_sources:
            # Failed sources and cooldowns are not a reliable ready reserve,
            # even when the retry delay has expired.
            category = 'failed'
        else:
            category = 'ready'
            ready_by_account[owner] += 1
        categories[category] += 1
    consumption = sum(recent.values()) / 24
    return dict(queue_total=len(rows), ready=categories['ready'],
                ready_by_account={a: ready_by_account[a] for a in ACCOUNTS},
                categories=dict(categories),
                completed_by_account={a: completed_by_account[a] for a in ACCOUNTS},
                uploads_last_24h=sum(recent.values()), uploads_per_hour_24h=round(consumption, 2),
                stock_hours_24h=round(categories['ready'] / consumption, 2) if consumption else None)


def should_prepare(metrics, preparation_state, low_water=LOW_WATER, target_stock=TARGET_STOCK):
    if not 0 < low_water <= target_stock:
        raise ValueError('Invalid reserve watermarks')
    threshold = target_stock if preparation_state.get('refilling') else low_water
    return metrics['ready'] < threshold


def main():
    import argparse
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', required=True)
    parser.add_argument('--decision', choices=('prepare', 'audit'))
    parser.add_argument('--low-water', type=int, default=LOW_WATER)
    parser.add_argument('--target-stock', type=int, default=TARGET_STOCK)
    args = parser.parse_args()
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path(__file__).resolve().parents[2]))
    directory, _, rows = load(root, args.generation)
    from .source_repair import SourceRepairFeed
    plan = json.loads((directory / 'plan.json').read_text())
    path = directory / 'source-repairs.jsonl'
    feed = SourceRepairFeed(plan, rows, lambda: path.read_text() if path.exists() else '')
    feed.refresh()
    target = json.loads((directory / 'state.json').read_text())
    metrics = stock(rows, target, fresh_sources=feed.ready(target))
    state_path = root / 'state/reserve-preparation.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    wanted = should_prepare(metrics, state, args.low_water, args.target_stock)
    if args.decision:
        print(str(wanted if args.decision == 'prepare' else not wanted).lower())
    else:
        print(json.dumps(metrics))


if __name__ == '__main__':
    main()

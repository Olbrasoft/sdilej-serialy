"""Read pipeline state and safely keep its serialized workflows scheduled."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

ACTIVE = {'queued', 'pending', 'in_progress', 'requested', 'waiting'}
WORKFLOWS = {'source': 'prepare-reserve.yml', 'upload': 'dual.yml'}


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def decision(kind, *, enabled, runs, now, needed=True, halted=False, report=None):
    """Never cancel media transfers or bypass an integrity halt."""
    if not enabled:
        return dict(action='wait', reason='disabled')
    if kind == 'upload' and halted:
        return dict(action='wait', reason='integrity_halt_requires_review')
    active = [run for run in runs if run['status'] in ACTIVE]
    if active:
        if kind == 'source':
            for run in active:
                if run['status'] != 'in_progress':
                    continue
                # GitHub starts the workflow clock while it is still pending
                # behind the concurrency group. Only the actual worker's age
                # can exhaust its startup grace period.
                if 'worker_started_at' in run and not run['worker_started_at']:
                    continue
                started = timestamp(run.get('worker_started_at') or run.get('run_started_at') or run.get('created_at'))
                if started is None or now - started < timedelta(minutes=30):
                    continue
                saved_heartbeat = timestamp((report or {}).get('updated_at'))
                matching_report = str((report or {}).get('run_id')) == str(run['id'])
                legacy_report = (not (report or {}).get('run_id') and saved_heartbeat
                                 and saved_heartbeat >= started)
                heartbeat = saved_heartbeat if matching_report or legacy_report else None
                # A source-only worker can be replaced after it stops saving
                # heartbeat/progress. All published rows remain durable. The
                # media workflow is never cancelled by this supervisor.
                if heartbeat is None or now - heartbeat > timedelta(minutes=20):
                    return dict(action='cancel_source', reason='missing_source_heartbeat', run_id=run['id'])
        return dict(action='wait', reason='already_scheduled', run_ids=[run['id'] for run in active])
    if not needed:
        return dict(action='wait', reason='reserve_sufficient')
    latest = max(runs, key=lambda run: run.get('created_at', ''), default=None)
    if latest:
        finished = timestamp(latest.get('updated_at'))
        report_matches = str((report or {}).get('run_id')) == str(latest['id'])
        productive = report_matches and (report.get('prepared_this_run', 0) or report.get('repaired_this_run', 0))
        recovered_exit = report_matches and report.get('search_health', {}).get('restart_required')
        failed = latest.get('conclusion') in {'failure', 'timed_out', 'startup_failure'}
        empty_success = (kind == 'source' and latest.get('conclusion') == 'success'
                         and not (productive or recovered_exit))
        # A setup failure or exhausted source catalog must not create a tight
        # self-dispatch loop. The periodic watchdog retries after ten minutes.
        if (finished and now - finished < timedelta(minutes=10)
                and (failed or empty_success)):
            return dict(action='wait', reason='restart_backoff',
                        retry_after=(finished + timedelta(minutes=10)).isoformat())
    return dict(action='dispatch', reason='enabled_workflow_missing')


def github(repository, path, *, payload=None):
    command = ['gh', 'api', f'repos/{repository}/{path}']
    if payload is not None:
        command += ['--method', 'POST', '--input', '-']
    try:
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError('GitHub operation timed out; no repeated mutation in this run') from None
    if result.returncode:
        raise RuntimeError('GitHub operation failed; no repeated mutation in this run')
    return json.loads(result.stdout) if result.stdout.strip() else None


def settle_runs(repository, runs):
    """Workflow status can lag after all jobs have released their runner."""
    settled = []
    for run in runs:
        if run['status'] == 'in_progress':
            payload = github(repository, f'actions/runs/{run["id"]}/jobs?filter=latest&per_page=100')
            jobs = payload['jobs']
            starts = [job['started_at'] for job in jobs
                      if job['status'] == 'in_progress' and job.get('started_at')]
            run = dict(run, worker_started_at=min(starts) if starts else None)
            if (jobs and len(jobs) == payload['total_count']
                    and all(job['status'] == 'completed' and job.get('completed_at') for job in jobs)):
                conclusions = {job.get('conclusion') for job in jobs}
                conclusion = ('success' if conclusions <= {'success', 'skipped'}
                              else 'cancelled' if conclusions <= {'success', 'skipped', 'cancelled'} else 'failure')
                run = dict(run, status='completed', conclusion=conclusion,
                           updated_at=max(job['completed_at'] for job in jobs))
        settled.append(run)
    return settled


def main():
    import argparse
    from .dual import load
    from .reserve import stock, should_prepare
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    repository = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path(__file__).resolve().parents[2]))
    directory, _, rows = load(root, args.generation)
    target = json.loads((directory / 'state.json').read_text())
    progress_path = root / 'state/reserve-preparation.json'
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    report_path = root / 'reports/reserve-preparation.json'
    source_report = json.loads(report_path.read_text()) if report_path.exists() else {}
    metrics = stock(rows, target)
    now = datetime.now(UTC)
    result = dict(checked_at=now.isoformat(), stock=metrics, decisions={})
    for kind, workflow in WORKFLOWS.items():
        payload = github(repository, f'actions/workflows/{workflow}/runs?per_page=50&branch=main')
        runs = settle_runs(repository, payload['workflow_runs'])
        enabled = (os.environ.get('SOURCE_PREPARATION_ENABLED') == 'true' if kind == 'source'
                   else os.environ.get('DUAL_ENABLED') == 'true' and os.environ.get('DUAL_FULL_ENABLED') == 'true')
        chosen = decision(kind, enabled=enabled, runs=runs, now=now,
                          needed=should_prepare(metrics, progress) if kind == 'source' else True,
                          halted=bool(target.get('halted_at')), report=source_report if kind == 'source' else None)
        result['decisions'][kind] = chosen
        if args.apply and chosen['action'] == 'dispatch':
            github(repository, f'actions/workflows/{workflow}/dispatches',
                   payload=dict(ref='main', inputs={} if kind == 'source' else dict(mode='full')))
        elif args.apply and chosen['action'] == 'cancel_source':
            github(repository, f'actions/runs/{chosen["run_id"]}/cancel', payload={})
    print(json.dumps(result, ensure_ascii=False))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write('```json\n' + json.dumps(result, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()

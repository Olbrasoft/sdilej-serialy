from datetime import UTC, datetime, timedelta

import pytest

from sdilej_serialy.supervise import decision, github

NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)


def run(status='completed', minutes=60, **fields):
    at = (NOW - timedelta(minutes=minutes)).isoformat()
    return dict(id=123, status=status, created_at=at, updated_at=at,
                conclusion='success', **fields)


@pytest.mark.parametrize('kind', ['source', 'upload'])
def test_missing_enabled_lane_is_scheduled_but_disable_is_respected(kind):
    assert decision(kind, enabled=True, runs=[], now=NOW)['action'] == 'dispatch'
    assert decision(kind, enabled=False, runs=[], now=NOW)['reason'] == 'disabled'


@pytest.mark.parametrize('status', ['queued', 'pending', 'in_progress', 'waiting', 'requested'])
def test_existing_lane_is_not_duplicated(status):
    assert decision('upload', enabled=True, runs=[run(status)], now=NOW)['action'] == 'wait'


def test_source_heartbeat_can_replace_only_read_only_worker():
    stale = dict(run_id='123', updated_at=(NOW - timedelta(minutes=25)).isoformat())
    assert decision('source', enabled=True, runs=[run('in_progress')], now=NOW, report=stale) == dict(
        action='cancel_source', reason='missing_source_heartbeat', run_id=123)
    assert decision('upload', enabled=True, runs=[run('in_progress')], now=NOW, report=stale)['action'] == 'wait'
    fresh = dict(stale, updated_at=(NOW - timedelta(minutes=2)).isoformat())
    assert decision('source', enabled=True, runs=[run('in_progress')], now=NOW, report=fresh)['action'] == 'wait'
    # A still-running pre-upgrade producer has no run ID in its heartbeat.
    fresh.pop('run_id')
    assert decision('source', enabled=True, runs=[run('in_progress')], now=NOW, report=fresh)['action'] == 'wait'


def test_startup_gets_a_bounded_grace_period_and_old_heartbeat_cannot_mask_stall():
    assert decision('source', enabled=True, runs=[run('in_progress', minutes=29)], now=NOW)['action'] == 'wait'
    old = dict(run_id=122, updated_at=NOW.isoformat())
    assert decision('source', enabled=True, runs=[run('in_progress')], now=NOW, report=old)['action'] == 'cancel_source'


def test_integrity_halt_and_full_reserve_are_not_overridden():
    assert decision('upload', enabled=True, runs=[], now=NOW, halted=True)['reason'] == 'integrity_halt_requires_review'
    assert decision('source', enabled=True, runs=[], now=NOW, needed=False)['reason'] == 'reserve_sufficient'


def test_early_source_exit_is_followed_without_waiting_for_cron_but_empty_runs_back_off():
    latest = run(minutes=1)
    report = dict(run_id=123, prepared_this_run=2)
    assert decision('source', enabled=True, runs=[latest], now=NOW, report=report)['action'] == 'dispatch'
    report['prepared_this_run'] = 0
    assert decision('source', enabled=True, runs=[latest], now=NOW, report=report)['reason'] == 'restart_backoff'
    report['search_health'] = dict(restart_required=True)
    assert decision('source', enabled=True, runs=[latest], now=NOW, report=report)['action'] == 'dispatch'
    assert decision('source', enabled=True, runs=[run(minutes=11)], now=NOW)['action'] == 'dispatch'


def test_failed_setup_does_not_create_an_immediate_restart_loop():
    latest = run(minutes=1)
    latest['conclusion'] = 'failure'
    for kind in ('source', 'upload'):
        assert decision(kind, enabled=True, runs=[latest], now=NOW)['reason'] == 'restart_backoff'


def test_cancelled_pending_schedule_does_not_delay_automatic_handoff():
    latest = run(minutes=1)
    latest['conclusion'] = 'cancelled'
    for kind in ('source', 'upload'):
        assert decision(kind, enabled=True, runs=[latest], now=NOW)['action'] == 'dispatch'


def test_uncertain_dispatch_is_not_repeated_or_logged_with_secrets(monkeypatch):
    import subprocess
    from sdilej_serialy import supervise
    calls = []
    def timeout(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired('private-token', 45, output='signed-address')
    monkeypatch.setattr(supervise.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError) as exc:
        github('owner/repo', 'actions/workflows/dual.yml/dispatches', payload={'ref': 'main'})
    assert len(calls) == 1
    assert 'private-token' not in str(exc.value) and 'signed-address' not in str(exc.value)

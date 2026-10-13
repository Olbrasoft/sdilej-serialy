import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdilej_serialy import continuous, dual, pipeline
from test_dual import setup_plan, source, stub_live
from test_idle_refill import stub


def test_expired_deadline_never_claims_initial_or_refilled_rows(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    result = continuous.upload_continuously([source()], state, claim_deadline=0,
        refill_rows=lambda: pytest.fail('Admission closed'), **options)
    assert result['claim_deadline_reached'] and result['uploaded_or_reconciled'] == 0
    assert state.data['episodes'] == {}


def test_deadline_drains_both_active_workers_but_leaves_pending_rows_untouched(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    clock = [0]
    monkeypatch.setattr(continuous.time, 'monotonic', lambda: clock[0])
    rows = [source(number=i) for i in range(1, 5)]
    entered = threading.Barrier(2, timeout=5)
    finished = []
    stop = threading.Event()
    def relay(_target, _source, candidate, name, description, *, on_prepared):
        on_prepared(candidate.source_id, candidate.size_bytes)
        entered.wait()
        clock[0] = 11
        finished.append(candidate.source_id)
        return SimpleNamespace(video_id=candidate.source_id)
    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    result = continuous.upload_continuously(rows, state, claim_deadline=10, stop_event=stop,
        refill_rows=lambda: pytest.fail('Do not refill after deadline'), **options)
    assert sorted(finished) == ['101', '102']
    assert result['claim_deadline_reached'] and result['uploaded_or_reconciled'] == 2
    assert set(state.data['episodes']) == {r['identity'] for r in rows[:2]}
    assert all(r.get('upload') and not r.get('claim') and not r.get('attempts')
               for r in state.data['episodes'].values())
    assert not stop.is_set() and not state.data.get('halted_at')


def test_admission_rechecked_after_a_slow_quality_check(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    clock = [0]
    monkeypatch.setattr(continuous.time, 'monotonic', lambda: clock[0])
    def eligible(_row):
        clock[0] = 11
        return True
    monkeypatch.setattr(continuous, 'upload_eligible', eligible)
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    result = continuous.upload_continuously([source()], state, claim_deadline=10, **options)
    assert result['claim_deadline_reached'] and state.data['episodes'] == {}


def test_deadline_closes_idle_refill_even_while_peer_is_active(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    clock, polls = [0], []
    monkeypatch.setattr(continuous.time, 'monotonic', lambda: clock[0])
    def refill():
        polls.append(1)
        clock[0] += 3
        return []
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    result = continuous.upload_continuously([], state, claim_deadline=10,
        refill_rows=refill, keep_waiting=lambda: True, **options)
    assert len(polls) == 4 and result['claim_deadline_reached']
    assert state.data['episodes'] == {}


def test_rows_refilled_after_deadline_do_not_start_transfers(tmp_path, monkeypatch):
    options = stub(monkeypatch)
    clock = [0]
    monkeypatch.setattr(continuous.time, 'monotonic', lambda: clock[0])
    def refill():
        clock[0] = 11
        return [source()]
    state = pipeline.EpisodeState(tmp_path / 'state.json')
    result = continuous.upload_continuously([], state, claim_deadline=10, refill_rows=refill, **options)
    assert result['claim_deadline_reached'] and state.data['episodes'] == {}


@pytest.mark.parametrize('deadline', [float('nan'), float('inf'), -float('inf')])
def test_invalid_worker_deadline_is_rejected(tmp_path, monkeypatch, deadline):
    options = stub(monkeypatch)
    with pytest.raises(ValueError, match='claim deadline'):
        continuous.upload_continuously([], pipeline.EpisodeState(tmp_path / 'state.json'),
                                       claim_deadline=deadline, **options)


def test_both_accounts_share_one_absolute_deadline_across_batches(tmp_path, monkeypatch):
    directory, _, _ = setup_plan(tmp_path, monkeypatch)
    saved = json.loads((directory / 'state.json').read_text())
    saved.update(initialized_at=pipeline.now_iso(), pilot_verified_at=pipeline.now_iso())
    pipeline.atomic_json(directory / 'state.json', saved)
    stub_live(monkeypatch)
    wall, monotonic, deadlines = [1000], [100], []
    monkeypatch.setattr(dual.time, 'time', lambda: wall[0])
    monkeypatch.setattr(dual.time, 'monotonic', lambda: monotonic[0])
    def upload(rows, state, **kwargs):
        assert kwargs['workers'] == 2
        deadlines.append(kwargs['claim_deadline'])
        return dict(uploaded_or_reconciled=0)
    monkeypatch.setattr(dual, 'upload_continuously', upload)
    dual.run(tmp_path, 'test', 'full', stop_claiming_at=1010)
    wall[0], monotonic[0] = 1007, 107
    report = dual.run(tmp_path, 'test', 'full', stop_claiming_at=1010)
    assert deadlines == [110, 110, 110, 110]
    assert report['stop_claiming_at'] == 1010 and not report['claim_deadline_reached']


@pytest.mark.parametrize('deadline', [0, -1, float('nan'), float('inf')])
def test_invalid_absolute_deadline_does_not_halt_queue(tmp_path, monkeypatch, deadline):
    directory, _, _ = setup_plan(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='claim deadline'):
        dual.run(tmp_path, 'test', 'full', stop_claiming_at=deadline)
    assert not json.loads((directory / 'state.json').read_text()).get('halted_at')


def test_cli_passes_absolute_deadline_to_runner(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_WORKSPACE', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['dual', 'run', '--generation', 'test', '--mode', 'full',
                                    '--stop-claiming-at', '2000000000'])
    calls = []
    monkeypatch.setattr(dual, 'run', lambda *args: calls.append(args) or {})
    dual.main()
    assert calls[0][-1] == 2000000000


def test_workflow_leaves_drain_headroom_and_passes_job_deadline():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/dual.yml').read_text()
    hard = int(re.search(r'timeout-minutes: (\d+)', workflow)[1])
    admission = int(re.search(r'dual_deadline=.*\+ (\d+) \* 60', workflow)[1])
    assert hard - admission >= 140
    assert '--stop-claiming-at "$dual_deadline"' in workflow

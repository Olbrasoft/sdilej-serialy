from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from sdilej_serialy import git_state
from sdilej_serialy.git_state import GitCheckpointPersister


def test_hung_git_is_bounded_and_never_leaks_transport_output(monkeypatch, tmp_path):
    calls = []
    def timeout(*args, **kwargs):
        calls.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired('private-token', kwargs['timeout'], output='signed-url')
    monkeypatch.setattr(git_state.subprocess, 'run', timeout)
    persister = GitCheckpointPersister(tmp_path, command_timeout_seconds=12)
    with pytest.raises(git_state.CheckpointError, match='git fetch timed out') as exc:
        persister._run('fetch', 'origin', 'main')
    assert 'private-token' not in str(exc.value) and 'signed-url' not in str(exc.value)
    assert persister._run('push', 'origin', 'HEAD:main', check=False).returncode == 124
    assert calls == [12, 12]


def test_repeated_slow_git_failures_have_a_total_checkpoint_deadline(monkeypatch, tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{}')
    now, pushes = [100], []
    persister = GitCheckpointPersister(tmp_path, checkpoint_timeout_seconds=5)
    def run(*args, **kwargs):
        if args[0] == 'push':
            pushes.append(1)
            now[0] += 3
        return SimpleNamespace(returncode=int(args[0] == 'push'), stdout='')
    monkeypatch.setattr(persister, '_run', run)
    monkeypatch.setattr(git_state.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(git_state.time, 'sleep', lambda _: None)
    with pytest.raises(git_state.CheckpointError, match='deadline exceeded'):
        persister(path)
    assert len(pushes) == 2 and persister.last_pushed_at is None


def test_checkpoint_survives_busy_remote_branch(monkeypatch, tmp_path: Path):
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    persister = GitCheckpointPersister(tmp_path)
    pushes = 0

    def fake_run(*args, check=True):
        nonlocal pushes
        if args[:3] == ("diff", "--cached", "--quiet"):
            return SimpleNamespace(returncode=1, stdout="")
        if args[0] == "push":
            pushes += 1
            return SimpleNamespace(returncode=0 if pushes == 8 else 1, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(persister, "_run", fake_run)
    monkeypatch.setattr(git_state.time, "sleep", lambda _: None)

    persister(state)

    assert pushes == 8


def test_no_sleep_between_rebase_and_push(monkeypatch, tmp_path):
    state=tmp_path/'state.json'
    state.write_text('{}')
    persister=GitCheckpointPersister(tmp_path)
    calls=[]
    def run(*args,check=True):
        calls.append(args[0])
        return SimpleNamespace(returncode=1 if args[0]=='diff' or args[0]=='push' and calls.count('push')==1 else 0,stdout='')
    monkeypatch.setattr(persister,'_run',run)
    monkeypatch.setattr(git_state.time,'sleep',lambda _:calls.append('sleep'))
    persister(state)
    assert calls[calls.index('rebase')+1]=='push'


def test_clean_index_still_pushes_unpublished_checkpoint(monkeypatch, tmp_path):
    state = tmp_path / 'state.json'
    state.write_text('{}')
    persister = GitCheckpointPersister(tmp_path)
    calls = []
    def run(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(persister, '_run', run)
    persister(state)
    assert 'commit' not in calls
    assert 'push' in calls


def test_optional_live_reserve_can_be_absent_but_remote_errors_remain_fatal(monkeypatch, tmp_path):
    persister = GitCheckpointPersister(tmp_path)
    calls = []
    def run(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(persister, '_run', run)
    assert persister.read_remote_file('dual/test/additions.jsonl', missing_ok=True) == ''
    assert calls == ['fetch', 'ls-tree']
    def unavailable(*args, **kwargs):
        raise git_state.CheckpointError('Remote unavailable')
    monkeypatch.setattr(persister, '_run', unavailable)
    with pytest.raises(git_state.CheckpointError):
        persister.read_remote_file('dual/test/additions.jsonl', missing_ok=True)


def test_source_checkpoints_leave_quiet_window(monkeypatch, tmp_path):
    state = tmp_path / 'state.json'
    state.write_text('{}')
    persister = GitCheckpointPersister(tmp_path, min_interval_seconds=15)
    sleeps = []
    monkeypatch.setattr(persister, '_run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=''))
    monkeypatch.setattr(git_state.time, 'monotonic', lambda: 100)
    monkeypatch.setattr(git_state.time, 'sleep', sleeps.append)
    persister(state)
    assert sleeps == []
    persister(state)
    assert sleeps == [15]


def test_exhausted_push_never_returns_success_even_with_clean_index(monkeypatch, tmp_path):
    state = tmp_path / 'state.json'
    state.write_text('{}')
    persister = GitCheckpointPersister(tmp_path)
    monkeypatch.setattr(persister, '_run', lambda *a, **k: SimpleNamespace(
        returncode=int(a[0] == 'push'), stdout=''))
    monkeypatch.setattr(git_state.time, 'sleep', lambda _: None)
    with pytest.raises(git_state.CheckpointError):
        persister(state)
    assert persister.last_pushed_at is None


def test_source_checkpoint_retries_same_snapshot_before_returning(monkeypatch, tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{"prepared": 1}')
    calls, sleeps = [], []
    def persist(candidate):
        calls.append((candidate, candidate.read_bytes()))
        if len(calls) < 3:
            raise git_state.CheckpointError('Remote branch advanced')
    monkeypatch.setattr(git_state.time, 'sleep', sleeps.append)
    git_state.persist_source_checkpoint(persist, path)
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert sleeps == [15, 30]


def test_source_checkpoint_permanent_failure_remains_fatal(monkeypatch, tmp_path):
    calls = []
    def persist(path):
        calls.append(path)
        raise git_state.CheckpointError('Cannot save safely')
    monkeypatch.setattr(git_state.time, 'sleep', lambda _: None)
    with pytest.raises(git_state.CheckpointError):
        git_state.persist_source_checkpoint(persist, tmp_path / 'state.json')
    assert len(calls) == 3


def test_source_checkpoint_round_recovery_with_real_git_preserves_other_writer(monkeypatch, tmp_path):
    def git(root, *args):
        return subprocess.run(['git', *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(remote))
    producer, uploader = tmp_path / 'producer', tmp_path / 'uploader'
    git(tmp_path, 'clone', str(remote), str(producer))
    git(producer, 'config', 'user.name', 'Test')
    git(producer, 'config', 'user.email', 'test@example.invalid')
    (producer / 'source.json').write_text('{"prepared": 0}')
    (producer / 'upload.json').write_text('{"uploaded": 0}')
    git(producer, 'add', '.')
    git(producer, 'commit', '-m', 'initial')
    git(producer, 'push', 'origin', 'main')
    git(tmp_path, 'clone', str(remote), str(uploader))
    git(uploader, 'config', 'user.name', 'Test')
    git(uploader, 'config', 'user.email', 'test@example.invalid')
    (uploader / 'upload.json').write_text('{"uploaded": 1}')
    git(uploader, 'add', 'upload.json')
    git(uploader, 'commit', '-m', 'other writer')
    git(uploader, 'push', 'origin', 'main')
    path = producer / 'source.json'
    path.write_text('{"prepared": 1}')
    persister = GitCheckpointPersister(producer)
    original_run = persister._run
    pushes = []
    def run(*args, **kwargs):
        if args[0] == 'push':
            pushes.append(1)
            if len(pushes) <= 40:
                return SimpleNamespace(returncode=1, stdout='')
        return original_run(*args, **kwargs)
    monkeypatch.setattr(persister, '_run', run)
    monkeypatch.setattr(git_state.time, 'sleep', lambda _: None)
    git_state.persist_source_checkpoint(persister, path)
    assert len(pushes) == 41
    assert git(remote, 'show', 'main:source.json') == '{"prepared": 1}'
    assert git(remote, 'show', 'main:upload.json') == '{"uploaded": 1}'
    assert git(remote, 'rev-list', '--count', 'main') == '3'

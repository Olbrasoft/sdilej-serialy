from pathlib import Path
from types import SimpleNamespace

import pytest

from sdilej_serialy import git_state
from sdilej_serialy.git_state import GitCheckpointPersister


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

from pathlib import Path
from types import SimpleNamespace

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

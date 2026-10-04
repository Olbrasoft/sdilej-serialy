"""Fail-closed GitHub Actions checkpoints for the upload state file."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path


class CheckpointError(RuntimeError):
    """A checkpoint was not confirmed remotely; no new transfer is safe yet."""


def persist_source_checkpoint(persister, path):
    """Retry the same unpublished source snapshot; never advance discovery first."""
    for attempt in range(3):
        try:
            persister(path)
            return
        except CheckpointError:
            if attempt == 2:
                raise
            print(f'source_checkpoint_retry={attempt + 1}', flush=True)
            time.sleep(15 * (attempt + 1))


class GitCheckpointPersister:
    def __init__(self, root: Path, extra_paths: tuple[Path, ...] = (), *, min_interval_seconds=0):
        self.root = root
        self.extra_paths = extra_paths
        self.lock = threading.RLock()
        self.min_interval_seconds = min_interval_seconds
        self.last_pushed_at = None

    def __call__(self, path: Path) -> None:
        paths = (path, *self.extra_paths)
        relative_paths = [
            candidate.resolve().relative_to(self.root.resolve())
            for candidate in paths
            if candidate.exists()
        ]
        if not relative_paths:
            return
        with self.lock:
            # Source-only jobs can review unavailable episodes in milliseconds.
            # Leave a quiet window for safety-critical upload checkpoints, while
            # still durably publishing every source review before returning.
            if self.last_pushed_at is not None:
                delay = self.min_interval_seconds - (time.monotonic() - self.last_pushed_at)
                if delay > 0:
                    time.sleep(delay)
            self._run("add", "--", *(str(relative) for relative in relative_paths))
            if self._run("diff", "--cached", "--quiet", check=False).returncode != 0:
                self._run("commit", "-m", "chore(sync): persist episode checkpoint")
            # A previous call may have committed locally but exhausted its push
            # retries. A clean index alone is NOT proof of a durable checkpoint.
            # The producer and uploader intentionally checkpoint different
            # files on the same branch.  A six-worker upload burst can advance
            # main several times between fetch/rebase/push, so five immediate
            # retries are not enough even though there is no content conflict.
            # Keep rebasing until that short burst settles.
            rebase_conflicts = 0
            for attempt in range(40):
                if self._run("push", "origin", "HEAD:main", check=False).returncode == 0:
                    self.last_pushed_at = time.monotonic()
                    return
                # Back off before fetching, never let a rebased HEAD go stale.
                time.sleep(min(0.25 * (attempt + 1), 3.0))
                self._run("fetch", "origin", "main")
                if self._run("rebase", "--autostash", "origin/main", check=False).returncode == 0:
                    continue
                rebase_conflicts += 1
                self._run("rebase", "--abort", check=False)
            raise CheckpointError(f'Checkpoint could not be pushed after 40 attempts '
                                  f'({rebase_conflicts} rebase conflicts); refusing further work')

    def read_remote_file(self, relative_path: str) -> str:
        if relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise RuntimeError("Remote path must stay inside the repository")
        with self.lock:
            self._run("fetch", "origin", "main")
            result = self._run("show", f"FETCH_HEAD:{relative_path}")
            return result.stdout

    def _run(self, *args: str, check: bool = True):
        result = subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True, check=False)
        if check and result.returncode:
            raise CheckpointError(f"git {args[0]} failed")
        return result

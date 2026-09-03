"""Fail-closed GitHub Actions checkpoints for the upload state file."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path


class GitCheckpointPersister:
    def __init__(self, root: Path, extra_paths: tuple[Path, ...] = ()):
        self.root = root
        self.extra_paths = extra_paths
        self.lock = threading.RLock()

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
            self._run("add", "--", *(str(relative) for relative in relative_paths))
            if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
                return
            self._run("commit", "-m", "chore(sync): persist episode checkpoint")
            # The producer and uploader intentionally checkpoint different
            # files on the same branch.  A six-worker upload burst can advance
            # main several times between fetch/rebase/push, so five immediate
            # retries are not enough even though there is no content conflict.
            # Keep rebasing until that short burst settles.
            for attempt in range(40):
                if self._run("push", "origin", "HEAD:main", check=False).returncode == 0:
                    return
                self._run("fetch", "origin", "main")
                if self._run("rebase", "--autostash", "origin/main", check=False).returncode == 0:
                    time.sleep(min(0.25 * (attempt + 1), 3.0))
                    continue
                self._run("rebase", "--abort", check=False)
            raise RuntimeError("Upload checkpoint could not be pushed; refusing further transfer")

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
            raise RuntimeError(f"git {args[0]} failed")
        return result

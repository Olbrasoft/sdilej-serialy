"""Durable queue of verified, stable Sdilej.cz episode detail URLs."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .pipeline import atomic_json


class SourceManifest:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.rows: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._validate(row)
                    self.rows[str(row["identity"])] = row

    @staticmethod
    def _validate(row: dict) -> None:
        selected = row.get("selected") or {}
        if "download_url" in selected or "sample_url" in selected:
            raise ValueError("Authenticated source URLs must never enter the manifest")
        if not row.get("identity") or not selected.get("url"):
            raise ValueError("Manifest entry needs a stable episode identity and source URL")

    def add(self, row: dict) -> None:
        self._validate(row)
        with self._lock:
            self.rows[str(row["identity"])] = row

    def merge_jsonl(self, payload: str) -> int:
        incoming: dict[str, dict] = {}
        for line in payload.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            self._validate(row)
            incoming[str(row["identity"])] = row
        with self._lock:
            changed = sum(self.rows.get(identity) != row for identity, row in incoming.items())
            self.rows.update(incoming)
            return changed

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(json.dumps(self.rows[key], ensure_ascii=False) + "\n" for key in sorted(self.rows))
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.path)

    def pending(self, uploaded: set[str], *, limit: int) -> list[dict]:
        with self._lock:
            return [row for key, row in self.rows.items() if key not in uploaded][:limit]

    def identities(self) -> set[str]:
        with self._lock:
            return set(self.rows)

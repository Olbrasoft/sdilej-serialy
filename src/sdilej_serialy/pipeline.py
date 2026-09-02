from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import timedelta
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto import prehrajto

from .episodes import EpisodeSourceProvider, display_name
from .models import Episode


TARGET_EMAIL = "share.series@email.cz"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class EpisodeState:
    def __init__(self, path: Path, *, on_save=None):
        self.path = path
        self.on_save = on_save
        self._lock = threading.RLock()
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"schema_version": 1, "episodes": {}}
        if self.data.get("schema_version") != 1:
            raise RuntimeError("Unsupported episode state schema")
        self.data.setdefault("episodes", {})

    def row(self, episode: Episode) -> dict:
        with self._lock:
            return self.data["episodes"].setdefault(episode.identity, {})

    def uploaded(self, episode: Episode) -> bool:
        with self._lock:
            return bool(self.row(episode).get("upload", {}).get("target_video_id"))

    def tracked_identities(self) -> set[str]:
        with self._lock:
            return set(self.data["episodes"])

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = now_iso()
            atomic_json(self.path, self.data)
            if self.on_save:
                self.on_save(self.path)

    def claim(self, episode: Episode, worker_id: str, *, lease_hours: int = 6) -> bool:
        with self._lock:
            row = self.row(episode)
            if row.get("upload", {}).get("target_video_id"):
                return False
            existing = row.get("claim") or {}
            expiry = existing.get("lease_expires_at")
            if expiry and datetime.fromisoformat(expiry) > datetime.now(UTC):
                return False
            row["claim"] = {
                "worker_id": worker_id,
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "claimed_at": now_iso(),
                "lease_expires_at": (datetime.now(UTC) + timedelta(hours=lease_hours)).isoformat(),
            }
            self.save()
            return True

    def release_orphaned_claims(self) -> int:
        """A new serialized Actions run may safely release prior-run leases."""
        current_run = os.environ.get("GITHUB_RUN_ID")
        if not current_run:
            return 0
        with self._lock:
            released = 0
            for row in self.data["episodes"].values():
                claim = row.get("claim") or {}
                if claim and claim.get("run_id") != current_run:
                    row.pop("claim", None)
                    released += 1
            if released:
                self.save()
            return released

    def prepared(self, episode: Episode, candidate: Candidate, name: str) -> None:
        with self._lock:
            self.row(episode)["source"] = {
                "source_id": candidate.source_id,
                "source_url": candidate.url,
                "source_title": candidate.title,
                "source_filename": candidate.filename,
                "size_bytes": candidate.size_bytes,
                "duration_sec": candidate.duration_sec,
                "width": candidate.width,
                "height": candidate.height,
                "audio_language": candidate.audio_language,
                "language_probability": candidate.language_probability,
                "language_tier": candidate.language_tier.name.lower(),
                "match_evidence": candidate.match_evidence,
                "display_name": name,
                "prepared_at": now_iso(),
            }
            self.save()

    def success(self, episode: Episode, video_id: str, name: str) -> None:
        with self._lock:
            row = self.row(episode)
            row["upload"] = {"target_video_id": str(video_id), "display_name": name, "uploaded_at": now_iso()}
            row.pop("prepared_target", None)
            row.pop("claim", None)
            self.save()

    def failure(self, episode: Episode, error: Exception) -> None:
        with self._lock:
            row = self.row(episode)
            row.setdefault("attempts", []).append({"at": now_iso(), "error": type(error).__name__})
            row["attempts"] = row["attempts"][-3:]
            row.pop("prepared_target", None)
            row.pop("claim", None)
            self.save()


def plan_sha(rows: list[dict]) -> str:
    stable = [
        {
            "episode_id": row["episode"]["episode_id"],
            "identity": row["identity"],
            "source_id": row["selected"]["source_id"],
            "source_url": row["selected"]["url"],
            "display_name": row["display_name"],
        }
        for row in rows
    ]
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan(
    episodes: list[Episode],
    provider: EpisodeSourceProvider,
    state: EpisodeState,
    limit: int,
    *,
    on_prepared: Callable[[dict], None] | None = None,
    on_inspected: Callable[[Episode], None] | None = None,
    deadline_monotonic: float | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for episode in episodes:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        if state.uploaded(episode):
            continue
        candidate = provider.discover(episode)
        if on_inspected:
            on_inspected(episode)
        if candidate is None:
            continue
        name = display_name(episode, candidate)
        state.prepared(episode, candidate, name)
        row = {
            "episode": episode.to_dict(),
            "identity": episode.identity,
            "selected": candidate.to_dict(),
            "display_name": name,
        }
        rows.append(row)
        if on_prepared:
            on_prepared(row)
        if len(rows) >= limit:
            break
    return rows


def target_session(email: str, password: str):
    if email.strip().casefold() != TARGET_EMAIL:
        raise RuntimeError(f"Refusing target account other than {TARGET_EMAIL}")
    # The uploader is shared with the film pipeline. Its account guard is set
    # for this process only; secrets stay external to the repository.
    prehrajto.EXPECTED_EMAIL = TARGET_EMAIL
    return prehrajto.login(email, password)


def upload_plan(rows: list[dict], state: EpisodeState, source_email: str, source_password: str, target_email: str, target_password: str) -> int:
    source_provider = EpisodeSourceProvider.authenticated(source_email, source_password)
    target = target_session(target_email, target_password)
    uploaded = 0
    for row in rows:
        episode = Episode.from_dict(row["episode"])
        if state.uploaded(episode):
            continue
        candidate = Candidate.from_dict(row["selected"])
        try:
            known = prehrajto.uploaded_video_id_by_name(target, row["display_name"])
            if known:
                state.success(episode, known, row["display_name"])
                uploaded += 1
                continue
            refreshed = source_provider.refresh(candidate, session=source_provider.session)
            if refreshed.source_id != candidate.source_id or refreshed.url != candidate.url:
                raise RuntimeError("Source identity changed before upload")

            def record_prepared(video_id: str, size: int) -> None:
                state.row(episode)["prepared_target"] = {"target_video_id": video_id, "size_bytes": size, "at": now_iso()}
                state.save()

            result = prehrajto.relay_upload(target, source_provider.session, refreshed, row["display_name"], episode.description, on_prepared=record_prepared)
            state.success(episode, result.video_id, row["display_name"])
            uploaded += 1
        except Exception as error:
            state.failure(episode, error)
            raise
    return uploaded

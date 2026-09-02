"""Concurrent, leased upload workers for an already verified source manifest."""

from __future__ import annotations

import concurrent.futures
import queue
import uuid
from pathlib import Path

from sdilej_to_prehrajto import prehrajto
from sdilej_to_prehrajto.models import Candidate

from .episodes import EpisodeSourceProvider
from .models import Episode
from .pipeline import EpisodeState, target_session


def uploaded_identities(state: EpisodeState) -> set[str]:
    return {
        identity
        for identity, row in state.data["episodes"].items()
        if row.get("upload", {}).get("target_video_id")
    }


def target_confirmed(session, video_id: str, display_name: str) -> bool:
    return prehrajto.uploaded_video_count(session) is not None and prehrajto.uploaded_video_confirmed(session, video_id, display_name)


def upload_continuously(rows: list[dict], state: EpisodeState, *, workers: int, source_email: str, source_password: str, target_email: str, target_password: str) -> dict:
    if not 1 <= workers <= 6:
        raise ValueError("workers must be between 1 and 6")
    released = state.release_orphaned_claims()
    pending: queue.Queue[dict] = queue.Queue()
    for row in rows:
        pending.put(row)

    def login_pair(_index: int):
        provider = EpisodeSourceProvider.authenticated(source_email, source_password)
        return provider, target_session(target_email, target_password)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pairs = list(executor.map(login_pair, range(workers)))
        before = prehrajto.uploaded_video_count(pairs[0][1])
        execution = uuid.uuid4().hex

        def worker(index: int) -> int:
            provider, target = pairs[index]
            completed = 0
            while True:
                try:
                    row = pending.get_nowait()
                except queue.Empty:
                    return completed
                try:
                    episode = Episode.from_dict(row["episode"])
                    if not state.claim(episode, f"{execution}-worker-{index}"):
                        continue
                    candidate = Candidate.from_dict(row["selected"])
                    existing = prehrajto.uploaded_video_id_by_name(target, row["display_name"])
                    if existing:
                        if not target_confirmed(target, existing, row["display_name"]):
                            raise RuntimeError("Existing target name was not confirmed by listing and statistics")
                        state.success(episode, existing, row["display_name"])
                        completed += 1
                        continue
                    refreshed = provider.refresh(candidate, session=provider.session)
                    if (refreshed.source_id, refreshed.url) != (candidate.source_id, candidate.url):
                        raise RuntimeError("Verified source identity changed before upload")

                    def prepared(video_id: str, size: int) -> None:
                        state.row(episode)["prepared_target"] = {"target_video_id": video_id, "size_bytes": size}
                        state.save()

                    result = prehrajto.relay_upload(target, provider.session, refreshed, row["display_name"], episode.description, on_prepared=prepared)
                    if not target_confirmed(target, result.video_id, row["display_name"]):
                        raise RuntimeError("Target listing and statistics did not confirm the uploaded episode")
                    state.success(episode, result.video_id, row["display_name"])
                    completed += 1
                except Exception as error:
                    reconciled = prehrajto.uploaded_video_id_by_name(target, row["display_name"])
                    if reconciled and target_confirmed(target, reconciled, row["display_name"]):
                        state.success(episode, reconciled, row["display_name"])
                        completed += 1
                    else:
                        state.failure(episode, error)
                    print(f"upload_failed identity={row.get('identity')} error={type(error).__name__}", flush=True)
                finally:
                    pending.task_done()

        completed = sum(executor.map(worker, range(workers)))
        after = prehrajto.uploaded_video_count(pairs[0][1])
    return {"released_orphaned_claims": released, "queued": len(rows), "uploaded_or_reconciled": completed, "target_video_count_before": before, "target_video_count_after": after}

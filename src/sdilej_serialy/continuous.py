"""Concurrent, leased upload workers for an already verified source manifest."""

from __future__ import annotations

import concurrent.futures
import collections
import threading
import time
import uuid
from collections.abc import Callable

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


def upload_continuously(
    rows: list[dict],
    state: EpisodeState,
    *,
    workers: int,
    source_email: str,
    source_password: str,
    target_email: str,
    target_password: str,
    refill_rows: Callable[[], list[dict]] | None = None,
    refill_interval_seconds: float = 15,
) -> dict:
    if not 1 <= workers <= 6:
        raise ValueError("workers must be between 1 and 6")
    released = state.release_orphaned_claims()
    pending = collections.deque(rows)
    known_identities = {str(row["identity"]) for row in rows}
    queue_condition = threading.Condition()
    in_flight = 0
    refilling = False

    def login_pair(_index: int):
        provider = EpisodeSourceProvider.authenticated(source_email, source_password)
        return provider, target_session(target_email, target_password)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pairs = list(executor.map(login_pair, range(workers)))
        before = prehrajto.uploaded_video_count(pairs[0][1])
        execution = uuid.uuid4().hex

        def take_next_row() -> dict | None:
            nonlocal in_flight, refilling
            while True:
                refill_leader = False
                with queue_condition:
                    if pending:
                        in_flight += 1
                        return pending.popleft()
                    if refill_rows is None or in_flight == 0:
                        return None
                    if not refilling:
                        refilling = True
                        refill_leader = True
                    else:
                        queue_condition.wait()
                        continue
                if refill_leader:
                    fresh_rows: list[dict] = []
                    try:
                        fresh_rows = refill_rows()
                    except Exception as error:
                        print(f"queue_refill_failed={type(error).__name__}", flush=True)
                    added = 0
                    with queue_condition:
                        for row in fresh_rows:
                            identity = str(row["identity"])
                            if identity in known_identities:
                                continue
                            known_identities.add(identity)
                            pending.append(row)
                            added += 1
                    if added:
                        with queue_condition:
                            refilling = False
                            queue_condition.notify_all()
                        print(f"queue_refilled={added}", flush=True)
                    else:
                        time.sleep(refill_interval_seconds)
                        with queue_condition:
                            refilling = False
                            queue_condition.notify_all()

        def worker(index: int) -> int:
            nonlocal in_flight
            provider, target = pairs[index]
            completed = 0
            while True:
                row = take_next_row()
                if row is None:
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
                    with queue_condition:
                        in_flight -= 1
                        queue_condition.notify_all()

        completed = sum(executor.map(worker, range(workers)))
        after = prehrajto.uploaded_video_count(pairs[0][1])
    return {"released_orphaned_claims": released, "queued": len(known_identities), "uploaded_or_reconciled": completed, "target_video_count_before": before, "target_video_count_after": after}

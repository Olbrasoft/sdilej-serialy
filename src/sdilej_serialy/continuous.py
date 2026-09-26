"""Concurrent, leased upload workers for an already verified source manifest."""

from __future__ import annotations

import concurrent.futures
import collections
import threading
import time
import uuid
from collections.abc import Callable

import requests
from sdilej_to_prehrajto import prehrajto
from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto.sdilej import SdilejError

from .episodes import EpisodeSourceProvider
from .models import Episode
from .pipeline import EpisodeState, target_session
from .target import existing_episode
from .quality import upload_eligible
from .resilience import (TargetPending, error_evidence, receipt_matches,
                         receipt_requester, transient_http)


class SourceUnavailable(RuntimeError):
    """A source-only operation failed before any target allocation."""


def prepare_source(provider, candidate, require_original_size):
    for attempt in range(3):
        try:
            refreshed = provider.refresh(candidate, session=provider.session)
            if (refreshed.source_id, refreshed.url) != (candidate.source_id, candidate.url):
                raise RuntimeError('Verified source identity changed before upload')
            if require_original_size:
                from .source_detail import resolve_original
                refreshed = resolve_original(provider.session, refreshed)
                if not candidate.size_bytes or refreshed.size_bytes != candidate.size_bytes:
                    raise RuntimeError('Recovery original size changed; source requires review')
            return refreshed
        except (SdilejError, requests.RequestException) as error:
            if attempt == 2:
                raise SourceUnavailable('Source preparation failed before target allocation') from error
            print(f'source_retry={attempt + 1} error={type(error).__name__}', flush=True)
            time.sleep(2 ** (attempt + 1))


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
    require_original_size: bool = False,
    target_login: Callable[[], object] | None = None,
    stop_event: threading.Event | None = None,
    select_source: Callable[[dict], dict] | None = None,
    recover_source_errors: bool = False,
    recover_target_errors: bool = False,
    transient_pause: threading.Event | None = None,
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
        try:
            provider = EpisodeSourceProvider.authenticated(source_email, source_password)
        except (SdilejError, requests.RequestException) as error:
            if recover_source_errors:
                raise SourceUnavailable('Source login unavailable') from error
            raise
        return provider, (target_login() if target_login else target_session(target_email, target_password))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pairs = list(executor.map(login_pair, range(workers)))
        before = prehrajto.uploaded_video_count(pairs[0][1])
        execution = uuid.uuid4().hex

        def take_next_row() -> dict | None:
            nonlocal in_flight, refilling
            while True:
                refill_leader = False
                with queue_condition:
                    if stop_event is not None and stop_event.is_set():
                        return None
                    if transient_pause is not None and transient_pause.is_set():
                        return None
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
                    if not upload_eligible(row):
                        print(f"upload_deferred=quality_review identity={row['identity']}", flush=True)
                        continue
                    episode = Episode.from_dict(row["episode"])
                    if not state.claim(episode, f"{execution}-worker-{index}"):
                        continue
                    candidate = Candidate.from_dict(row["selected"])
                    if recover_target_errors and state.row(episode).get('prepared_target'):
                        record = state.row(episode)
                        if receipt_matches(record):
                            target_id = record['prepared_target']['target_video_id']
                            name = record.get('source', {}).get('display_name', row['display_name'])
                            if target_confirmed(target, target_id, name):
                                state.success(episode, target_id, name)
                                completed += 1
                                continue
                        raise TargetPending('Allocated target requires confirmation; no new upload')
                    existing = existing_episode(target, row["display_name"],
                                                state.row(episode).get('prepared_target', {}).get('target_video_id'))
                    if existing:
                        if require_original_size and state.row(episode).get('prepared_target'):
                            raise RuntimeError('Recovery transfer is uncertain; retain target for review')
                        state.success(episode, existing, row["display_name"])
                        completed += 1
                        continue
                    if state.row(episode).get("prepared_target"):
                        raise RuntimeError("Existing upload requires reconciliation")
                    if select_source:
                        # Resolve an upgrade only after the global episode claim,
                        # and never change a source with an allocated target.
                        row = select_source(row)
                        candidate = Candidate.from_dict(row['selected'])
                        state.prepared(episode, candidate, row['display_name'])
                    refreshed = prepare_source(provider, candidate, require_original_size)

                    def prepared(video_id: str, size: int) -> None:
                        state.row(episode)["prepared_target"] = {"target_video_id": video_id, "size_bytes": size}
                        state.save()

                    state.row(episode)["prepared_target"] = {"creation_intent": True}
                    state.save()
                    options = {'upload_requester': receipt_requester(state, episode)} if recover_target_errors else {}
                    result = prehrajto.relay_upload(target, provider.session, refreshed, row["display_name"], episode.description,
                                                   on_prepared=prepared, **options)
                    if not target_confirmed(target, result.video_id, row["display_name"]):
                        raise RuntimeError("Target listing and statistics did not confirm the uploaded episode")
                    state.success(episode, result.video_id, row["display_name"])
                    completed += 1
                except Exception as error:
                    source_deferred = (recover_source_errors and isinstance(error, SourceUnavailable)
                                       and not state.row(episode).get('prepared_target'))
                    target_deferred = recover_target_errors and not source_deferred and (transient_http(error)
                        or isinstance(error, TargetPending)
                        or isinstance(error, prehrajto.PrehrajtoError) and bool(state.row(episode).get('prepared_target')))
                    if target_deferred and transient_http(error) and transient_pause is not None:
                        transient_pause.set()
                    if stop_event is not None and not (source_deferred or target_deferred):
                        stop_event.set()
                    try:
                        # A newly allocated target can appear in the listing
                        # before its bytes arrive. Strict replay must not call
                        # that a successful transfer after an exception.
                        reconciled = None if require_original_size else existing_episode(
                            target, row["display_name"],
                            state.row(episode).get('prepared_target', {}).get('target_video_id'))
                    except Exception:
                        reconciled = None
                    if reconciled:
                        state.success(episode, reconciled, row["display_name"])
                        completed += 1
                    else:
                        state.failure(episode, error)
                    outcome = 'source_deferred' if source_deferred else 'target_deferred' if target_deferred else 'upload_failed'
                    print(f"{outcome} identity={row.get('identity')} error={error_evidence(error)}", flush=True)
                finally:
                    with queue_condition:
                        in_flight -= 1
                        queue_condition.notify_all()

        completed = sum(executor.map(worker, range(workers)))
        try:
            after = prehrajto.uploaded_video_count(pairs[0][1])
        except Exception as error:
            if not recover_target_errors or not transient_http(error):
                raise
            after = None
            if transient_pause is not None:
                transient_pause.set()
            print(f'target_statistics_deferred error={error_evidence(error)}', flush=True)
    return {"released_orphaned_claims": released, "queued": len(known_identities), "uploaded_or_reconciled": completed, "target_video_count_before": before, "target_video_count_after": after}

"""Attach exact-source Czech subtitles after a requested English upload is ready."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time

from requests.adapters import HTTPAdapter

from sdilej_to_prehrajto import prehrajto
from sdilej_to_prehrajto.models import Candidate

from .continuous import target_confirmed
from .dual import credentials, directory_for, load
from .episodes import EpisodeSourceProvider
from .git_state import CheckpointError, GitCheckpointPersister
from .models import Episode
from .pipeline import atomic_json, now_iso, target_session
from .quality import SUBTITLE_POLICY, upload_eligible
from .resilience import error_evidence
from .source_cache import RequestGate
from .source_repair import SourceRepairFeed
from .subtitle_media import extract_original, normalize_srt, SubtitleUnavailable
from .subtitle_target import SubtitleTarget, TargetUnavailable, parse_listing, trusted_detail


def eligible_rows(queue, uploads, state):
    now = datetime.now(UTC)
    for row in queue:
        if row.get('source_policy') != SUBTITLE_POLICY or not upload_eligible(row):
            continue
        record = uploads.get('episodes', {}).get(row['identity'], {})
        upload = record.get('upload')
        if not upload:
            continue
        if record.get('target_account') != row['target_account']:
            raise ValueError('Subtitle owner changed')
        previous = state.get('videos', {}).get(str(upload['target_video_id']), {})
        if previous.get('status') == 'attached_verified' and previous.get('title_updated'):
            continue
        recover_processing = (previous.get('reason') == 'target_detail_identity_unverified'
                              and previous.get('processing_guard_revision', 0) < 1)
        if not recover_processing and previous.get('retry_after') and datetime.fromisoformat(previous['retry_after']) > now:
            continue
        yield row, record


def read_target_view(api, video_id, listing, detail_url):
    """Require the owned upload form to be ready before reading its player."""
    if listing is None:
        raise TargetUnavailable('target_listing_video_missing')
    if not listing.ready:
        return listing
    view = api.refresh(video_id, listing.title, detail_url)
    view.ready = bool(view.ready and listing.ready)
    view.action = listing.action
    by_id = {track['id']: track for track in listing.tracks}
    for track in view.tracks:
        track['language_action'] = by_id.get(track['id'], {}).get('language_action', '')
    return view


def exact_source(row, record):
    source = record.get('source', {})
    selected = row['selected']
    return (source.get('source_id') == selected['source_id']
            and source.get('source_url') == selected['url']
            and source.get('size_bytes') == selected['size_bytes'])


def finish(entry, view, save, rename, dry_run):
    entry.update(status='attached_verified', verified_at=now_iso())
    ready_title = re.sub(r' \(titulky čekají\)(?=(?:\.[A-Za-z0-9]{1,8})?$)', '', view.title)
    if not dry_run:
        save()
        if ready_title != view.title:
            rename(ready_title)
        entry['title_updated'] = True


def attach(row, record, entry, view, api, provider, refresh, save, rename, *, deadline, dry_run=False):
    """One durable POST intent; an uncertain result is only ever read again."""
    if not exact_source(row, record):
        entry['status'] = 'source_binding_missing'
        return
    expected = row['subtitles']['sha256']
    if not view.ready:
        entry['status'] = 'target_processing'
        return
    intent = entry.get('intent')
    if not intent:
        if view.has_czech:
            for track in view.tracks:
                if track['czech'] and not track['processing'] and api.verify_content(track, expected):
                    finish(entry, view, save, rename, dry_run)
                    return
            entry['status'] = 'existing_czech_requires_review'
            return
        if view.unknown_tracks:
            entry['status'] = 'existing_tracks_uncertain'
            return
        episode, candidate = Episode.from_dict(row['episode']), Candidate.from_dict(row['selected'])
        current = provider._inspect(episode, candidate)
        if current is None or current.size_bytes != candidate.size_bytes:
            raise SubtitleUnavailable('source_original_changed')
        payload, _ = extract_original(provider.session,
            dict(source_id=candidate.source_id, source_url=candidate.url, source_filename=candidate.filename), deadline=deadline)
        payload = normalize_srt(payload)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise SubtitleUnavailable('source_subtitle_changed')
        if dry_run:
            entry['status'] = 'ready_to_attach'
            return
        filename = f"episode-{view.video_id}-cs-{expected[:16]}.srt"
        entry['intent'] = dict(filename=filename, sha256=expected,
                               before_ids=[t['id'] for t in view.tracks], created_at=now_iso())
        entry['status'] = 'upload_intent'
        save()  # Must reach Git before the only subtitle POST.
        api.upload(view, filename, payload)
        entry['status'] = 'upload_unconfirmed'
        save()
        view = refresh()
        intent = entry['intent']
    own = [track for track in view.tracks
           if track['id'] not in intent['before_ids']
           and (track['id'] == intent.get('subtitle_id') or intent['filename'].rsplit('.', 1)[0] in track['name'])]
    if len(own) != 1:
        entry['status'] = 'upload_unconfirmed'
        return
    track = own[0]
    if not track['czech']:
        if intent.get('language_attempted') or not track.get('language_action'):
            entry['status'] = 'language_unconfirmed'
            return
        if dry_run:
            entry['status'] = 'language_pending'
            return
        intent.update(subtitle_id=track['id'], language_attempted=True)
        save()
        api.set_own_czech_language(track, view.video_id)
        entry['status'] = 'verifying_language'
        return
    if track['processing'] or not api.verify_content(track, expected):
        entry['status'] = 'verifying_content'
        return
    intent['subtitle_id'] = track['id']
    finish(entry, view, save, rename, dry_run)


def run(root, generation, *, persist=False, dry_run=False, runtime_minutes=25):
    directory, plan, queue = load(root, generation)
    uploads = json.loads((directory / 'state.json').read_text())
    repairs_path = directory / 'source-repairs.jsonl'
    repairs = SourceRepairFeed(plan, queue, lambda: repairs_path.read_text() if repairs_path.exists() else '')
    repairs.refresh()
    queue = [repairs.select(row) for row in queue]
    path, report_path = directory / 'subtitles.json', directory / 'subtitles-report.json'
    state = json.loads(path.read_text()) if path.exists() else dict(schema_version=1, generation=generation, videos={})
    if state.get('schema_version') != 1 or state.get('generation') != generation:
        raise ValueError('Subtitle state generation changed')
    pending = list(eligible_rows(queue, uploads, state))
    if not pending:
        return dict(checked=0, statuses={})
    publisher = GitCheckpointPersister(root, (report_path,)) if persist else None

    def save():
        state['updated_at'] = now_iso()
        atomic_json(path, state)
        atomic_json(report_path, dict(updated_at=now_iso(), statuses=dict(Counter(r['status'] for r in state['videos'].values()))))
        if publisher:
            publisher(path)

    provider = EpisodeSourceProvider.authenticated(os.environ['SDILEJ_EMAIL'], os.environ['SDILEJ_PASSWORD'],
                                                   request_gate=RequestGate())
    accounts = credentials(plan)
    deadline = time.monotonic() + runtime_minutes * 60
    checked = 0
    try:
        for alias in ('a', 'b'):
            owned = [(row, record) for row, record in pending if row['target_account'] == alias]
            if not owned:
                continue
            email, password = accounts[alias]
            with target_session(email, password, expected_email=email) as session:
                # Subtitle language changes use GET, but are still mutations.
                # Only the explicit read wrappers may retry in this worker.
                session.mount(prehrajto.BASE_URL + '/', HTTPAdapter(max_retries=0))
                api = SubtitleTarget(session)
                for row, record in owned:
                    if time.monotonic() >= deadline - 90:
                        break
                    upload = record['upload']
                    video_id = str(upload['target_video_id'])
                    entry = state['videos'].setdefault(video_id, dict(identity=row['identity'], account=alias,
                                                                      target_video_id=video_id, status='pending'))
                    if entry['identity'] != row['identity'] or entry['account'] != alias:
                        raise ValueError('Subtitle target identity changed')
                    entry['processing_guard_revision'] = 1
                    try:
                        if not target_confirmed(session, video_id, upload['display_name']):
                            raise TargetUnavailable('target_owner_unverified')

                        def refresh():
                            phrase = f"{row['episode']['series_title']} S{row['episode']['season']:02d}E{row['episode']['episode']:02d}"
                            response = session.get(prehrajto.BASE_URL + '/profil/nahrana-videa',
                                                   params={'searchPhrase': phrase}, timeout=45)
                            response.raise_for_status()
                            listed, _ = parse_listing(response.text)
                            listing = next((target for target in listed if target.video_id == video_id), None)
                            if listing and trusted_detail(listing.detail_url):
                                entry['detail_url'] = listing.detail_url
                            if not trusted_detail(entry.get('detail_url', '')):
                                raise TargetUnavailable('target_detail_url_missing')
                            return read_target_view(api, video_id, listing, entry['detail_url'])

                        view = refresh()
                        attach(row, record, entry, view, api, provider, refresh, save,
                               lambda title: prehrajto.rename_video(session, video_id, title),
                               deadline=deadline, dry_run=dry_run)
                        entry.pop('error', None)
                        entry.pop('reason', None)
                    except CheckpointError:
                        raise
                    except Exception as error:
                        entry['status'] = 'upload_unconfirmed' if entry.get('intent') else 'deferred'
                        entry['error'] = error_evidence(error)
                        if isinstance(error, (SubtitleUnavailable, TargetUnavailable)):
                            entry['reason'] = str(error)
                    entry.update(checked_at=now_iso(), retry_after=(datetime.now(UTC) + timedelta(minutes=15)).isoformat())
                    save()
                    checked += 1
                    print(f"episode_subtitles identity={row['identity']} status={entry['status']}", flush=True)
    finally:
        provider.session.close()
    return dict(checked=checked, statuses=dict(Counter(r['status'] for r in state['videos'].values())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', required=True)
    parser.add_argument('--persist-git-state', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--decision', choices=['needed'])
    args = parser.parse_args()
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path(__file__).resolve().parents[2])).resolve()
    directory = directory_for(root, args.generation)
    if args.decision:
        _, _, rows = load(root, args.generation)
        uploads = json.loads((directory / 'state.json').read_text())
        path = directory / 'subtitles.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        print(str(any(eligible_rows(rows, uploads, state))).lower())
        return
    print(json.dumps(run(root, args.generation, persist=args.persist_git_state, dry_run=args.dry_run)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'episode_subtitles_failed error={error_evidence(error)}', flush=True)
        raise SystemExit(1) from None

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .catalog import fetch_episode_rows, load_jsonl, prepare_episodes, readonly_connection, write_jsonl_gzip
from .continuous import upload_continuously, uploaded_identities
from .episodes import EpisodeSourceProvider
from .git_state import GitCheckpointPersister
from .manifest import SourceManifest
from .models import Episode
from .pipeline import EpisodeState, build_plan, plan_sha, upload_plan


ROOT = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parents[2])).resolve()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def export_catalog(args) -> int:
    connection = readonly_connection(require_env("DATABASE_URL"))
    try:
        rows = prepare_episodes(fetch_episode_rows(connection))
        connection.rollback()
    finally:
        connection.close()
    # A targeted pilot must not first be discarded by the general top-series
    # limit.  Apply its identity filters before the optional broad ranking cap.
    if args.series_id:
        rows = [row for row in rows if row["series_id"] == args.series_id]
    if args.season is not None:
        rows = [row for row in rows if row["season"] == args.season]
    if args.episode is not None:
        rows = [row for row in rows if row["episode"] == args.episode]
    if args.series_limit and not args.series_id:
        rows = [row for row in rows if row["series_priority_rank"] <= args.series_limit]
    if args.episode_limit:
        rows = rows[: args.episode_limit]
    write_jsonl_gzip(args.out, rows)
    print(f"exported_episodes={len(rows)} output={args.out}")
    return 0


def prepare(args) -> int:
    episodes = [Episode.from_dict(row) for row in load_jsonl(args.backlog)]
    state = EpisodeState(args.state)
    provider = EpisodeSourceProvider.authenticated(require_env("SDILEJ_EMAIL"), require_env("SDILEJ_PASSWORD"))
    rows = build_plan(episodes, provider, state, args.limit)
    document = {"schema_version": 1, "created_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(), "rows": rows}
    document["sha256"] = plan_sha(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"prepared={len(rows)} plan_sha={document['sha256']} output={args.out}")
    return 0


def prepare_queue(args) -> int:
    episodes = [Episode.from_dict(row) for row in load_jsonl(args.backlog)]
    state = EpisodeState(args.state)
    manifest = SourceManifest(args.manifest)
    source_email = require_env("SDILEJ_EMAIL")
    source_password = require_env("SDILEJ_PASSWORD")
    if args.limit < 1:
        raise ValueError("--limit must be at least 1")
    if not 1 <= args.workers <= 2:
        raise ValueError("--workers must be between 1 and 2")
    if not 0 <= args.runtime_minutes <= 330:
        raise ValueError("--runtime-minutes must be between 0 and 330")
    providers = [
        EpisodeSourceProvider.authenticated(source_email, source_password)
        for _ in range(args.workers)
    ]
    persister = GitCheckpointPersister(ROOT, (args.manifest,)) if args.persist_git_state else None
    inspected: set[str] = set()
    inspected_lock = threading.Lock()
    deadline = time.monotonic() + args.runtime_minutes * 60 if args.runtime_minutes else None

    def persist_prepared(row: dict) -> None:
        manifest.add(row)
        manifest.save()
        if persister:
            persister(args.state)

    def mark_inspected(episode: Episode) -> None:
        with inspected_lock:
            inspected.add(episode.identity)

    def prepare_batch(candidates: list[Episode]) -> list[dict]:
        worker_count = min(len(providers), args.limit, len(candidates))
        base_limit, extra = divmod(args.limit, worker_count)

        def prepare_worker(index: int) -> list[dict]:
            worker_limit = base_limit + (1 if index < extra else 0)
            return build_plan(
                candidates[index::worker_count],
                providers[index],
                state,
                worker_limit,
                on_prepared=persist_prepared,
                on_inspected=mark_inspected,
                deadline_monotonic=deadline,
            )

        if worker_count == 1:
            return prepare_worker(0)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            batches = executor.map(prepare_worker, range(worker_count))
            return [row for batch in batches for row in batch]

    rows: list[dict] = []
    while True:
        known = manifest.identities()
        with inspected_lock:
            candidates = [
                episode
                for episode in episodes
                if episode.identity not in known and episode.identity not in inspected
            ]
        if candidates:
            rows.extend(prepare_batch(candidates))
        if deadline is None or time.monotonic() >= deadline:
            break
        if not candidates:
            # Every missing episode was inspected during this run. Retry the
            # unresolved set after a short pause instead of spinning hot.
            with inspected_lock:
                inspected.clear()
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    manifest.save()
    print(f"prepared={len(rows)} queue_size={len(manifest.identities())} manifest={args.manifest}")
    return 0


def upload(args) -> int:
    document = json.loads(args.plan.read_text(encoding="utf-8"))
    rows = document.get("rows") or []
    actual = plan_sha(rows)
    if document.get("sha256") != actual or args.approved_sha != actual:
        raise SystemExit("Plan SHA does not match the reviewed plan")
    count = upload_plan(rows, EpisodeState(args.state), require_env("SDILEJ_EMAIL"), require_env("SDILEJ_PASSWORD"), require_env("PREHRAJTO_EMAIL"), require_env("PREHRAJTO_PASSWORD"))
    print(f"uploaded={count} plan_sha={actual}")
    return 0


def continuous(args) -> int:
    if os.environ.get("CONTINUOUS_ENABLED") != "true":
        raise SystemExit("Continuous mode requires CONTINUOUS_ENABLED=true")
    persister = GitCheckpointPersister(ROOT, (args.report,)) if args.persist_git_state else None
    state = EpisodeState(args.state, on_save=persister)
    manifest = SourceManifest(args.manifest)
    rows = manifest.pending(uploaded_identities(state), limit=args.limit)

    def refill_rows() -> list[dict]:
        payload = (
            persister.read_remote_file("manifests/selected-episodes.jsonl")
            if persister
            else args.manifest.read_text(encoding="utf-8")
        )
        merged = manifest.merge_jsonl(payload)
        if merged:
            print(f"verified_sources_refreshed={merged}", flush=True)
        return manifest.pending(uploaded_identities(state), limit=args.limit)

    result = upload_continuously(
        rows,
        state,
        workers=args.workers,
        source_email=require_env("SDILEJ_EMAIL"),
        source_password=require_env("SDILEJ_PASSWORD"),
        target_email=require_env("PREHRAJTO_EMAIL"),
        target_password=require_env("PREHRAJTO_PASSWORD"),
        refill_rows=refill_rows,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if persister:
        persister(args.state)
    print("continuous=" + json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-catalog")
    export.add_argument("--out", type=Path, default=ROOT / "backlog" / "series-episodes.jsonl.gz")
    export.add_argument("--series-limit", type=int)
    export.add_argument("--series-id", type=int)
    export.add_argument("--season", type=int)
    export.add_argument("--episode", type=int)
    export.add_argument("--episode-limit", type=int)
    export.set_defaults(func=export_catalog)
    prepare_cmd = commands.add_parser("prepare")
    prepare_cmd.add_argument("--backlog", type=Path, default=ROOT / "backlog" / "series-episodes.jsonl.gz")
    prepare_cmd.add_argument("--out", type=Path, default=ROOT / "plans" / "pilot-plan.json")
    prepare_cmd.add_argument("--state", type=Path, default=ROOT / "state" / "episodes.json")
    prepare_cmd.add_argument("--limit", type=int, default=1)
    prepare_cmd.set_defaults(func=prepare)
    queue_cmd = commands.add_parser("prepare-queue")
    queue_cmd.add_argument("--backlog", type=Path, default=ROOT / "backlog" / "series-episodes.jsonl.gz")
    queue_cmd.add_argument("--state", type=Path, default=ROOT / "state" / "source-scan.json")
    queue_cmd.add_argument("--manifest", type=Path, default=ROOT / "manifests" / "selected-episodes.jsonl")
    queue_cmd.add_argument("--limit", type=int, default=20)
    queue_cmd.add_argument("--workers", type=int, default=1)
    queue_cmd.add_argument("--runtime-minutes", type=int, default=0)
    queue_cmd.add_argument("--persist-git-state", action="store_true")
    queue_cmd.set_defaults(func=prepare_queue)
    upload_cmd = commands.add_parser("upload")
    upload_cmd.add_argument("--plan", type=Path, default=ROOT / "plans" / "pilot-plan.json")
    upload_cmd.add_argument("--state", type=Path, default=ROOT / "state" / "episodes.json")
    upload_cmd.add_argument("--approved-sha", required=True)
    upload_cmd.set_defaults(func=upload)
    continuous_cmd = commands.add_parser("continuous")
    continuous_cmd.add_argument("--state", type=Path, default=ROOT / "state" / "episodes.json")
    continuous_cmd.add_argument("--manifest", type=Path, default=ROOT / "manifests" / "selected-episodes.jsonl")
    continuous_cmd.add_argument("--report", type=Path, default=ROOT / "reports" / "continuous.json")
    continuous_cmd.add_argument("--limit", type=int, default=50)
    continuous_cmd.add_argument("--workers", type=int, default=int(os.environ.get("UPLOAD_WORKERS", "6")))
    continuous_cmd.add_argument("--persist-git-state", action="store_true")
    continuous_cmd.set_defaults(func=continuous)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

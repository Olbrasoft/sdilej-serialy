# Ranked two-account upload

The dual generation is independent of all previous upload history. It uses only
saved `original-media-v4` Czech sources (confirmed `cs`, confidence >= 0.65),
deduplicates semantic episodes, and keeps the best resolution / smallest file
within that resolution. No source search or language detection runs during replay.
Authenticated original download URLs are refreshed; original size must still match.

The separate [low-resolution audit](SOURCE_AUDIT.md) can publish verified source
upgrades. Before a new SD/720p transfer, the uploader reads this overlay while
preserving the frozen episode identity, order and target-account assignment.
Already uploaded or allocated targets are never replaced.

Ranking uses the local catalog's IMDb rating descending, then IMDb vote count,
series ID, season and episode. Unknown ratings follow rated series. Only episodes
with a saved eligible source participate; missing episodes are not discovered.
The catalog is a snapshot, not a claim about today's live IMDb ratings.

The frozen manifest assigns odd queue positions to account A and even positions
to account B. Assignment never changes on retries. All four workers run inside
one Actions job with a shared, locked state and durable pre-transfer claims.
Each account has exactly two workers. No matrix or independent per-account jobs
may be started. The shared `sdilej-serialy-target-transfer` concurrency group also
excludes every legacy uploader. The state and plan pin account email hashes;
secrets are the only source of credentials.

## Setup and acceptance

1. Keep legacy workflows and their enable flags disabled. Configure only the new
   `PREHRAJTO_A_EMAIL`, `PREHRAJTO_A_PASSWORD`, `PREHRAJTO_B_EMAIL`,
   `PREHRAJTO_B_PASSWORD` secrets. The old target credentials are never read.
2. With the two email environment variables, run
   `python -m sdilej_serialy.dual prepare --generation pair-YYYY-MM-DD`.
   Existing generations cannot be reset. Commit the new manifest, plan and state.
3. Set `DUAL_GENERATION`, `DUAL_ENABLED=true`, `DUAL_FULL_ENABLED=false` and dispatch
   `dual-sync` in `pilot` mode. Both accounts must initially be empty.
4. The first four ranked episodes upload (two on each account). The pilot checks
   each confirmed target in both account listings: exactly once on its owner and
   absent on the other. Both statistics must show at least two videos.
5. Only after this verification set `DUAL_FULL_ENABLED=true` and dispatch `full`.
   Batches contain at most 25 episodes per account, with a 60-second pause between
   batches and a 300-minute polling budget. Later schedules resume the same state.

Source refresh and original-link failures before target allocation are retried
three times with bounded backoff. If still unavailable, only that episode is
deferred for fifteen minutes (persisted across restarts), and both queues continue.
The next batch includes it again after the delay; account assignment never changes.
A source-login outage defers that account's batch without setting a permanent
halt. The other account can continue and the workflow retries in its next batch.

Failures after target creation, changed source identity/size, target-account errors,
and checkpoint failures still stop new claims while existing transfers finish.
A durable `halted_at` circuit breaker prevents unsafe retries across scheduled
runs. Prepared or uncertain target IDs are retained and never replaced automatically.
Investigate and reconcile the exact target before clearing a halt; never remove
prepared targets or reset a generation to retry it blindly.

The workflow reinstalls the pulled project before every batch. Merely pulling Git
does not update Python's installed package, so reinstalling is required to activate
source upgrades and recovery fixes without interrupting transfers.

Disable `DUAL_FULL_ENABLED` and `DUAL_ENABLED`, and disable the `dual-sync` workflow
to prevent new runs. An already-running job must also finish or be cancelled;
changing repository variables does not interrupt an existing process.

Previous recovery snapshots and uploaded history are preserved but not consulted
for the new queue. No production database writes or reads are needed for setup.

## September 25 acceptance

Generation `pair-2026-09-25` contains 8,435 unique eligible episodes. The cached
catalog has no IMDb rating for 91 selected episodes; these are sorted last.
All 135 local tests and CI run 36145688884 passed. Pilot run 36145094610 finished
successfully, with both account statistics increasing from zero to two and all
four cross-account uniqueness checks passing at 14:23:33 UTC.

| Account | Episode | Confirmed target |
| --- | --- | --- |
| A | Zázračná planeta S01E02 — Hory | 29706198 |
| B | Zázračná planeta S01E09 — Mělká moře | 29706195 |
| A | Zázračná planeta II S01E02 — Hory | 29706196 |
| B | Zázračná planeta II S01E03 — Džungle | 29706200 |

All four saved originals are Czech 1920×1080. The two "Hory" entries belong to
different series. Sources 9823060 and 10974880 have different runtimes (49:59 and
49:12); visual inspection at 90 seconds showed a volcanic landscape in the first
and a snow leopard in the second. Both second episodes are officially named
"Mountains": [Planet Earth](https://www.bbcearth.com/shows/planet-earth) and
[Planet Earth II](https://www.bbcearth.com/shows/planet-earth-ii).

The old continuous, pilot-upload, source-discovery and single-account recovery
workflows remain disabled. Only the new dual workflow may continue this queue.

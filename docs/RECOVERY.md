# Restoring an empty replacement account

This is the legacy single-account workflow. It is disabled as of September 25,
2026 and must not be enabled for the current account pair. See
[DUAL_UPLOAD.md](DUAL_UPLOAD.md) for the independent ranked shared queue.

Recovery reuses selected Sdilej originals. It never calls search, discovery or
language detection. It refreshes the authenticated fast-download URL and checks
the original byte length against the saved selection before creating a target.
Unavailable or changed sources fail closed and remain pending for review.

## Separate, immutable source plan

`sdilej-series prepare-recovery --generation account-YYYY-MM-DD` creates:

- `history.json.gz`: an exact compressed copy of historical upload state;
- `manifest.jsonl`: only previously uploaded Czech sources with the current
  quality policy, deduplicated by normalized series/season/episode;
- `plan.json`: original commit, hashes, account and counts;
- `excluded.json`: missing sources, legacy Czech policy and non-Czech audio;
- `state.json`: new-generation progress, with no inherited target IDs/claims.

The source files and original upload state are preserved. Existing generations
cannot be recreated. Every replay validates snapshot hashes, policy, generation,
queue uniqueness and state membership. Older selections are not silently
relabeled as current-policy evidence. Restoring those requires a separate
reviewed plan, not editing the frozen manifest of an active generation.

## Deployment

1. Disable `continuous-sync` and `prepare-sources`; wait for any uploader to
   finish before switching. Recovery uses the same concurrency group as all
   target writers. Do not cancel a live transfer to reload code.
2. Verify credentials for `share.series@email.cz`; the first recovery run
   requires an empty account. Keep credentials in Actions secrets.
3. Commit the generated directory. Set `RECOVERY_ENABLED=true`,
   `RECOVERY_GENERATION=account-YYYY-MM-DD`, `RECOVERY_FULL_ENABLED=false`.
4. Dispatch `recovery-sync` with `mode=pilot`. It transfers two small HD sources
   using one worker. It verifies durable completed upload records, target counts
   and exactly one matching target per episode. Only then is
   `pilot_verified_at` persisted. Processing/transcoding is allowed; partial or
   interrupted transfers are not accepted merely because a target ID exists.
5. After reviewing the pilot, enable `RECOVERY_FULL_ENABLED=true` and dispatch
   `mode=full`. Six workers replay batches of 50. Hourly scheduling resumes the
   same state after a run ends. No refill from the ordinary producer is used.

Reports contain total/completed/remaining counts. Failed rows have a persistent
15-minute retry delay and cannot consume queue slots during that delay. An
uncertain `prepared_target` is retained, never reset to create another copy.
If an account with existing recovery progress becomes empty again, execution
stops instead of mixing generations.

Stop safely by setting `RECOVERY_FULL_ENABLED=false` (no scheduled successor)
and allowing the current run to finish. `RECOVERY_ENABLED=false` disables new
recovery jobs. Do not re-enable ordinary discovery/upload until its generation
and restored progress have been explicitly migrated; the old ordinary state
still refers to the deleted account.

The September 24 plan includes 8,413 current-policy Czech episodes. It preserves
10,767 exceptions (8,938 legacy Czech and 1,829 non-Czech/Slovak) separately.

## September 24 acceptance

All 115 local tests and CI run 35976223148 passed. Pilot run 35976412625
completed successfully: the replacement account count rose from 0 to 2.
Star Trek: The Next Generation S04E07 completed at 08:39:11 UTC as target
29663921; That '70s Show S01E23 completed at 08:39:32 UTC as target 29663929.
Authenticated exact-episode checks found one target each. The pilot gate was
persisted at 08:39:37 UTC. The original `state/episodes.json` still matched the
archived historical SHA-256. Full recovery and scheduled continuation were
then enabled; ordinary discovery and ordinary upload remain disabled.

Full run 35976594549 started with six workers. The next live check showed eight
target entries: two completed pilot uploads plus six in-flight transfers, with
no recorded recovery failures. The six included two saved 4K Czech sources for
1670. Target allocation/statistics alone are not reported as transfer completion.

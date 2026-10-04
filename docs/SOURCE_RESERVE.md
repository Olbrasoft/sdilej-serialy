# Continuous source reserve

The `prepare-source-reserve` workflow reads the saved 2,000-series catalog in
`backlog/series-episodes.jsonl.gz`; it does not access the production database or
hold target-account credentials. It is gated by `SOURCE_PREPARATION_ENABLED=true`
and runs manually or every two hours. A single source worker searches for up to
110 minutes per run, sharing the source-preparation concurrency group with the
quality audit. While preparation is enabled, the audit yields after 60 minutes
instead of occupying that group for five hours. Target uploading is independent.

Missing semantic episodes are prioritized by IMDb rating, votes, series, season
and episode. Already queued/uploaded/allocated episode identities and aliases are
not searched again. Existing current-policy verified Czech sources are reused;
otherwise authenticated discovery inspects original fast-download media and
verifies speech, resolution, duration and episode identity. The existing selection
policy remains Czech first, highest original resolution, then smallest file.
Only confirmed Czech matches enter this upload reserve. Inconclusive, foreign or
failed discoveries retain existing data and are eligible for another attempt in
24 hours; they never become proof that a Czech match does not exist.

Each verified result is atomically checkpointed in Git with:

- `manifests/selected-episodes.jsonl`: reusable selected source.
- `dual/<generation>/additions.jsonl`: append-only upload reserve.
- `state/reserve-preparation.json`: durable per-episode preparation progress.
- `reports/reserve-preparation.json`: prepared/deferred counts and reserve size.

The original frozen manifest, plan and upload state are not modified by the
producer. Additions bind to the generation and original manifest digest, extend
queue ranks consecutively and preserve alternating account ownership. The uploader
validates the combined queue for duplicate identities, episode names and source
IDs. At the next normal batch reload it sees new additions automatically. Existing
claims, target IDs and confirmed uploads retain their identity and owner. New
episodes go at the end; existing work is not reordered or replayed.

The legacy `prepare-sources` workflow remains disabled. Do not run it alongside
this producer: it exports a different catalog and is not the reserve publisher.

The Whisper extra pins faster-whisper 1.2.1 and PyAV below 19: PyAV 19 removed
the `metadata_errors` argument used by that decoder. Both preparation workflows
decode a generated WAV at startup, before any source search, so dependency
incompatibilities fail visibly rather than deferring hundreds of episodes.
Unexpected type/import/attribute errors also persist sanitized stack locations
and fail the preparation job. Revision 2 retries old `TypeError` records once
without their 24-hour delay; other source cooldowns and all upload reservations
are preserved. This does not relax Czech, resolution, size or duplicate checks.

For end-to-end acceptance after a repair, the manual workflow accepts an optional
`identity` (`series:season:episode`). It applies the same source verification,
cooldown, ownership and duplicate guards and appends through the normal durable
publisher. Scheduled/default runs scan the full cached catalog.

To avoid starving uploads when the reserve runs dry, search scheduling interleaves
eight unseen episodes with two due retries, retaining IMDb/season order inside
both groups. Old audio-bug records remain eligible for their one-time recovery.
After three consecutive unsuccessful checks of one series, the remaining episodes
of that series are skipped for this run only; no unsearched episode is marked
missing or put on cooldown. A successful result resets the series counter.
Explicit targeted checks bypass this per-series budget. The existing frozen
upload order, owners, quality policy and all duplicate guards are unchanged.

Source preparation and quality auditing retry a failed Git checkpoint up to three
rounds, waiting 15 then 30 seconds between rounds. Each round retains the same
unpublished commit/snapshot; discovery never advances before the checkpoint is
durable. Exhausted retries still fail closed rather than report false success.
Git failures report the retry count and number of rebase conflicts without
printing authenticated remotes or credentials.

## Historical source revalidation

The saved manifest also contains thousands of previously selected Czech sources
without the `original-media-v4` marker. These are not an empty reserve and must
not be discarded merely because their validation metadata predates that policy.
Preparation now prioritizes unreviewed saved Czech sources before broad discovery,
preserving IMDb/season/episode order within two lanes: known 1080p-or-higher
sources first, then SD/720p sources requiring complete quality discovery.

For the HD lane, the authenticated saved detail URL is opened directly. The
actual fast-download original is resolved, measured and probed again; series,
episode and duration checks still apply. Czech speech is checked again, not
inferred from the old language label. If several saved variants have the same
semantic episode key, their original resolutions and byte lengths are inspected
before choosing the highest-resolution Czech variant and the smallest file at
that resolution. This is revalidation of known sources, not a claim that a new
full-site search found no better 1080p/4K alternative. It follows the existing
policy of searching again for SD/720p upgrades, not for already-HD selections.
An original found to be SD/720p despite its historical HD label is deferred;
the fast lane never silently publishes it without a full quality search.

Only successful checks publish the current policy marker, a
`saved-original-revalidation-v1` audit record, exact original metadata and an
append-only queue assignment. Existing uploads, claims and the frozen plan are
unchanged. Stable source IDs and semantic episode keys retain the usual duplicate
guards. Historical rows absent from the cached catalog retain their saved episode
metadata instead of disappearing from migration.

Old search cooldowns are bypassed once for this new saved-source review. Failed
checks retain their original manifest row and a 24-hour retry delay; later normal
discovery can find alternatives. There is no slow full-search fallback inside
the HD pass. A source-only worker and the existing shared Actions concurrency
group serialize publication with other preparation/audit jobs. The uploader
keeps two transfer workers per account and consumes each published result at its
next batch reload. `saved_reviewed_this_run` and `saved_prepared_this_run` report
direct HD checks separately from general preparation counts; `reserve_total`
remains cumulative and is not the currently unused stock.

# Continuous source reserve

The `prepare-source-reserve` workflow reads the saved 2,000-series catalog in
`backlog/series-episodes.jsonl.gz`; it does not access the production database or
hold target-account credentials. It is gated by `SOURCE_PREPARATION_ENABLED=true`
and runs manually or every 30 minutes. Two source workers by default (up to four
through `SOURCE_DISCOVERY_WORKERS`) search for up to
110 minutes per run, sharing the source-preparation concurrency group with the
quality audit. A low reserve skips automatic audits before installing Whisper;
explicit targeted audits remain available. Target uploading is independent.

Missing semantic episodes are prioritized by IMDb rating, votes, series, season
and episode. Already uploaded/allocated episode identities and aliases are
not searched again. Queued identities are not appended again; failed originals
use the source-only repair lane described below. Existing current-policy verified Czech sources are reused;
otherwise authenticated discovery inspects original fast-download media and
verifies speech, resolution, duration and episode identity. The existing selection
policy remains Czech first, highest original resolution, then smallest file.
Only confirmed Czech matches enter this upload reserve. Search/original transport
failures retry after 15, 30, then 60 minutes; inconclusive speech retries after an
hour. Conclusively foreign, missing or rejected originals retain a daily cooldown.
None of these outcomes is proof that a Czech match can never exist.

The sole publisher checkpoints source results together in Git after five completed
checks or 60 seconds, and at normal shutdown. While workers are busy it also
checkpoints reusable partial evidence. These source-only batches include:

- `manifests/selected-episodes.jsonl`: reusable selected source.
- `dual/<generation>/additions.jsonl`: append-only upload reserve.
- `state/reserve-preparation.json`: durable per-episode preparation progress.
- `reports/reserve-preparation.json`: prepared/deferred counts and reserve size.
- `state/source-evidence-cache.json`: expiring parsed search, probe and speech evidence.

The original frozen manifest, plan and upload state are not modified by the
producer. Additions bind to the generation and original manifest digest, extend
queue ranks consecutively and preserve alternating account ownership. The uploader
validates the combined queue for duplicate identities, episode names and source
IDs. Idle workers can also fetch validated additions during a running batch, with
the original 25-row/account batch budget unchanged. Existing
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

Revision 3 recognizes underscore-delimited episode codes and series words in
release filenames. Alphanumeric boundaries, multi-episode rejection and exact
series/sequel checks remain enforced. The explicit `MASH` alias is also searched
for the catalog's `M*A*S*H`; compact episode ranges are rejected as multi-episode
files. Pre-v3 `no_verified_czech_match` records
receive one new attempt without the old cooldown; a new failure restores the
normal 24-hour delay. Source selection still measures originals and verifies
Czech audio before publishing, regardless of the filename language label.
A successful original-media probe proving that a file has no video stream
rejects that candidate without blocking real videos. Failed probes and
unresolved video metadata still defer selection rather than permit a downgrade.

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
unpublished commit/snapshot; the publisher does not dispatch more discovery or
advance publication before the checkpoint is durable. Already running workers
are read-only. Exhausted retries still fail closed rather than report false success.
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
checks retain their original manifest row and the appropriate retry delay; later normal
discovery can find alternatives. There is no slow full-search fallback inside
the HD pass. A single publisher and the existing shared Actions concurrency
group serialize publication with other preparation/audit jobs. The uploader
keeps two transfer workers per account and consumes each published result at its
next refill or batch reload. `saved_reviewed_this_run` and `saved_prepared_this_run` report
direct HD checks separately from general preparation counts; `reserve_total`
remains cumulative and is not the currently unused stock.

## Parallel evidence and reserve control (2026-10-10)

`SOURCE_DISCOVERY_WORKERS` defaults to two and accepts one through four. Each
worker has its own requests session and cookies. A shared request gate preserves
the site's two-second request spacing; one audio lock protects the lazy Whisper
model and bounds CPU-heavy speech work. Search, original probing and audio sample
downloads can overlap; only model loading and inference hold the audio lock.
Dispatch retains IMDb priority; a completed result is published without waiting
for an unrelated slower episode. Only the publisher checks final uniqueness and
assigns consecutive alternating account ranks.

Parsed search pages expire after six hours. Original-media and conclusive audio
evidence expire after 24 hours. Both Czech and foreign evidence can be reused,
but failures and low-confidence speech cannot. Every original still gets a fresh
authenticated detail and fast-download resolution before cache reuse. Changed
source ID, stable URL, filename, exact byte count, detail metadata, ETag or
Last-Modified invalidates the evidence. Only whitelisted parsed values are saved;
HTML, cookies and signed download/sample URLs are excluded. A failed later
candidate therefore does not force a restart of all successful earlier probes.
The cache is bounded to 10,000 live entries and shared with quality auditing.
Empty search pages are never reused or newly cached. Discovery confirms an empty
result with a second complete, fresh search; if that confirmation fails, the
episode is transiently deferred instead of marked missing.

`stock.ready` excludes uploaded, allocated, claimed and previously failed rows,
including failures whose backoff has elapsed. A newly verified, not-yet-attempted
source repair is ready without clearing the previous attempt history. Counts are split by account, and
the report estimates stock hours from confirmed uploads over the previous day.
Below 1,000 ready episodes preparation starts; `refilling` keeps it running until
3,000 ready episodes are reached. These are targets, not a promise that Czech
originals exist in the catalog. The report also exposes source-worker elapsed
time, prepared/hour, cache hits/misses and search/probe/audio computation time.

No source batching applies to target safety checkpoints. Claims, creation
intent, target allocation and complete-transfer receipts still persist immediately.
Existing uploads, uncertain allocations, the frozen queue and target concurrency
(two per account, four total) remain unchanged. A live refill validates the whole
combined queue and refuses any modification of its existing prefix.

An empty account also polls for new additions for up to five minutes rather than
waiting for the other account's long transfer. The idle timer resets on new work;
the owner's 25-row budget still ends the batch. This is bounded waiting, not an
extra uploader or an increase in account concurrency.

## Unavailable queued originals

A queue can contain hundreds of `SourceUnavailable` rows while discovery skips
them as already assigned. The reserve producer now interleaves two due repair
checks with four normal preparation checks. Repairs only apply to source failures
without a confirmed upload, target allocation or active claim. Discovery refreshes
all search pages (bypassing both search caches), checks actual original media and
verifies Czech speech using the normal best-resolution/smallest-file policy.
It never blindly reuses the old current-policy selection. Inconclusive search,
probe or language checks retain the old source and retry later.

Successful repairs update the reusable source manifest and publish
`dual/<generation>/source-repairs.jsonl`. This overlay binds each selection to
the generation, frozen manifest digest and original row fingerprint. It preserves
the existing identity, rank and account instead of adding another queue row.
The publisher rereads current target state before publication; uploaded,
allocated or active rows are left alone. Source IDs remain unique across the
queue, repairs and new additions. Upload state/history is never edited by the
producer. Preparation reports expose `repaired_this_run` and `repair_statuses`.

The uploader validates the overlay, admits fresh repairs during idle refill and
resolves a repair only after acquiring the global episode claim, before target
allocation. A newly verified repair bypasses the old source's backoff once; its
token is consumed durably before the first network attempt. Another failure
therefore keeps the normal backoff rather than retrying without limit. Existing
uncertain allocations still require receipt-based reconciliation and never change
source or create a second target. A full discovery may select a lower resolution
than the unavailable old file only when it is the best currently verified Czech
original; an unresolved better candidate still defers the entire selection.

Repair-search revision 2 retries old `no_matches` repair exclusions once without
their former daily delay. A known unavailable original with a confirmed empty
search now retries after 15, 30, then 60 minutes: a temporarily empty index is not
proof that a previously selected episode disappeared for a day. Ordinary new
catalog episodes still keep their daily no-match cooldown. An active claim seen
at publication also uses short backoff; no active transfer is changed or replayed.

## Pipelined audio and useful discovery throughput (2026-10-11)

The built-in `PipelinedLanguageDetector` lets each bounded source worker extract
its own temporary audio sample while another worker is detecting language.
One shared model still serializes loading and inference. The original download
source, 75-second sample, offsets, CPU model, VAD, confidence threshold and SDK
multi-sample consensus are unchanged. Custom detectors retain the outer lock.
Temporary samples are removed even after failures; reports contain aggregate
download, inference-wait and inference time, never URLs or audio contents.

Of each eight unseen-episode slots, up to four first explore the same seasons as
successful Czech preparations/repairs in the previous two days. The other four
explore the remaining IMDb-ranked catalog, followed by two due retries. Empty
lanes give their capacity to the other unseen lane. IMDb/episode order remains
stable within each lane. This is only a dispatch hint: every episode still gets
full identity, original resolution, size and Czech speech verification. Legacy
saved-source review retains priority. Existing target queue order/owners do not
change. No series is permanently excluded.

The existing three-miss per-series budget now also applies to queued-source
repairs; previously that lane ignored the pause and could exhaust a run checking
many missing episodes from the same series. Unsearched episodes receive no
failure record or cooldown, and explicit targeted checks bypass this budget.

`available_this_run` and `available_per_hour` count both new additions and usable
repaired sources. `prepared_this_run`/`prepared_per_hour` remain the narrower new
addition counters for compatibility. Neither cumulative additions nor successful
repairs are a substitute for `stock.ready`, the actual unused upload reserve.
Raising source workers does not raise the target limit of two uploads per account.

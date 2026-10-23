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
`identity` (`series:season:episode`, or up to 50 comma-separated identities). It applies the same source verification,
cooldown, ownership and duplicate guards and appends through the normal durable
publisher. Scheduled/default runs scan the full cached catalog.

Newly requested series can be added to the cached catalog without rebuilding the
historical upload plan. On October 23, VisionQuest S01E01-S01E04 were appended
from a transaction explicitly opened `READ ONLY` against the CR production
catalog (series 10496; episode IDs 203716, 203715, 203764, 203765). The existing
catalog rows, frozen queue, owners and completed targets were retained. A targeted
batch uses the same serialized source publisher as scheduled preparation; each
verified episode receives one normal alternating queue assignment. It does not
authorize re-uploading existing episodes or accepting title-only quality/audio
claims. Preparation jobs never receive production database credentials.

### Explicit English uploads with original Czech text subtitles

The ordinary reserve still accepts verified Czech audio only. A catalog row may
opt into `requested-english-original-czech-subtitles-v1` only for the explicitly
requested Tulsa King season 4 (series 581). The season registry preserves this
request after later catalog imports, and due requested episodes precede broad
exploration. Search pages are refreshed for this new season. A conclusively
missing English/full-Czech-subtitle source is retried after 30 minutes.
Its English speech, episode identity,
original resolution/size and a full Czech text subtitle track must be verified
before publication. Selection inspects all originals, then chooses the highest
eligible resolution and smallest file in that tier. Unknown better originals or
subtitle extraction failures defer the episode; they do not authorize a downgrade.
Normalized subtitle SHA-256, exact source ID/URL/byte size, cue count and timing
are retained. Subtitle text and authenticated media addresses are not stored in Git.

The publisher first requires `dual/<generation>/uploader-capabilities.json` from
the policy-aware uploader, including `subtitle_queue_version: 2` for its append-only
queue loader. Until a safe batch reload advertises that capability,
the verified source is saved without appending a foreign row to the shared queue.
This prevents an older running uploader from rejecting its live refill. Existing
owners, ranks, claims, completed uploads and the two-workers-per-account limit
remain unchanged. Small encoder crops such as 3832x1632 retain the 4K tier in new
selection/display; existing target names are not rewritten.

`episode-subtitles` runs hourly or manually, separately from media transfers and
with one worker. Only completed, explicitly opted-in uploads are eligible. The
worker verifies account ownership and the actual recorded upload source, waits
for processing, and extracts the exact original Czech text again. A changed hash,
unknown existing track or missing provenance prevents the write. A durable intent
with a unique filename/hash is committed before the single subtitle POST. An
uncertain response is reconciled by reads, never a repeated POST. Language changes
also have a single recorded attempt. Completion requires the created Czech track
and an identical normalized content hash from the same target ID. Existing tracks
are never deleted or overwritten. The waiting suffix is removed only after proof.

The local subtitle media/target helpers reuse the shared transfer project's
subtitle implementation without changing the pinned media relay SDK. Production
database access is never part of this follow-up workflow.

The owned upload listing must expose its ready subtitle form before the worker
reads the public player. A processing detail page can omit its numeric video ID;
that expected state is reported as `target_processing`, not an identity failure.
Writes also require the verified player to be ready and use the observed owned
form action. Missing listings never authorize a subtitle write. Earlier failures
from this processing-page identity check receive one bounded recheck.

The October 23 read-only production export supplied S04E01 (episode 203749).
S04E02 was already present on Sdilej but absent from that catalog. Its local
metadata therefore uses the explicitly provisional negative ID -581004002 with
source provenance; the duplicate key remains `581:4:2`, including after a later
canonical catalog import. The observed S04E02 original had English audio and
English subtitles only, so it must remain unqueued until full Czech text exists.
No production database rows were added or modified.

One S04E01 listing (35366844) returned a 2865732633-byte placeholder rather than
readable video: its first and last 4096 bytes were zero-filled, with data in the
middle. After two failed original probes, this policy can exclude only an original
independently confirmed with zero-filled beginning and end at exact HTTP ranges
(beginning, middle, end; 4096 bytes each). An ignored range, changed size, short
read, nonzero byte in either end or network failure preserves the deferral.
The selected-source record retains the public source ID, byte size and check time,
never the signed address or media bytes. Earlier subtitle requests deferred by
this old original check receive one recovery attempt; ordinary cooldowns remain.

This requested English path has an additional bounded 15-minute allowance for
audio and full-text extraction beyond the ordinary discovery budget. If its first
75-second audio sample fails, the second attempt reads another 75 seconds starting
at 60 seconds (clamped for short sources), rather than repeating the same slow
180-second seek. Confidence and preferred-language consensus checks remain active;
the alternate offset has a separate cache key. Older inconclusive subtitle/audio
records receive one recovery attempt. Ordinary Czech discovery is unchanged.

Media-tool initialization in source preparation and subtitle follow-up has its
own ten-minute limit. On October 23 a slow hosted runner spent over twenty minutes
installing packages without preparing any source. Such initialization must not
occupy the serialized publisher for its entire job timeout; the next scheduled
run can start on a fresh runner. Active media relays use a separate group.

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

## Evidence-led discovery and restart fairness

A running producer is not proof of an adequate reserve. The preparation report
now exposes per-account time since the last completed upload, recent discovery
outcomes, and `health.status=starved` when an account has no ready source or active
claim and no completion for 30 minutes. Such checkpoints also emit an Actions
warning. Allocated-but-uncertain historical rows do not count as active progress.

Production scheduling uses the existing positive search cache as a cross-episode
index. Complete public candidate rows are matched to catalog aliases and exact
episode codes with the existing sequel/range guards. Expired cache entries are
not imported. Every minute the dispatcher imports new hints discovered by the
other workers. An index hit only prioritizes a full normal discovery: it is not
verified Czech speech, the best available original, a ready upload, or permission
to bypass any episode cooldown, target reservation or duplicate guard.

Each round first dispatches up to six indexed candidates (Czech filename hints
before unknown language), two unexplored series probes, and two due retries.
Unused indexed capacity goes to exploration. All lanes share a pending-identity
set. Candidate IMDb order is preserved within the indexed lane. Exploration
rotates through series and durably saves its cursor, including its ordering
anchor when a series disappears from the eligible set. A restart therefore does
not repeatedly spend the run on the same first unavailable series. Legacy saved
source revalidation still precedes these lanes. Without an evidence cache the
older bounded scheduler remains available for adapters and targeted tests.

Three distinct unsuccessful episodes persist a bounded series dispatch pause
across runs: four hours for missing/foreign/rejected originals, 30 minutes for
transient or inconclusive checks. Success resets the budget; expiry grants a new
probe budget. Unsearched episodes are never marked missing or modified. Explicit
targeted verification bypasses series pauses, but not per-episode safety checks.
When the reserve is low, one queued repair is interleaved with eight preparation
tasks; positive repair hints go first. This prevents repeatedly missing old
sources from monopolizing the preparation capacity while keeping repair work
alive. Frozen queue ownership and the two-target-workers/account limit remain
unchanged. `indexed_candidates_in_scan` counts search hints, not ready stock.

### Stable original evidence despite request-time Last-Modified

The download handler can set `Last-Modified` equal to the HTTP response `Date`
on every range request. Treating this as a file version invalidated both the media
and audio cache even on consecutive checks of the same original. If these dates
are within five seconds, the request timestamp is discarded. A strong ETag is
retained; without one, cache reuse requires a fresh SHA-256 fingerprint of three
64-KiB ranges (start, middle, end) plus the existing stable identity, metadata and
exact size. This is a bounded sampled-content check, not a full-file checksum.
Normal stable Last-Modified/ETag validators retain their original behavior.

Failed, truncated or unsupported sample ranges disable cache reuse for that
check and fall back to a fresh probe and audio verification. They never authorize
stale Czech evidence or trigger an unbounded full-file download. Raw samples and
authenticated URLs are not persisted. Target transfer preparation is unchanged;
the extra read-only samples apply only to cached source inspection.

### Duration-aware language sampling

The SDK consensus assumes a minimum 900-second movie. On seven-minute episodes
this selected offsets beyond EOF, causing repeated ffmpeg failures and false
`inconclusive_audio` deferrals. The built-in detector now clamps the initial
sample to the probed original duration and places short-episode consensus samples
inside that duration. Duplicate clamped offsets do not count as extra votes.
Normal-length SDK consensus, speech confidence and Czech preference are unchanged.
Short-original audio evidence uses a new cache key; media evidence is retained.

Preparation revision 4 rechecks previously inconclusive, known-under-15-minute
episodes once without the old episode/series cooldown. It does not release other
failures or target reservations. Newly inconclusive checks restore normal backoff.

### Idle account while its peer transfers

The five-minute idle refill window no longer closes an account's workers while
the other account still has an active transfer claim. They continue polling at
the normal interval and can consume later verified additions/repairs without
waiting for the peer's large file and a whole new batch. Once neither account
has active work, the original bounded idle exit applies. Historical uncertain
allocations do not prolong waiting; stop signals, queue integrity failures and
the 25-episode/account budget still close refill. No extra workers are created.

A validated, unused repair token can also re-dispatch an episode whose old
original failed earlier in the same batch, before any target allocation. Both
the reserve and worker deduplicate dispatch versions; each repair retry consumes
one of the 25 dispatch slots. The token is rechecked against the repair feed and
the global episode claim still precedes source selection and durable token
consumption. Uploaded, allocated, active and already-consumed repairs cannot be
replayed. This avoids waiting for an unrelated large transfer and batch restart
merely to consume an already verified replacement.

### Graceful workflow admission deadline

Checking a job deadline only between 25-row batches was insufficient: a batch
started just before the cutoff could still claim episodes near the runner's
350-minute hard limit. GitHub then killed active transfers, leaving uncertain
allocations that correctly cannot be replayed automatically. This occurred on
October 11 and 12 even though the next scheduled job resumed the rest of the queue.

The dual workflow now passes one absolute `--stop-claiming-at` timestamp to every
batch. Both accounts convert it to the same monotonic deadline. Pending rows,
live refills and peer-active idle waiting cannot extend admission beyond that
deadline. An already claimed transfer continues through receipt persistence and
confirmation; the deadline neither cancels its HTTP thread nor marks it failed.
Unclaimed rows remain untouched for the next serialized run. Reports expose
`stop_claiming_at` and `claim_deadline_reached` to distinguish a safe drain from a
failure or exhausted reserve.

Admission lasts 210 minutes, leaving roughly 140 minutes of the 350-minute job
budget for draining, setup and checkpoints (including the SDK's long response
timeout). This is headroom, not a guarantee against arbitrarily slow services or
an external cancellation. The concurrency lock and two workers per account are
unchanged. Existing uncertain allocations are preserved, not retried or deleted.
Workflow changes apply to newly dispatched runs; do not cancel active transfers
merely to reload the workflow definition.

### Prevent indexed retries from starving fresh discovery

On October 19 the reserve was empty even though the positive search index still
contained untouched episodes from recently productive Czech seasons. The indexed
lane admitted due retries as well as fresh identities. A filename marked CZ could
therefore send an inconclusive source to the front again after 80+ failed checks,
bypassing the intended two-retry budget. Production also ignored productive-season
hints that were only used by the cache-less fallback scheduler.

Indexed and productive lanes now admit only fresh identities (including explicitly
version-released checks). Each round allows up to four fresh episodes from seasons
with a Czech success in the last two days, then other indexed hints up to six
prioritized identities in total. Exploration fills the eight-fresh budget; two
additional slots remain for due retries. Retries with fewer than three failures
precede persistent failures, then recent productivity and positive search evidence
break ties before catalog order. Cooldowns, paused series and immutable queue
reservations still apply. New Czech successes update the shared productive-season
set during the run; the next index refresh can prioritize neighboring episodes.

These are dispatch hints only. Every selected episode still requires complete
original-resolution, Czech speech and smallest-file checks. No language or quality
threshold is weakened, and no uncertain/uploaded target is replayed.

### Release elapsed series pauses during a running producer

On October 20 the 09:02 producer retained its initial paused-series snapshot for
the entire run. For example, series 3713 was paused until 09:07 UTC, but its fresh
S03E01 was not inspected until the next run at 10:49; it then produced a verified
Czech original. A long stretch of unsuccessful exploration exhausted the reserve.

The producer now removes elapsed series pauses on periodic checkpoints and index
refreshes, resetting the in-run miss budget for those series. It does not restore
pauses deliberately released by version-specific recovery, shorten active
cooldowns, or change episode-level retry eligibility. Exploration lanes are also
rebuilt from the shared pending set at each index refresh: previously skipped
unhinted episodes must not disappear permanently from a deque after their pause
expires. The saved exploration cursor, fresh/retry budgets, global ownership and
all source-quality checks remain unchanged.

### Re-evaluate episode backoff during discovery

On October 21 the producer started at 17:24 UTC with some episode retry deadlines
still in the future. Outer Banks S01E09 became due at 17:29, but startup filtering
had removed it from that run entirely; at 18:01 both upload accounts had been idle
for over 30 minutes. Expired series pauses were already refreshed, but individual
episode backoffs were not. This was an additional avoidable delay, not proof that
every deferred source is usable (a manual check still found an incomplete original
for this particular episode).

The indexed dispatcher now retains unassigned, cooling-down episode identities
and checks their current retry deadline before dispatch. Its normal minute refresh
can restore skipped retries once due. Active episode/series cooldowns remain in
force, version-specific recovery exceptions are unchanged, and each identity is
still dispatched at most once per run. Newly due work stays in the bounded retry
lane; it cannot consume the fresh lane or bypass Czech/original/duplicate checks.
If all remaining work is unavailable, the scan finishes without busy-waiting;
the next scheduled producer resumes it. Upload state and existing targets are
never changed by this scheduling repair.

### Distinguish compact ranges from quality labels and repeated numbers

On October 22 the unused reserve was empty after 280 source checks produced only
one usable episode. A cache audit found seven unassigned episode identities with
otherwise matching titles rejected because a compact suffix repeated the same
episode number, such as `Famílie S05E22-22`. The same range expression incorrectly
treated `+720p` as another episode. A live full discovery after the parser repair
verified Famílie S05E22, source 33672689, as a Czech original (640x352,
352242672 bytes, Czech probability 0.9752).

The matcher now collects the actual episode numbers from compact suffix chains.
Repeated identical numbers and explicitly recognized resolution labels do not
create another episode. A different number anywhere in the chain still rejects
the file, including `S01E01-01-02` and `S01E01+720p+E02`. Exact series/sequel,
original duration, original resolution, Czech speech and smallest-file checks
remain required. An ambiguous episode/season-total notation such as `S05E01-22`
is still rejected; the fix does not assume that a different suffix is a total.

Preparation revision 5 gives one recovery check only to old deferred identities
with a positive, newly accepted suffix match in the live search cache. At startup
these specific series scheduling pauses can be released; unrelated episode
cooldowns remain active. A new failure restores ordinary backoff and cannot reuse
this migration. Unsearched recovered hints remain fresh discoveries, not verified
sources. No uploaded or allocated target is replayed or modified.

### Requested new series and Czech/Slovak audio (October 23)

`backlog/requested-series.json` records the requested series, their aired episode
inventory plus additional CR-catalog episodes, and an immutable baseline of series
already present on either target. CR episodes can precede TMDB (for example Hriech
S01E08); they still need full source verification before publication. CR seasons
absent from the canonical season inventory are not inferred or renumbered.
The October 23 request covers the first two Czech-filtered CR listing pages:
30 new series, with 18 existing series excluded (including Tulsa and VisionQuest).
Positive CR episode IDs are retained where available; supplementary TMDB episode
metadata uses deterministic negative local IDs and explicit provenance. Production
CR data is read only. Catalog rows, source history and uploaded targets are retained.

The producer rotates these series in IMDb order, inspecting one episode per series
before returning to its neighbors so one slow series cannot occupy every source
worker. It tries the first available
episode of the first regular season, then at most two neighboring episodes until
CZ/SK audio is verified. Each subsequent season has its own samples and status in
`reports/reserve-preparation.json`; foreign-only samples are marked as requiring
subtitles. A negative first season does not permanently exclude later seasons.
Sampling controls scheduling only: every published episode still requires exact
identity, runtime, original dimensions/bytes and verified audio. A positive sample
unlocks that season's remaining episodes, not their upload eligibility. Registered
series continue after their first upload; restarting does not skip the whole series.
In-flight samples can wake gated work in the same producer run without busy-waiting.
Neighboring Czech episodes reuse cached series-title search pages. Initial season
samples, repairs and the final Czech-absence check for Slovak fallback use fresh
searches; every original and its audio still need verified evidence.

Selection order is Czech before Slovak, then highest original resolution, then
smallest original file. A Slovak fallback requires a completed fresh source search
and conclusive audio checks establishing that no acceptable Czech candidate was
found. Unresolved originals/audio defer the episode. Its source-bound evidence is
stored with `source_policy=czech-preferred-slovak-fallback-v1`; names use `SK Dabing`.
Stable queue eligibility does not expire, while reusing Slovak discovery evidence
for a new publication requires a recent check. Existing Czech and Tulsa subtitle
policies remain compatible.

The producer saves verified Slovak evidence but waits for an uploader capability
checkpoint (`audio_queue_version=1`) before appending a Slovak queue row. The
uploader reloads code between batches, retaining its two transfers per account,
global ownership, duplicate reservations and all historical receipts. No active
media transfer needs cancellation to deploy this change.

### Recover contradictory missing results in requested seasons

The October 23 follow-up confirmed 13 completed new episodes but found that the
producer had marked Hriech S01E04 missing for a day. An authenticated full local
check selected Slovak source 35348222, and a separate read-only Actions runner
found the same source. The long-running producer's negative result was therefore
insufficient evidence of absence; the precise upstream/session cause is not yet
established.

Only the first successful season sample needs the initial discovery refresh.
Later episodes retain individual original/audio checks while reusing recent
complete series search pages. Dispatch re-evaluates the season evidence after
in-flight results, and reusing a verified sample preserves its availability tag.
A search that omits an exact episode still present in recent positive evidence
now receives transient backoff, rather than a daily missing-source verdict.
Old requested-series missing results with exact positive hints get one scoped
recovery check; unrelated cooldowns, uploaded targets and ownership remain intact.

Failed search checkpoints retain only bounded response diagnostics (canonical
search path, HTTP status, byte/row counts and heading-match boolean), with no
HTML, cookies or authenticated stream addresses. `check-source-search` is a
manual read-only workflow for comparing a fresh Actions session; it never
publishes sources or uploads media.

### Extend the request to listing pages two and three

The second page was already registered. The third page adds 19 new series and
837 episode metadata records to inspect, bringing the plan to 49 requested
series and 4,684 episode identities. Rick and Morty, House of the Dragon,
The Gentlemen, The Agency and Ride or Die were excluded using active-generation
upload evidence. An authenticated search on both targets found no additional
existing series among the 19 new titles. Existing requests, exclusions, claims,
source selections and completed uploads are preserved. Catalog metadata is
not a verified upload reserve: each new season and episode still follows the
same CZ-before-SK, original-resolution and smallest-file checks.

Source runner setup had repeatedly timed out downloading FFmpeg dependencies
from the regional Azure Ubuntu mirror at under 60 kB/s. Preparation now uses
the official Ubuntu archive over HTTPS, omits optional package recommendations,
and bounds connection retries while retaining its ten-minute setup deadline
and the Whisper decoder startup check.

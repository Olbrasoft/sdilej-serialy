# Review saved SD and 720p sources

`python -m sdilej_serialy.source_audit` reviews only saved entries below Full HD.
It does not export a catalog or access the production database. It includes
already-uploaded episodes because the reusable source catalog must improve too,
but never changes uploaded videos, upload history, target ownership or frozen
generation manifests. Existing Full HD, 1440p and 4K entries are not searched.

Selection retains the existing policy: verified Czech audio first; then the best
original resolution; then the smallest file in that resolution. A higher-resolution
foreign file does not replace a known Czech source. If reviewing an existing
non-Czech source, the same ordering can improve its saved foreign-language
fallback; this does not enable foreign uploads in the current Czech-only queue.

The reviewer logs into Sdilej, enumerates complete search results, resolves the
authenticated fast-download original, probes its media and verifies audio with
Whisper. Preview resolution and filename language hints are not sufficient.
Series/episode identity and runtime compatibility still reject remakes and wrong
episodes. Incomplete discovery is `deferred`, never proof that no better source
exists. Such entries are eligible for retry after six hours; completed reviews
are skipped unless the input selection changes or `--force` is supplied.

## Durable files

- `manifests/selected-episodes.jsonl`: reusable best selections, changed only on
  a verified improvement; larger files at equal resolution are not selected.
- `manifests/quality-upgrades.jsonl`: audited replacements published to uploaders.
- `audit/low-resolution/state.json`: per-episode evidence, before/after selections,
  review status, input fingerprint and retry deadline.
- `audit/low-resolution/report.json`: aggregate progress and status counts.

All changed files are published in one Git checkpoint. Authenticated media URLs
and source credentials are never written to these files. A single audit worker
uses the source-preparation concurrency group; it can run alongside the uploader,
which continues to have at most two transfers per account.

## Upload integration

The dual uploader fetches the latest upgrade overlay immediately before each
new SD/720p transfer, after claiming the episode and before allocating a target.
An upgrade cannot change the episode identity, account assignment or queue rank.
The actual selected source and resolution are saved in the episode's upload state.
Completed or already allocated transfers are never switched or uploaded again.

Deployments take effect when the active Python batch finishes and the existing
workflow pulls the next revision. In-flight batches are not cancelled to reload
code, since that would create uncertain targets. Once the new code is loaded,
upgrades are checked per transfer, not merely at the beginning of a batch.

## Running

Set `QUALITY_AUDIT_ENABLED=true` and supply only `SDILEJ_EMAIL` and
`SDILEJ_PASSWORD`. No target-account credentials are required.

```bash
python -m sdilej_serialy.source_audit --generation pair-2026-09-25 \
  --identity 748:1:5 --limit 1
python -m sdilej_serialy.source_audit --generation pair-2026-09-25 \
  --limit 500 --runtime-minutes 300 --persist-git-state
```

`source-quality-audit` resumes manually or every six hours when its repository
enable flag is true. It prioritizes entries from the active IMDb-ranked queue,
then the remaining saved low-resolution sources. It never starts an upload.

## Avatar S01E05 check, September 25

Source 32410502 is a 1920×1080 live-action episode lasting 3,101 seconds. A frame
at 90 seconds confirms live-action footage. It is not the animated "The King of
Omashu" episode, whose selected Czech source 33102536 lasts 1,341 seconds at
900×720. A complete authenticated re-discovery retained 33102536 as the best
verified Czech match; the remake was correctly rejected by runtime compatibility.
This is not evidence that every saved low-resolution selection is already optimal.

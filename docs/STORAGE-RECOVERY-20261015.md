# Storage forecast stop and recovery

Run `scan-20261015-215627-bb0a2529`: hBN on 90 nm SiO2, one scattered chip.
The run closed normally with `storage_budget_reached` and
`next_chip_exceeds_free_storage`, after about 9.5 minutes. There was no camera
exception or failed disk write. Reference preparation succeeded; 25 edge probes
produced a closed, approximately 23.8 by 18.7 mm rectangle and a 4,690-tile plan.
No raster tiles were acquired.

## Cause

Campaign planning required 227,992,520,192 free bytes including a 10 GiB reserve,
but measured 128,953,200,640 bytes free. Its estimate treated each tile as 1.5
times the uncompressed camera array. A second independent whole-plan requirement
in `full_chip.py` would have blocked acquisition even after the first was fixed.

All 25 saved TIFFs already use lossless LZW compression (TIFF tag 259 = 5).
Median file size is 20,206,964 bytes, maximum 20,540,982 bytes, versus an
uncompressed array of 30,395,904 bytes. Extrapolating those sizes to 4,690 tiles
gives 88.26–89.72 GiB of raw TIFFs before previews, metadata, autofocus and
derived files. This is an observed projection, not a completion guarantee.
Changing scientific image format or deleting old runs was unnecessary.

## Changes

- `chip_campaign.estimate_campaign_storage` retains its conservative
  `sufficient` forecast and records separate `can_start`, admission reason,
  and next-capture requirements for each volume. Whole-chip size forecasts
  no longer terminate a scan with room for further images.
- Full-chip acquisition likewise treats its whole-plan estimate as advisory.
  Both normal and overlapped processing capture paths check actual output and
  vendor space before capture. They preserve the larger of the existing
  10 GiB reserve and an explicitly configured reserve, plus at least 512 MiB
  for the next capture. ZEN also checks the Drive upload cache before each job.
  These checks do not delete data or claim protection from unrelated writers.
- A real shortage stops before the next capture and retains committed tiles.
  Explicit resume verifies existing evidence and skips completed tiles.
- A saved storage/time-deferred boundary candidate is eligible for a targeted
  continuation before the known-chip recovery gate. This does not authorize
  blind new-chip searching or an automatic hardware restart.
- Focus geometry diagnostics now pass the actual rectangle plan to the same
  bounded-plane predictor used during raster acquisition. The previous 1,668
  geometric rejects become zero for the saved plan; the synthetic equal-Z
  calculation is expressly not measured focus evidence.
- The status API explains historical forecast stops with required/free bytes
  instead of a generic incomplete-run message. Historical run status is intact.

## Evidence and verification

`research/chip-edge-20261015/replay_storage_215627.py` reconstructs admission at
the original free-space reading, reads TIFF sizes/compression, and writes
`storage-replay-215627.json`. Result: `can_start=true`, `sufficient=false`.
`focus-plan-215627.json` records the reconstructed plan. All four saved edge
images pass focus-ROI selection offline. There are no verified discovery-edge
focus measurements to reuse for this run; four anchor measurements are needed.

Regression coverage includes forecast shortfall with usable actual space,
separate vendor-volume shortage, explicit reserves, guards on both ordinary and
overlapped autofocus captures, restoration of adapter methods, interruption
after one committed tile followed by resume without recapture, and targeted
continuation of a known storage-deferred chip. Existing focus, ownership,
provenance, vendor retention, campaign continuation and storage-routing tests
are included in validation.

Combined result: **169 passed** across the capture-storage, full-chip,
checkpoint/resume/focus-seed, campaign admission/continuation/recovery,
vendor-retention/overlap, scan-service and storage-routing test modules.

No scientific files, old run status, STOP records or holds were rewritten.
No acquisition was restarted during this repair. Full live acquisition under
the updated policy remains unverified; a future run can still stop for a real
resource shortage, hardware error, operator STOP or unresolved focus.

## Authorized continuation

After the user instructed "ok make the run start", continuation
`scan-20261015-223250-f43d96e7` was launched through ScanService using the saved
hBN settings and same-placement handoff contract. The handoff entry point now
accepts a closed storage-stopped owner and retains a deferred candidate's retry
state. Its 31 handoff tests pass; the combined handoff/continuation check before
the additional regression passed 51 tests. Original evidence/STOP records and
original absolute deadline are retained. Launch receipt:
`research/chip-edge-20261015/storage-continuation-start.json`.

That first continuation closed before raster acquisition on a transient Drive
`FileNotFoundError` while hashing copied `probe_000018.tiff`; the file was present
on inspection and had passed copy verification. Sparse resume now uses the
existing bounded, cancellation-aware provider-read retry for evidence hashes,
labels and frame metadata. Hash mismatch remains a hard failure. A regression
injecting one missing-file response passes without recapture (8 rectangle
material-discovery tests passed). The retry starts again from the immutable
215627 donor; its distinct receipts use `storage-continuation-retry-*`.

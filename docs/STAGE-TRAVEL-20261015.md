# Stage-bound discovery failure, October 15

The hBN run `scan-20261015-142425-51cf3378` failed during autonomous reference
discovery, before any chip scan. Starting off-chip is supported and does not
require a manual substrate reference.

The saved motion log shows a Y request to -1681.667 µm from Y=0, rejected by
ZEN with `Position could not be reached`. The preceding attempt moved from
Y=378.358 toward -2483.071 and stopped at exactly zero. This was a travel-envelope
error, not a chip classifier failure. The worker stopped and closed hardware.

The read-only leased diagnostic `research/probe_stage_limits_20261015.py` saved
`research/stage-limits-live-20261015.json`. Both axes reported calibration mode
`OnLowerLimit` and coordinate direction `Positive`, but nominal ranges remained
X ±85000 µm and Y ±65000 µm. Enabled software limits also included negative
coordinates, so clipping to software limits alone would repeat the failure.
The installed vendor MTBApi.xml documents OnLowerLimit as placing the coordinate
origin at the lower hardware limit. The raw Y hardware-switch flag said Upper at
zero; retain it as diagnostic evidence, not a numeric upper bound.

The corrected envelope intersects nominal bounds, enabled software limits, and
the verified calibration-origin constraint. It never expands travel or changes
controller limits, reference, or calibration. Search centres are inset for the
30 µm backlash approach and 1 µm endpoint margin. If the current position lies
in that margin, the worker first enters the nearest interior XY while retaining
the current Z, verifies arrival, and saves `search-entry.json` before bootstrap.

The user explicitly requested repair and initiation from the current off-chip
position. Use a fresh plan with the saved hBN settings; do not restart the failed
worker or reuse its authorization. Old raw frames, hold and STOP records remain.

Deployment: corrected live read-only validation saved in
`research/stage-limits-corrected-live-20261015.json`; effective ranges were
X [0, 85000], Y [0, 65000] µm. Focused MTB/worker/motion and bootstrap/scattered
tests passed. Fresh run `scan-20261015-143112-e42aa0a5` launched through the normal
authenticated scan API with the prior hBN settings. Its search-entry receipt
confirms Y=0 to Y=31 µm, with unchanged Z=19074.218 µm. The new search rectangle
is X [31, 84999], Y [31, 50000] µm. Consult its live scan status for later progress.

## Later hBN run: boundary classification and integration failure

User subsequently ran `scan-20261015-145135-db809db3` (`hBN 1`); the earlier
143112 run is recorded stopped. The later run finished `search_sampling_complete`
with no exception, zero chips and zero raster tiles. Bootstrap accepted its
reference and measured focus at Z=19079.843 µm. No recurrence of negative-Y
travel failure was recorded in its final status.

`batch.json` records 131 seed records, 122 uncertain records and nine unresolved
boundary attempts, each with 24–26 probes. Every discovery ended with
`Fewer than three coherent edge directions`, zero accepted coherent edge images,
and only substrate/unknown classifications. Example candidate001/probe_000024 is
visibly dark holder, yet only 0.64% of pixels were labelled holder. Raw subsampled
median RGB [100,123,60] against reference [413,772,486] gives ratios
[0.2421,0.1593,0.1235]. `chip_discovery.py` requires every RGB channel below 0.15
of reference for its dark-holder fallback, so this image cannot satisfy that rule.
This prevents useful substrate/holder bracketing despite successful initial
reference edge detection.

Ordinary fresh `scan_worker` calls `campaign_runner` without `rectangle_mode`,
whose default is false. All nine saved discoveries confirm rectangle_mode=false:
the improved rectangle recovery path was not active. They retained a fixed
9000 µm boundary radius, 1500 µm coarse steps and 32-probe budget. In candidate001,
north/west remained substrate at the 9 mm local boundary.

Campaign exhaustion unconditionally reports `search_sampling_complete`; its
detail correctly says `sampling_exhausted_with_unresolved_evidence`, but the
worker/UI summary omits the unmet one-chip target and unresolved boundary reason.
This is an unsuccessful acquisition requiring attention, not a completed scan.
Diagnosis was read-only; no new acquisition was launched during this inspection.

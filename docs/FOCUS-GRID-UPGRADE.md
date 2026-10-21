# Focus reuse and grid search — October 13, 2026

Production: `research/software-next-20261009`. Isolated development:
`research/software-focus-grid-20261013`. Read the current run pointer and status
before making any claim about acquisition. No production code is edited while
an acquisition worker owns the microscope.

## User intent and placement

Same unmoved October 13 placement: graphene chips at 20×, a roughly 4 × 4 grid,
approximately 10 mm pitch, no chip under 5 mm. The first chip's grid index is
unknown. Discover neighboring chips from measured evidence, refine the grid,
measure each outline and focus plane, then scan. Reuse successful edge autofocus
measurements for that chip instead of repeating them for a separate plane.

The user also asked to remove arbitrary algorithm limits that stall acquisition.
Controller travel limits, STOP, exclusive hardware ownership, data provenance,
storage availability and the original user deadline are operational constraints.
Algorithm geometry thresholds must be justified by the measured chip and image
evidence, rather than fixed millimetre limits unrelated to chip size.

## Diagnosed failures

1. Discovery retained only the last autofocus Z as a search-height hint. It did
   not pass registered edge ROI XYZ measurements into the scan focus plane.
   A crop's stage centre and its physical ROI centre differ; use the latter.
2. Grid fitting required two raster-validated enclosures. A rectangle supported
   by four observed sides was excluded, so the first chip produced no useful
   grid predictions. Search ordering also preferred proximity to any predicted
   site before travel distance, permitting unnecessary long jumps.
3. `scan-20261013-215137-c4f3127d` measured all four focus anchors successfully,
   then deferred chip001 at tile 0 because the focus hull had a fixed 1.5 mm
   extrapolation limit. With only that distance cutoff lifted in offline replay,
   all 999 planned tiles satisfy the unchanged 2 µm conditional uncertainty
   limit. Maximum predicted mean-plane sigma: 0.68173 µm. Maximum anchor
   residual: 0.10518 µm. Maximum hull extrapolation: 4.47019 mm. These are model
   predictions conditional on a planar chip, not independent focus measurements
   at every tile or a measurement of chip warp.

The worker was gracefully stopped with hardware closed after this failure.
Its four measured points, outline, plans and raw data are preserved. No raster
tiles had been acquired on chip001. The next invocation must resume these
prepared anchors, not redo reference discovery or focus acquisition.

## Implemented behavior

- `edge_focus_reuse.py` binds each successful discovery focus result to its
  confirmed fixed crop, image/profile/calibration, actual registered ROI XYZ,
  chip discovery and hashes. Future chip scans load these as measured support.
  Existing resumes retain their original seed provenance and newer checkpointed
  anchors. A replay of chip001's east/south evidence leaves only north/west to
  measure; four consistent observations provide a plane and a residual check.
- `full_chip.rectangle_plane_domain` uses the four measured rectangle sides and
  calibrated camera footprint to define where that chip's plane can guide the
  raster. It replaces the unrelated hull-distance cap for this geometry. Fit
  residual, conditional prediction uncertainty, actual controller ranges and
  membership of the anchors in this chip remain checked.
- `grid_search.py` accepts a four-side rectangle as an explicitly uncertain
  search anchor. From the first chip it retains all 16 possible grid slots.
  Chip-side orientation guides initial neighbor proposals; extra angular
  proposals cover uncertainty in the array orientation. Two or more observed
  centers refine pitch and rotation. Nearby grid neighbors and short travel
  precede far-away possible sites. Repeated separated misses reduce priority
  without deleting sites. Periodic baseline search prevents a bad prior from
  trapping acquisition. Predictions never become chip detections.
- Campaign counts distinguish observed rectangle chips from raster-validated
  enclosures. Overlapping estimates of the same chip do not spend multiple
  places in the requested 16-chip budget. A forecast that the next chip may
  exceed remaining time is diagnostic; the original actual deadline governs
  partial acquisition and checkpointing.
- Rectangle discovery allocates images from missing ray lengths, saved
  brackets, bisection resolution and the remaining permitted focus work.
  This replaces the fixed 32-geometry / 64-total / 600-second candidate caps
  and the four-extra-image handoff cap for this mode. The current rectangle
  focus policy permits one short quick-focus attempt, at most eight focus
  images plus a full-field recapture; earlier legacy discovery could spend
  much more on full autofocus. Saved attempts are not replenished on resume.
  The campaign passes its remaining actual user deadline. Standalone calls
  derive a finite time allowance from remaining work and observed throughput.
- A side only suppresses further search in that direction when its measured
  half-plane actually contains this chip's seed. This fixes a wrongly oriented
  edge suppressing reanalysis of candidate002's useful saved south-edge image.

## Validation and deployment

Saved-data replay is in
`research/scan-readiness-20261013/focus-grid-upgrade/saved-data-replay.json`.
It checks both real edge ROI focus points, all 999 first-chip tile predictions,
and 16 first-chip grid-slot hypotheses while retaining every pending search
sample. Grid ordering takes approximately 0.1 seconds on the saved search plan.
The combined relevant suite passed 221 tests with one optional replay skipped;
two additional work-budget tests passed with one optional replay skipped.
Separate real donor replay ran four tests successfully, including candidate002
with zero new captures/motion and 78 unchanged pinned evidence files. Candidate002
is 9.499 × 4.841 mm with 165.49 µm planning uncertainty; the 5 mm user prior is
approximate guidance, not a hard observed-size rejection.
`validation.json`, `candidate002-replay.json` and
`campaign-resume-preflight.json` retain these checks.
Deployment helper:
`research/scan-readiness-20261013/deploy_focus_grid.py`. Its default mode only
reports changed files; `--deploy` checks the exact closed acquisition owner and
unchanged source fingerprint before backing up and copying the tested delta.
It never starts or stops hardware. Use the existing immutable same-placement
handoff to retain the original deadline, reference and scientific evidence.

The tested delta was deployed only after donor215137 closed and its process
exited. `focus-grid-upgrade/deployment.json` pins all before/after hashes and
`production-before` contains original files. The new invocation's handoff
contract explicitly queues the diagnosed prepared-focus chip for retry while
retaining its four anchors and unchanged original seed provenance. It does not
replay unrelated failed chips blindly or clear old STOP records.

New invocation: `scan-20261013-221727-131d9ced`, started at 22:17 October13.
`handoff-plan.json` pins 445 donor files, four prepared anchors and the original
deadline. At22:21, `live-validation.json` records9/999 first-chip raster tiles,
all4 focus points unchanged and zero additional autofocus attempts. Every saved
tile uses `measured_rectangle_plane_prediction`; no active worker error.
Read actual status/manifest for subsequent progress.
After chip001, saved candidate reanalysis runs before remaining grid-guided
search. A chip's row/column remains ambiguous until sufficient neighbors are
observed; there is no assertion that the first chip is a corner.

# Graphene scan preparation and repository guide

## October 16: partial hBN scan analysis

Acquisition `scan-20261015-223549-98fae566` stopped after 1,780 committed tiles with
`ZEISS MoveTo: Position could not be reached.` The operator requested offline hBN
segmentation instead of further acquisition. Keep the microscope stopped.
See [HBN-SEGMENTATION-20261016.md](HBN-SEGMENTATION-20261016.md) for source identity,
model assumptions, pilot findings, hBN review selectors and missing sidecars.
Do not apply graphene empirical bands or graphene thickness labels to this run.
Current selection: hbn-image-full-v6, published progressively while segmentation runs.
Review runs opens All regions / All thicknesses and refreshes every 30 seconds.
First access builds a local field cache; later polls index only new commits.
Existing all-region caches are reused when applying the gallery area floor.
Committed regions are preliminary until boundary/halo review completes; they are
not promoted to candidates merely for being visible. The earlier pilot
hbn-image-pilot-v6 / hbn-halo-review-v7 remains preserved. Full offline
hbn-image-full-v6-controller is running; old morphology-only v3 is STOPPED.
Optical periodicity and nearby thick-stack ambiguity are explicit. Operator thin
examples remain retrievable but have no calibrated thickness assignment.

## October 16: progressive gallery and human flake labels

The gallery returns loaded fields while indexing the remaining commits in short
slices; the page displays indexing progress and continues loading automatically.
Source path/stat checks run in a bounded pool. Color plots use a separate reader
so building the all-region cloud cannot hold the gallery lock. Fetch failures
clear the spinner and retry; they no longer leave an indefinite loading screen.

Open a region in hBN review and click Thin hBN, Encapsulation hBN, Bulk, or Not a
flake. Undo clears the label while preserving history. Positive human labels
appear in Candidates immediately; negative labels exclude them. These labels
are visual judgements, not measured thickness or an automatic model retrain.
Graphene has Good flake / Bulk / Not a flake controls too.

Human feedback is stored in `<project>/annotations/flake-review/<run>/current.json`
with atomic audit/current publication, per-region revisions, and idempotent
click IDs. Labels bind to the exact run request, analysis plan, committed field
hash, source image hash and region ID. A different source generation does not
inherit the label; stale saves fail with a conflict. Full source region geometry
and measurements remain in the audit for later model/segmentation training.
Original images, masks, model pins and the current offline job are unchanged.
The hBN document records implementation and validation details.

## October 15: whole-chip storage forecasts no longer stop acquisition

Run `scan-20261015-215627-bb0a2529` passed reference preparation and measured a
closed rectangle in 25 probes, then stopped before raster acquisition because
a conservative whole-chip forecast required 212.3 GiB including reserve versus
120.1 GiB free. Both the campaign and full-chip forecast gates are now advisory;
actual next-capture output/vendor/cache space remains checked. A storage-deferred
known chip can be retried on explicit continuation instead of being blocked by
the no-new-search gate. The 1,668 predicted local-focus count was an outdated
geometry diagnostic; the rectangle-aware calculation reports zero geometric
rejections (actual focus must still be measured). Details and offline replay:
[STORAGE-RECOVERY-20261015.md](STORAGE-RECOVERY-20261015.md).

## October 15: reference exposure failure repaired

Run `scan-20261015-173045-69478f68` failed before chip discovery: autofocus
succeeded, but exposure adjustment discarded usable reference frames and returned
a later clipped capture. The helper now retains the best usable saved capture,
stops brightness oscillation after encountering clipping, and restores the exact
selected exposure and metadata context. Reference validation thresholds remain
unchanged. Saved-image replay accepts both previously discarded references.
Preparation fields now appear on the stage map with an explicit preparation
phase label. See [REFERENCE-EXPOSURE-20261015.md](REFERENCE-EXPOSURE-20261015.md).
The repaired viewer is loaded; the failed hBN run remains stopped. No new live
scan has yet validated the complete discovery/raster workflow.

## October 15: loose hBN chips, fresh rectangle discovery

Latest: operator run163303 stopped after a bulk-flake edge was mistaken for the
chip's east boundary. Subsequent repair separates measured stage from generic
colour mismatch, validates materials on both sides, samples around ambiguous
bracket images, remeasures size-prior conflicts, and pauses on known-chip focus/
planning failure instead of searching elsewhere. Real saved-edge replay rejects
the false edge and accepts the three true edges. The stopped run has zero raster
images; no new live scan has validated the repair. Details and regression tests
are in the loose-chip document below.

The user clarified that hBN chips are directly on the stage, with no separate
holder. Both recent hBN run requests retained hBN; the setup UI incorrectly
reverted to graphene after a terminal run. Fresh UI loads now retain the prior
material and show it explicitly in the run header. See
[LOOSE-CHIP-DISCOVERY-20261015.md](LOOSE-CHIP-DISCOVERY-20261015.md) for fresh-run
rectangle integration, measured stage-background calibration, saved hBN replay,
and truthful needs-attention outcomes. No run was initiated during this repair;
the operator intends to start a fresh run after deployment.

## October 15: off-chip hBN discovery travel repair

Negative-Y discovery requests failed at the actual Y=0 endpoint because nominal
MTB ranges were mistaken for calibrated reachable coordinates. See
[STAGE-TRAVEL-20261015.md](STAGE-TRAVEL-20261015.md) for the read-only controller
evidence, calibration/software-limit intersection, and initial entry into the
backlash-inset search area. The user authorized a fresh hBN run after repair;
check the current run pointer for its live status.

## October 15: disabled Start for hBN fixed

The scan form previously kept automatic analysis checked for hBN / 90 nm and
disabled Start while saying "Ready to start." Unsupported analysis profiles now
automatically select scan-only mode, with an explicit notice. Returning to
graphene / 90 nm makes analysis available but leaves it off until selected.
Start and its explanation use the same blocker list, including session/status
failures and invalid fields. Existing tabs can turn off "Analyze images after
scanning" under Advanced settings without losing their form; newly loaded tabs
get the fix. This change does not start acquisition or clear operator holds.
Regression check: `node tests/test_scan_console_gate.cjs` from the production
release directory; browser checked hBN selection without connecting hardware.

## October 15: flake revisits and provisional thickness

Graphene detail panels now link to `/revisit?run=...&observation=...` and load
optical thickness estimates on demand. See [REVISIT-20261015.md](REVISIT-20261015.md)
and [GRAPHENE-THICKNESS-20261015.md](GRAPHENE-THICKNESS-20261015.md).

Revisit is a fresh, explicit operator session, separate from the old scan holds.
Opening review, metadata, thickness or revisit pages never connects hardware.
Unchanged placement also requires an unchanged stage reference (no homing/reset).
Reloaded chips use two corresponding source/current points for rigid XY alignment;
one point requires explicit unchanged-orientation confirmation. Source corners
from the line fit are inferred, with about 235 µm allowance. Prefer clicking the
actual corner or landmark in a saved image. The UI sorts reference images by
distance from the selected corner. Manual positioning releases hardware ownership
for ZEN; recording a point reconnects and reads current XYZ. Moves preserve current
Z and require the original objective. Old focus planes are not reused after reload.

The thickness sidecar is a separate versioned derivative; images, masks and
candidate ranking remain unchanged. The assumed-linear forward model puts the
three operator-observed band means near 1, 2 and 3 equivalent layers; broader
oxide/illumination/camera-response scenarios widen these ranges. Model mismatch
is displayed explicitly, including for the strongest band mean. These are
provisional optical estimates, not measured or confirmed layer assignments.
All 233 candidate observations have saved estimates; other assessed regions can
be evaluated lazily. Read the thickness report for numerical residuals and sources.

Implementation tests use simulated hardware. No live relocation was performed:
there is no newly registered chip placement for this task. Prior scan holds remain.
The deployed UI passed browser checks with the real saved data; the revisit
controller remained idle and hardware-closed.160 focused tests passed. Evidence
is under `research/revisit-20261015/`. Refresh an old review tab to load the new
Thickness and Revisit controls.

## Current task — empirical graphene bands and capture timing

The user authorized live microscope speed experiments on October14, with no chips
on stage. Use a fresh scoped authorization and one hardware owner. Preserve old
STOP/hold files and do not resume acquisition plans. Restore tested optical
settings. The previous manual-takeover pause still applies to old scan campaigns.
The same request directs actual pixel re-segmentation of the saved graphene run
using operator-observed RGB bands, candidate promotion, literature-based
extrapolation and concise UI text. New scientific outputs must be versioned.

This study is complete. Hardware ownership closed, optical settings were restored,
and prior STOP/hold records are unchanged. The persistent capture prototype is
bench-only: individual raw frames verified, but the enclosing ZEN job ends as
ABORTED. It is not a production backend. Local-versus-Drive save timing is recorded
in the acquisition-speed document; production storage was not redirected.

## New operator workflow — October 14

Use the single desktop **FlakeFinder** shortcut or `Open FlakeFinder.cmd`.
Home offers New scan and Review runs; historical MoTe2 review moved to
`/library`. Read [OPERATOR-WORKFLOW-20261014.md](OPERATOR-WORKFLOW-20261014.md)
for the simplified setup, scattered/grid discovery, automatic offline processing
and qualification limits. This upgrade has not started a real scan. The manual
takeover hold remains intact. New acquisitions require a person's fresh Start.
Automatic analysis jobs can recover independently without reconnecting hardware.
Measured raster baseline and conservative output-writing improvements are in
[ACQUISITION-SPEED-20261014.md](ACQUISITION-SPEED-20261014.md).

## Current work: offline graphene segmentation, October 14

Open saved results using desktop **FlakeFinder** -> **Review runs**.
The `/review` library exposes the curated graphene
run and MoTe2 collection. The launcher reuses/starts only the local viewer.
Graphene cards load compact metadata and visible previews; opening a card or
enabling outlines loads geometry. See the segmentation document for the new
all-useful-region boundary pass and corrected geometric-size assessment.
The selected analysis is `operator-empirical-full-v1`, re-segmented using the
operator's observed RGB bands. All999 raw fields were processed:942 assessed,
57 unassessable, zero failures. All999 derived records are present; this also
regenerated the previously missing tile524 record. Candidate observations rose
from41 to233 before grouping/artifact display filters;115 newly promoted masks
overlap a previously uncertain region by at least50% of the new mask.

The new boundary pass checked53,513 regions:36,937 accepted,15,047 require review,
and1,529 remain unresolved.94 observations carry likely-artifact flags. Some
outlines remain fragmented, including new candidate tile269/region32; unresolved
refinements display the new semantic mask rather than an unaccepted contour.
Ranking uses observed color bands first, extrapolated matches second, then other
unknowns and the operator-negative population (`empirical_support_then_joint_v1`).
Area/boundary evidence orders each tier. No ranking score deletes observations.
Review preserves source observations under spatial groups. The plot shows the
fitted RGB trend and first-band cutoff, not an absolute layer calibration.
Final grouping is complete: 53,513 observations form 45,248 groups, including
7,481 groups with multiple observations. With the default 25 µm² minimum and
artifact hiding, the live GUI shows 224 candidates (previously 31) and 44,940
uncertain groups. Final API evidence is `empirical-after-gui-counts.json`;
candidate/uncertain requests took 2.22/1.04 seconds in this check, not a general
browser latency benchmark.

The earlier manual takeover paused scan acquisition. Local, production and shared
operator holds record that instruction. The scoped timing experiments described
at the top of this runbook are separately authorized; old scans remain paused.
Any future scan needs a new start instruction and fresh placement/frame checks.
The saved analysis source is `scan-20261013-221727-131d9ced/chip001` (999 committed
raw fields), nominal graphene on 90nm SiO2 at20x. Offline segmentation work and
evidence are under `research/graphene-segmentation-20261014/`.
Read [GRAPHENE-SEGMENTATION-20261014.md](GRAPHENE-SEGMENTATION-20261014.md) for
the full-chip color plot, reversible boundary/artifact refinements, spatial
grouping, literature findings, and full offline pass: 999 processed,
942 assessed, 57 unassessable, zero unexpected analysis failures. The normal
Flakefinder review is `/graphene?run=scan-20261013-221727-131d9ced`; the failed
restart links to this donor rather than mixing run identities. Status is in the shared project's
`analysis/graphene/scan-20261013-221727-131d9ced/chip001/operator-empirical-full-v1/`.
The sibling `-boundaries` and `-review` outputs hold refinement and grouping.
Comparison/equivalence evidence is in `empirical-full-*.json` under the local
research directory. See [GRAPHENE90-LITERATURE-20261014.md](GRAPHENE90-LITERATURE-20261014.md)
for why extrapolated color matches remain uncertain and no layer numbers are assigned.
The interface has concise labels, a two-analysis metadata cache, and an eight-image
preview cache. Switching analysis refreshes the image list and color plot.
The continuation history below is historical, not current acquisition authority.

Updated October 14, 2026, America/Los_Angeles. The stage was empty for the latest
capture experiments; old chip coordinates are historical. The same chips had
been moved after October 11, and a fresh placement was established October 13.
The recovery notes below describe that earlier placement and deadline. They do
not authorize motion or establish the current specimen layout.

## Historical interrupted-copy recovery

Read [SCAN-RESUME-20261014.md](SCAN-RESUME-20261014.md). Invocation
`scan-20261014-090615-88fb3166` failed during a source read in the bulk copy,
before acquiring another chip. Its original donor remains complete; the failed
destination contains an incomplete copy and must not be used as the science
source. The next repair uses pinned read-only completed-chip references, avoiding
the repeated 20 GiB copy. It also repairs transient status-read failures in the
memory supervisor. Evidence: `research/scan-resume-20261014`. Read current run
status and deployment records before claiming restart or acquisition.

The repair passed 145 combined tests and the full saved 999-field replay.
New invocation `scan-20261014-105506-f3cb07a9` launched at 10:55 October14,
retaining chip001 read-only at its original location and the original deadline.
Its request excludes chip001 from the copy inventory. Coverage replay now
reports completed fields and tolerates bounded transient filesystem reads.
It then exited before motion because the live nosepiece reports position 6
(empty), not position 2 (20x). ZEN is in Free Mode; API commands were read-only.
The user has been asked to seat 20x and confirm unchanged chip placement.
Do not claim acquisition resumed or bypass the optical check.
API Mode was restored at 11:00:46; ZEN confirmed control allowed=True. The
remaining block is physical objective engagement and placement confirmation.

## Completed-chip transition recovery — preceding invocation

Read [SCAN-RECOVERY-20261014.md](SCAN-RECOVERY-20261014.md) first. Invocation
`scan-20261013-221727-131d9ced` completed all 999 chip001 fields, then failed
at 00:48 October14 on a missing boundary-label sidecar during continuation.
All raw TIFF paths are present. Seventeen sidecars are missing; the disappearance
cause is unproven. Exact label reconstruction and independently archived vendor
registration replay all 999 fields to the exact saved coverage map.

The transition repair passed 138 combined regressions and was deployed after
confirming worker closure. It also fixes the skipped memory-supervisor launch
on rectangle handoffs and adds content-addressed exact-byte sidecar bundles for
new tiles. Preserve completed chip001, same placement, reference and original
deadline. Follow `research/scan-recovery-20261014/handoff-plan.json` and the
project's `scan-control/current.json` for the new invocation and live status.
Do not treat the historical progress snapshots below as current state.

New invocation `scan-20261014-090615-88fb3166` started at 09:09 October14.
Its first observed phase is `validating_handoff_files`, before hardware
connection. All 999 raw TIFF hashes match the original tile manifest. The new
worker retains the original deadline (21:20 October14) and completed chip001.
File verification/copy can take minutes; inspect `file_progress` and heartbeat
timestamps before interpreting this phase as stalled. Read live status for
subsequent acquisition, memory-supervisor launch and chip discovery progress.

## Focus/grid upgrade — preceding invocation

See [FOCUS-GRID-UPGRADE.md](FOCUS-GRID-UPGRADE.md). Invocation
`scan-20261013-215137-c4f3127d` was stopped with hardware closed for this upgrade.
Chip001 has four successful saved focus anchors and a 999-tile plan, but no
raster tiles: a fixed 1.5 mm focus-hull distance limit rejected the first tile.
All 999 predictions pass the unchanged uncertainty check when bounded by the
measured rectangle instead. Development is isolated in
`research/software-focus-grid-20261013`; 221 combined regressions and two
additional work-budget tests passed, plus separate real-data replays.
New invocation `scan-20261013-221727-131d9ced` was started at 22:17 October13.
At22:21,9/999 chip001 raster tiles were saved using the measured rectangle
plane, all4 anchors unchanged and zero additional autofocus attempts.
Read live status for subsequent progress. Preserve the same placement,
reference, original deadline and prepared focus anchors across the handoff.

## Rectangle upgrade — preceding invocation

See [RECTANGLE-DISCOVERY.md](RECTANGLE-DISCOVERY.md) for the implemented model,
real-image replay, preserved evidence and handoff. Current invocation is
`scan-20261013-215137-c4f3127d`, launched21:51 from paused212016. The first chip
has an offline recovered four-side outline and999-tile plan; read live status
before claiming focus-plane or raster completion. Source is frozen.

The historical invocation details below explain earlier startup fixes.

## October 13 autonomous grid run

Current invocation: `scan-20261013-212016-7f8d2797`, launched21:20 October13.
At21:20:36 reference-validation accepted=true (whole-field support0.88562,
central_patch_policy=informational_only); batch state running and worker phase
substrate_seed_probe. It is using the retained focused reference as requested.
No raster or fully measured chip has been established yet. Read live state.
Prior211822 failed beforemotion due solely to tuple/list calibration comparison;
canonical serialization comparison fixed, full real-data helper replay passed.
Uses `reference_reuse` hash-pinned contract from205538 at explicit user request.
`retained_reference.py` verifies donor raw/receipt/confirmed focus/independent
edge correspondence, sets exact exposure and returns within fresh bounds to
that session's measured reference XYZ. It repeats neither reference autofocus
nor reference capture. Metadata is preserved, proof labels it retained, and
prepare-only reuse is refused. New request/source hashes remain immutable.
55 worker/service tests pass, including JSON calibration roundtrip. Source is frozen.

Attempt211022 failed closed at21:13 after a2.5mm move and verified focus: its
new edge candidate lacked independent polarity. This is why reuse of205538
replaces further bootstrap retries; no Oct11 positions/reference are reused.
Read live status before claiming chip discovery or raster acquisition.
14 worker tests and34 backlog tests pass (separate suites). The exact previously
rejected raw reference passed offline replay with central-patch purity diagnostic.

Attempt210620 stopped before capture on the pending vendor queue: STOP of210432
returned a camera frame but cancellation preceded caller persistence. Retention
review `ddfd277a98cd481d9b056c02ddc1dd08` now preserves all8 original files and
pins exact closed-owner single-returned-capture, time/XYZ/optics evidence. It
explicitly does not assert a missing adapter-session receipt, raw acknowledgement
or scientific acceptance. Three older unknown jobs remain pending. Do not repeat
this reconciliation or delete originals. Review validates on each capture.

Latest change at user request: the central 40 µm reference-patch purity
classification is now **informational only**, not a startup gate. Flakes in the
centre do not invalidate the full-field substrate estimate. Full-field support,
independent bright-edge correspondence, optics and exposure checks remain.
Do not restore the arbitrary 90% central purity requirement or require manual
positioning. The user explicitly asked to use the focused reference anyway.

Attempt `scan-20261013-205538-8bc94cc9` confirmed focus at Z15624.453 µm and
passed independent bright-reference correspondence, then stopped only on the
central patch heuristic (82.2% versus 90%; whole field 88.6%). The patch-search
repair passed saved-image replay, but user rejected requiring patch purity.
Attempt `scan-20261013-210432-d3fc65a2` was gracefully stopped with hardware
closed to apply this requested policy change. A new immutable request follows;
read `scan-control/current.json` and its scan-status.json for authoritative state.

Earlier attempt `scan-20261013-204741-cfb93379` confirmed edge focus at
Z15624.219 µm (23 samples), then failed post-focus edge proposal on texture.
Proposal repair now carries the fixed ROI and uses bounded smoothed proposals,
while acceptance still uses independent raw RGB edge evidence. These Z values
are observations, never targets for a new placement.

First autonomous attempt `scan-20261013-203955-009f2158` passed live XY limits,
coordinate agreement and 20× checks, then failed before capture/motion because
four historical vendor exports are unacknowledged. Hardware closed. The older
backlog diagnosis below is historical; this fresh failure is established by
this run. Recovery: exact completed capture `2ac81d48556c476393756eb2cc8fe6a9`
was tied to a saved focus crop and its closed October 11 invocation. New
`make_crop_review` keeps all eight vendor files and pins source/inventory
hashes; its retained review frees one slot without claiming a full-frame raw
duplicate. The other three unknown/canceled jobs remain counted. Do not raise
the cap, delete originals or fake raw acknowledgements.


Current user priors: sixteen chips in a 4 × 4 arrangement, approximately 10 mm
spacing, no chip under 5 mm. Chips moved since October 11; current XY is an
arbitrary point on the grid. User authorized finding all chips, their edges,
per-chip focus planes and scientific rasters. Do not ask for manual first-chip
positioning or reuse historical coordinates.

New fresh-start path: `autonomous_reference.py` searches from current XYZ,
selects a reference-free chip/holder edge, performs independently confirmed
edge autofocus, moves to its bright interior and validates a fresh substrate
reference against raw bright/dark edge controls. Every capture is bounded and
saved with a vendor receipt. Uniform fields cannot certify themselves as chip
substrate. Severe defocus or exhausted bootstrap limits fail explicitly.
Stationary **Check microscope & image** remains stationary and is not this test.

Select automatic reference and the 4 × 4 grid prior; set minimum exposed chip
diameter to 5 mm, grid spacing to 10 mm. Search dimensions remain a requested
travel envelope around the fresh readback, intersected with live controller XY
limits after confirming ZEN/MTB coordinate agreement. A random point is not the
grid centre; a 40 mm rectangle around it is insufficient. Grid predictions only
reorder the existing lattice. `grid_search.py` fits translation, rotation and
spacing from measured enclosures, preserving ambiguity and unknown coverage.

The current campaign scans each chip after its edge and focus measurements,
then searches for the next. It does not inventory/focus all sixteen before the
first raster. This starts collecting data sooner. Expected sixteen and measured
chip counts remain separate. Sparse outlines are convex straight-edge estimates;
raster boundary checks can flag missed extent, but automatic boundary expansion
is still absent. Never describe `complete_with_boundary_limits` or finished
sampling as proof all physical chips were fully scanned.

User now explicitly authorized focused implementation/review agents. Use bounded
agent tasks, not periodic spawning or minute-by-minute model supervision.

## Start a fresh run

### October 13 startup block: XY hardware unavailable

**Resolved after physical power cycle:** user confirmed microscope power cycle.
ZEN reopened and Free Mode initialized hardware around 20:23. Fresh MTB logs
no longer show missing XY devices. The calibration dialog was observed running
XY then focus calibration and subsequently closed; Stage controls became
enabled, displaying X50731.02/Y25273.85 um and Z15676.41 um with 20x engaged.
These values are diagnostic observations, not reusable targets. API Mode was
verified ready at 20:24:53. No new scan worker was launched: the live image
still showed a strongly defocused chip/backing boundary. ZEN was returned to
Free Mode temporarily; autonomous reference startup now replaces the earlier
manual positioning request. API Mode was re-enabled at 20:36 on October 13.

**Correction after the second ZEN restart:** calibration-only advice did not
resolve this fault. ZENService.log.xml at 20:14:33 reports MTBStage,
MTBStageAxisX and MTBStageAxisY unavailable, explicitly requesting power/cabling
checks. MTB 3.11.9.0 logs say both stage devices failed to respond with version
information. ZEN's UI nevertheless reports initialization completed successfully.
Do not interpret that aggregate status or the API's uncalibrated error as proof
that XY hardware is connected. Evidence is saved in
`research/scan-readiness-20261013/stage-startup-diagnosis.json`.

Active MTB version is 3.11.9.0, configuration New Configuration1, Axioscope 7
motorized 80x60 stage, CAN nodes 38/39 over stand USB. Do not change this
configuration to bypass hardware errors. The manufacturer diagram (Axioscope
5/7 manual Rev.2, p70) shows a short cable joining the underside of the stage
(callout 8) to the top of the base near the round illumination opening
(callout 7). A separate stage-controller box should not be assumed. Diagram
saved as `research/scan-readiness-20261013/stage-cable-location.png`. Physical
connection check is pending; no remote motion or hardware restart was issued.

Follow-up: user confirmed the cable is connected. ZENcore and ZENService were
closed cleanly after exiting Free Mode. Both absent on process/window checks.
MTB and CZCanSrv processes predate the app restarts (started October 8).
An attempted Restart-Service for MTBService_3.11.9.0 failed because Windows
denied service access; no service restart occurred. Operator was asked to cycle
the microscope's main left-side power switch, leaving ZEN closed. Await that
physical restart, then reopen ZEN and inspect fresh XY initialization results
before any qualification or acquisition. Do not claim a controller fault is
proven: missing axis responses are established; their physical cause is not.

After the user restarted ZEN and selected 20x, stationary qualification
`scan-20261013-200711-738e91b2` failed at the first GetPosition call with
`FAILED_PRECONDITION: Stage is not calibrated.` Hardware closed; no reference
capture or scan motion occurred. The current pointer now names that failed
qualification, not the October 11 continuation described below.

ZEN Free Mode showed the whole XY Stage panel disabled, including Calibrate.
The verified startup setting is **Home > Maintenance > General Options >
Startup/Shutdown > Request Stage/Focus Calibration on Startup**. It was already
checked; no setting was changed. The installed ZEN core 3.12 Software Manual,
page 818, says this enables a stage/focus calibration dialog at software startup.
Once XY communication is restored, follow that dialog with the operator checking physical
clearance for reference travel. Do not bypass disabled controls or emulate
calibration by setting zero. If no dialog appears, inspect hardware/system
messages rather than repeatedly launching scan workers. After referencing,
restore 20x, return to API Mode, and create a new immutable request with
autonomous reference enabled. The failed request must not be reused.

Manual: `C:\Program Files\Carl Zeiss\ZEN core\Manuals\ZEN core 3.12 - Software Manual.pdf`.

### Normal scan workflow

1. Double-click `Launch Microscope Console.cmd`, or open
   [the scan console](http://127.0.0.1:8770/scan). The launcher reuses the local
   viewer if present, otherwise starts the production viewer without opening a
   hardware connection. It does not launch acquisition.
2. Enter a new sample name and the width/height of the current placement. The
   search rectangle is centered on the position recorded at Start. Fresh setup
   leaves these fields blank, resets duration to two hours and enables fresh
   exposure adjustment. Material, oxide and other optical preferences remain
   visible for review. “Use setup”/“Restore last saved setup” are explicit
   alternatives and always clear readiness and hold authorization.
3. In ZEN, use normal API Mode. Engage 20× manually, with travel clearance for
   the selected area and Z policy. Leave automatic reference enabled to locate
   and focus the initial chip without manual positioning. With that option off,
   the legacy path requires a focused bare-substrate starting field. The previous context was graphene on 90 nm SiO₂,
   sixteen chips; verify the displayed setup against today's placement.
4. Confirm readiness and the displayed run-only hold release when the microscope
   is actually available. **Check, save & start scan** now performs setup checks,
   saves a new immutable draft when needed, then requests one worker. Separate
   Check setup and Check & save draft remain available. These two actions do not
   access hardware. **Check microscope & image** applies optics and captures a
   stationary reference; it is a hardware action, not a focus validation.
5. Judge progress by accepted outlines, verified focus anchors and committed
   raster images. Search probes and a living process are not completed coverage.
   Stop remains cooperative. Do not restart stopped work by deleting STOP.

Do not resume the October 11 session: the chips moved and that session's
deadline expired. Do not run dated `start_*20261010.py` scripts, old one-off
recovery scripts, or import historical XY into the new placement. A 100× revisit
is a later manual objective change with measured calibration and fresh focus.

## Where the current system lives

| Area | Location and purpose |
| --- | --- |
| Production source | `research/software-next-20261009/flakefinder/` |
| Production tests and scripts | `research/software-next-20261009/tests/` and `scripts/` |
| Older implementation | Root `flakefinder/`, root `tests/`; historical console/simulator work |
| Interpreter | Root `.venv/Scripts/python.exe` |
| Output policy | `configs/storage.json`; shared Drive required, no local scientific-output fallback |
| Scientific project | `G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy` |
| Current invocation | `<project>/scan-control/current.json`, then `<project>/data/<id>/scan-status.json` |
| Current UI | `flakefinder/scan_console.html`, `scan_service.py`, `suite_server.py` in production |
| Acquisition entry | `scan_worker.py`; creates new frame/reference and calls `chip_campaign.py` |
| Edges and geometry | `sparse_acquisition.py`, `sparse_edges.py`, `photometric_edges.py` |
| Focus and coverage | `focus_recovery.py`, `autofocus.py`, `focus_surface.py`, `full_chip.py` |
| Storage and resource safeguards | `vendor_lifecycle.py`, `vendor_backlog.py`, `memory_capacity.py` |
| This preparation | `research/scan-readiness-20261013/`: source backups and offline replay |
| Historical detailed diagnoses | `<project>/research/session-20261011-handoff/geometry-focus/` and `open-issues.json` |

The root README contains commissioning and older MoTe₂ history. Its earlier
tray coordinates and 8766 viewer instructions are historical. Most repository
files are untracked, so Git history alone does not describe the working state.

## Verified previous stopping point

At the initial repository review, the pointer named `scan-20261011-162824-1400155a`, a special storage
continuation invocation. Its images and campaign live in
`scan-20261011-155805-5c2c4a02`. Status is **stopped**, hardware closed, worker
absent. Only the viewer was running at inspection; it was restarted with the
updated service. The original STOP and acquisition records remain unchanged.

Candidate007 supplied one accepted estimated outline. Chip001 had an 890-tile
plan but **zero committed raster tiles and zero completed chips**. The STOP
record identifies a three-planned/four-required anchor mismatch. This supersedes
the older handoff's claim that there were no accepted outlines. The old
coordination record still said running and must not be trusted as live status.

The service previously crashed with `KeyError: settings` because the special
continuation request did not use the standard request schema. Its display now
loads settings and counts from the hash-verified campaign donor. The normal
start path remains strict; this display support cannot start the old request.

## Sticking points and October 13 changes

| Issue | Evidence and resolution |
| --- | --- |
| Autofocus midpoint stall | Candidate005 had a real rising response and a near-tied plateau, but refinement requested an already sampled midpoint and stopped before confirmation. Refinement now selects a new unsampled interval inside the same significant bracket. The 6 µm bracket gate, fixed metric/ROI, independent confirmation, physical limits and operation budgets remain in force. |
| Three anchors for a four-point plane | Anchor selection discarded interior observations by keeping only convex-hull vertices. It now selects from all measured support points and requires the requested count, at least four, before acquisition. The actual saved case still has insufficiently separated edge anchors, so the existing substrate-support fallback now selects four points instead. No synthetic corner is treated as a measured edge. |
| Missing east boundary | Candidate006's transition was near the right border; sideways probes did not center it. A new bounded normal-centering proposal uses the largest connected substrate and holder regions, rejecting overlap and opposite polarity. One new field is proposed within the existing substrate/holder bracket before lateral probes. Every fresh field must pass unchanged raw-edge/focus gates and shared budgets. Saved-image replay proposed X2341.467 from X2173.429 at unchanged Y; these are historical diagnostic values, not today's targets. |
| Inverted reference labels | A defocused mixed chip/backing reference previously inverted the material labels. Later October 11 corrections and two fresh corresponding references were accepted. For moved chips use a new clean reference; the old correction coordinates are invalid. |
| Vendor-output backlog | The early four-unacknowledged-job failure was repaired later October 11 with durable observation/raw receipts. Do not revive it as the current failure, raise its cap, or acknowledge/delete unknown output. |
| Storage forecast | An earlier 2.35 TB forecast was corrected to about 54 GB under runtime limits and passed the last continuation. This was not evidence of raster completion. Current Drive/cache reserve checks pass; remote sync remains unverified. |
| Slow/confusing startup | Root launcher now opens production browser UI, and Start combines check/save/launch. Previous run evidence is explicitly labeled; fresh setup does not inherit old placement dimensions or name. Recent-run setup restoration now receives the settings it previously lacked. |

## Remaining live checks and analysis limitation

The new run must verify clean-reference polarity, one independently confirmed
focus result, a closed accepted outline, at least four supported focus anchors,
and the first manifest-committed raster tile. A software test does not establish
those physical outcomes. Keep source frozen while its worker runs.

The optional `scripts/watch_graphene_scan.py` still has obsolete 4 GiB/2.5 GiB
physical-free-RAM thresholds. It is not launched by this preparation. Acquisition
uses commit/address-capacity admission and can page normally. Before enabling
concurrent analysis, reconcile that watcher with measured incremental allocation
capacity, or analyze committed tiles after acquisition. Do not silently remove
the thresholds without an allocation estimate. Analysis labels remain optical
proposals, not confirmed graphene or layer counts.

Focused implementation agents were used after explicit user authorization. No
recurring model monitors or automatic retries were created. Use cheap
saved-state/process checks during a run; request model diagnosis when useful
progress stops, a real error occurs, or user input is needed.

## Validation and rollback

109 targeted tests passed across focus recovery, sparse discovery, edge focus,
full-chip acquisition, focus seeds, scan service and worker. The replay in
`research/scan-readiness-20261013/offline-replay.json` records source hashes,
four-anchor selection and the historical edge-centering proposal. The real
dashboard loads without browser errors and shows truthful zero-raster counts.

Run tests from the production directory, using the root interpreter:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/test_focus_recovery.py tests/test_sparse_acquisition.py tests/test_sparse_edge_focus.py tests/test_edge_focus_anchors.py tests/test_full_chip.py tests/test_full_chip_focus_seeds.py tests/test_scan_service.py tests/test_scan_worker.py -q
```

Five pre-edit production files are preserved in
`research/scan-readiness-20261013/before/`. Restore individual files only after
confirming no active worker, then restart the viewer and create a new draft;
source-bound drafts must never be started after their software changes.

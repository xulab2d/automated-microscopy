# Commissioning and validation â€” 2026-10-08

## Detection result

The initial nine-field 10Ã— analysis failed visual review: the user rejected its 79 proposed outlines as tape, debris, noise and useless small regions. This failure is recorded with the preserved dataset. Whole-field registration of three revisits does not validate the outlined objects. No monolayer has been established in this batch.

A visually located elongated crystal in original tile 8 provides a real positive control. Its ROI-assisted GrabCut mask covers1599.6ÂµmÂ², at stageX48275.093/Y30795.832Âµm. The ROI and foreground seed are recorded. This is a manually located positive control with an algorithm-derived outline, not a successful unseeded search. Its appearance is thick; material and thickness remain unconfirmed. See data/reference-crystal-10x.

The MaskTerial synthetic checkpoint matched 7/8 useful annotated public-control regions, with seven unmatched predictions, but returned zero confident proposals on all 20 live 20× fields. This is not a validated MoTe2 detector. Its memory-safe implementation preserves control masks exactly. The empirical contrast/GrabCut mode keeps physical-size-qualified objects and records morphology evidence without hard vetoes; it produced 52 proposals on those old fields, mostly residue and bulk on visual review. No thin crystal was confirmed. New focused multi-chip acquisition is required. Synthetic software tests cannot establish real detection accuracy. See RESEARCH.md for method provenance and limitations.

## Instrument measurements

The actual nosepiece is encoded/coded and manual. Registered positions are1:10Ã—,2:20Ã—,3:50Ã—,4:100Ã—. Position0 means between stops or unavailable; it is rejected by the20Ã— acquisition profile. Reflected illumination is an LED despite the legacy MTBRLHalogenLamp API identifier. The camera is an Axiocam305 color,2464Ã—2056,12-bit output stored in16-bit RGB TIFFs.

- Earlier10Ã— bidirectional stage calibration: maximum fit residual0.458Âµm, image return error0.206Âµm. The nine-field2.1Ã—1.8mm raster returned to its first image within0.132Âµm. All nine raw images and positions are distinct.
- After the operator seated20Ã—, encoded position2 and nominal0.1725Âµm/pixel were read correctly. Fresh20Ã— calibration: maximum fit residual0.444Âµm, image return error0.092Âµm. Evidence:data/20x-commissioning/calibration.
- The20Ã— autofocus stack resolved a peak nearZ15612.2Âµm atX48527.631/Y30237.225. At a new field the first local sweep reached its lower boundary; a broader search inside the configured envelope resolvedZ15605.156Âµm. The failed sweep and recovery are preserved.
- A new20-field20Ã— acquisition is in progress under data/20x-search-01. The physical15-chip tray has not been mapped or completely scanned.

## Fixed imaging settings

Camera-only quantitative job registered inZEN: ff_exposure_ms(double), ff_white_balance(string). Auto exposure, automatic white balance andHDR are disabled. Typed resource readback and stationary images confirm the settings change the camera output.

The vendor5500K preset left red weak and blue dominant on this substrate. A fixed acquisition channel balance,0.25007564,1,2.61244442, was measured and selected for this scan. It is not a neutral-reference true-color calibration. Exposure is266.602ms, reflected LED93.444%,ADC white level4095. Each frame records these settings and encoded objective. Single-frame medianRGB was2281/2596/1994; repeat noise approximately3.95%/1.70%/1.95% per pixel. Bright objects may clip even while substrate is valid; clipped colors cannot support thickness classification. No further optimization is being run during the current search.

## Software checks

The combined suite passed 102 tests after learned/empirical analysis and batch/viewer additions; the latest initial-focus-recovery and batch cleanup changes passed an additional targeted 18-test run. Tests cover motion/cancellation, units and channel order, raw provenance, acquisition profiles, manual objective handoff, autofocus, stage calibration, scan resume, source-field registration, reviewer annotations/security, seeded synthetic masks, and temporal averaging. Averaging is implemented and tested offline but is not enabled for this search.

The revised browser reviewer starts with raw source images and optional overlays, a100ÂµmÂ² usefulness filter, parent/terrace overlays, independent pixel/stage annotations, and explicit validation status. Browser-rendered interaction QA remains unverified. The source data and all rejected baseline results are retained.


## Batch interruption and repair

The first two chip interiors completed (40 preserved TIFF fields). Initial focus recovery on R2C3 then stopped before a command because fixed Z waypoints, combined with settled encoder offsets, would have exceeded the unchanged 10 micrometre step limit. The last readback was 15534.531; the next planned step would have been about 10.005 micrometres. No out-of-limit move was sent.

Autofocus, focus-surface moves and revisits now share a bounded-Z helper that replans from each actual readback and reserves 0.25 micrometres below that limit. Quantized-encoder, cancellation, stalled-motion, autofocus, resume and integration checks passed (37 tests). The batch resumed from the same fingerprint and retained all 40 saved fields. Interrupted analysis is queued for retry; successfully completed results remain intact.

## Current checkpoint: 2026-10-08 evening

The operator is using the microscope manually. Automated acquisition is stopped; no automatic restart is scheduled. Further work is offline until the operator hands the microscope back. The interior batch has 80 preserved fields across R2C1/R2C2/R2C3/R1C2; these are centred patches, not complete chip scans.

The later Z interruption was a precision mismatch. Active MTB reports a 0.156 micrometre step and 0.234 micrometre maximum deviation. The adapter reads these values and uses 0.235 including readback rounding, replacing 0.12. Stationarity and the travel envelope remain checked. A live diagnostic accepted the reproducible 0.1565 micrometre no-op, a one-micrometre displacement and its return. Evidence: data/chip-batch-01/focus-motion-spec.json and focus-precision-validation.json. Unchanged XY axes no longer perform backlash round trips; optical dwell occurs once after the final axis. The boundary run median interval was approximately 3.9 seconds/image, with 1.8–2.0 seconds in its camera job. This excludes full-scan autofocus overhead.

The R2C3 boundary pass was stopped at 282 frames / 1, 141 seconds.49 initial fields established four directional brackets; 233 additional fields came from neighbour expansion, including 113 fields over 90% substrate. A1.5% minority non-substrate threshold mistook interior flakes/debris for boundary evidence. Requiring a continuously observed holder collar amplified that design error.

The replacement defaults to coarse cardinal searches and bracket bisection, stopping at coherent substrate/holder edges. It has a hard 32-frame budget and no neighbour frontier. The old dense method is an explicit option. A targeted synthetic rectangle used 17 frames and resumed without new frames; this is software evidence, not a hardware performance claim. Offline processing of all 282 saved maps took 0.96 seconds and retained 18 coherent edge fields. Its estimated convex outline uses a 75–77 micrometre planning margin; corners remain inferred. The full-chip plan contains 403 fields and has NOT run. Evidence: data/boundary-efficiency-audit/estimated-outline.json, full-chip-plan.json and boundary-geometry.png.

Full-chip planning accepts an explicit estimated outline. It checks extent using the scientific raster images, without another dense boundary pass. Acquisition of the planned polygon and validation of actual chip extent are separate results. Focus anchors require measured substrate support; interpolation stays inside their hull. Nearby measured height at an unsupported edge is explicitly focus-limited. The default is four spatially separated anchors, replacing eight partly redundant anchors.

The R2C1 bulk-colour review has 504 per-field source observations, separate from five unique catalog objects above 100 square micrometres. This user-identified bulk population is not a universal flake class or layer calibration. A compact visual audit found a cyan platelet near pixel(1383, 1812), tile 0, with contrast approximately(+0.361,-0.616,-0.354). It lies outside the both-negative red/green box. More fundamentally, the original GrabCut routine correctly segmented it but retained only the disconnected component containing one strongest seed, located on a neighbouring white flake. It deleted the cyan platelet BEFORE size filtering. RGB mixtures also split this physical object into several colour domains, so those domains cannot be equated with whole flakes. The compact examples identify mechanisms, not exhaustive recall or material identity. No monolayer is confirmed. Evidence: data/manual-visual-audit-01/annotated-contact-sheet.png and benchmark.json.

The focus-policy cost audit subsequently found that hull-only interpolation would force autofocus near the raster border, potentially adding thousands of frames. The replacement uses all verified anchors in a direct plane fit, rejects any residual above 1 micrometre, and permits bounded extrapolation to the smaller of 1500 micrometres or 25% of the measured anchor diameter. Conditional prediction sigma includes a 0.5 micrometre measurement floor and must be at most 2 micrometres; normal Z limits still apply. Plane-derived fields explicitly do not claim local autofocus verification. The saved 403-field geometry is 245 interpolated +158 bounded-extrapolated positions. If measured flatness and anchor autofocus pass, the budget is 500 camera frames (96 anchor images, 1 profile check, 403 science images), replacing the per-edge autofocus policy. This is a calculated budget, not observed hardware performance. See data/boundary-efficiency-audit/bounded-plane-cost.json.

The deterministic shared component repair completed R2C1 reanalysis offline:3688 per-field observations, 21 size-qualified catalog proposals. The cyan control has 0.892 mask IoU against the independent visual polygon. Dust/haze merging and touching-instance separation remain unresolved; the four colour-only thin-screen matches were visually rejected as dust/haze. No useful thin-flake discovery is claimed. The updated gallery is data/chip-batch-01/R2C1/analysis-component-repair/review.html; original results remain available.

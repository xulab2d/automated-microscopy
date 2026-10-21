# Reference exposure failure, October 15

Run: `scan-20261015-173045-69478f68`, hBN on 90 nm SiO2, one scattered chip.
Failure: `Reference is clipped or too dark; adjust fixed exposure/light and create a new run.`
Hardware closed normally; chip discovery and raster acquisition had not started.

## Cause and saved evidence

Autofocus resolved a peak near Z 19077.109 um. The stationary exposure helper
then tried to reach a preferred 99th-percentile intensity, returning its last
capture regardless of whether earlier captures were usable. Bright features
clipped before that ideal target could be reached. Candidate 2 at 101.992 ms
and candidate 3 at 130.614 ms both pass unchanged reference validation, with
96.61% and 96.33% substrate support. Their subsequent exposures at 200.945 and
191.725 ms clipped and were rejected. Bootstrap correctly propagated failure;
it never accepted a clipped reference.

Evidence lives under the run's `autonomous-reference/proof.json` and
`reference-candidate-02/03/calibration/` directories. Original image and sidecar
hashes were checked; originals were not edited.

## Repair

`scan_worker._capture_reference` distinguishes usable photometry from ideal
brightness, retaining the best usable attempt. Once a usable capture and a
clipping limit have both been measured, it stops instead of oscillating.
Selecting an earlier capture verifies its TIFF and metadata hashes, reloads it,
and restores its exposure in the scope and configuration. The returned vendor
receipt belongs to that selected capture. Cancellation is checked before
restoration. Audits identify the selected attempt, hashes, usability, ideal-target
convergence, and whether exposure was restored.

Clipping, minimum brightness, optical context, stage-background correspondence,
and substrate-coherence checks are unchanged. An all-clipped or all-dark sequence
still fails. An acceptable frame below the ideal brightness target is explicitly
reported as usable without claiming ideal convergence. No extra captures are
needed for this recovery.

`live_progress.py` now exposes saved reference-preparation positions separately
from chip-discovery and raster counts. The scan UI draws these points and labels
the preparation phase, including failure. The saved failed run displays 11
preparation fields and zero raster tiles. No chip outline is invented.

## Verification and deployment

78 tests passed, 2 skipped across `test_scan_worker.py`,
`test_autonomous_reference.py`, `test_reference_exposure_saved_replay.py`,
`test_live_progress.py`, and `test_suite_server.py`. The two actual saved-image
replay cases passed. Synthetic cases cover usable-then-clipped,
clipped-then-usable, final-attempt regression, all-dark/all-clipped rejection,
exact exposure/context/receipt restoration, cancellation, and corrupted evidence.
The existing bootstrap test verifies the independent edge control is recaptured
at the selected exposure. JavaScript scan-console checks passed.

Viewer restarted only after verifying both scan and revisit hardware ownership
were closed. Failed run and saved hBN settings are preserved. No new acquisition
was started; complete live discovery and raster success remain unverified.

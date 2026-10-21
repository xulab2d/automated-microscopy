# Loose-chip discovery repair

The sample is hBN on 90 nm SiO₂/Si, placed directly on the microscope stage.
There is no separate holder. Historical internal `holder` label/key names mean
non-substrate background; they do not establish what physically supports a chip.

Both October 15 requests `scan-20261015-143112-e42aa0a5` and
`scan-20261015-145135-db809db3` specify hBN and disable automatic analysis.
The latter's acquisition and campaign configuration also specify hBN. No switch
to graphene occurred in those records. The setup form was displaying graphene
defaults after a terminal run, which made the material appear changed.

The scan UI now restores the last run's material/sample settings on initial
load, preserves edits during polling, and resets readiness confirmations.
The current/previous run header shows material and oxide thickness explicitly.
Unsupported automatic analysis remains off for hBN. The initial material control
shows Loading until server settings arrive, avoiding a transient graphene label.

Fresh scattered/grid runs now explicitly select rectangle discovery across the
current clipped search envelope. The old 9 mm local radius and 32-frame cap do
not govern this route. Capture work is derived from remaining ray distances and
the original remaining deadline. Coarse coverage cells scale to keep the
evidence map within its memory budget; raw edge fits retain their precision.
Edge-focus reuse and explicit historical handoff checks remain in place.

Accepted bootstrap edges now calibrate the exposed-stage background at matched
camera settings. The model is bound to the reference pixels, optical context,
pixel scale and hashed edge evidence. It uses measured stage chromaticity with a
brightness ceiling, excluding substrate-coloured shadows. It replaces the fixed
15% dark fallback for newly calibrated references; old saved data are unchanged.
The implementation does not claim chemical identification of hBN or background.

Replay: `research/stage-background-20261015/replay-v1/report.json`. Four real stage
probes improved from roughly 0.6–1.1% to >99.9% background classification. The
checked substrate classifications remained unchanged. The mixed raw edge still
passed its independent photometric validator. This is saved-image validation,
not a completed live scan of the new route.

Unmet expected acquisition counts/zero completed chips now produce needs_attention
with explicit reasons. Once the requested completed-chip count is met, earlier
unresolved attempts become warnings instead of preventing completion. Historical
exhausted runs are projected truthfully without rewriting their evidence.

Combined checks: 176 passed, four optional replay skips, plus JavaScript gate,
material restoration and draft-preservation checks. This work does not initiate
a scan; the operator will start a fresh run after deployment. See
STAGE-TRAVEL-20261015.md for the preceding travel fix and detailed failure evidence.

Deployment verified: restarted only the idle suite viewer after checking scan and
revisit hardware closure. The running API now reports the prior run as
`needs_attention`; the browser restores hBN, 90 nm oxide, one scattered chip and
scan-only mode. Read-only preflight passes with no blockers. Physical readiness
and the existing per-run hold acknowledgement remain operator inputs. No new
scan was started. UI evidence: `research/stage-background-20261015/scan-ui.png`.

Additional saved-image regression passed. An expanded discovery test run found
one existing CLI fixture failure: its temporary output is outside mandatory
managed Drive storage. This is separate from the passing 176-test change suite.

## Live outcome: 163303 hBN run, October 15

The operator-started `scan-20261015-163303-8fcd7fa0` exposed a remaining false-edge
acceptance bug. Candidate001 east probe `frames/probe_000001.tiff` shows a large
flake surrounded by substrate, but the rectangle RGB-profile fallback accepted
its edge (14/64 profiles, 184.6 um span, no substrate/background spatial bracket).
The whole image was classified substrate. West probe000012 shows a real chip edge.
The resulting measured rectangle was 1713.5 by 18055.1 um despite a 10000 um
minimum chip-width prior. The expanded acquisition polygon was about 4.1 by20.3 mm.

chip001 ended `focus_unresolved` before raster capture: "No substrate-supported
focus ROI or bounded local focus point for tile_000000; nearest is1915.5 um away."
The campaign then resumed scattered seed searches because its stopping condition
counts completed chips, not observed chips. This explains the empty-stage previews.
On the user's report, a checkpoint stop was requested via the normal API and
confirmed: stopped, hardware_closed=true, process_alive=false, zero raster images.
Material remained hBN throughout. Saved evidence was preserved.

These defects were subsequently repaired as described below. This stopped run
and its scientific evidence remain unchanged.

## Repair after the 163303 failure

`classify_boundary` now treats only measured stage-compatible pixels as background
when a stage model is available. Generic colour mismatches (including bulk hBN)
become unknown for boundary finding. Generic material classification is unchanged.
Rectangle RGB profiles require substrate on the inner plateau and stage on the
outer plateau. Both primary and fallback edges also require locally supported
substrate/stage strips 6–30 um from measured edge points. This tolerates dirty
segments without treating arbitrary contrast as a chip boundary.

Replay `research/chip-edge-20261015/replay-v2.json` rejects false east probe1 and
accepts true north9, west12 and contaminated south19. The false image's stage
fraction becomes zero; all four images retain exactly their original substrate
pixels. No missing east boundary is invented from these saved images.

An end-to-end raw-image simulation exposed another fault: bisection stopped at an
ambiguous internal flake. It now subdivides the untested intervals around ambiguous
samples, retaining the actual known substrate/background endpoints. Existing
travel bounds, capture accounting and the user deadline still apply.

If measured dimensions fall below the minimum-width prior beyond local uncertainty,
the search measures opposing sides at two independent tangent positions apiece.
Confirmed smaller chips keep their measured dimensions; no padding to the prior.
Unconfirmed geometry retains its evidence and pauses on that candidate instead
of planning a dubious raster or continuing blind stage-wide search. An explicit
source-upgrade replay revalidates saved sides without rewriting original records.

The campaign also pauses for recovery if the expected distinct chip count has
already been observed but focus/raster acquisition remains incomplete, including
planning failures before creation of chip001. Observed and acquired counts remain
separate; duplicate outlines do not inflate the count. Recovery is explicit:
this change does not silently restart a failed focus/raster operation.

The 163303 focus logs show all four anchors succeeded. The false rectangle's large
orientation margin pushed the first planned raster tile off chip,1915.5 um from
the nearest focus support. Focus travel/support limits were not weakened.

Regression entrypoints: `test_boundary_stage_semantics.py`,
`test_rectangle_material_discovery.py`, `test_rectangle_edges.py`,
`test_sparse_acquisition.py`, and `test_campaign_known_chip_recovery.py` in the
production tests directory. The raw-image simulations cover an internal bright
flake, off-centre starting locations, genuinely smaller chips, and unconfirmed
size remeasurement. No live acquisition was initiated during this repair.

Resume regressions also cover interrupted size checks and repeated rejection of
an old RGB-only flake edge across both explicit source upgrades and later
same-source resumes. Saved edges are revalidated on every rectangle resume;
reanalysis caches bind the boundary-classifier source hash. Rejected old records
remain immutable evidence, not executable acceptance. Pending size checks cannot
be bypassed by a four-side early return or capture-budget exhaustion. Resume work
allowances do not assume old sides will survive revalidation; actual captures and
the original user deadline remain bounded and recorded.

Final combined regression: 199 passed, four optional saved-data cases skipped.
The current hBN false-edge/three-real-edge replay ran and passed. The idle viewer
was restarted after verifying scan and revisit hardware closure. API verification
retains stopped run163303, hBN90, one expected chip, hardware closed and no worker.
The fixes are deployed for subsequent requests; a complete live scan has not yet
validated them. Refresh the Scan page before starting a fresh run.

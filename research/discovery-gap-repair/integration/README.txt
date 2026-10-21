REVIEWABLE SPARSE-DISCOVERY INTEGRATION — OFFLINE, UNDEPLOYED

Algorithm: sparse_photometric_halfplanes_v2
Status: isolated source copies and one complete integration.patch. Production
modules, active manifests/configuration, microscope ownership and STOP untouched.

What is implemented
1. The existing public coherent_edge label-only API retains its original HoughP
   behavior, including unrelated callers and old saved-outline utilities.
2. New validated_coherent_edge requires an explicit dark-holder/manual-20x
   optical profile. Up to 12 standard-Hough proposals are considered only when
   HoughP yields no valid label candidate. Original side purity, side area and
   support-span gates remain unchanged. Independent raw RGB dark-step profiles
   then validate each line. Coherent raw coordinates may refine NEW fallback
   proposals only. Old accepted fast-path coordinates remain unchanged or fail.
3. fit_validated_outline enforces cross-observation half-plane consistency:
   three independent >=90%-substrate 40um patches outside a line's planning
   margin reject the model. Failed models retain diagnostics/lines but no
   polygon. This does not prove the separated-convex-chip assumption.
4. Unknown-centre bracket inference requires connected whole-field support,
   not aggregate fractions alone. It preserves the original classification.
   All-unknown or spatially divided fields remain unknown.
5. Up to three additional angular-gap directions can use REMAINING frames after
   cardinal rays. Each ray uses the original guarded acquire/check callbacks,
   bounds and total max_probes (at most32); no rectangle is imposed. Every fit,
   including intermediate gap planning and finalization, uses registered checks.
6. Campaign and standalone discover_chip_boundaries.py CLI wiring passes the
   profile. Standalone sparse profile/identity/STOP/probe-budget preflight occurs
   BEFORE connect(). Explicit dense legacy mode and seed-search retain their
   prior behavior; their transform and STOP checks also run before connection.
   Camera/illumination/objective drift
   fails acquisition. Unsupported profile returns unresolved before any direct
   discovery motion; campaign preflight refuses it before opening hardware.

Profile and provenance
manual20x-dark-holder-profile.example.json is tied to the SAVED reference's
actual optical metadata: manual encoded position2, EC Epiplan20x/0.4, fixed
200ms, fixed WB, lamp93.444227%. It is an EXAMPLE for review, not a newly changed
hardware configuration or permission to assume all holders are dark. Bright
holder/unknown profiles stay unresolved. Raw bright-holder counterexample is
also rejected even when the wrong dark model is declared.
Use --edge-validation-profile <reviewed JSON> for a NEW campaign or place the
same object at config.chip_discovery.edge_validation_profile. This initial
validator intentionally rejects other magnifications pending measured review.

Discovery schema2 fingerprints include algorithm version, complete profile,
sparse-acquisition source hash, sparse-edges source hash, photometric-module
hash, exact reference pixels/context, transform, limits and unchanged options.
Per-frame TIFF/sidecar/labels hashes are retained. New frame records include
the original label proposal, raw validation/refinement measurements and spatial
bracket inference. The cross-observation result identifies contradicting images.
Raw/images/labels are never reclassified in place during legacy replay.

IMPORTANT RESUME INCOMPATIBILITY
Legacy v1 discoveries and campaign continuation do not silently become v2.
--allow-source-change still only permits a sparse-acquisition source transition
when every other fingerprint field is identical. It cannot waive changed
edge-model/profile/version hashes. The staged campaign also rejects a legacy
discovery contract before connection. Current known-chip raster-only --resume
retains its existing behavior; it does not initiate new v2 discovery.
Therefore this patch is usable for NEW campaign lineage after a clean exited
owner checkpoint. Continuing the existing campaign with imported v2 derived
evidence would require a separate explicit, reviewed migration preserving old
identity/budgets and lineage. That migration is NOT implemented or implied.
The historical --recover-seed-rejection route is explicitly rejected before
connection under v2; it cannot mix a v1 failed discovery into a v2 campaign.
Do not edit old fingerprints/configuration/coordinate_frame_id to bypass this.

Offline evidence
replay-results.json contains19 selected original raw fields and their recorded
hashes, using one fixed reference profile across all fields. Candidates010 and
015 produce four-line estimates; candidate005 remains four-sided. All registered
checks use EVERY saved label observation from each respective attempt. Nine
previously accepted edges stay identical; three new proposals pass unchanged;
four new proposals use measured raw coordinates. False013/probe21 and016/probe13,
plus occluded015/probe7, are rejected. This bounded regression set is not a
statistically validated chip detector or a live acquisition test.
The synthetic rotated/off-centre rectangle has duplicate cardinal-side hits;
the integrated acquisition actually measures the missing side within32 frames.
Other focused tests cover STOP preservation/callback restoration, fixed budgets,
raw hashes on resume, profile drift, unsupported holders, incompatible identities,
registered contradictions, and unknown/disconnected bracket evidence.

2026-10-09 CLI/compatibility review follow-up
Standalone CLI was missing the profile argument/preflight in the first patch;
that gap is corrected here. Initial affected existing suite:30passed/14failed.
All14 failures were expected contract/fixture changes: four calls lacked the
new profile; four focus tests mocked the old label-only hook; six continuation
paths omitted the new contract (including one intentionally incompatible v1
seed-recovery success expectation). Isolated copies update synthetic optical
metadata, explicit profile calls and the new validated-edge hook. The legacy
seed-recovery success test now requires rejection before connection/rewriting.
Original known-chip resume tests remain UNCHANGED and pass. Original campaign
envelope/storage and edge-focus-anchor tests remain UNCHANGED and pass.
The affected44 existing tests pass with the explicit fixture updates. Eight
additional standalone CLI tests pass, including profile-file forwarding and
owned adapter cleanup. A separate legacy campaign identity rejection contract
test also passes. New tests and updated fixtures are INCLUDED in integration.patch.
No19-frame replay was repeated: edge algorithms/source hashes are unchanged.

Reproduce OFFLINE from repository root
  .venv/Scripts/python.exe research/discovery-gap-repair/integration/build.py
  .venv/Scripts/python.exe research/discovery-gap-repair/integration/replay.py
  .venv/Scripts/python.exe -m pytest research/discovery-gap-repair/integration/tests/test_integration.py -q --basetemp data/test-temp-v2-review-NEW
The build refuses a changed pinned production baseline instead of silently
rebasing. The isolated review_ff package imports candidate copies and existing
unchanged dependencies; it never replaces production files/imports in workers.

Future application — ONLY after no active acquisition owner / clean exit
1. Review source-manifest.json and verify all production baseline SHA256s still
   match. Do not stack the older separate Hough/gap/spatial patches; this complete
   patch supersedes them. The Hough-only proposal remains unsafe by itself.
2. From repository root, read-only patch check:
     git apply --check --ignore-space-change research/discovery-gap-repair/integration/integration.patch
   CRLF source contexts require --ignore-space-change; hash checks prevent this
   option from silently accepting an unrelated baseline.
3. Only at the reviewed exited-owner checkpoint apply the SAME patch using
   git apply --ignore-space-change. Then verify resulting content against the
   staged source copies (normalize line endings if git preserves CRLF).
4. Use new campaign lineage/profile, or explicitly design/review a migration.
   No automatic scanner restart/STOP removal is supplied by this package.

Remaining limits
Dark contamination adjacent to a real edge remains an ambiguity. The quality
heuristics and planning margin are not statistical confidence intervals. New
angular samples may still cross unsampled gaps under the declared separated-chip
model. Missing evidence stays unresolved at32; no all-chips-found guarantee.
Completed chip002 still needs a separately measured NORTH-TIP EXTENSION: its
planned raster is complete, but observed chip extends past the saved perimeter.
This patch does not relabel that coverage as complete or repair it by inference.

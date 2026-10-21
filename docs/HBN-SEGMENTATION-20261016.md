# Partial hBN scan: offline segmentation

The operator stopped acquisition work and requested segmentation of saved data.

Do not resume the scan to complete this analysis.

## Live review (October 16)

The selected analysis is now `hbn-image-full-v6`. Review runs links to
`/graphene?run=scan-20261015-223549-98fae566&filter=all&target_band=all`.
Completed fields appear during segmentation, with automatic 30-second refresh.
Unreviewed regions are explicitly preliminary and remain Uncertain until the
separate boundary/halo and optical review supplies evidence. All thicknesses
keeps unresolved regions visible without relaxing candidate criteria.
The controller continues unchanged; no acquisition or analysis restart occurred.
The pilot selection was backed up under `hbn-live-view-publication`; original
pilot outputs and masks remain intact. The full controller will publish
`hbn-image-full-review-v6` as its review stage begins.

Gallery indexing now applies the requested minimum area before building the
in-memory region index. Source totals still count every saved region; direct
observation lookup and the all-region color plot retain small regions. Disk
caches distinguish area thresholds and reuse existing all-region projections
without rereading the source shards. The obsolete 32 MB field-record rejection
was removed after live verification found valid 33–61 MB hBN records; shard
retention instead evicts by both count and source-byte budget (one current field
remains readable). Pending evidence no longer counts as a Filtered decision.
Live verification: 538/1,780 fields processed, 16,064 area-qualified preliminary
regions, no reader warnings, and 48 rendered cards. Warm gallery API: 0.48 s.
49 Python tests and the Node hBN UI checks passed.
Tests cover new field commits, replacement
of pending rows by review evidence, explicit filter choices and live polling.

## Clickable human feedback and gallery loading repair

The hBN detail panel now offers Thin hBN, Encapsulation hBN, Bulk, Not a flake,
and Undo. Reviewer name is optional. Positive labels become human-reviewed
Candidates immediately; negative labels remove candidate status and are visible
under Filtered. Labels override retrieval/selection only; conditional optical
thickness and model results are left unchanged. Visual positives appear under
Both targets even when the optical model is unresolved; they do not fabricate
below-5-nm or 5–30-nm estimates. Selecting a label does not train a new model.

`review_feedback.py` atomically publishes one per-run JSON state containing its
logical append-only event audit and lightweight current projection. The source
field hash binds the exact region geometry; raw-image, request and plan hashes
bind its context. Undo increments a per-identity revision, and event IDs make
retries idempotent. Source changes and concurrent stale edits return conflicts.
Audit events retain original polygons (including holes), bbox, image shape,
contrast, area and stage coordinates for later training/export. No fake operator
labels were inserted into this scientific run for testing.

Long first-load hangs were caused by waiting for an entire growing field index.
HTTP gallery requests now return partial, explicitly counted results in roughly
2-second indexing slices (one slow field may take longer); remaining fields are
loaded through single scheduled follow-ups. Source path validation/stat calls
share a bounded worker pool, and color-plot indexing has its own reader lock.
The UI retains loaded results on errors and times out/retries failed requests.
This is incremental display, not skipped segmentation or relaxed source checks.

Validation: durable store tests, source-bound reader tests, actual local HTTP
POST/undo/conflict/token tests, and mocked browser timer/save/recovery tests.
The live hBN detail panel was visually checked without labeling a real flake.

## Source

- Run `scan-20261015-223549-98fae566`, chip001, hBN on nominal 90 nm SiO2/Si.

- 1,780 manifest-committed tiles out of 4,690 planned. Acquisition terminated with

  `ZEISS MoveTo: Position could not be reached.` Hardware ownership is closed.

- 20x, NA 0.4; exposure 97.866 ms; white balance

  `[0.25007564, 1, 2.61244442]`; lamp approximately 90.02%.

- Scientific project is the shared Drive root in `configs/storage.json`.

- Output namespace is `analysis/hbn/<run>/chip001/<version>`.

- At the initial audit, tile000100 and tile000159 lacked the original independent

  substrate-label NPZ despite manifest hashes. The recorded tile000100 recovery

  ZIP was also missing. No labels were inferred or raw files changed. These

  fields must remain explicitly unassessable unless exact originals are recovered.

## Method and pilot findings

Reuse `prepare_material_input`, conservative block-mean x2 masks, measured

background/noise, `MaterialContrastClassifier`, and `segment_material_terraces`.

Each source TIFF, metadata and independent label file is hash-verified. The

original TIFF and masks remain unchanged; output masks record reduced resolution

and source/stage coordinate transforms.

`scripts/prepare_hbn_model.py` builds a conditional transfer-matrix ensemble from

`configs/materials/hbn-sio2-90nm.json`: n=2.2, k=0, manufacturer Axiocam305 response,

85/90/95 nm oxide, illumination NA 0/0.4, equal-energy and 3000/6500 K source

assumptions, energy/photon response interpretations, and assumed linear gamma.

The 1% RGB engineering mismatch allowance is not a measured uncertainty.

The first pilot used a sparse 0–100 nm grid. It found clear geometric flakes, but

all 212 regions above 25 um2 were unclassified. Extending the thickness range is

necessary for many bright bulk flakes; coarseness within 0–100 nm explained very

little of the mismatch. The revised model uses 0.333 nm spacing to about 10 nm,

2 nm spacing through 120 nm, and 5 nm spacing through 500 nm. Both signs of

contrast are retained, including values below -1.

The original segmentation also split each connected feature at every numerical

thickness class boundary. The hBN driver now opts into

`merge_material_populations=True`: connected material-compatible pixels within

one parent feature form a single proposal. Original pixel class maps remain

saved. Unknown components, holes and disconnected islands remain separate.

Merged polygons do not claim uniform thickness or a uniform usable rectangle.

Default graphene segmentation is unchanged. Mean contrast and material occupancy

still determine optical compatibility; size is an independent review priority.

Unknown regions remain reviewable. Model mismatch does not establish debris:

illumination, anisotropy, heterogeneous terraces, water and residue can contribute.

No empirical graphene contrast bands or graphene layer labels are applied to hBN.

## Edge-halo review filter (October 16)

The operator reported low-contrast rims around flakes being shown as separate
flakes. This is a review classification problem: image-only geometry legitimately
separates blurred edge populations, but they must not be promoted as flake bodies.
The new `hbn_halo_screen.py` checks a conjunction of physical narrowness/little
interior, close adherence to substantially stronger observed RGB regions,
intermediate aligned contrast, and measured decay toward unlabeled exterior.
It does not reject a feature for being narrow or near bulk alone. No optical
thickness fit participates. Operator-positive retrieval overrides a halo flag.

Serial and parallel refiners attach the evidence inside `hbn_screen.halo_screen`;
compact indexes preserve it. The reader hides only these new hBN halo flags by
default, not the superseded straight-edge/shape screen. “Show flagged artifacts”
restores them; direct observation lookup and the complete RGB point cloud remain
available. All original masks, interior regions and raw images are retained.
The ongoing `hbn-image-full-v6` analysis source is unchanged. The full controller
will use the updated refiner after segmentation, without restarting completed
fields. Do not modify source pinned by that segmentation process.

Halo review deployed: `hbn-halo-review-v7` selected on unchanged analysis
`hbn-image-pilot-v6`. All 452 useful regions were reviewed; 25 likely halos are
hidden across thickness views (3 in Both targets, 22 in Thicker or unresolved).
Default Both targets now shows 4 operator candidates and 60 uncertain regions.
“Show flagged artifacts” restores all records. Live API verified both views and
all four candidates, with no warnings. Final tests: 47 focused tests plus UI
JavaScript checks passed. Saved-image halo scoring takes 0.12–0.31 s per field.
QA: `hbn-halo-pilot-v1/contact-1.jpg`, `contact-2.jpg`, `summary.json`.

Specific checks: tile1700 rim1288 is flagged while lobes1282/1284 remain;
rims1435/1437 are flagged while central1436 remains. All four known positive
interiors fail the raw halo conjunction independently of the operator override.
This remains a reversible image heuristic, not proof of artifact identity.

## Selected result and running work

Selected and live: `hbn-image-pilot-v6` / `hbn-halo-review-v7`, six assessed

fields, 452 refined regions >=25 um2. Default Both targets view: 4 candidates

(operator visual positives), 60 uncertain after halo filtering. No automatic candidate survived the

combined optical, alternative-film and nearby-thicker-context checks in this

pilot. This is NOT evidence that no additional thin hBN is present. The 385

other regions remain in Thicker or unresolved; four visual positives also appear

there because their thickness is unresolved.

Old-to-new examples: 1200/1189 -> 1200/1822; 1700/307 -> 1700/1282 and1284;

1700/83 -> 1700/511 (triangle interior). All five bulk points remain excluded.

52 focused regression tests passed in the final combined run, plus the earlier

100-test integration and browserless UI checks. Live API verified hBN material,

correct selectors, four operator candidates and no warnings.

Full offline pass launched: `hbn-image-full-v6-controller`, analysis

`hbn-image-full-v6`, refinement `hbn-image-full-review-v6`, two bounded workers.

The six-field pilot stays selected until the full analysis produces a reviewed

generation. Launch record: `research/hbn-segmentation-20261016/full-image-v6-launch.json`.

No acquisition or microscope access occurs. Read the controller status, not old

PIDs. The older v3 STOP remains. Pilot v5 aborted before its first field because

an agent's final source correction raced with source pinning; it has no results.

## Current correction: image geometry and thin/bulk ambiguity

The previous morphology-only screen failed: all eight shortlisted objects were

visually bulk. The `hbn-partial-full-v3-controller` was stopped cooperatively

after 128 fields; its STOP remains in place. Do not restart that version.

The older processing description below records history, not the current policy.

Operator examples, all from this run and chip001:

- Visually thin (believed below 5 nm, not measured): tile001200/1189,

  tile001700/307, and tile001700/83.

- Visually bulk: tile001200/925, /769, /956; tile001700/546, /550.

The old mask for 83 was a bright rim next to the intended thin triangle. Never

use its old mean contrast as a positive training value. The raw triangle interior

is near source pixel (696,550); (695,585) lies on the bright rim. The image-only

pilot separates these. Original masks of 1189 and 307 retained only 45% and 31%

of their foreground parents because hard optical pixel classes carved away

parts of the visible objects.

New geometry uses connected image regions and regularized RGB populations,

independent of material lookup tables. Optical evidence is measured afterward.

No hull, hole filling, straight-edge gate, or uniform-thickness claim is used.

The thin-film evidence compares both target ranges (below 5 nm and 5–30 nm)

with the supplied thicker branches through 500 nm, rather than accepting every

hBN-compatible color. Transparent-film alternatives (illustrative n=1.33, 1.5,

1.6; 36 matched optical scenarios each) expose color degeneracy with residue.

These alternatives do not establish a specific contaminant chemistry.

Operator labels are explicit retrieval evidence anchored to raw source hashes

and image points, not thickness calibration. The fuller image masks do NOT fit

the current optical model reliably for all three thin examples: their target-fit

distances are about 4.4, 8.2, and 11.3. Do not increase covariance merely to force

a fit or convert the operator belief into a measured sub-5nm label. Explicit

visual positives appear in Candidates / Both targets, and Thicker or unresolved;

only model-supported ranges enter the narrow thickness filters.

Three additional image-mode regions (tile600/959, tile1700/1850 and /1864) still

looked bulk despite isolated thin-branch fits. The new contextual ambiguity test

uses actual mask distance (within 2 um) to a substantial (>=25 um2) region whose

model favors a thicker explanation by at least 20:1, or an operator bulk label.

Poor absolute fit is recorded and does not establish bulk identity. Thin-positive

operator regions never seed a bulk-context flag merely from model preference.

This flags all three pilot false positives; it preserves every region and changes

automatic thin support to uncertain. It does not assign height from a shadow or

claim texture alone identifies thickness. These explicit engineering thresholds

need validation on more examples; ambiguous neighbors may include real thin flakes.

Unlabelled ambiguous objects remain reviewable. Thin/bulk alternatives and model uncertainty must not be presented

as measured thickness. UI target ranges overlap when the supported model grid

spans both bands. All source data and previous results remain preserved.

## Historical review and processing (superseded)

The operator correctly questioned whether the 125 color-compatible regions in

the revised six-tile pilot included tape residue. Color alone is insufficient.

The hBN shortlist now additionally requires a passing, reversible morphology

screen (`hbn_review_screen.py`): well-supported sharp boundaries, at least two

substantial nonparallel straight sides, a measured interior with limited relative

RGB variation, and non-filamentary geometry. Thresholds are explicit review

heuristics, not validated chemical discrimination. Smooth blobs, weak boundaries,

missing measurements and pending screens remain Uncertain, never deleted.

The screen reduced the pilot's 125 optical matches to **8 shortlisted regions**.

The viewer shows 388 uncertain regions and hides 4 pre-existing artifact flags by

default (400 total above the review area). All 400 have boundary refinement.

The large number of color-only matches must not be reported as a flake count.

The RGB plot uses the same screened candidate definition as the gallery, retains

all finite measurements, and fits hBN signed contrasts instead of graphene axes.

Selected pilot: `hbn-pilot-v2`, refinement `hbn-pilot-screen-v3`.

Full saved-tile workflow launched: `hbn-partial-full-v3`, followed automatically

by `hbn-partial-screen-v3`, via `scripts/run_hbn_offline.py --defer-selection`.

The pilot remains selected until the full pass has a screened refinement

generation; then the controller changes selectors automatically. This avoids

showing unreviewed color matches as new candidates during segmentation.

Status/logs: sibling `hbn-partial-full-v3-controller` directory. A `STOP` file

there cooperatively stops this offline workflow only. The controller never starts

acquisition. Launch record: `research/hbn-segmentation-20261016/full-offline-launch.json`.

The shared review reader uses `hbn-analysis-selection[-chip001].json` and

`hbn-review-selection[-chip001].json`, validates the exact hBN request/model/oxide,

and exposes candidates, uncertain regions, RGB contrast plots and spatial dedup.

The existing `/graphene?run=<run>` route is a legacy URL; its displayed material

and title are hBN. Graphene thickness requests and overlays are disabled for hBN.

The offline driver uses two bounded worker processes, a durable field ledger,

source/code pins and independent STOP files. Analysis failures are distinct from

negative findings. Useful-region refinement works on both compatible and unknown

regions above 25 um2, independently of material class, and preserves originals.

## Literature and limits

- [Gorbachev et al., Small 2011](https://arxiv.org/abs/1008.2868): optical visibility

  of atomically thin BN, approximately 2.5% white-light contrast per layer on

  90 nm oxide, n approximately 2.2, and contamination comparable to a monolayer.

- [Zhang et al., Nano Letters 2024](https://doi.org/10.1021/acs.nanolett.4c04241):

  red-filtered imaging and SHG support thickness inference. Its reported layer

  precision does not transfer directly to this broadband RGB camera channel.

This run has no independently known thickness reference, measured lamp spectrum,

or confirmed radiometric linearity. Report optical compatibility, not confirmed

chemical identity or calibrated layer counts.


# Graphene segmentation of the October 13 chip

Acquisition is paused at the user's October 14 instruction after manual takeover
by another lab member. Installation, production and shared-project operator holds
record the pause. No instrument connection, objective change or stage move is
part of this analysis. Historical coordinates require fresh verification before
future use. The previous acquisition worker was already closed and absent.

## Review entry, performance and useful-size correction (October 14)

Use the desktop **FlakeFinder Review** shortcut or root `Open FlakeFinder Review.cmd`.
Both call `scripts/launch_flake_review.py`, which starts/reuses the viewer and opens
`http://127.0.0.1:8770/review`. No acquisition or hardware connection starts.
The library validates selected graphene analysis plans, lists all those curated
runs plus the existing MoTe2 collection, and optionally includes earlier nonempty
incremental analyses. Empty failed restart invocations are omitted.

A measured cause of the slow Uncertain switch was full field-shard reloads:
48 cards span more than the previous 12-shard cache. A warm request took15.83s
and returned1.45MB. The viewer now separates compact card metadata from full
polygons, keeps a disposable checksum-verified SQLite projection under the local
temporary `flakefinder-graphene-review` directory, and loads geometry on demand.
Card previews are viewport-lazy; the large field selector is sent on initial load.
Source plan/ledger identity and file-stat invalidation remain checked. The cache
is not a scientific output or a replacement for source records.
Measured after cache build:1.07–1.25s warm category requests;91KB per48-card
summary (about94% smaller). First cache construction took113s for309,825
regions/998 readable fields. Live HTTP verification measured1.096s unchanged
and1.663s when a refinement generation changed. Details and outlines deliberately
load more data. During a running refinement, background updates show an update
button instead of moving cards under the cursor. Evidence: `review-performance-v2.json`
and `live-ui-performance-v3.json` in the local research folder.

The apparent size rejection was a real logic error: unknown optical classes
skipped the usable-rectangle calculation, received width0, then failed a3um width
cut despite large measured area. Future production material regions now use the
geometric area criterion independently of optical class. Width is diagnostic,
not an automatic rejection. Existing saved masks and original reasons are retained;
review derivatives supply corrected size assessment. An uncertain optical class
can remain uncertain while being a useful gate-sized flake.

The initial `candidate-refinement-v1` pass covered only41 original proposals.
The new field-cached useful-region pass covers every readable saved region with
area>=25um2, including Uncertain. It denoises each field once, assesses local
boundary support and nearby dark features, and ranks geometric evidence without
using the optical color class. Shadow penalties combine weak/diffuse boundaries
with nearby dark features; darkness alone does not reject a flake. Rankings and
artifact flags remain heuristic and reversible. Raw source masks stay frozen.
Camera-truncated regions retain an extent warning, but supported visible edges
can be accepted; camera-border samples are excluded from edge evidence.
Per-field shards and immutable index generations allow progressive GUI display
without a giant all-polygons catalog. The selected stream is pinned to its plan;
`latest.json` points to a checksum-verified immutable index generation.

## Current data and output

The source is the original `data/scan-20261013-221727-131d9ced/chip001`: 999
manifest-committed raw TIFF fields, nominal graphene on90nm SiO2/Si,20x/NA0.4,
0.1725um raw pixel sampling. Use the original run, not the incomplete October14
handoff copies. Scientific outputs are under the managed shared project:

`analysis/graphene/scan-20261013-221727-131d9ced/chip001/`

- `model-comparison/`: conditional optics, external90nm populations, six-field
  contrast audit, prior-distribution plot, candidate-mask comparison and
  native-resolution check.
- `pilot-conditional-v3/`: current frozen driver, ten-field pilot;6 assessed,
  4 unassessable,0 unexpected failures. The exclusions are2 deliberately missing
  sidecars and2 chip-edge background-fit limitations.
- `pilot-external-v1/`: same6 interior fields with the external population prior;
  explicit camera-context mismatch is retained. All6 assessed.
- `pilot-native-resolution-v1/`: native-resolution check of tile500;1 assessed.
- `full-chip-conditional-v1/`: complete:999 processed,942 assessed,57 unassessable,
  zero unexpected failures. One committed analysis JSON (tile524) is currently
  unavailable; the GUI indexes998 readable records and explicitly flags it.
- `useful-refinement-v3/`: selected progressive all-useful-region pass; inspect
  `status.json` and `latest.json` for current completion. Plan byte SHA:
  `c55131d1ebc3d6dc61cd66c3fba7fb8d3a8261a195169185de9305620bdd8483`.
  Resume command: `research/graphene-segmentation-20261014/useful-refinement-continuation.txt`.
  V2 was stopped after95 fields to fix missing-source continuation and partial-frame
  boundary acceptance; retained as historical evidence, not selected.
- `candidate-refinement-v1/`:41 proposal refinements;22 accepted,18 review-required,
  1 unresolved, with7 reversible likely-artifact flags. Raw masks remain frozen.
- `review-v2/`:bounded SQLite cross-tile grouping of all readable regions>=25um2,
  including Uncertain; inspect dedup-progress.json/dedup.json for final counts.

Pilotv1 is a failed pre-field launch: the agent changed the new driver after an
early readiness message; source pinning detected that and closed the process.
Pilotv2 is preserved with valid scientific outputs but older failure-category
labels and unavailable memory telemetry. V3 corrects these reporting issues;
it does not change the segmentation model or masks.

## Reuse and research

See [the primary-literature audit](GRAPHENE90-LITERATURE-20261014.md) and
`research/graphene-segmentation-20261014/pipeline-audit.md`.

The MoTe2 implementation supplies raw normalization, covariance-aware optical
comparison, connected masks with holes, eroded-interior measurements and review
infrastructure. Its material colors,285nm priors and empirical controls do not
transfer to graphene. Existing graphene-specific modules already implement the
appropriate material profile. Contrary to the initial concern,90nm oxide has
direct published precedent:2DMatGMM includes graphene90nm at20x, but its Nikon/
DFK camera differs from this ZEISS/Axiocam acquisition. Matching magnification
does not make its color classes calibrated here. The published90nm work also
supports treating blue contrast as more oxide-sensitive than red/green.

The baseline uses the existing conditional graphene90nm optical ensemble:
85/90/95nm oxide,0/.4 illumination NA, two assumed lamp spectra and manufacturer
camera response. Gamma1 and zero dark offset are unmeasured assumptions; the
additional1% RGB mismatch spread is an engineering allowance, not calibrated
uncertainty. Neither the baseline nor imported1–4L population names establish
chemical identity or layer counts. Background systematic uncertainty remains
unquantified. Raw data and previous outputs are unchanged.

## What the real pilot showed

The6 interior fields (100,300,400,500,600,950) produced1764 connected optical
regions under the conditional model;79 were color-compatible. The external
model produced1737 regions,85 color-compatible. Three conditional-compatible
regions and four external-compatible regions were >=25um2. None of the
conditional-compatible pilot regions met the existing combined25um2 area and
3um inscribed-width ranking. These are region counts, not unique flakes,
confirmed graphene, precision/recall, or a finding that graphene is absent.
All unmatched and small retained regions remain available for review.

Visual examination of the largest tile500 matches found thin dark features but
also partial rings around blurred particles and streak-like regions near bright
thick material. Both priors can select these artifacts. The key next step is
same-camera positive/debris/background review and held-out-field evaluation,
not importing the external layer names or accepting color agreement alone.
No labels were fabricated or used to train a local empirical model.

Factor2 block means produce1.27M analysis pixels at0.345um sampling, with AND
valid/substrate masks, OR saturation masks, and reestimated substrate covariance.
No hole filling, closing or merging disconnected regions was added. Raw input
normalization remains full resolution. Masks are exact on the analysis grid;
source coordinates use `x_source=2*x_analysis+0.5` in each axis. Upsampling a
mask does not confer native boundary accuracy.

Native tile500 gave31 compatible regions versus35 at factor2,4 versus3 above
25um2, and no useful-size region under either setting. Compatible-mask overlap
IoU was0.7674; this is agreement between settings on one field, not ground-truth
accuracy. Native process peak RSS was1.045GB; the reduced pilot peaked at0.523GB
(decimal bytes). Native/reduced segmentation times on this field were5.14/1.87s,
excluding preparation, hashing and output. This motivates a reduced full-chip
screen followed by native-resolution inspection of promising candidates.

## Implementation and operation

`research/software-next-20261009/scripts/analyze_graphene_run.py` is the new
standalone driver. It calls existing `prepare_material_input`,
`MaterialContrastClassifier` and `segment_material_terraces`, with no microscope
imports. It processes one field at a time at below-normal Windows priority and
one logical CPU. Before each field it checks a configurable768MiB free-memory
reserve. Allocation failures pause without committing partial masks. Native
Windows memory telemetry records RSS/private bytes and cumulative peak RSS.

Input request, manifest, model, configuration and code hashes are pinned.
Per-field masks and JSON are checksummed before ledger commits. The independent
output `STOP` file stops analysis; it does not touch acquisition STOP or another
lab member's instrument session. Reusing the exact command resumes committed
fields; changed code/settings require a new version. Missing sidecars remain
unassessable; no camera metadata is synthesized. Known substrate-fit exclusions,
budget exhaustion, invalid evidence and unexpected exceptions are distinct.
Seven fields in the source inventory lack required metadata/labels; other
quality exclusions can increase the unassessable count. Full-chip success must
be reported with the complete status breakdown.

From the workspace, using its Python, the full pass is:

```powershell
$projectRoot=(Get-Content configs/storage.json | ConvertFrom-Json).project_root
.venv/Scripts/python.exe research/software-next-20261009/scripts/analyze_graphene_run.py --project "$projectRoot" --run-id scan-20261013-221727-131d9ced --chip chip001 --version full-chip-conditional-v1 --tiles all --model "$projectRoot/analysis/graphene/scan-20261013-221727-131d9ced/chip001/model-comparison/conditional90.json"
```

Do not run a second owner for the same version. First inspect status and PID.
Analysis does not use the original acquisition deadline and does not grant any
future scan authorization. No automatic model polling was created.

## Review and validation

`research/graphene-segmentation-20261014/build_review.py <analysis-folder>` builds
a portable `review/index.html` with field selection, area filters, known/unknown
outlines, candidate crops, source-hash-bound provisional visual annotations and
JSON export. Open the HTML with its sibling files preserved. Annotations are
kept in browser-local storage until exported; they are not physical labels.
Review generation verifies the request, manifest and committed field JSON.
The images are saved JPEG copies used only for display, never photometry.
The review is a snapshot; rebuild it to include later full-chip commits.

Forty focused regressions passed: new driver tests plus material profiles,
material selection and existing graphene watcher. Coverage includes reduction,
source geometry, source mutation, resume/STOP behavior, explicit exclusions,
failure categories and Windows memory reads. Review JavaScript passes Node's
syntax check. Mask comparisons and the contrast plot were visually inspected.
Automated interactive browser QA was unavailable: a local server start was
rejected by policy, and the browser rejects file URLs. No workaround was used.
The static review is saved; browser interaction remains unverified.

Local evidence/scripts are in `research/graphene-segmentation-20261014/`; this
directory also preserves prior operator holds and the explicit pause recorder.


## Full pass and normal GUI integration — October 14

The full conditional90 pass finished: 999 processed, 942 assessed, 57
unassessable, zero budget exhaustion/invalid-source/unexpected failures.
The existing Flakefinder GUI now reads this version at
`http://127.0.0.1:8770/graphene?run=scan-20261013-221727-131d9ced`.
The failed105506 restart shows a verified link to its original acquisition donor.
It does not silently combine different run identities.

`data/<source-run>/graphene-analysis-selection.json` selects the version with an
exact plan byte hash. The reader validates request, manifest, model, config and
field hashes, projects saved source-pixel polygons, and keeps compact indices
with at most12 full shards. Existing incremental watcher results still work
when no offline selector exists. No microscope connection is involved.

Live GUI/API verification found 309825 regions in998 readable records, with41
color-compatible useful-size proposals at the default25um2 minimum. Another
53797 uncertain regions meet that area minimum. Categories can overlap with
Not selected by filters; counts are regions, not deduplicated physical flakes.
One committed field JSON, tile_000524, is currently absent from Drive. Its ledger
assessment remains historical; the GUI flags the absent record and excludes its
regions. Do not report998 as the number of assessed fields or silently reconstruct
this output. The source data and analysis version were not modified.

First index construction took85seconds; subsequent requests reuse compact
indices. Viewer process working set after loading was approximately277MiB.
18 reader regressions passed, frontend JavaScript syntax passed, and browser
verification confirmed the failed-restart donor link, proposal cards, original
JPEG preview, and source-coordinate outline. Initial inspected candidate660/167
includes a halo around blurred debris: these are preliminary optical candidates,
not validated graphene or layer assignments. Native-resolution/false-positive
refinement remains scientific follow-up work.

Verification snapshot: `research/graphene-segmentation-20261014/gui-api-verification.json`.
The previous static-review browser limitation above is historical; normal GUI
verification succeeded using the existing8770 server, reloaded through the
standard launcher. Acquisition remains paused for manual lab takeover.


## Review refinement, artifact flags, color plot and spatial groups

The October14 user review found useful real flakes, ragged outlines, dust/edge
false positives, and useful Uncertain objects for gates. Do not turn color
uncertainty into rejection or restrict useful devices to the thin-graphene prior.

The normal Graphene GUI now has an expandable **Color space** panel. Its API
`/api/graphene/colors?run=<exact run>` streams every finite region mean from the
active readable records:309825 points, including small/unselected objects and
all repeated observations. Packed float32 transport avoids millions of JSON
number objects. It is not a pixel-level plot or a deduplicated population.
Axes can be RG/RB/GB, fitted to all points or focused on thin-feature contrast;
orange points identify the original41 proposals. The six-field optical-prior
comparison remains linked separately and is labeled as a pilot. Do not confuse
its sampled pixel distribution with the new full-region measurement plot.

`flakefinder/graphene_refinement.py` provides candidate-local raw-RGB boundary
refinement: physical-scale bilateral denoising, measured foreground/background
likelihood, bounded growth and independent boundary evidence. It does not use a
convex hull to force an attractive shape. Frozen originals and photometry remain
unchanged. `research/graphene-segmentation-20261014/refine_candidates.py` replayed
all41 current proposals into sibling `candidate-refinement-v1`; the exporter
produced `gui-catalog.json`. Results:22 accepted contour changes,18 review-required,
1 unresolved. Review-required/unresolved cases retain original displayed outlines.
Seven candidates have a reversible likely-artifact flag requiring diffuse-boundary
AND nearby-dark-feature evidence. Darkness or material-color mismatch alone does
not reject a flake. Some sharp chip-edge features still evade this heuristic.
This pass refines41 proposals, not every Uncertain region.

The GUI offers Refined boundaries, Show flagged artifacts, and original/refined
switching in the detail viewer. It retains all source observations. The default
view hides the seven likely artifacts but has no general rejection of Uncertain
or useful gate candidates. Refinement metrics and warnings are in the inspector.

`flakefinder/graphene_dedup.py` groups different-tile observations using calibrated
stage-space polygons (including holes), intersection/overlap, and measured RGB
agreement. Same-tile observations cannot merge; complete-link membership avoids
transitive bridges. A real observation is chosen as representative based on
interior measurement, border completeness and color uncertainty; geometry is not
unioned. Thresholds remain explicit engineering settings, not identity certainty.
The41-observation replay found three pairs (639/640,626/627,368/369), or38 groups.

`scripts/group_saved_graphene.py` extends this to all saved regions >=25um2,
including Uncertain. It streams one verified shard at a time to SQLite/R-tree,
keeps a32-entry prepared-geometry cache, and groups with bounded memory. Below25um2
observations remain separate; this is the declared scope of this derivative,
not an acquisition stop. Its independent DEDUP_STOP and progress files are in
sibling `review-v2`. Do not delete the raw input, frozen analysis, or originals.

`data/<run>/graphene-review-selection.json` explicitly selects derivative JSON
paths and byte hashes, bound to the original analysis plan. Both derivatives
carry source-field hashes checked against the active ledger. No silent model or
source-version substitution is allowed. The GUI can switch between grouped and
all observations and inspect each member in the detail-view selector. Grouping
is applied within the active category/field/area filter, so a visible member can
represent a group whose preferred member is outside that filter. Grouping never
changes material disposition or hides an entire group solely because one member
is unavailable or artifact-flagged.

64 combined reader/refinement/grouping/server regressions passed before the final
UI check. Scientific checks include replay of clean flakes329/153 and639/121 and
dust-shadow660/167, with saved input hashes. Initial gallery/color API construction
took91seconds; subsequent reads reuse the index. The independent grouping worker
started at55MB RSS and was85MB peak after164 fields. Exact final counts and UI
verification should be appended after completion.


### Completed grouping and deployment

The all-region grouping finished in671.2s:53838 observations>=25um2 became45801
proposed groups (7303 multi-observation groups,38498 singletons), consolidating
8037 repeated observations. There were0 ineligible observations,55027 nearby
pair checks,998 readable field records and the known missing524 record.
Peak worker RSS was104329216bytes (99.5MiB). The worker exited normally.
`review-v2/dedup.json` is8440979bytes, SHA256
`cf9b5b1cd8238c9a3e955dd6c3eeef1c6ef9ee3078baa934432a08a66fef166f`.
The selector now includes this artifact and candidate-refinement-v1/gui-catalog.json.

The default GUI shows31 Proposed groups after hiding7 likely-artifact observations.
Uncertain shows45766 groups and All assessed45794 with these same defaults.
Group counts across categories are not additive: different observations of one
physical location can retain different original material dispositions.
Showing every observation and flagged artifact restores the original41 proposals
and53797 Uncertain observations at25um2. Below25um2 observations remain available,
but this grouping derivative does not group them.

64 combined regressions pass. Live browser checks confirmed the full309825-point
plot (Fit all displays every point), original/refined boundary toggle, reversible
artifact filtering,31-card default gallery, and original639/640 member selector.
GUI page index checks now invalidate cached records on file-size/mtime changes
and omit previously indexed shards that disappear. Cached requests can take
several seconds over the Drive mount; the UI reports Loading saved results.
Local evidence:gui-color-verification.json,gui-v2-verification.json,
dedup-completion.json,gui-refined-boundary.png and gui-review-v2.png under
research/graphene-segmentation-20261014. No hardware access or acquisition resume.

## Viewer deployment verification

October14 viewer deployment:75 targeted tests pass (review, refinement, useful
regions, dedup, library, server); scientific agent also passed30 tests including
material-size and missing-source recovery. JavaScript syntax checked. Browser
verified both curated run links, Uncertain compact cards, lazy detail geometry,
corrected size notes and accepted partial-frame outline329/5. Saved UI evidence:
`gui-saved-runs.png`, `gui-uncertain-boundary-v3.png`. Live scientific API checks
are in `live-science-check-v3.json`. Full v3 batch continues independently; the
delivery-time checkpoint is `review-delivery-status-v3.json`. Do not interpret
that checkpoint or this deployment as full-batch completion. Read status/latest.

## Completed useful-region pass and ranking regression

V3 completed999 field attempts and53,689 region refinements in1380s. The worker
exited0; final index is `index-000999-complete.json`, SHA
`a7ec39a57e14e16d5bb0b596aa5c9ad857c0b0aea3ea99f23599bb4efc9c2cd8`.
Peak process RSS303MiB.939 fields refined,57 were originally unassessable,
two original masks were unavailable (265 and475;149 eligible regions retain
original outlines), and524's original field JSON remains missing. Do not
reconstruct or overwrite missing source evidence. The final ledger skips its
terminal unavailable entries; simply restarting it does not retry them.

The geometric-only v3 sorting introduced a real regression: strong clean edges
and large area promoted bright bulk flakes over thin candidates. It had replaced
the original area × material-compatible-pixel-fraction score, and refined-first
ordering also biased a partial batch. Shadow flags alone cannot distinguish a
clean bulk flake from a thin flake. The user requested one integrated ranking,
not separate optical and geometric modes. The correction combines existing
continuous optical-model evidence with geometry, useful area and artifact
penalties; it does not change masks or add a hard contrast exclusion.

### Joint evidence review priority v1

The viewer now uses one `joint_evidence_v1` score. Existing observations,
dispositions, outlines and dedup groups are preserved. No score threshold drops
observations. The optical term is a smooth1/(1+d²/r²), with d the saved
region-interior Mahalanobis distance and r² derived from the saved classifier's
3D match quantile. Pixel-level material support contributes a bounded0.5–1 factor;
unknown-class/zero pixel support is therefore not a zero score. Substrate
likelihood support is bounded at1; overwhelming contrast against substrate
cannot by itself establish graphene identity or boost a bulk flake indefinitely.

The geometric term uses the saved refinement rank, normalized by log41. That
rank already includes diminishing area utility, boundary support, SNR, shape and
shadow/domain penalties. Area is not multiplied twice. Missing/unresolved geometry
uses a provisional0.5 quality factor multiplied by area utility, explicitly marked
unmeasured. It is not treated as observed zero quality. Other missing optical
inputs receive conservative, explicitly reported fallback factors. Unknown optical
class alone never excludes an observation.

This is a versioned, inspectable ranking heuristic, not a calibrated probability
or a layer count. Distance and pixel support are related optical measurements,
not independent evidence. Bright gate/bulk flakes can remain useful and remain
accessible, but their lower optical compatibility reduces their review priority.
Independent checked examples:639/121 clean reference outranks216/39 subtle
Uncertain, which outranks bright639/23 and849/175; diffuse dust660/167 stays low.
These are regression examples, not a measured false-positive-rate claim. A full
first-page visual check and cached API timing accompany deployment. The v3 local
cache adds compact optical evidence; source science files are unchanged.

Joint ranking deployment verified:79 targeted tests pass. Cache enrichment took
71.10s once; warm category requests0.73–0.87s. Live server Uncertain request
0.813s,48 cards/146,603 bytes,45,642 matching grouped observations (count unchanged).
First-page inspection shows subtle optical candidates back near the top, alongside
some conspicuous uncertain features; no claim of perfect contamination filtering.
Only10/48 first-page observations have negative red contrast, versus the earlier
geometry-first leaders being strongly negative; this is a descriptive comparison,
not a thickness label. `joint-ranking-top48.json`, `joint-ranking-performance.json`,
`joint-ranking-example-proof.json` preserve the inspected ranking evidence.

Final UI verification: hydrated detail for chip001/tile_000940/75 (124.5 um2) correctly suppresses the original obsolete useful-size warning; original sieve evidence stays available in its collapsed section. Uncertain gallery shows 1-48 of 45,642 groups. Screenshot: research/graphene-segmentation-20261014/gui-joint-ranking.png.


## Operator contrast-space evidence, October 14

The operator reports three visually consistent flake groups. The first has red
contrast approximately 0.10–0.15 with an upper green boundary running from
(0.10, 0.09) to (0.15, 0.12), or G <= 0.6 R + 0.03. The second sits near red
0.26, separated from four visually false orange points lying farther left and
higher in green. A third flake point lies near red 0.35. These are visual
thickness groups, not calibrated layer assignments.

The exact saved proposed-region coordinates support the second separation:
four outliers have R=0.21712–0.23579, G=0.22816–0.23236 and B=0.19290–0.23152;
the eight nearby lower-green observations have R=0.24206–0.27023,
G=0.18516–0.20100, B=0.11037–0.12898. This blue-channel separation offers an
additional discriminator against nearly achromatic shadows. Raw proposed
points include repeated observations; the visually single third group has
four nearby saved observations, whose physical identity must be checked.

Evidence: `research/graphene-segmentation-20261014/empirical-contrast-proposals.json`
and `operator-contrast-observations-20261014.json`. Exact IDs are inferred from
the operator's plot description, not individually confirmed annotations. The
joint ranking was unchanged at the time of this initial note. The completed derivative below now applies these observations. They began as a
traceable run-specific hypothesis for subsequent RGB-prior fitting; they must
not be silently transferred across illumination, substrate, camera settings or
runs, nor used to eliminate useful gate candidates outside these groups.

Across these inferred flake groups, median normalized RGB directions are
(1, 0.7626, 0.4756), (1, 0.7742, 0.4787), and (1, 0.7707, 0.4981).
That suggests a shared RGB trajectory with different contrast magnitudes,
rather than three unrelated boxes. The next model experiment should compare
perpendicular distance to this trajectory plus along-trajectory bands against
near-achromatic artifacts, using independently verified labels and preserving
useful out-of-band candidates. These summaries include repeated observations
and do not establish independent sample counts.


## Completed operator-band derivative — October 14 evening

The selected analysis is now `operator-empirical-full-v1`, with
`operator-empirical-full-v1-boundaries` and `operator-empirical-full-v1-review`.
All 999 saved fields were processed: **942 assessed, 57 unassessable, zero
unexpected failures**. This was an offline operation; original raw images,
acquisition records and the earlier conditional analysis remain intact. Prior
selector bytes were preserved before publishing the new selection.

This is actual pixel re-segmentation, not just reranking. A run-specific empirical
RGB trajectory uses the operator bands and first-band green boundary, retaining
substrate/noise tests and a fixed perpendicular residual envelope. The disclosed
transition margins are engineering choices, not calibrated probabilities.
Beyond the observed amplitude range, trajectory-like regions remain extrapolated
and uncertain. Useful out-of-band observations remain accessible. No absolute
layer numbers or chemical identities are inferred. The literature qualifications
are in `GRAPHENE90-LITERATURE-20261014.md`.

Under the same live GUI defaults (minimum area 25 µm², grouping enabled,
likely artifacts hidden), Candidates increased **31 → 224**; Uncertain changed
45,642 → 44,940; All changed 45,670 → 45,164. The current viewer exposes
330,196 source segments and all 999 field records. The old viewer had 998 records
because its derived field524 JSON was missing; the new field524 is assessed.
Before grouping/artifact hiding, Candidate observations increased 41 → 233.
115 new Candidates have at least 50% new-mask overlap with a formerly Uncertain
observation. These are review observations, not a count of independently verified
physical flakes. All four reported second-band false examples are unpromoted and
retained. Checked positive examples215/36,627/26,639/121 and547/176 remain Candidates.
Exact point IDs were inferred from conversation, rather than independently
confirmed object annotations.

Boundary refinement assessed 53,513 regions: **36,937 accepted, 15,047
review-required and 1,529 unresolved**, with 94 likely-artifact flags. The checked
215/639/547 contours improve coverage. The269/32 example still has a fragmented
original mask and remains unresolved; this update does not fix every outline.
Nearby-dark-feature warnings remain visible but do not alone veto an otherwise
supported contour. The full-run eight-field validation exactly reproduces the
qualified pilot masks and geometry: all5,074 segmentation-region records and all
426 boundary-region records, excluding only legitimate version provenance.
The contact sheet uses actual displayed polygons, not unaccepted proposals.

Spatial grouping indexed all999 fields with no unavailable inputs. It grouped
53,513 area-qualified observations into45,248 possible-object groups, including
7,481 groups with multiple observations;8,265 repeated observations are combined
reversibly. Every original observation remains accessible. Optical/color and
stage-polygon evidence constrain grouping; this is not a ground-truth identity
assignment. The final selection includes both the finished boundary index and
the grouping artifact.

Measured execution: segmentation about54m38s including deliberate quiet pauses,
boundaries15m32s, grouping8m45s; about79minutes overall. All segmentation,
boundary, grouping and runtime-affinity processes are confirmed closed. Exact
executed code archives and receipts are retained beside each derivative. Only
after closure, the future analyzer dispatcher was updated to choose separate
physical CPU cores automatically;16 focused tests pass and the actual host
selects masks[1,4]. That later source version did not produce this derivative.

Evidence under `research/graphene-segmentation-20261014/`:
`empirical-full-completion.json`, `empirical-full-comparison.json`,
`empirical-full-refinement-summary.json`, `empirical-full-pilot-equivalence.json`,
`empirical-full-boundary-pilot-equivalence.json`,
`empirical-before-gui-counts.json`, `empirical-after-gui-counts.json`,
`ui-empirical-transition.json`, `empirical-full-review-comparison.png`, and
`future-cpu-dispatch-validation.json`. The historical masks265/475 and old524
field JSON are missing, so the comparison explicitly excludes unsupported
matches there. The GUI transition, final224 Candidate/44,940 Uncertain galleries,
color trajectory overlay and accepted215 contour were visually verified with no
browser warnings or errors.

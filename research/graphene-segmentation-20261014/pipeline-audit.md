# Saved graphene segmentation: pipeline audit

October 14, 2026. Read-only code and documentation review; no hardware, raw-data modification, segmentation execution, or new accuracy measurement. The acquisition is paused for manual laboratory use. Code paths below are relative to `research/software-next-20261009/` unless stated otherwise.

## Recommendation

Use a standalone, sequential offline runner around `material_input.prepare_material_input`, `MaterialContrastClassifier`, and `material_terraces.segment_material_terraces`. Reuse the committed-field ledger and provenance conventions in `scripts/watch_graphene_scan.py`, but give this analysis a fresh, explicit versioned output directory. Point it directly at the original `scan-20261013-221727-131d9ced/chip001` manifest and TIFFs. Do not copy the approximately 30 GB chip, use the incomplete October 14 handoff copy, or invoke acquisition continuation.

The existing implementation already makes optical proposals, retains unknown regions, records exact analysis masks and uncertainty qualifications, and never assigns confirmed layers. A small runner is preferable to starting another material detector or transferring the older MoTe2 color model. Bootstrap is implementing `scripts/analyze_graphene_run.py`; this audit does not duplicate that work.

## Reusable infrastructure

| Path / API | Useful behavior and limits |
| --- | --- |
| `scripts/watch_graphene_scan.py`: `committed_fields`, `verify_source`, `input_context`, `prepare_model`, `analyze_field` | Enumerates manifest commits, pins request/source/model/geometry, hashes TIFF and required sidecars, processes one field at a time, commits field masks and JSON before a ledger entry, supports independent analysis STOP and restart. No hardware imports. Current output is fixed to `analysis-graphene-incremental`; source-version changes cannot reuse its old plan. |
| `flakefinder/material_input.py`: `prepare_material_input` | Reads uint16 raw TIFF, never JPEG photometry. Inspects shape before decode; verifies metadata/raw against the manifest; separates geometric eligibility from independently recorded substrate support. Fits background only from substrate support and retains unmeasured radiometry assumptions. Writes nothing. |
| `flakefinder/terrace_domain.py`: `ChipDomain.masks`, `normalize_supported` | Applies physical chip-edge margin, hash-verified substrate labels, spatially held-out background-fit selection. A provisional rectangle is explicitly `provisional_plan_unclosed`, not a measured enclosure. Substrate labels sample background rather than excluding differently colored objects from the domain. |
| `flakefinder/material_terraces.py`: `segment_material_terraces` | Connected noise-qualified parents, pixel-class terraces, holes and disconnected components, local interior contrast, patch-based covariance, useful inscribed rectangles, descriptive edge/texture evidence. Returns unknown features; size is a ranking sieve. A terrace is an optical population, not necessarily one physical flake or one layer. |
| `flakefinder/graphene_review.py`: `GrapheneReview.data` | Paged read-only source/region review, all per-field shards rather than only the top-1000 catalog, uncertain/rejected/pending views, source identity checks and small shard cache. Currently expects the fixed output folder inside the requested run and direct `chip*/manifest.json` files. A new output directory needs an explicit pinned analysis selector or a separate static review adapter. |
| `flakefinder/review.py`: `validate_reviews`, `serve_review` | Existing general reviewer supports keep/reject/uncertain decisions and manual flake/background/debris/uncertain ROIs with source hashes and pixel/stage geometry. Review writes are separate annotations. Its input schema is not the graphene shard schema; do not assume drop-in annotation support. |
| `flakefinder/material_contrast.py`: `fit_empirical_material_model` | Fits balanced local labeled populations with covariance, explicit optical context and held-out groups. Produces `labeled_local_population_fit_not_layer_validation`; groups, not random pixels of one field, form the validation split. |
| `scripts/analyze_material.py` | Existing offline `predict`, `fit`, `prepare-input`, `segment` commands, useful for controlled field/ROI experiments. Saved input and model hashes are checked. |

`scripts/select_material.py` and `material_selection.classify_saved_analysis` demonstrate reclassifying existing exact instance masks without replacing their geometry. `measure_existing_regions.py` provides local contrast measurement while preserving existing labels, holes and IDs. These are useful for later review-driven calibration, not a reason to replace graphene segmentation with the MoTe2 selector.

## This run's source limitations

The earlier recovery audit recorded 999 committed raw fields, 995 surviving boundary-label files, and 17 total missing small sidecars. It also verified the 3,979 surviving small sidecars. The parent separately verified all 999 raw hashes. Those are prior recovery results, not checks repeated by this audit. Evidence is retained under root `research/scan-recovery-20261014/` and `docs/SCAN-RECOVERY-20261014.md`.

The current material-input path needs metadata and boundary labels. Seven distinct fields therefore need explicit handling:

- Missing boundary labels: `tile_000025`, `tile_000116`, `tile_000845`, `tile_000859`.
- Missing raw metadata: `tile_000048`, `tile_000117`, `tile_000846`.

Initially mark these fields **unassessable: required source evidence unavailable**, while retaining their raw references and continuing other fields. Missing flake JSON or vendor JSON in other fields does not itself block this pipeline. Do not convert missing evidence into an empty successful segmentation.

`boundary_replay_recovery.py` can recompute the four missing label artifacts to their original exact digests; three surviving pinned vendor archives independently support the missing-metadata fields' registration and optics. That proof does not recreate original metadata timestamps or every camera-radiometry field required by `load_frame`/`prepare_material_input`. Reuse it only through an explicit in-memory analysis adapter preserving alternate-proof provenance. The minimal first pass can defer those seven fields without modifying their source directories.

## Memory, performance and durability

The watcher still enforces 4 GiB free physical RAM before a field and 2.5 GiB during a field, originally to leave space for acquisition. Root `docs/RUNBOOK.md` already flags these thresholds as obsolete. Its 240-second field budget and 6-million-pixel admission are explicit implementation settings, not scientific thresholds. A new offline policy should record measured allocation and processing time instead of silently inheriting acquisition-era reserves.

Process one raw field at a time with one CPU worker. Cache the immutable manifest/request once with pinned digests; stream raw hashing; release field arrays after commit. Only compact region summaries should accumulate. Downsampling after `prepare_material_input` reduces classification memory but **does not reduce full-resolution raw loading and background-normalization peak memory**. Benchmark that peak before claiming the whole pipeline fits a given allowance. Avoid loading every TIFF or every exact mask into the review process.

The proposed factor-two area-mean analysis is reasonable as a recorded exploratory resolution change, provided that:

- Valid and substrate support require all contributing pixels; any saturated contributor marks the reduced pixel saturated. Invalid values must not contaminate retained averages.
- Pixel size, area, width and source-to-analysis coordinates change consistently. Record the pixel-center affine, cropping/odd-dimension policy and effective resolution; simple polygon multiplication without center alignment can shift overlays.
- Reestimate covariance on reduced independently supported substrate samples. Do not divide covariance by four as though adjacent camera pixels were independent. The segmenter subsequently estimates smoothed substrate covariance itself.
- Keep exact reduced masks as the analysis result. Upsampling them for display does not make them original-resolution segmentation. Preserve raw source hashes and inspect promising or ambiguous candidates at full resolution before stronger conclusions.
- Keep original parent masks and historical review annotations separate. Historical MoTe2 work showed that replacing accepted object masks with whole-field plateau masks could erase known objects.

Commit per-field arrays/JSON and their hashes before a ledger entry; keep a source/model/config fingerprint so unchanged restarts reuse completed fields. Missing inputs, insufficient substrate, timeout, resource pause and algorithm error must remain different statuses. A budget-exhausted field needs a new explicit retry/version rather than being mistaken for a successfully empty field. A fresh output version also avoids overwriting any older graphene results.

Drive reads have previously returned transient EINVAL/ENOENT. Bounded retries should reread the same pinned bytes; a digest mismatch must fail rather than be retried into acceptance. This is an I/O consideration, not a license to relax provenance.

## Scientific interpretation and calibration

The watcher's default graphene model is a **conditional optical sensitivity ensemble** using the 90 nm graphene profile, manufacturer camera response, several oxide/illumination assumptions, unmeasured illumination spectra, assumed linear response and explicitly unmeasured zero dark offset. Finite scenarios are not confidence intervals or guaranteed bounds. Selecting the best-fitting scenario is permissive and can match contamination. Its extra 1% RGB model covariance is an engineering allowance, not a measured calibration error.

The input preparer explicitly reports background systematic covariance as unquantified. The segmenter accepts a separately supplied systematic covariance; if absent, its report retains that limitation. Do not describe Gaussian match distances, optical thickness-grid labels or patch-mean errors as calibrated graphene probability, layer identity or complete measurement uncertainty. Keep `layer_number=null`, `chemical_identity_confirmed=false`, and all unknown/ambiguous features available for review.

An empirical model answers a different question: whether a new optical population resembles same-context labeled examples. It can include debris and unknown references and flags ambiguous competing populations. It requires graphene-run visual controls and preferably independent confirmation, with whole fields/objects held out. Old MoTe2 positives, 285 nm oxide curves and camera-fixed negative controls are not portable graphene calibration. Even a local visual-positive fit remains an appearance model until material identity is independently established.

Existing documents explicitly record that older MoTe2 learned/empirical pipelines were not validated thin-material detectors; synthetic tests did not establish precision or recall. They also record camera-fixed false positives in the old plateau search. Reuse the infrastructure and lessons, not those labels or thresholds as graphene truth.

For a pilot, use the parent's raw/preview inspection to select a feature-bearing interior, a bare interior, a boundary/holder transition, debris or clipping, and a faint ambiguous field; add spatially separated manifest-index samples for coverage. No image-level quality ranking or candidate tile selection was performed in this audit. Missing-evidence tiles are useful failure-path tests, not negative optical controls. Review backgrounds and uncertain fields as well as the highest-ranked candidates. Cross-field overlap can duplicate a physical object, so per-field region counts must not be presented as unique flakes without an explicit deduplication step.

## Existing regression coverage and remaining validation

Relevant existing test modules are `test_graphene_watcher.py`, `test_graphene_review.py`, `test_material_profiles.py`, `test_material_selection.py`, and `test_review.py`. Their source covers mutation detection, idempotent ledger reuse, isolated STOP, low-memory exits, uncommitted memory aborts, explicit budget exhaustion, coordinate provenance, unknown controls, shard/path checks, grouped empirical fitting, and mask/holes preservation. This audit read those tests; it did not execute them or establish scientific accuracy.

The new runner should add focused checks for reduced-coordinate/area consistency, conservative reduced masks, recomputed noise, missing-field continuation, source mutation rejection, versioned restart and a memory-bounded real-field pilot. The final report should separate committed source fields, assessed fields, unassessable fields, proposed regions and manually reviewed outcomes.

Do **not** import or run `scripts/calibrate_local.py`: unlike the offline material commands, it connects and arms hardware at module top level and edits camera geometry configuration. Optical/material-model fitting is the separate offline `analyze_material.py fit` path.

# Operator-band graphene derivative — October 14, 2026

Source: saved `scan-20261013-221727-131d9ced/chip001`, all999 committed fields. No microscope calls or acquisition-state changes are made by this pipeline.

## Model and qualifications

The new `operator-empirical-model-v3/model.json` is pinned to the exact run optical profile, original request/manifest hashes and operator-observation JSON. The recorded point IDs were inferred from the conversation; they are not asserted to be independently verified labels or distinct flakes. The operator confirmed the three contrast bands and the four achromatic false examples.

The classifier projects pixels onto a continuous RGB direction estimated from the three supported groups. It retains full noise/substrate tests, a fixed empirical residual envelope, and the first-band green rule G≤0.6R+0.03 with an explicit0.003 engineering transition margin. Large field noise cannot widen the empirical corridor. The observed red interval has a disclosed0.01 engineering margin. Trajectory matches outside this interval remain extrapolated/unknown; other off-trajectory features also remain available. No layer numbers or chemical identity are assigned. Mixed boundary pixels can match the trajectory and are not evidence of a separate terrace.

New masks are actually classified pixel by pixel, connected components are measured, and all regions≥25µm² receive independent image-boundary refinement. The nearby-dark-feature diagnostic remains visible and affects review quality; it no longer vetoes a contour that independently passes edge, area-change and domain checks. Weak/unbounded contours still require review. Spatial deduplication follows refinement and retains all observations.

## Validation before full launch

- Eight real fields in `operator-empirical-pilot-v2`: all assessed, zero processing errors.
- All four reported second-band outliers are retained as negative optical evidence/unpromoted. The first-band gray patches660/167 and635/1537 are also no longer promoted.
- Confirmed example regions215/36,627/26,639/121 and547/176 remain candidates under new masks.
- Actual UI-proposed count in these fields:13 before,11 after. Three newly proposed observations meet the conservative ≥50% new-mask overlap criterion with formerly uncertain observations. A fourth tentative match overlaps47% and is excluded from this count. This is not an instance-recall estimate because connected regions can split/merge.
- Eight-field boundary pilot:426 regions, zero failures,13.9seconds,265MB peak RSS. Root independently inspected original/raw/new/refined contact sheets.
- Two-field-process pilot: all8 mask NPZ files byte-identical to serial; all region geometry/measurements exactly identical. Proof: `parallel-pilot-equivalence.json`.
-59 focused tests passed; an additional real two-process CLI smoke on a separate synthetic missing-input project completed with explicit unassessable/invalid-source records, not false negative science.

## Execution

`operator-empirical-full-v1` started about21:04 local. One parent commits the ledger and at most two field processes run, each pinned to one logicalCPU. No image arrays cross the process boundary. The measured531MiB worker peak establishes a1,062MiB combined working allowance plus25% uncertainty; available Windows commit/address capacity is checked at each batch. Low idle physical RAM alone is not a failure. Memory allocation failures remain uncommitted and can resume.

Sequence: all999 segmentation→all-useful boundary refinement→explicit selection→spatial grouping→final selection. Every prior selector is backed up byte-for-byte under the new output. Original analysis, raw images, sidecars and acquisition manifests remain unchanged. A dedicated `PAUSE` file can park between batches for quiet acquisition timing; it is unrelated to acquisition STOP/holds. The supervisor has a separate `PIPELINE_STOP` that forwards to the current offline stage.

Segmentation completed all999 fields:942 assessed,57 unassessable, zero failed/invalid-source/time-budget states. It took approximately54m38s wall time, including the deliberate quiet pauses. Boundary refinement completed in932.484s (15m32s):53,513 regions, zero failed/unavailable fields. Spatial grouping also completed and the final selected GUI was verified. The complete pipeline took about79minutes including quiet pauses.

## Additional exact boundary validation and CPU evidence

The current boundary policy was tested on the same eight fields:426 full region records and all8 mask archives were exactly equal between serial and two-process execution. Serial elapsed11.766s; two separate physical cores elapsed8.469s. Proof:`parallel-boundary-pilot-equivalence.json`. The full derivative has an explicit version-specific two-worker refinement execution policy; unrelated versions retain their defaults.

Windows topology identified logicalCPU0/1 as siblings on the same physical core. The frozen segmentation driver originally requested those CPUs. A separately recorded runtime supervisor verifies the exact worker identities and assigns CPU0 andCPU2 instead; no scientific code or numerical policy changes. `empirical-runtime-affinity.json` records this adjustment. Future analyzer topology wiring was deferred until all active source pins were released; the completed dispatch-only update is recorded below.

## Full scientific result checks

- All8 full segmentation mask archives are byte-identical to the qualified pilot; all5,074 corresponding region records are exactly equal. Proof:`empirical-full-pilot-equivalence.json`.
- All8 full boundary mask archives and426 region records match the updated pilot. The only omitted per-region comparison field is the independently pinned source-field JSON hash, which differs with execution provenance. Actual displayed polygons/status/quality/artifact metrics are equal. Proof:`empirical-full-boundary-pilot-equivalence.json`.
- Boundary states:36,937 accepted,15,047 review-required,1,529 unresolved;94 likely-artifact flags. Original masks remain available. The difficult269/32 example retains its original fragmented mask; not every outline is fixed.
- Before artifact hiding and spatial grouping, saved UI Candidate disposition rises from41 to233. There are115 new Candidate observations with at least50% new-mask overlap with a previously Uncertain observation. This is neither a distinct-flake count nor a ground-truth accuracy estimate.
- All4 operator-negative second-band examples remain available but are no longer Candidates. The checked first/second/third positive examples215/36,627/26,639/121 and547/176 remain Candidates. Recorded IDs were inferred from conversation; only the operator bands were explicitly confirmed.
- The new analysis records330,196 segments. The original ledger records310,165, but only309,825 can be read because the old524 field shard is absent. The shared readable-field comparison covers329,807 new segments. Changes include splitting and merging, not simply more physical flakes.
- Missing historical comparison inputs are explicit: old masks265/475 prevent overlap analysis there; old524 field JSON prevents an old/new comparison there. No historical evidence is recreated by this audit.
- Parent visually checked `empirical-full-review-comparison.png` using actual displayed polygons, confirming improvements on215/639/547 and the remaining269 fragmentation.

Full audit:`empirical-full-comparison.json`; boundary totals:`empirical-full-refinement-summary.json`.


## Final publication and closure

The pipeline reports `complete/review_ready`. Grouping indexed999 fields with no unavailable inputs:53,513 observations form45,248 groups, including7,481 multi-observation groups and8,265 reversibly combined repeats. Elapsed grouping524.79s. Final GUI defaults (area≥25µm², grouped, likely artifacts hidden): Candidates31→224, Uncertain45,642→44,940, All45,670→45,164. Both default and chip-specific selectors point to the new analysis, finished boundaries and final grouping. Parent verified the live galleries, color overlay,215 contour and automatic524 transition without browser errors.

All10 known worker/wrapper/supervisor PIDs were strictly checked absent, including the runtime-affinity helper. `empirical-full-completion.json` records this and final selector hashes. The exact executed analyzer remains in `pinned-segmentation-source.zip`; boundary/grouping archives separately preserve their pinned sources. After every process exited, future analyzer CPU dispatch was updated to choose separate physical cores within its allowed mask.16 focused tests pass; actual host selection is[1,4]. `future-cpu-dispatch-validation.json` explicitly distinguishes this future source from the completed execution. No science outputs were regenerated by that dispatch-only update.

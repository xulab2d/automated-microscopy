# Stationary capture experiments — October 14, 2026

Fresh user authorization covered an empty stage and controlled capture experiments. All historical campaign STOP/operator holds were preserved. Each experiment had its own request, PID/nonce-scoped authorization, deadline, independent STOP, and the normal exclusive hardware lease. No stage/focus/objective/illumination commands were issued. Current optics were 10x, 140 ms, lamp 90.01957%, XYZ 52022.998/9434.548/19074.218 um. This is timing/freshness evidence, not thin-flake image-quality qualification.

Scientific project: `G:/Shared drives/Xu Lab - Microscopy and Instruments/Automated Microscopy`.

## Measured paths

- `data/capture-benchmark-20261014-205438-f5339f6c`: proven backend baseline, three frames. Warm `capture()` including unload 5.945/5.958 s; retained-review guard 1.790/1.649 s. These times EXCLUDE subsequent managed TIFF persistence. Cold first capture 73.979 s, dominated by 67.766 s review I/O.
- `data/capture-benchmark-20261014-210449-3f104004`: four persistent experimental requests. Warm request to local retained CZI 0.684–0.718 s; AcquireImage 0.550–0.609 s; CZI save 0.072–0.084 s. First request includes optional display-TIFF export. Job load 0.185 s; ready including load 0.726 s. These times EXCLUDE managed scientific persistence.
- `data/capture-benchmark-20261014-211039-257869e5`: four persistent requests with CZI decoding and production `save_frame`, fsync of TIFF/JSON, full TIFF reread/pixel equality, and raw/file SHA. Full interval 2.968/2.808/2.456/2.667 s. CZI decode 0.051–0.078 s; managed save/verify 1.500–1.812 s. Originals and copies remain retained.
- `data/capture-benchmark-20261014-211108-65885a1e`: preliminary locked-SHA trial, warm full managed raw+ack 8.567/7.053 s, capture-only 6.675/5.375 s, review guard 1.073/0.957 s. A fallback edit occurred after Python module import; exact request-pinned executed source and an explicit audit note are archived. Use the subsequent frozen paired experiment for qualification.

## Raw context and pitfalls

Four first persistent CZI files decode to 2056x2464x3 uint16, max4095, with distinct raw SHA and increasing creation timestamps. Camera metadata records exposure140ms, white balance0.155781499789/0.715557325164/1.15834443839, analog gain mode0 (1x), AutoExposure false, CameraGamma1. RGB medians approximately918/1844/2040 agree with the proven baseline920/1843/2040. Metadata also records AxioCamHDR false and AxioCamNativeHDR false, while EasyHDR true appears elsewhere and needs semantic disambiguation. GetAllParameterNames still returns empty, so pre-request live parameter bindings remain unresolved.

**ZEN Application.Save(image, .tiff) produces an 8-bit display export. It is not a scientific raw conversion.** The later experiment instead decodes the retained CZI with czifile2026.10.14, which converts BGR to RGB, and saves/verifies uint16 through the existing scientific writer. Do not use the optional display TIFF for analysis.

The persistent macro completes each correlated request and closes its image after client acknowledgement, but the enclosing vendor job finishes ABORTED with 'Job execution has failed' (no registered workflow result). This is explicitly recorded rather than called production success. A production backend needs an independent request lifecycle/commit/recovery protocol, verified optical readbacks, cancellation tests and retained-output accounting before deployment. No persistent production backend was enabled.

## Reproducible harness

`live_probe.py` performs scoped stationary readbacks/inventory. `stationary_benchmark.py` compares the existing backend and optionally locked review hashes. `persistent_benchmark.py` drives the separately registered `FlakeFinder Experimental Stationary Snap 20261014` job. Source and built macro are in `persistent/`; original prototype files were untouched. Each session retains transport files and managed copies. The scripts are explicitly hardware-active entry points and must not be imported as tests. The persistent helper currently uses an explicit140ms profile and four requests; it never changes exposure. Runtime dependency: czifile2026.10.14.

Cold Drive hydration and transient host load vary widely; report phase boundaries and warm paired results, not sensor latency alone. Reduced duplicate XY/Z settle waits remain a separate unvalidated motion optimization and were not changed here.

## Frozen paired verification and enabled conservative cache

`data/capture-benchmark-20261014-211335-2d6f6330` alternated six captures in one process with unchanged settings/source pins. Excluding first-read initialization, baseline capture-only was5.983/5.062s and complete managed raw+ack8.126/7.056s. Cached capture-only7.664/4.716s and managed10.058/6.728s. One cached capture had a2.200s vendor completion tail. **This small sample does not demonstrate a robust end-to-end camera speedup from caching.** No timings were discarded as successes/failures; initialization and warm samples are identified separately in receipts.

The enabled production change reuses SHA and at most4MiB of already-hashed small JSON/metadata bytes only while Windows read handles deny both writes and deletion. File IDs, sizes, modification timestamps, path identity and non-redirection are rechecked. The full original retained-review validator, current review receipts, inventory membership and ownership/semantic checks still execute everycapture. Unsupported locks/file identities fall back to ordinary reads. At most128 files are held; all handles release in adapter.close. Existing pending limits and unknown-job accounting are unchanged. This is not a full-review-result cache or a raw acknowledgement.

Real offline alternating full-review timings after initialvalidation: ordinary1.471/1.627/2.224s, cached1.246/1.591/0.857s. All seven calls returned exactly3 pending unknown jobs and4 reviewed retained jobs.55 files were locked and448,612 bytes of small text retained. Improvement is modest and variable; evidence is `cache-comparison.json`. The baseline cold-read delay remains on firstuse. G: scratch verification denied both overwrite and deletion while locked; NTFS tests do the same. Production changes: `flakefinder/locked_hashes.py`, `vendor_lifecycle.py`, `vendor_backlog.py`, and cache release in`zen.py`.65 focused regression tests pass, including unsupported-lock fallback, immutablebytes, release, lazy initialization, receipt/inventory mutation rejection and original vendor retention tests.

Final closure/readback is `data/capture-benchmark-20261014-211621-92092b86/scan-status.json`: hardwareclosed, sameXYZ/objective/lamp, camera140ms; no priorjob running/loaded, inventory-only job unloaded. The stage and focus never moved. No further hardware interruption is needed for this change.

## Offline persistence comparison (no hardware, no acquisition changes)

`save_benchmark.py` used one exact verified 140 ms raw frame from
`capture-benchmark-20261014-211039-257869e5/frames/frame_004.tiff`.
Three alternating C:/G: pairs used the unchanged production `save_frame`, then
TIFF/JSON fsync, full reread, exact pixel/metadata comparison, pixel SHA and file SHA.
One CPU (mask4), below-normal priority; the two segmentation workers continued.
New outputs only: `data/save-benchmark-20261014-212742-b446e579` on the shared
project and the identically named local Temp folder. All eight roundtrips were
pixel-exact; all six default-compression TIFFs have identical file SHA. Source raw
and metadata hashes remained unchanged. All benchmark files are retained.

| Verified persistence | Local C: | Drive G: |
| --- | ---: | ---: |
| Default LZW, three samples | 0.602 / 0.702 / 1.080 s | 1.658 / 1.949 / 1.891 s |
| Default median | 0.702 s | 1.891 s |
| Median raw encoding + write | 0.311 s | 1.206 s |
| Uncompressed, one supplementary sample | 0.261 s | 1.281 s |

The paired Drive penalty was 0.811–1.247 s. Most of it occurs inside the raw
writer, with identical encoding/bytes; small-file/provider overhead also grows.
Pixel comparison/hash costs roughly0.06–0.11 s and file SHA0.02–0.10 s here, so
hashing alone does not explain the original1.5–1.8 s persistence interval.
This implicates the Drive-mounted write/provider path, not necessarily network
upload: provider fsync is not evidence of completed remote synchronization.
Concurrent segmentation and always-C-then-G ordering limit precision.

The cheap uncompressed TIFF trial remained pixel-exact and was faster, but grew
from22,783,866 to30,408,398 bytes (+33.5%). This is one supplementary pair, not a
production recommendation. A future local spool/copy commit protocol would need
crash recovery and explicit durable shared-copy acknowledgement; changing raw
compression would need representative compression/time/storage tests. Neither
production destination nor compression/protocol was changed.

Machine-readable evidence: `save-benchmark-summary.json`,
`save-benchmark-latest.json`, and the shared folder's pinned `plan.json` and
`results.json`. No PAUSE, STOP, hold, microscope state, or scientific source was
modified by this offline experiment.


Cache compatibility review: all original retained-review builders already reject
symlink/reparse/redirected paths through `vendor_backlog.direct`; that policy is
unchanged. Initial cache-specific identity failures now release the new handle
and use ordinary baseline hashing, avoiding new provider-compatibility gates.
Changes during hashing or after an entry was cached still fail without fallback.
The focused suite passes65 tests, including initial fallback, original-rejection
propagation, and cached/mid-hash mutation rejection. No hardware was used.

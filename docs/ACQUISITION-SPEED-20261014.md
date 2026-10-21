# Acquisition timing and conservative I/O improvements

October 14, 2026. Acquisition remains paused for manual takeover. This work used
saved evidence and synthetic tests only; it does not authorize a hardware start.

## Measured baseline

Managed project root:
`G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy`.
Exact evidence relative to that root:

- `data/scan-20261013-221727-131d9ced/chip001/manifest.json`
- `data/scan-20261013-221727-131d9ced/chip001/tiles/tile_000000.json`
  through `tile_000998.json`: timing sample indices 0,50,100,...,950,998.
- `data/scan-20261013-221727-131d9ced/configuration.json`
- `data/motion.jsonl`, restricted to Unix times 1791955210 through 1791964121.

The first and last raster frame timestamps are 1791955210.463381 and
1791964120.2687178. Their difference is 8909.8053 seconds, or 148.50 minutes
over 998 intervals: 8.92766 seconds per field. All 999 committed fields report
`measured_rectangle_plane_prediction`. The one recorded local-focus attempt
belongs to the preceding failed invocation, not this successful raster.

The 21 evenly spaced frame receipts report these mean times:

| Component | Seconds |
| --- | ---: |
| Capture including cleanup | 4.400 |
| Job-load interval, including pre-capture guards | 1.865 |
| Resource setup | 0.008 |
| Subscription wait | 0.100 |
| Request to first image | 1.116 |
| Image decode | 0.042 |
| Completion after image | 1.241 |
| Cleanup | 0.003 |

These components are partly nested; do not sum every row. `job_load` starts
before backlog, storage, memory and optics validation: it is not a measurement
of the LoadJob RPC alone. Exposure was 200 ms, settling 0.5 seconds, backlash
approach 30 micrometres, and processing overlap disabled.

The same interval contains 2,464 paired motion request/settled records:
1,430 X moves totaling 1430.3 s, 36 Y moves totaling 36.4 s, and 998 Z moves
totaling 906.7 s. Total motion is 2373.49 s (39.56 minutes), approximately
2.38 seconds per field. Combining this complete motion sample with the camera
sample leaves approximately 2.15 seconds per field for persistence, processing,
callbacks and other unsplit costs. This is an estimate, not a measured phase.

Sixteen chips of comparable raster size would require about 40 hours of raster
time at this rate, excluding search, focus, failures and segmentation. The UI
must estimate coverage from the requested area and measured rate rather than
promise that an arbitrary campaign will finish overnight.

## Implemented changes

Production source paths relative to this repository:

- `research/software-next-20261009/flakefinder/full_chip.py`
- `research/software-next-20261009/flakefinder/model.py`
- `research/software-next-20261009/tests/test_full_chip_checkpoints.py`

Every tile still has a complete atomic, flushed manifest commit before its
progress callback. Only this manifest opts into compact JSON encoding; JSON
values, raw hashes, focus provenance and all validation remain unchanged.
The shared atomic writer retains its temporary-file, fsync and atomic-replace
protocol, with pretty formatting remaining the default for other callers.

Read-only serialization of the actual 999-field manifest, five repeats on this
host, measured 5,197,296 bytes and a median 0.09021 seconds for the old pretty
encoding versus 3,438,797 bytes and 0.02595 seconds compact: 33.8% fewer bytes
and about 71% less serialization time for that final-size snapshot. This is
not an end-to-end disk or acquisition benchmark. New phase diagnostics add
some bytes to future manifests.

Derived overview exports now occur every 25 committed field indices instead
of every 10, plus the mandatory successful final export. A 999-field run thus
performs 40 rather than 100 overview exports, avoiding 60 full overview
rebuilds/writes. Every frame still contributes to the in-memory mosaic and the
final mosaic is numerically identical. Progress remains per field; the live
overview may lag by up to 24 newly acquired fields. On STOP/error, durable raw
and coverage evidence retain the existing finalization behavior; overview
generation is skipped and `derived_outputs_pending` identifies deferred work.

Successful finalization no longer recomputes/writes the same coverage map and
enclosure twice. The dirty flag is set whenever a new or replayed boundary
label contributes, and cleared only after successful coverage computation.

Each completed row now includes `acquisition_phases_s`: XY travel, focus
prediction/Z movement (including any exceptional focus recovery), capture and
validation, raw/preview persistence, vendor acknowledgement, boundary/sidecar,
hashes/backup, mosaic addition, manifest commit, overview export, callback and
total tile time. These monotonic diagnostics are outside raw acceptance
evidence. Post-commit timing values become durable in the next tile commit or
final save; a sudden process loss can omit those last diagnostic values without
losing the tile commit.

No camera overlap, exposure, motion, focus uncertainty, settling or checksum
policy changed. Full manifest rebuilding cadence remains per tile deliberately:
existing UI, recovery and campaign readers rely on that durable complete view.
Replacing it with a journal requires a separate reader/recovery migration.

## Validation and remaining opportunities

Focused offline tests cover compact/pretty value equivalence, refusal of NaN
without replacing a prior checkpoint, per-field durable commit before callback,
fewer overview exports, exact final mosaic and boundary-label equivalence,
one terminal coverage calculation, timing availability, and STOP/resume before
the first scheduled overview without rewriting committed raw fields.

Command from `research/software-next-20261009`:

```powershell
../../.venv/Scripts/python.exe -m pytest tests/test_full_chip_checkpoints.py tests/test_full_chip.py tests/test_full_chip_retention.py tests/test_full_chip_resume_source.py -q
```

Result: 20 tests passed across the focused acquisition/checkpoint and
retention/source-resume suites. No hardware was used.

The saved 1.24-second camera-completion tail may permit useful pure CPU overlap
through the existing `capture_with_processing` API, but that API requires
profile-specific acceptance and must retain completion/unload/persistence
fences. It is not enabled here. Consolidating optical dwell at the final XYZ
could remove a redundant 0.5-second dwell per field, but requires explicit motion
qualification; it is not enabled here. Never trade away raw reread/hash checks
or vendor ownership proofs for throughput. New phase timings should identify
which remaining costs warrant the next optimization.


## October14 live empty-stage capture experiment

Detailed receipts/harness: `research/capture-speed-20261014/README.md`.
Fresh scoped user authorization was used without removing historical holds.
Stationary10x/140ms persistent requests delivered localrawCZI in0.68â€“0.72s warm;
request through managed16-bitraw save/fsync/fullreadback/hash measured2.46â€“2.97s.
The standard path's comparable managedraw interval measured7.05â€“8.57s in oneblock
(and7.06â€“8.13s on warm baselineframes in the later alternatingblock).
These are small timing samples on an empty stage, not20xflakequality qualification.
ZEN's direct TIFF export was found to be8-bitdisplay data; rawCZI decoding is required.
The persistent macro's enclosingvendorjob still endsABORTED despite successful
per-request images. Therefore that backend remains experimental, with fullraw
retained, and was not deployed for acquisition.

A conservative production speedup now caches only OS-locked verifiedSHA/smalltext
bytes within the adapter lifetime. All existing review semantics and inventories
are revalidated eachcapture. Windows deny-write/delete locks and fileidentity
checks bind bytes; unsupportedlocks fall back to uncachedreads. Cache memory is
bounded4MiB and128files; adapterclose releasesallhandles. Actual fullreview offline
pairs were1.471â†’1.246s,1.627â†’1.591s,2.224â†’0.857s. The livepairedtotal was noisy and
does not establish a netcapturethroughput gain.65 targetedtests pass. No motion
settling or camera exposure defaults were changed. Fullcomparison definitions,
source-audit caveat for the preliminary run, and finalclosure are in the research
README. Currentraw/STOP/holdfiles remain preserved.


Offline identical-frame persistence follow-up: three alternating pairs gave median
0.702s on local C: versus1.891s on the G: Drive mount, including production
raw/preview/metadata writing, fsync, exact reread and hashes. The paired Drive
penalty was0.811–1.247s, mainly inside the raw write, not pixel hashing. All
roundtrips were exact and source hashes unchanged. One uncompressed supplementary
pair gave0.261s C:/1.281s G: but increased bytes33.5%; no compression or
production destination change was made. Two segmentation workers remained active,
and provider fsync does not establish remote-upload completion. Full evidence is
`research/capture-speed-20261014/save-benchmark-summary.json` and its README.


Cache compatibility review: all original retained-review builders already reject
symlink/reparse/redirected paths through `vendor_backlog.direct`; that policy is
unchanged. Initial cache-specific identity failures now release the new handle
and use ordinary baseline hashing, avoiding new provider-compatibility gates.
Changes during hashing or after an entry was cached still fail without fallback.
The focused suite passes65 tests, including initial fallback, original-rejection
propagation, and cached/mid-hash mutation rejection. No hardware was used.

## October 15: format comparison and 1,000-image estimate

An offline comparison used the actual saved tile215, three local trials per
format, with encode/write, fsync, reread and pixel comparison. Source data and
production settings were unchanged. Medians (seconds; decimal MB):

| Format | Write + fsync | Including reread/check | Size |
| --- | ---: | ---: | ---: |
| Current 16-bit LZW TIFF | 0.406 | 0.532 | 20.13 MB |
| Uncompressed 16-bit TIFF | 0.116 | 0.159 | 30.41 MB |
| 16-bit PNG, compression 1 | 0.727 | 0.940 | 18.45 MB |
| 16-bit PNG, compression 3 | 1.214 | 1.536 | 18.03 MB |
| JPEG quality95, 4:4:4 | 0.033 | 0.103 | 1.30 MB |

All TIFF/PNG pixels round-tripped exactly. JPEG first used a linear conversion
from16-bit to8-bit based on this image's maximum; conversion time is excluded.
JPEG did not preserve those8-bit pixels exactly either. This is a timing/size
comparison, not a segmentation-accuracy validation or a proposed conversion
calibration. Metadata, previews, hashes, capture and Drive transfer are outside
these new timings. Evidence: `research/capture-speed-20261014/format-comparison-20261015/results.json`.

The current TIFF is compressed. Compression adds CPU cost: uncompressed TIFF
was faster, while PNG saved few bytes and took longer. JPEG was smaller and
faster but changes measured colors; it has not been substituted for raw science
input. A local uncompressed spool can avoid most encoding overhead without
discarding pixel precision; durable transfer/recovery still needs implementation.

Planning estimate for1,000 raster images using the experimental persistent path:
2.46–2.97s capture/verified persistence plus the old scan's2.38s mean XY/Z motion
gives81–89min before other scan-loop work. Allowing roughly0.5–1.0s/field of
unmeasured callbacks/checkpoints/processing and rounding gives **90–110min**.
This is an estimate, not a measured moving-stage run. The camera experiment
used140ms at10x versus the prior scan's200ms at20x; matching exposure alone adds
about1min over1,000 images. Other optical differences remain unqualified.
Local staging might reduce this to roughly **70–90min**, based on the separate
~1.19s/field Drive-versus-local median difference; combining those independent
benchmarks is provisional. Neither range includes chip discovery, focus-plane
setup, offline segmentation or completed Drive upload. Persistent-job lifecycle
qualification and local staging remain undeployed.

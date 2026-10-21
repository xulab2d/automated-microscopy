# Completed-chip transition recovery — October 14, 2026

Read `<project>/scan-control/current.json` and its `scan-status.json` for live
state. Production is `research/software-next-20261009`; isolated repair is
`research/software-transition-20261014`. Evidence and deployment records are
under `research/scan-recovery-20261014`.

## Failure and retained work

Invocation `scan-20261013-221727-131d9ced` completed all 999 planned chip001
tiles, then failed at 00:48 October14 while preparing discovery continuation.
The exception was a missing `boundary-labels/tile_000025.npz`. Hardware closed
and the acquisition worker exited. Batch state is
`chip_complete_discovery_pending`; do not rescan chip001 or repeat its focus.

The completed chip records `planned_area_acquired=true` and
`focus_coverage_complete=true`. `complete_with_boundary_limits` means that its
planned raster completed but a fully enclosed boundary was not established by
the raster labels. The four-side rectangle remains useful search evidence.

The narrow inventory found all 999 raw TIFF paths and 17 missing, previously
hash-pinned sidecars:

- Boundary labels: 25, 116, 845, 859.
- Frame metadata: 48, 117, 846.
- Flake placeholders: 23, 30, 116, 845, 859.
- Vendor archives: 30, 116, 388, 845, 859.

All 3,979 present small sidecars match their recorded hashes. The narrow replay
first verified the seven affected raw images; the subsequent handoff integrity
pass reread and verified **all 999 raw TIFFs** against their original manifest
hashes. All four
missing label arrays reproduce the exact original compressed-file hashes.
The missing metadata files have no copy embedded in their TIFFs. Their surviving
hash-pinned vendor archives independently retain stage XYZ, optics, raw TIFF
identity and the original metadata digest. Registration can be checked against
those archives without inventing the missing original JSON bytes.

The disappearance mechanism is not established. Existing writers fsync JSON
before atomic replacement, and commit tile hashes only after writing artifacts.
No relevant storage deletion or disk error was established by the narrow log
review. Do not describe Google Drive, RAM pressure or antivirus as a proven cause.

## Memory

Initial availability was approximately 0.5 GiB on this 16 GiB computer, with
approximately 26 GiB of commit capacity remaining. The failure was
`FileNotFoundError`, not an out-of-memory exception. ZEN's last recorded capture
had completed and unloaded; no document leak was established.

The rectangle handoff branch returned before launching the memory supervisor.
The isolated fix launches that existing supervisor on both normal and handoff
paths, after state becomes running, and retains the original session deadline.

A one-time identity/SID/renderer-checked working-set trim kept applications and
documents open. Available RAM changed from 1.26 to 1.49 GiB during that action;
availability had already risen since the initial observation. It reclaims
resident pages temporarily, not committed application allocations. See
`renderer-reclamation.json`. No ZEN process was trimmed, closed or restarted.

## Recovery policy

Preserve original manifests, raw files, hashes, missing-artifact evidence,
placement and deadline. Rebuild labels only when their bytes match the original
digest; validate missing registration from pinned independent evidence and
require replayed aggregate coverage to match the saved coverage map. Future
tiles receive a small separately stored backup of sidecar bytes, without raw
TIFF duplication. Existing changed files are errors, not repair targets.

## Validation and deployment

All 138 combined recovery, sidecar, worker, full-chip, campaign, handoff, grid,
deadline and memory regressions passed. Real-data replay verified registration
for all 999 fields and reproduced the saved aggregate coverage exactly; see
`research/scan-recovery-20261014/coverage-replay.json`.

Production deployment is recorded in `deployment.json`, with previous source
under `production-before`. The worker was closed and absent, and its entire
source fingerprint matched before deployment. Source is frozen for acquisition.

Sidecar ZIPs use content-addressed filenames. A STOP/crash after bundle creation
but before manifest commit leaves an orphan intact; a later recapture can pin
its distinct bundle without collision. This interruption/retry was tested in
the actual full-chip runner. Bundles provide redundancy on the same filesystem,
not proof of remote synchronization or independent backup.

The continuation's ID, retained deadline and checks are saved in
`handoff-plan.json`; read live status before claiming it is acquiring.
Invocation `scan-20261014-090615-88fb3166` started at 09:09 October14. Worker
PID15812 first entered `validating_handoff_files`; no new acquisition was
claimed at that checkpoint. The original absolute deadline is retained.

A further prelaunch review found that a 20 GiB completed-chip transfer could
trip the old 180-second no-progress timer. `deployment-progress.json` records
the tested follow-up: hash/copy loops check STOP and the original deadline
between 4 MiB blocks; only completed-file work updates watchdog progress.
Persisted status is throttled to once per second. Verification remains before
hardware connection, with watchdog coverage during that work. All 46 focused
worker/handoff/memory-capacity tests passed after this change. No timeout was
extended, and contract hashes are unchanged by the optional progress callbacks.

Remaining startup cost: this legacy handoff pins and rechecks every scientific
file, then copies the completed chip into a new invocation. Chip001 alone is
19.88 GiB of TIFFs. On this Drive mount, each full pass takes minutes. Progress
is now observable/cancellable, but the repeated reads and copy are still a
performance bottleneck. A future immutable shared-artifact design could avoid
duplication; do not simply omit integrity checks or mutate the completed donor.

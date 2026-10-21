# Interrupted handoff recovery — October 14, 2026

Latest reported failure: `scan-20261014-090615-88fb3166` ended at 09:41 local,
before another chip was acquired. Its worker closed hardware and exited.
`recovery-needed.json` identifies `OSError: [Errno 22] Invalid argument` in
`rectangle_handoff._copy_file`, specifically the source `reader.read()`.
The last verified copy was tile917's metadata. The next destination TIFF is
zero bytes. The original tile917 TIFF subsequently reads successfully and
matches its original manifest hash. This is a filesystem read failure during
recovery, not an autofocus failure or an out-of-memory exception.

The original complete scientific donor remains
`scan-20261013-221727-131d9ced`. Its 999 raw TIFFs were hash-verified in the prior
recovery. The interrupted destination is not a complete scientific donor and
must not become the source of a new acquisition. Neither directory is deleted.

The memory supervisor separately exited at 09:21 when `scan-status.json` was
temporarily unavailable. Its last `monitoring` record is stale, not proof of a
live supervisor. The fix retries brief read failures and continues its monitor
loop if status/report storage is temporarily unavailable. Cleanup is suspended
until fresh run identity is available; actual worker exit and the deadline still
terminate it. Twenty-six focused memory tests passed.

Implementation is isolated in `research/software-resume-20261014`; diagnostics,
deployment and runtime evidence are in `research/scan-resume-20261014`.
The continuation repair references completed chips read-only rather than copying
their raw images into every new invocation. Newly acquired chips retain their
own writable folders. Exact source evidence and the original deadline remain
part of the continuation contract. Consult deployment and live evidence before
claiming the new continuation has started.

The combined completed-source, boundary-recovery, handoff, worker, campaign,
grid/deadline and memory suite passed 145 tests on the final frozen source. Handoff reads now retry
transient EINVAL/ENOENT errors, preserving STOP/deadline checks and verifying
the final bytes against their pinned hash. A retry never earns watchdog
progress merely by waiting. Referenced completed chips are explicitly rejected
as acquisition resume targets. The console's existing batch-count fallback
retains their saved image counts; original image files remain in the original
run and its catalog entry.

Coverage replay now checks cancellation and the retained deadline between
fields, reports actual completed fields at most once per second, and emits the
final field. Small sidecars are read once, hashed, and decoded in memory; ZIP
parsing no longer performs repeated small reads directly against Drive. The
same verified bytes are parsed. Transient read retries occur before adding a
field to the coverage map, so a retry cannot double-count coverage or invent
progress. Hash mismatches remain errors.

Final real-data replay verified all 999 fields and reproduced the aggregate
coverage exactly in 380.6 seconds. `coverage-replay.json` distinguishes the real
original-data replay from synthetic new-invocation resolver tests; the latter
verify source pinning, read-only behavior and missing-label replay. The source
mapping itself reads no raw TIFFs. Production deployment is pinned in
`deployment.json`, with prior source retained under `production-before`.

Invocation `scan-20261014-105506-f3cb07a9` launched at 10:55 October14 from the
original complete donor. Its contract carries 362 pending discovery/control
records and a read-only source for chip001, with the original 21:20 deadline.
Read live status for subsequent acquisition; launch alone is not scan progress.

This invocation exited at 10:55:45 before copy or scan motion: live objective
verification failed. Direct read-only MTB diagnosis reports coded nosepiece
position 6 (empty); the required 20x is position 2. ZEN was displaying Free Mode
and rejected API Stop commands with PERMISSION_DENIED. Worker hardware is closed
and PID19548 is absent; the repaired memory guard exited with its owner.
The user was asked to seat 20x and confirm the chips have not moved. Do not bypass
the objective check or alter its label. Evidence: `live-optics.json` and
`read_optics.py` in the recovery evidence directory. A further immutable run is
needed after physical optics and API mode are restored.

At 11:00:46 ZEN was returned to API Mode through its Home screen. Its visible
console confirmed hardware initialization CompletedSuccessfully and API mode
control allowed: True. No scan was started; physical 20x engagement and unchanged
chip placement still require user response and subsequent objective readback.

# Microscopy repository orientation

Read `docs/RUNBOOK.md` first. It is the current entry point, updated October 13,
2026 (transition recovery updated October 14). Read narrow evidence only when needed; do not reload entire old chats or
the large research tree for every status check.

- Production Python/UI: `research/software-next-20261009/flakefinder/`.
  The root `flakefinder/` is an older implementation, not the current scan UI.
- Python: root `.venv/Scripts/python.exe`; run production tests with cwd
  `research/software-next-20261009` so imports resolve to that release.
- Start UI: desktop **FlakeFinder** or root `Open FlakeFinder.cmd` ->
  `scripts/launch_flakefinder.py` -> `/` (scan and review home).
  Existing `Launch Microscope Console.cmd` ->
  `scripts/launch_scan_console.py` -> production `flakefinder.suite_server`.
  UI: `http://127.0.0.1:8770/`. Opening it does not connect hardware.
- Saved-result entry: Home -> Review runs (`/review`). Legacy launcher aliases
  also open the consolidated home; historical MoTe2 workbench is `/library`.
  The run library lists selected graphene/hBN analyses and the curated MoTe2 library.
  It starts only the viewer if needed, never acquisition.
- New operator workflow, scattered discovery and durable scan-to-review queue:
  `docs/OPERATOR-WORKFLOW-20261014.md`. Automatic analysis is opt-in per new
  request, graphene90 only, and starts after hardware ownership closes. Existing
  run requests are not retroactively queued. Offline jobs recover separately
  from acquisition. Speed evidence: `docs/ACQUISITION-SPEED-20261014.md`.
- Scientific project root: `configs/storage.json`; current run pointer:
  `<project>/scan-control/current.json`. Never infer current state from old PIDs.
- Latest October14 capture study used an empty stage. All old chip placements
  and deadlines are historical. A fresh placement/session was established
  October13 after the chips moved; that history does not establish current XY
  locations or authorize resuming a scan.
- Latest review and backups: `research/scan-readiness-20261013/`.
  Historical handoff `research/NEW_CHAT_HANDOFF.txt` predates later October 11
  recovery; it is not the current runbook or current authorization.
- One acquisition owner. Verify worker closure/absence before production edits.
  Latest October14 instruction authorizes controlled microscope speed experiments
  with no chips on stage. Use a fresh, scoped experiment authorization and the
  hardware lease; preserve previous STOP/hold records and do not resume a scan.
  Stationary captures first; record and restore optics. This supersedes the
  earlier no-hardware restriction for these experiments only.
  Earlier October14 instruction: manual lab takeover; scan acquisition explicitly
  paused. Continue graphene segmentation of saved221727 chip001.
  Do not resume prior scan plans without a new user start request.
  Preserve raw data, source/request hashes, STOP and operator-hold files. A
  viewer process is not an acquisition worker. Never start hardware on import.
- Graphene review: `docs/GRAPHENE-SEGMENTATION-20261014.md` records the current
  analysis, refinement/artifact flags, full-region RGB plot and spatial grouping.
  The original221727 run owns the images;105506 is a failed continuation.
  GUI selection sidecars choose versioned offline outputs; do not overwrite masks.
- October16 hBN: `docs/HBN-SEGMENTATION-20261016.md`. User accepted partial
  `scan-20261015-223549-98fae566` (1,780 tiles) after a ZEISS MoveTo failure and
  requested segmentation instead of acquisition. Keep hardware stopped. Separate
  hBN model/namespace/selectors. The old morphology-only shortlist failed (bulk
  flakes); `hbn-partial-full-v3-controller` is STOPPED after 128 fields. Do not
  resume it. New work separates image geometry from thin/bulk and transparent-film
  evidence; exact operator examples and current selection are in the hBN document.
- October15 revisit/thickness: `docs/REVISIT-20261015.md` and
  `docs/GRAPHENE-THICKNESS-20261015.md`. Revisit uses a separate explicit operator
  session and worker, never the paused scan's authority. Review GETs never connect.
  After reloading, source/current point correspondences establish a new rigid XY
  transform; old focus planes are not reused. Current implementation tests are
  simulated; no chip has been physically relocated in this task.
- User wants low token overhead: no periodic agent spawning, repeated whole
  repository audits, or automatic minute-by-minute model supervision. Use
  ordinary process/file telemetry; schedule model followups only when requested.
- Use small relevant regression suites for acquisition changes. Live experiments
  require explicit user authorization (provided for the current speed study).
  No scope expansion into unrelated detector research.

The repository currently reports most source files as untracked. Do not use
`git clean`, reset, or replace entire source folders. Preserve existing work.

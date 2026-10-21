# Consolidated FlakeFinder workflow

Updated October 14, 2026. This upgrade is developed and tested without touching
the microscope. The manual takeover hold remains in force until a person starts
a fresh run through the UI. A browser refresh never resumes acquisition.

## Entry points

The desktop **FlakeFinder** shortcut and root `Open FlakeFinder.cmd` open `/`.
The home page has **Start a new scan** and **Review your flakes**. `/scan` remains
the acquisition console, `/review` lists saved analyses, and `/library` contains
the historical MoTe2 workbench that previously occupied `/`. The old desktop
review shortcut is backed up under `research/scan-console-runtime/shortcut-backup`.
The unrelated Xu Lab Microscope shared-folder shortcut is unchanged.

## Operator contract

The standard form asks for operator name, scan name, material, substrate/oxide,
chip count, minimum chip width and scattered/grid arrangement. Grid dimensions
and approximate spacing are shown only for grid mode. Names are saved with the
request and acquisition context. Chip count is distinct from the advanced
chip-attempt budget. Existing requests default to legacy discovery and do not
silently opt into automatic postprocessing.

Advanced settings retain search dimensions, optics, focus travel and resource
limits. This station's default search request is 100 by 100 mm about the fresh
starting stage position, clipped to live controller travel, matching the prior
run request. It is not a measured holder boundary. The actual requested travel
is displayed next to readiness confirmation; narrow it for a smaller holder.
The operator must select 20x and confirm clearance. Mechanical loading and
objective selection cannot be automated by this software. Controller readback,
objective checks, ownership, STOP and current hold checks remain enforced.

Start performs preflight, saves an immutable request and launches the acquisition
owner. Automatic first-reference discovery, autofocus, measured edge geometry,
reuse of successful edge-focus samples, raster acquisition and downstream
processing do not require agent calls. Closing the browser does not stop them.

Automatic segmentation is currently supported for nominal graphene on 90 nm
SiO2/Si. Other supported acquisition materials can be scanned by explicitly
turning off automatic analysis; the form does not promise an unconfigured model.
The existing MoTe2 historical result collection remains accessible. Optical
classification is conditional, not proof of material identity or layer count.

## Scattered discovery

`scattered_search.py` orders the existing finite seed lattice by recursive
spatial bisection, visiting coarse spatial subdivisions early and filling gaps
later. At each step, a bounded eight-point frontier trades new spatial coverage
against travel distance. All choices are deterministic and the plan/policy hash
is persisted for continuation.

The geometric fallback is retained: every original sample position remains
available unless existing measured-interior or observed-connected-coverage
evidence justifies its skip. Estimated outlines only lower a position's priority.
Missing or ambiguous observations do not certify absence. The minimum-size
condition is an exposed inscribed disk; a rectangular chip whose short side
meets the minimum satisfies this shape condition. Narrow irregular, obscured,
or boundary-crossing chips are outside that guarantee.

The initial substrate-reference search uses the same whole-area coverage approach
for new grid/scattered workflows, before the campaign has a reference model.
It no longer stops at the historical 64 nearby survey fields. Survey, bracket
and travel allowances derive from the finite route; the original run deadline
continues to govern time. Autofocus starts only after a bright/dark edge proposal.
All-bright/all-holder views or an unusable initial optical condition can still
prevent a qualified reference and are reported explicitly.

Pure random probes can cluster and leave holes. Space-filling methods address
that general issue ([SciPy QMC overview](https://docs.scipy.org/doc/scipy/reference/stats.qmc.html)),
but this implementation uses a deterministic finite lattice and spatial
bisection, not Sobol or Monte Carlo sampling. A holder sample between two
substrate samples is useful evidence, but three points alone do not establish
two chips without shape/connectivity assumptions. Separate chip identities
come from the existing geometry and acquired coverage evidence.

Grid mode keeps measured-neighbor search and infers the first chip's grid
position from evidence. Explicit row/column counts support grids beyond 4x4.
The new expected count terminates after distinct completed rasters reach it;
observed, geometrically measured and completed counts remain separate. Exhausted
search with unresolved observations is reported as partial, never all chips found.

## Offline completion

The production `analysis_lifecycle.py` queues only explicitly requested jobs.
The offline queue and supervisor are established before the acquisition process
launches, avoiding a lost-analysis job if the viewer closes during Start.
Its separate process waits for acquisition to close and the owner process to
exit, then runs chips serially through segmentation, boundary refinement and
spatial grouping. Per-chip analysis/review selectors preserve ownership and
make every processed chip accessible from the saved-run library. Work is pinned
to the scan request, closed manifests and processing code. Existing source images,
masks and curated selectors are not silently replaced.

Only one heavy offline workflow runs at a time. RAM shortages are recorded and
waited out; stage progress persists. Viewer startup can recover a previously
queued offline job, but never restarts acquisition. A stopped/failed acquisition
can still yield reviewable committed fields. Completion must be read together
with scan coverage: finishing analysis of available images does not mean all
requested chips were scanned. Missing source files and ambiguous hardware errors
remain visible failures rather than automatic motion retries.

## Speed and qualification

See [ACQUISITION-SPEED-20261014.md](ACQUISITION-SPEED-20261014.md). The measured
999-field raster took 148.5 minutes. The changes compact every-tile durable
manifest writes, reduce derived overview export frequency and remove redundant
final coverage computation. They preserve raw writes and acknowledgements.
Per-phase timers will make the next run's bottlenecks directly visible.

No live speedup or unattended hardware run has been demonstrated by this upgrade.
The form defaults to a 24-hour acquisition deadline. It does not display a
predicted completion time; offline processing can outlast acquisition.


## Verification and deployment

The combined offline suite passed 211 tests, with two optional saved-image
replays skipped. A final 138-test suite covered the revised lifecycle,
ScanService, worker, per-chip reader/library, setup validation and HTTP routes.
All six page scripts passed syntax checks. A disposable two-chip fixture ran
six real CLI subprocesses through all stages; it tested missing-input reporting
and routing, not positive segmentation accuracy or microscope control.

The viewer was restarted after confirming no acquisition worker was present.
Browser verification checked Home -> Scan, grid-only controls, unsupported-model
feedback, disabled Start without readiness, Review runs -> original graphene
results (31 proposed groups), and the retained manual operator hold. Existing
scientific files were not regenerated. The saved scan UI and home screenshots
are under `research/scan-console-runtime/operator-scan-ui.png` and
`flakefinder-home.png`. Windows AC idle sleep is already disabled; its power
policy was read, not changed. No new scan or acquisition authorization was made.

## Concise interface update

The October14 follow-up removes slogans, workflow marketing and predicted
outcomes from Home, scan setup, saved scans, graphene review, processing and
legacy review pages. Primary actions are New scan and Review scans; graphene
shortlisted regions are labeled Candidates. Measurement and provenance details
remain available inside disclosure panels. Connection failures explicitly show
unknown scan status rather than implying that hardware stopped.

The graphene contrast plot can show the selected empirical model's fitted RGB
trend and the first-band green cutoff. These appear only when supplied by the
selected model; historical conditional analyses do not show an invented overlay.
Candidate range now extends to0.4 so the reported0.35 group is visible.
The launcher health check uses the stable scan-control element instead of a
page heading. All 11 inline scripts across 10 HTML pages pass JavaScript syntax checks; 16 HTTP route and
saved-library tests pass. Browser checks cover Home, saved results and scan
setup. No scan was started by these interface checks.

Review memory now retains at most two analysis indexes; older indexes can be
reloaded from the unchanged disk cache. 32 reader tests cover ranking, eviction,
selection changes and exact recovery. A changed analysis version also refreshes
the image summaries and clears the old color plot before loading its replacement.
The browser retains eight recently used full-resolution previews instead of 100;
older previews remain reloadable. This bounds application-held decoded images
to roughly 160 MiB for this 5 MP camera, excluding in-flight images and the browser's
own caches. Five cache checks cover retention, reuse, eviction recovery, late
errors and recency. These are structural memory reductions, not a measured RSS
or latency claim. Evidence: `research/graphene-segmentation-20261014/ui-code-checks.json`.

Final live review verification: the selected empirical analysis exposes all 999
image records and 330,196 measured regions. With grouping, artifact hiding and a
25 µm² minimum, the page shows 224 candidates and 44,940 uncertain groups. The
plot updates to the selected empirical model without a page reload. The previously
missing tile524 now appears as assessed. Tile215/region38 opens its saved image
with the accepted refined contour and retained shadow-nearness note. Browser
console checks found no errors or warnings. Evidence is saved as
`empirical-after-gui-counts.json`, `ui-empirical-transition.json`,
`empirical-final-review-ui.png` and `empirical-final-flake-ui.png` in
`research/graphene-segmentation-20261014/`.

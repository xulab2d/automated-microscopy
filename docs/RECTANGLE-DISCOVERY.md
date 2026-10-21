# Rectangle discovery and live upgrade — October 13, 2026

Production is `research/software-next-20261009/flakefinder`. Development/replay
copy: `research/software-rectangle-20261013`. The user explicitly requested this
upgrade and a smooth handoff of the same unmoved 4×4 chip array (about 10 mm pitch,
no chip under 5 mm). Do not restart reference discovery or discard previous work.

## What was wrong

Candidate001 of `scan-20261013-212016-7f8d2797` measured north/west/south, but its
visibly clear east edge failed a whole-field RGB support-span requirement.
Its inside/outside east interval was already 188 µm. The old routine spent 32
geometry images plus 31 autofocus images (~9 min), then left the outline open.
Geometry rejection was incorrectly being used as a reason to try autofocus.

## What changed

- `rectangle_edges.py` fits distributed local RGB transitions robustly to
  flakes/debris. It shares orientation across four approximately rectangular
  sides. Every side location still comes from image measurements; neither the
  rectangle nor grid prior invents a missing side position. Observed brackets
  constrain fitting, and an explicit allowance covers unobserved corners.
- `sparse_acquisition.py` first reanalyzes saved focused images for missing sides,
  preserving original records/rejections/hashes. Accepted sides and narrow
  search brackets are reused. Geometry failures alone do not launch autofocus.
  New outward steps grow 1.5 → 2.25 → 2.5 mm for these priors. Cached replay is pinned
  to the raw-image and model hashes. A continuation can add at most 4 geometric
  probes per saved candidate; it does not reset previously consumed work.
- `full_chip.py` uses recovered raw edge points for focus anchors, rather than
  inferred corners. Measured edge direction/location guides current-image ROI
  selection when flakes defeat the global detector. Focus still measures raw
  image sharpness and confirms its result. Uniform fields remain invalid ROIs.
- `rectangle_handoff.py` pins the closed donor, original deadline, reference,
  settings, bounds, and scientific file hashes. A new acquisition owner checks
  actual leased XYZ, camera profile and controller ranges, copies scientific
  progress and resumes saved candidates before remaining search points. It
  skips bootstrap and preserves original raw files and STOP records.
- `scan_service.py` accepts an explicit `rectangle_handoff` when saving a new
  immutable plan. It cannot be combined with another startup mode.

## Evidence and tests

Real candidate001 replay reanalyzed only `frames/probe_000006.tiff`, recovered
39 raw profiles across 329 µm, and produced an 8.41 × 9.89 mm estimated rectangle.
Planning uncertainty is 235 µm including extrapolation toward corners. It passes
full-chip planning with 4 photographed edge anchors and 999 tiles, using ZERO
new captures or stage moves. All 4 saved anchor images register 64 µm focus ROIs.
A fresh simulated 5 mm rectangle closes in at most 17 geometry frames with no autofocus.

The combined relevant suite passed 176 tests with 1 optional replay skipped;
separate saved-data replay ran successfully. The final anchor integration
passed 35 tests. Suites overlap; do not sum these counts.

## Deployment and current invocation

`scan-20261013-212016-7f8d2797` stopped cleanly at 21:40 with hardware closed.
New invocation `scan-20261013-215137-c4f3127d` launched at 21:51 and imports 350
pinned files. This is continuity of the existing campaign, not a new placement.
Read `<project>/scan-control/current.json` and current `scan-status.json` before
claiming raster progress. Live verification reached `measuring_focus_surface` for chip001 with its
999-tile plan and zero completed raster tiles. Saved-data reanalysis has passed
into actual acquisition; do not describe this as completed scanning.

Original production files are backed up under
`research/scan-readiness-20261013/rectangle-upgrade/production-before`;
`deployment.json` pins before/after hashes. Source is frozen while acquisition
owns hardware. Make further changes in an isolated copy, then hand off.

Stopping left export `88d078816aad4683884623011498f20d/chip1-207.czi` at the
shutdown boundary. Its retained review preserves all 8 original vendor files,
matching the closed owner/time/optics/envelope. It explicitly claims NO raw
acknowledgement, missing adapter receipt, focus verification or science result.
The 3 older unknown exports remain pending; do not delete originals or raise caps.

A rectangle remains a model of unsampled perimeter, not proof of complete chip
coverage. Expected 16 chips and measured distinct enclosures remain separate.

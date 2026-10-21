# FlakeFinder

**GitHub snapshot (October 20, 2026):** start with [docs/RUNBOOK.md](docs/RUNBOOK.md)
for current operation and [docs/GITHUB-SNAPSHOT.md](docs/GITHUB-SNAPSHOT.md) for
clone/setup instructions and the files kept outside Git. Production code is in
`research/software-next-20261009/`; the root package and commissioning notes below
are historical. Use `Open FlakeFinder.cmd` for the current scan/review home.
Opening the viewer does not connect hardware or authorize a scan.

**Current graphene workflow — October 13, 2026:** read [the runbook](docs/RUNBOOK.md)
and use `Launch Microscope Console.cmd` for the production browser console at
http://127.0.0.1:8770/scan. Production source is in
`research/software-next-20261009`, not the root package. The same graphene chips
have moved: start a fresh placement and reference. The notes below retain older
commissioning and MoTe₂ history; old tray coordinates and scan commands are not
the upcoming run plan.

General chip scanning, autofocus, flake review and candidate revisits for ZEISS ZEN core. Acquisition is independent of material. Initial live data are MoTe2 on 285 nm wet SiO2; layer identity remains unclassified.

**Start:** double-click `Launch Microscope Console.cmd` for this instrument or `Launch FlakeFinder.cmd` for the simulator. The hardware launcher selects the current **20Ã— quantitative configuration**, with the measured 20Ã— local configuration as a fallback. The console starts disconnected/disarmed and displays the encoded objective and illumination readback after connection. The current configuration covers the measured local test area, not the entire tray.

**Detection status:** the original nine-tile run's 79 proposals failed user review: they outlined contamination/noise rather than useful flakes. Those data are a failed baseline, not a validated detector. New proposals require review; image registration confirms a field location, not material identity or monolayer thickness.

## Current tray scan

The tray has been located from raw substrate/holder observations. Fifteen occupied positions are verified on an approximately 12 mm grid; R4C1 is displaced about 2 mm from its nominal position. R1C1 showed holder at its predicted centre and eight surrounding probes. The input and every source image are recorded in `data/chip-layout-grid-01/verified-layout.json`.

`data/chip-batch-01` is the initial 20× interior pass: 1.2 × 1.2 mm per chip, 20 overlapping fields per chip, 300 planned fields. This pass does not cover complete chip outlines. Each chip gets measured focus-hull anchors; incompatible planes fall back to local autofocus. Broad initial recovery uses only the configured absolute interval. A single analysis worker processes completed chips while acquisition continues.

The live progress viewer is at http://127.0.0.1:8766/. It shows progress, raw images, per-chip review links, and a Stop button. Its embedded proposal galleries are read-only; the standalone reviewer supports annotations. Proposed objects are not confirmed monolayers.

```powershell
# Reproduce the batch in a NEW folder (do not run alongside an active scan):
.venv\Scripts\python.exe scripts\scan_chip_batch.py --config configs\zen-20x-chip-batch.json --layout data\chip-layout-grid-01\verified-layout.json --output data\chip-batch-new --patch-size-um 1200
# Serve existing progress without connecting to the microscope:
.venv\Scripts\python.exe -m flakefinder.batch_viewer data\chip-batch-01 --port 8766
```

Resume requires identical configuration, layout and patch size. A Stop request is retained as `STOP` in the batch folder; remove it deliberately before an explicit `--resume`. Never run two hardware workers at once.

## Capabilities

- Bounded XYZ motion, settled vendor readback, cancellation, exclusive client ownership, raw TIFF acquisition and resumable checkpoints.
- Initial absolute-Z autofocus, local coarse/fine focus, chip-interior masks, confidence checks and focus-plane interpolation inside measured anchors.
- Measured camera/stage calibration, serpentine coverage, chip-outline detection, arbitrary chip counts and optional grid addresses with missing cells. The production detector has no hardcoded 15-chip requirement.
- A full-workflow button surveys the configured region, identifies chips, scans their interiors and analyzes the saved images.
- Analysis produces spatial/color proposals and explicit rejected regions for review. Physical size filters and usable width are configured in micrometres; color groups are descriptive rather than thickness assignments.
- A gallery with centered crops, outlines, full tiles, linked color-space plots, stage footprints, filters, keep/reject/uncertain labels, notes and export. The local server saves `reviews.json`; standalone HTML uses browser storage and export.
- Batch revisits use measured objective profiles, actual lens/scale checks, autofocus, raw images and specimen-feature registration against the original tile. Images with failed field registration stay on disk but are excluded from subsequent analysis. New regions link back to their original candidates.

Color groups C01, C02, etc. are **not layer numbers**. Low contrast alone is insufficient to identify a thin MoTe2 flake. Quality scores are heuristics, not calibrated probabilities.

## Actual hardware capability

Live tests passed XYZ motion, triggered acquisition, autofocus, camera calibration, a nine-field 2.1 x 1.8 mm raster and image return registration. Raster return error was 0.132 micrometres. Three proposed coordinates from different tiles were revisited, refocused and matched to their original fields at 10Ã—; this did not validate the outlined objects. The new 20Ã— transform has measured fit error 0.444 Âµm and return error 0.092 Âµm. See [the commissioning record](docs/VALIDATION.md).

MTB controls read objectives and change reflected-light intensity. The active stand configuration identifies an **RL LED lamp**, despite the legacy `MTBRLHalogenLamp` API name. Exposure and white balance can be fixed through typed camera-job resources with readback. The quantitative camera job starts from the installed Axiocam 305 5500 K preset; this preset is not a measurement of this lamp or a white-reference calibration. Keep the chosen lamp intensity and white balance fixed across a dataset. [Imaging profile notes](docs/IMAGING_PROFILE.md) describe channel clipping, exposure linearity and calibration requirements.

**Electrical objective switching was attempted.** After retracting Z by 150 micrometres, a 10x-to-20x command returned **Coded components cannot change their position.** Readback stayed at position 1; the original Z was restored. The active controller reports a coded turret. A separate installed configuration claiming motorization was not substituted for the connected hardware.

The console uses a **manual objective handoff** for this instrument. Registered positions are 1:10Ã—, 2:20Ã—, 3:50Ã—, 4:100Ã—; 5/6 are empty. Position 0 means the nosepiece is outside a click stop. Each objective needs its own measured transform and optical profile. No 50Ã—/100Ã— acquisition is claimed.

## Setup and operation

The virtual environment and generated vendor bindings already exist on this computer. To recreate them:

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.txt
& '.\.venv\Scripts\python.exe' scripts/generate_zen_api.py
```

Bindings come from installed ZEISS protobuf contracts. TLS uses the existing local CA/token without printing credentials. Optional MTB control uses pythonnet with ZEN's bundled .NET runtime and its already initialized hardware server. Connection does not home axes, reset coordinates, edit hardware configuration or initialize a second server.

Camera-only jobs are provided in `configs`. `FlakeFinder Quantitative Camera` binds `ff_exposure_ms` (double) and `ff_white_balance` (string); it contains no motion tools and disables automatic exposure, automatic white balance and HDR. On another installation, import the `.czjob` through ZEN Home -> Job Mode -> Import, then enter Home -> API Mode. The older camera jobs remain available for reproducing earlier acquisitions.

1. Copy `configs/zen.example.json`. Set a unique sample ID, material/substrate context, clear XYZ envelope, survey rectangle and initial focus interval. The survey must contain complete chip outlines with margins. All configuration distances are micrometres.
2. Connect and read the objective/illumination strip. Use the measured 20Ã— configuration on this instrument. Arm the envelope, inspect a frame, focus and verify camera-to-stage calibration. Set exposure on representative substrate; hold lamp intensity, camera mode and white balance fixed throughout the run. Exposure optimization preserves the white-balance resource.
3. Run the full workflow, or survey -> identify -> scan interiors separately. An optional grid shape adds cell addresses; blank gives ordered chip IDs for arbitrary layouts. These are geometric IDs, not OCR of printed serial numbers.
4. Review proposals, rejected objects and full source tiles. The default revisit selection is **Kept in gallery**. A separate choice includes large proposals needing review, with an explicit batch limit.

For high-magnification revisits, physically engage the desired objective, then use **Select / apply** next to Objective profile. The console checks its encoded position and the sample ID, replaces the transform, focus settings, exposure, white balance and camera job together, and disarms motion. The prepared session configuration is saved inside the session folder, leaving the original 20Ã— configuration intact. Arm, use local autofocus around the current height, then run the revisit batch. An absolute focus range from the old objective is cleared on a lens change; historical Z coordinates are never copied into the new profile. If that objective has no measured profile yet, use a configuration for its current focus interval, calibrate the camera-to-stage transform, then **Save current objective profile**.

**Revisit selected candidate** also supports a different measured objective on the same sample. It saves the new raw image, focus curve and field-registration result. A successfully registered field still requires visual evaluation of the proposed flake.

The validated profile uses a 30 micrometre approach from negative X/Y and 0.5 second settling. Keep that approach margin inside the envelope. Calibration rejects fit or image-return errors above 1 micrometre. A change of camera scale or recorded optical settings stops acquisition. These local measurements do not establish full-stage accuracy or clearance.

STOP / DISARM (Escape), CLI Ctrl+C, and `--config CONFIG stop` request vendor stage/focus stop. Cancellation initiates no return motion. Re-arm to continue. The configured envelope is a clear rectangle; software does not infer holder obstructions from defocused images.

## Commands

```powershell
# Offline analysis and review; no microscope connection:
& '.\.venv\Scripts\python.exe' -m flakefinder analyze data/run/detail
& '.\.venv\Scripts\python.exe' -m flakefinder review data/run/detail/analysis

# Read actual optics:
& '.\.venv\Scripts\python.exe' -m flakefinder --config configs/zen-20x-quantitative.json optics-status

# Find fixed exposure; copy the measured value into the new scan configuration:
& '.\.venv\Scripts\python.exe' -m flakefinder --config configs/zen-20x-quantitative.json optimize-exposure --output data/exposure-new

# Focus/calibrate the currently engaged objective and save its profile:
& '.\.venv\Scripts\python.exe' -m flakefinder --config config.local.json objective-profile --output configs/objective-measured.json

# Revisit three candidates from different source tiles:
& '.\.venv\Scripts\python.exe' -m flakefinder --config config.local.json revisit data/run/detail/analysis --profile configs/objective-measured.json --max-candidates 3 --output data/revisits-new

# Full simulated tray through color analysis:
& '.\.venv\Scripts\python.exe' -m flakefinder demo --output data/demo-new
```

CLI revisit selection is `priority` (large proposals requiring review), `kept` (saved review labels), or `all` (unrejected proposals). The GUI defaults to `kept`. A limited batch samples multiple source tiles. Resume checks the queue, profile and saved image hashes. Failed-baseline proposals are excluded unless explicitly kept in review. After a physical objective change, apply that objective's measured profile or calibrate before saving one. Sparse features can prevent cross-objective registration; those fields remain unverified.

Individual scan steps are `survey --output PATH`, `identify FOLDER`, and `detail --survey FOLDER --output PATH`. `focus --wide --output PATH` uses absolute initial bounds; `focus --output PATH` uses local settings. Scan `--resume` requires the original acquisition fingerprint. Changes to optics or fingerprint-producing software require a new output folder.

## Materials and validation

The current 20× batch configuration uses the **empirical component-repair boundaries**. The experimental `thin-contrast` segmentation was rejected after operator review: its shortlisted regions were bare substrate and it missed known crystal boundaries. Its historical outputs are retained as negative controls, not usable flake detections. Thin-contrast measurement is now attached to existing object masks without changing their geometry or catalog membership.

```powershell
# Measure local contrast on accepted saved boundaries; no hardware:
& '.\.venv\Scripts\python.exe' scripts/measure_existing_features.py --help
# Rebuild saved fields using the restored object-boundary analysis:
& '.\.venv\Scripts\python.exe' scripts/reanalyze_feature_contrast.py --help
```

Each source field is checkpointed against the raw bytes, metadata, acquisition manifest and analysis configuration. Repeating an unchanged completed run reuses masks and review assets. Bad fields are reported explicitly, while other fields can finish. The existing bulk/cyan reference-color analysis remains available separately; it is not a thin-flake calibration. Camera motion is never part of these offline commands. Acquisition uses one hardware owner; offline analysis can use independent worker processes.

The separate MoTe₂ reference-direction measurement follows the first-few-layer trend in Yamamoto et al. Fig. S4: resolved red darkening and stronger green darkening. The source has an oxide-thickness inconsistency (caption 285 nm, plot 300 nm), and published camera RGB values do not transfer directly to this microscope. No blue sign or layer number is imposed. This dataset has no independently confirmed thin positive; neither an empty shortlist nor agreement with that direction proves monolayer presence or absence.

The gamma/WB/lighting audit is in `docs/RESEARCH.md`. Scientific processing uses stored camera pixels, never display-gamma JPEGs. The saved camera state says gamma1, but the job does not verify every processing setting per frame. `scripts/assess_radiometry.py series.json --output report.json` evaluates a saved three-exposure repeat series, optional dark references, response nonlinearity, cross-channel noise, drift and clipping. It recommends only a measured exposure and keeps missing calibration evidence explicit. Keep illumination and WB fixed across a quantitative scan; making the oxide appear white is not an optimization criterion.

Contrast is (substrate - region) / substrate, with a spatial substrate estimate. Clustering raw foreground does not by itself distinguish flakes from tape or noise. Mixed optical/material contexts are rejected; raw images remain unchanged. An optional empirical material model uses independently confirmed examples under matched conditions; `train samples.json --output material.json` fits class contrast distributions. Applying a different optical profile clears a previously selected material catalog. Unknown and out-of-distribution regions remain unclassified.

Saved exact masks can also be selected against a local optical-reference profile without repeating segmentation:

```powershell
& '.\.venv\Scripts\python.exe' scripts/select_material.py data/chip-batch-01/R2C1/analysis-component-repair --profile configs/mote2-285nm-20x-material.json --output data/chip-batch-01/R2C1/analysis-material-selected
```

This reference selector uses full RGB covariance, separate bright/cyan crystal populations, and measured dust/haze/substrate populations. It preserves every mask and labels observations `material_like`, `uncertain`, or `excluded`. The material catalog additionally requires at least 100 µm² of **connected color-supported pixels**, not summed bright specks inside a large outline. Source-image overlays retain small objects independently of this catalog limit. The viewer opens with the material-selected group; Uncertain, Excluded, and All retain the other observations. An explicit human Keep can override the automatic revisit selection.

Set `analysis.material_profile` to the profile JSON path for future empirical analyses; relative paths resolve beside the configuration file. Classification reuses each frame's already computed contrast, with no extra image acquisition or segmentation pass. The profile is bound to the sample, substrate, objective, camera settings, illumination and white balance; incompatible contexts fail instead of silently importing thresholds. These locally selected crystal references are bulk/cyan optical controls, not confirmed thin MoTe₂. Faint out-of-reference colors remain unresolved, and colors overlapping reviewed debris are still inspectable. No layer number is assigned.

Run `.venv\Scripts\python.exe -m pytest -q`. Tests exercise motion failures and cancellation, raw provenance, known-mask segmentation, noise/shape rejection, arbitrary chip layouts, optical profile checks, cross-magnification registration and checkpoint recovery. The full simulator exercises survey-to-analysis integration. [Research notes](docs/RESEARCH.md) distinguish this implementation from published trained models.

The material-selection review has been inspected in the local browser, including source overlays and catalog filters. New whole-chip acquisition uses a fresh coordinate frame after the operator re-zeroed XY; old chip positions and focus anchors must not be resumed across that change. `scripts/scan_chip_campaign.py` interleaves count-independent discovery with whole-chip coverage and records unresolved search coverage explicitly.

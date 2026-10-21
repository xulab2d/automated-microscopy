# GitHub source snapshot

Published for xulab2d on October 20, 2026. This repository preserves the working
source tree, production and historical implementations, tests, configuration,
operator documentation, research scripts, review snapshots and retained evidence.
Read [RUNBOOK.md](RUNBOOK.md) before operating or changing the microscope system.

## Clone and setup

```powershell
git clone --recurse-submodules https://github.com/xulab2d/automated-microscopy.git
cd automated-microscopy
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

The three clean external checkouts are pinned as submodules at their existing
commits, preserving their upstream repositories and licenses. For a clone made
without `--recurse-submodules`, run `git submodule update --init --recursive`.
Optional analysis environments use the corresponding lockfiles and external
model source manifests. ZEISS integration requires the vendor software and
locally generated bindings; consult the existing setup documentation.

Production Python/UI lives in `research/software-next-20261009/flakefinder/`.
Run production tests from `research/software-next-20261009` with the root
`.venv/Scripts/python.exe`. Root-level tests target the older implementation.

## Storage outside Git

Git is the source/research-record copy, not a complete scientific-data backup.
The existing ignore rules omit `data/`, `generated/`, virtual environments,
Python caches, logs, and local configuration. The publishing pass also omits
test scratch directories, credential files, downloaded benchmark datasets and
model archives/weights, bulk transfer-state directories, and CZI/TIFF capture
study images. Nothing was deleted from the source workstation by this step.

Scientific runs and analysis outputs remain in the project selected by
`configs/storage.json`, currently:

`G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy`

On another computer, set `FLAKEFINDER_PROJECT_ROOT` to its mounted copy of that
project. The GitHub repository alone cannot recreate all saved scientific runs
or reproduce data-dependent analyses. Model manifests in `external/maskterial-hf`
record the downloaded weight sources. Preserve original images and versioned
analysis outputs in their scientific storage.

## Hardware and operational state

Publishing this snapshot performs no acquisition, motion, hardware connection,
scan restart, or hold release. Retained STOP/hold records and dated run requests
are historical evidence, not permission to resume a scan. A fresh operator start
and current placement/reference checks are required under the runbook.

The initial upload was checked for common credential patterns, oversized files,
and submodule integrity. It does not constitute a new software or hardware
qualification; existing validation records remain in the repository.

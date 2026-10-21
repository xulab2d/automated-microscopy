PORTABLE MICROSCOPY VIEWER

This is a read-only snapshot of the saved image gallery, exact proposal outlines,
screening/rejection reasons, recovery records and measured object RGB contrasts.
It requires Python 3.9 or newer, using only its standard library. It does not
require ZEN, Windows, microscope drivers, NumPy, OpenCV, pip packages or internet.
No camera or stage connections are made. No data can be edited through this viewer.

KEEP THIS DIRECTORY LAYOUT WHEN DOWNLOADING FROM DRIVE:

  Automated Microscopy/
    laptop-viewer/
      Launcher.py
      snapshot.json
      snapshots/...
      index.html, gallery.html, contrast.html
    data/
      whole-chip-standardized-01/...

Do not copy the TIFFs a second time for the viewer. It reads the accompanying
JPEG previews and instance-mask PNGs in their existing relative locations.
Download those files locally / mark them available offline in Google Drive.
The original TIFFs, analysis JSON, source code and recovery provenance remain in
the rest of the project and are needed for future analysis, not for rendering.

WINDOWS:
  Open a terminal in laptop-viewer and run:  py -3 Launcher.py
  Or double-click Start Windows.cmd if Python is already installed.

macOS / LINUX:
  Open a terminal in laptop-viewer and run:  python3 Launcher.py

A browser opens at a randomly chosen localhost port. Keep the terminal open.
Close it or press Ctrl+C to stop the viewer. No inbound network access is needed.

IF THE DATA FOLDER WAS MOVED:
  python3 Launcher.py --data "/path/to/whole-chip-standardized-01"
  Windows: py -3 Launcher.py --data "D:\data\whole-chip-standardized-01"

The landing page shows export time and counts. This is a frozen snapshot, so
Refresh reloads the saved snapshot; it does not acquire images or run analysis.
Use All/Selected/Rejected in the gallery. Clicking a contrast point opens its
source field with the exact feature selected. All contrast tails are retained;
zoom and pan in the plot to inspect populations. Selection is not material or
layer identification. Multiple overlapping images can show the same object.

Original scientific files are not changed by this export. Relative presentation
paths and compressed API snapshots are conveniences for laptop browsing. Source
checksums and primary/recovered identities remain in the dataset and field data.

TO REBUILD LATER FROM THE COMPLETE PROJECT (also standard-library only):
  python3 scripts/export_laptop_viewer.py --data data/whole-chip-standardized-01
Close the portable viewer before re-exporting its snapshots. No acquisition or
segmentation is triggered by export. The original dataset should be stable.

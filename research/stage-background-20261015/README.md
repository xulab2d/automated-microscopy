# Measured stage-background classification

The hBN run `scan-20261015-145135-db809db3` failed to identify exposed stage.
Its reference RGB median was [413,772,486], while probe24's stage was
[101,122,60]. Blue lay below the old valid-exposure floor (81.9 counts); the
fixed15%-of-reference fallback also missed its red and green channels. Nine
boundary attempts therefore could not establish the background side.

The accepted bootstrap had independent, focused same-context edge evidence.
Its dark side was [43,47,29], showing that stage brightness varies appreciably
across the placement. A tight absolute RGB model would also fail.

`stage_background.py` now fits robust log(R/G), log(B/G) statistics from raw
pixels more than20µm outside that independently validated edge. Samples are
bounded to at most512×512. Matching stage chromaticity is accepted only below
35% of the measured bright–dark RGB separation, above4raw counts and outside
the substrate chromatic core. Thus a dim substrate-colored shadow, saturated
pixel, zero pixel, or arbitrary dark color is not automatically stage.
This is measured compatibility, not proof of chemical identity.

The model is attached to the returned reference metadata after acceptance.
It records edge TIFF/sidecar/pixel hashes, independent raw validation and bright
correspondence, acquisition context, reference pixel SHA/shape/dtype/scale and
a checksum of the complete model. Loading rejects altered reference pixels,
context, scale or model parameters. `SubstrateReference` carries this model;
`classify_boundary` disables the fixed15% fallback for such measured references.
Uncalibrated historical references retain their previous labels and serialized
schema. No saved acquisition file was changed by this replay.

Offline report: `replay-v1/report.json`; fitted model: `replay-v1/model.json`.
`labels-NN.png` displays old/new labels: gray unknown, green substrate, orange
non-substrate (including background). New output directories are exclusive.

| Saved frame | Old non-substrate | Measured non-substrate |
|---|---:|---:|
| probe1, east stage |0.9505%|99.9512%|
| probe6, east stage |1.1014%|99.9421%|
| probe23, south stage |0.5576%|99.9624%|
| probe24, south stage |0.6439%|99.9521%|

Substrate support remained exactly unchanged in the reference and probes0,7,13,
18,22 (92.27%,92.27%,96.85%,94.07%,89.51%,99.21% respectively). The mixed focused
edge still passes the unchanged independent raw validator; its recovered label
line has94.98% substrate purity and92.09% background purity. It remains an edge
observation, not a reconstructed complete chip outline.

Run from the repository root, with a new output folder:

```powershell
.venv/Scripts/python.exe research/stage-background-20261015/replay.py --run '<project>/data/scan-20261015-145135-db809db3' --output research/stage-background-20261015/replay-v2
```

Tests cover context/raw-pixel binding, rejected edge evidence, changed exposure,
low-light stage, arbitrary dark color, substrate shadows, mixed boundaries,
save/reload, unchanged historical fallback, and the actual saved hBN probe24 plus
its focused edge. No hardware is imported or connected by the replay.

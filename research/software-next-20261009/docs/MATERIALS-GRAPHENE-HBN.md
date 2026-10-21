# Graphene and hBN material analysis

The upcoming graphene sample is **SiO₂/Si, nominal 90 nm oxide**, confirmed by the operator. `configs/materials/registry.json` selects that profile. Profiles for 285 and 300 nm remain explicit alternatives. hBN's substrate is not yet confirmed. Nothing in these modules connects to the microscope or changes acquisition settings. All new code is staged; the frozen production checkout is unchanged.

## Scientific basis and external priors

- **Graphene:** Blake et al., [Making graphene visible](https://arxiv.org/abs/0705.0259), [APL91,063124](https://doi.org/10.1063/1.2768624), model interference on SiO₂/Si with a0.34nm film and approximately2.6−1.3i index under their sign convention. Our passive `exp(−iωt)` solver uses2.6+1.3i. Wavelength and oxide thickness are essential; an oxide-independent RGB box is inappropriate. This approximation is a physical starting model, not a measurement of the actual graphene optical constants.
- **hBN:** Gorbachev et al., [Hunting for Monolayer Boron Nitride](https://arxiv.org/abs/1008.2868), [Small7,465](https://doi.org/10.1002/smll.201001628), report approximatelyn2.2,k0 and a contrast reversal near530nm on290nm oxide. At90nm oxide they observe around2.5% white-light contrast per layer. Their contamination/water warning matters: a monolayer can resemble a bilayer. Our0.333nm structural spacing is a conventional thickness coordinate, not an apparent AFM height. The finite-NA isotropic extension is explicitly approximate.
- **Fast empirical prior:** Uslu et al., [2DMatGMM](https://arxiv.org/html/2306.14845v3), [MLST5,015027](https://doi.org/10.1088/2632-2153/ad2287), use substrate-relative pixel RGB Gaussians before connected-component extraction, and a separately trained false-positive classifier. Their published graphene population is nominal90nm oxide; their paper identifies blue-channel sensitivity to oxide variation. The shipped JSON is converted from **BGR to RGB**, and from `(flake−substrate)/substrate` to our opposite sign. The imported full covariance and MIT notice are retained. Its camera/context differs; application requires explicit foreign-context override or a local labeled fit. We do not import its shape rejection threshold as a universal flake rule.
- **Learned shape prior:** [MaskTerial](https://arxiv.org/html/2412.09333v1), [Digital Discovery4,3744](https://doi.org/10.1039/D5DD00156K), separates material-agnostic mask prediction from contrast classification and demonstrates adaptation with a small labeled image set, including low-contrast hBN. Its public shape model is the most directly relevant learned proposal source already present in the project. Our previous microscope-domain run returned no proposals at its fixed threshold, so publication results do not validate this camera. Use locally labeled examples and held-out fields before deployment. DINO appearance embeddings can help organize review; they do not provide calibrated material or layer labels.

The substrate uses the existing hash-pinned [Aspnes silicon data](https://doi.org/10.1103/PhysRevB.27.985) and [Malitson silica dispersion](https://doi.org/10.1364/JOSA.55.001205). Fused silica is an approximation to the actual thermal oxide. Literature constants, data hashes, convention, response assumptions and model version are embedded in every prediction.

## Python interface

```python
from flakefinder.material_profiles import (
    load_material_profile, predict_material_contrast,
    manufacturer_response_scenarios, predict_material_ensemble,
)
from flakefinder.material_contrast import (
    MaterialContrastClassifier, fit_empirical_material_model, save_material_model,
)
from flakefinder.material_terraces import (
    MaterialTerraceOptions, segment_material_terraces,
)

profile = load_material_profile('configs/materials/graphene-sio2-90nm.json')
# For a real calibrated curve, supply effective illumination×camera RGB weights.
curve = predict_material_contrast(profile, [0, .34, .68, 1.02], response=response)
classifier = MaterialContrastClassifier(curve, model_covariance=model_uncertainty_rgb)
result = segment_material_terraces(
    contrast_rgb, valid_mask, recorded_substrate_samples, raw_noise_covariance_rgb,
    pixel_size_um, classifier, parent_labels=accepted_parent_labels,
    saturated_mask=clipped_pixels,
    background_systematic_covariance=background_fit_covariance_rgb,
    options=MaterialTerraceOptions(useful_area_um2=25),
)
```

`contrast_rgb` is raw, linear, offset-corrected fractional RGB contrast `(background−pixel)/background`, **not JPEG color**. `valid_mask` is the geometric chip domain; `substrate_mask` supplies independently supported background samples. These are deliberately different inputs: a differently colored flake remains in-domain. Use the existing `terrace_domain.normalize_supported()` and saved chip support when preparing raw fields. Do not normalize using a mask selected from the desired flake color.

The result contains exact integer `labels`, a bit-identical copy of supplied `parent_labels`, `regions`, `class_map`, material/debris compatibility masks, and timings/provenance in `report`. A zero class is the substrate null,−1 is unknown, and−2 means unmeasured/outside or saturated. Positive class indices refer to explicit reference populations or **conditional nm hypotheses**. They are not confirmed layer numbers.

Parent masks are retained even if all their pixels become substrate-like or fail a color sieve. Each classified terrace also reports an exact source-axis inscribed rectangle inside its eroded same-class mask, with width, height and area. Holes constrain this rectangle. `bounding_width_um` remains an exterior bounding-box measurement; `usable_width_um` is the smaller side of the inscribed rectangle. Unknown colors have no claim of a uniform usable rectangle. The calculation uses only each component ROI, and its cost is timed separately. Holes stay holes. Disconnected same-color islands are separate regions. There is no contour-fill or closing operation. Without supplied parents, the module can generate broad connected features from measured substrate-noise significance; this mode remains an experimental proposal source until compared against real annotations. The accepted-parent path is the safer integration default.

Rows expose `class_name`, contrast and full covariance, material-pixel occupancy, `selected_for_review`, and independent `sieve_results`. Size is a configurable review priority (default25µm²), not a deletion or selection veto. Color-compatible small regions remain selected for review; regions below50µm² receive an explicit extra edge/shape-review priority. No untrained automatic quality threshold is invented. Color selection requires a majority of pixel support plus a significant region-level contrast match; a giant parent containing one matching speck cannot pass on that speck alone. Edge gradient and noise-normalized texture are currently descriptive, independently inspectable evidence. There is no untrained hard polygon-shape veto.

The null/reference tests use explicitly reported Gaussian quantiles and log-likelihood ratios, not claimed material probabilities. Spatial patch means estimate region variation; supplied background systematic covariance is propagated through the local Weber denominator and is not divided by region pixel count. Missing systematic covariance is reported as unquantified. Saturation must be supplied from the raw white level; unavailable clipping information is recorded. Defocus and uncertainty in camera processing remain acquisition limitations, not evidence of absence.

## Calibrate from labeled examples

`fit_empirical_material_model(samples, context=..., model_id=..., validation_groups=...)` accepts rows with `label`, `role` (`material`, `debris`, or `unknown`), `group_id`, `source`, and anN×3 `contrast_rgb` array. Supply distinct flakes/fields as groups; do not relabel correlated pixels as independent examples. Each fitted class needs at least two independent training groups. Equal pixel counts per group prevent one large flake dominating. Entire validation groups are excluded from fitting. OAS covariance shrinkage stabilizes the3D population. Held-out ellipsoid coverage is recorded; it is **not** an instance detection benchmark.

Use semantic material labels independently of thickness labels. A Raman/AFM-confirmed layer can name a reference, but these APIs intentionally leave `layer_number=None` and `layer_assignment_allowed=False`; a reliable layer classifier needs target-context held-out validation and explicit agreement about acceptable error. Tape/debris examples should have their own reference roles. Out-of-distribution colors remain unknown and reviewable.

`save_material_model()` writes a new versioned file through the managed Drive storage policy and refuses replacement. Only explicitly illustrative optical predictions may opt into a local research output. Reuse requires the recorded context (material, oxide, optical profile, processing version as appropriate). The external90nm GMM context is `optical_profile_id='external_2dmatgmm'`, so it cannot silently act as the microscope's calibrated prior.

## CLI and validation

Saved scanner data needs no manual NumPy construction. First edit `configs/materials/graphene90-input-context.example.json` into a versioned context file with the actual acquisition profile and known radiometry. Missing dark calibration may be declared explicitly as zero/unmeasured; it remains a limitation. The preparation command reads the raw TIFF, sidecar, saved chip polygon and independently recorded substrate labels, checks the acquisition hashes, and preserves an optional accepted parent-label map:

```powershell
python scripts/analyze_material.py prepare-input --chip "G:/Shared drives/Xu Lab - Microscopy and Instruments/Automated Microscopy/data/GRAPHENE_RUN/chip001" --tile tile_000001 --context configs/materials/MY-graphene90-context.json --parent-labels "PATH/TO/accepted-instance-mask.png" --output data/graphene-material/prepared/tile_000001
python scripts/analyze_material.py segment --input-npz "G:/Shared drives/Xu Lab - Microscopy and Instruments/Automated Microscopy/data/graphene-material/prepared/tile_000001/input.npz" --model "PATH/TO/local-labeled-model.json" --output data/graphene-material/results/tile_000001
```

`--parent-labels` accepts a source-sized integer PNG or NPZ (`labels` key, configurable with `--parent-key`). Omitting it explicitly selects the experimental broad-proposal path. The context is carried into `input.json`, so the segment command need not repeat it; a conflicting override or changed NPZ is rejected. Relative scientific output paths are routed by the project storage policy. For external saved images use `prepare-input --tiff raw.tiff --domain-mask domain.png --substrate-mask background.png --context context.json --output ...`; both masks must be source-sized binary annotations, not a selected material-color population. The paired TIFF JSON must provide physical pixel calibration and sensor white level. Background support and geometric domain remain distinct throughout.

An optical RGB curve requires a supplied effective spectral response. The `segment` CLI rejects illustrative RGB unless `--allow-illustrative-model` is deliberately set, records that override, and always checks the optical material/oxide against the prepared context. The example context does not invent a measured response. Use `fit` with locally labeled source groups for the empirical route; its applicability context must match the prepared context.

`scripts/analyze_material.py predict` creates spectral/RGB curves; `fit` trains a labeled model; `segment` consumes anNPZ with `contrast_rgb`, `valid_mask`, `substrate_mask`, `noise_covariance`, `pixel_size_um`, and optional `parent_labels`, `saturated_mask`, `background_systematic_covariance`. It resolves scientific output through the required Drive policy, then writes new `masks.npz` and `analysis.json`, refusing an existing output directory. `--illustrative` explicitly uses unmeasured top-hat bands; it is useful for tests, not a calibrated production response. Manufacturer response scenarios are likewise conditional, not guaranteed uncertainty bounds on an LED spectrum.

`scripts/benchmark_materials.py` ran one CPU thread. The1.05MP synthetic graphene/hBN cases took1.24/1.07s, with pooled source-mask IoU0.949/0.962 and two selected large regions each. True inscribed usable rectangles cost0.049/0.062s of those totals. A previously reviewed mostly bare768×768 crop of chip001/tile000369 yielded zero selected regions for both priors in0.56/0.59s. That saved control is from the previous MoTe₂ batch and cannot establish graphene/hBN accuracy. Exact settings, source hashes and timings are in `material-benchmark-20261009-v2.json`. Ten targeted tests cover Fresnel/sign/gain behavior, null/unknown/debris classification, optical-context rejection, group-held-out fitting, parent/holes/island preservation, raw TIFF preparation and the substrate control. A read-only preparation check of the actual5.07MP saved tile000369 verified its raw/sidecar/label hashes and completed in2.94s; it did not apply a graphene prior to that old MoTe₂ field.

**Remaining evidence:** no locally acquired graphene/hBN labeled positive has yet validated these priors. The next useful calibration is a small fixed-optics set of confirmed graphene examples and tape/background controls, preserving raw values and source-group separation. Candidate contours and material scores should be reviewed before assigning layers or reporting a successful search.

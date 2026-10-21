# MaskTerial CPU bridge

This isolated runtime converts the authors' **Mask2Former R50 shape model** into Hugging Face Transformers. It does not classify material or layer thickness and never accesses microscope hardware. The microscope's `.venv` is unchanged; use `.venv-maskterial`.

The upstream converter maps Mask2Former's pixel and transformer decoder weights. `probe_cpu.py` adds an explicit ResNet50 mapping. All 557 model tensors load; the only newly initialized entries are 53 `num_batches_tracked` counters. The adapted backbone was compared with an independent torchvision ResNet50 using FrozenBatchNorm2d on a random 224×224 input: all four output feature maps had maximum absolute difference **0.0**. Full numerical parity with the original Detectron2 decoder has not been tested.

Pinned runtime: CPU PyTorch 2.8.0, torchvision 0.23.0, Transformers 4.57.3. Neither Detectron2 nor a CUDA compiler/toolchain is required. HF warns that ResNet is outside its officially listed Mask2Former backbones; this bridge checks every converted weight rather than accepting missing keys. The measured CPU is Intel i5-14500.

The installed environment uses Python 3.12.14. To recreate its dependencies from a Python 3.12 interpreter:

```powershell
python -m venv .venv-maskterial
.venv-maskterial/Scripts/python.exe -m pip install torch==2.8.0+cpu torchvision==0.23.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv-maskterial/Scripts/python.exe -m pip install -r external/maskterial-hf/requirements-lock.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

The authors' synthetic archive is `SEG_M2F_Synthetic_Data.zip` in the checkpoint record linked below. Extract its `model_final.pth` and `config.yaml` to `external/maskterial-hf/synthetic/`. The verified archive checksum is recorded below and in `weight_source.json`. The full dependency lock, converter source and checkpoint source metadata are retained locally.

From the workspace root:

```powershell
.venv-maskterial/Scripts/python.exe scripts/infer_learned_masks.py --input data/20x-search-01/tiles --output data/20x-search-01/learned-synthetic-gamma --display-transform gamma --gamma 0.6 --white-level 4095 --max-side 1536 --score-threshold 0.3
```

Add `--weights external/maskterial-hf/mose2/model_final.pth` to compare the authors' MoSe₂ fine-tuned shape model. Its color/layer classifier is not used. This is transfer to MoTe₂, whose accuracy remains unvalidated. A synthetic-pretrained mask model does not make material-specific layer decisions.

The TIFF reader preserves the raw files and BGR channel order. It converts to float, divides by the actual ADC white level, applies the explicitly chosen display transform, and only then quantizes to uint8. It never truncates 12-bit TIFF values into 8-bit integers. `linear` and fixed `gamma` display transforms are supported; no per-tile automatic contrast stretch is performed. Model normalization is BGR mean `[107,148,85]`, standard deviation `[33,34,40]`, as supplied in both checkpoints. The original scientific contrast analysis should still use the raw TIFF, not this network display transform.

`predictions.json` records source/checkpoint/config/converter/mask hashes, display parameters, software versions, original/processed image dimensions, acquisition metadata, and focus status. Each image links to an NPZ containing `masks` (boolean N×original-height×original-width), `scores`, and `query_indices` in matching order. Logits are resized bilinearly to original pixel coordinates before thresholding. No hand-written shape or contrast veto is applied here. `--resume` requires the same model and image processing settings and verifies prior input/output hashes. It permits a changed CPU thread budget, recording the old/new budget and per-image thread counts.

Use one inference process at a time on the microscope PC. The default is three CPU threads so camera/stage control stays responsive. Two simultaneous eight-thread model batches caused substantial memory/CPU contention and increased field inference from approximately 9 seconds to 27–60 seconds. Image-size logits are explicitly released between fields.

The production CLI uses `sparse_inference.py`: foreground class probability is an upper bound on the final class-times-mask-quality score, so below-threshold queries are skipped without changing eligible detections. Other queries are interpolated and scored one at a time. On the first raw public control field, all six retained masks were pixel-identical to the dense implementation; maximum score difference was 2.98×10⁻⁷. Random tensors also passed the direct dense-equation comparison. See `sparse-verification.json`. New field records include Windows peak/current process memory. Create a file named `STOP` inside the output directory to pause gracefully at a field boundary; remove that file before `--resume`.

The focus flag is conservative: `focus_limited` is true for a failed autofocus or a tile with no autofocus record of its own. Even a successful field autofocus does not verify focus on an atomically thin layer; `thin_flake_focus_validated` remains false. Masks remain proposals requiring review.

## Measured public control

The synthetic checkpoint, run on the existing ten-image MoSe₂ test selection with 1536-pixel maximum side, score ≥0.5 and native area ≥667 pixels, matched **7 of 8** annotated regions at IoU ≥0.5. There were **14 predictions**, hence seven unmatched predictions under that evaluation. Total inference time was **84.3 seconds**. Results and overlays are in `benchmark/`. This small public control measures implementation usefulness; it does not establish target-domain MoTe₂ precision, recall or thickness accuracy. The first convenience image `data/public-positive-first.jpg` had already drawn annotations and was discarded as a valid input test.

## Current 20× transfer result

The completed twenty-field raw-TIFF run is `data/20x-search-01/learned-synthetic-gamma/predictions.json`. Fixed parameters were ADC white level 4095, gamma 0.6, maximum input side 1536 and proposal score threshold 0.3. **No proposals passed in any of the twenty fields.** Fifteen fields had no separate autofocus record; all twenty lack verified thin-flake focus. This result does not demonstrate successful detection in the current MoTe₂ imaging domain, and does not establish that thin flakes are absent. No threshold was lowered to manufacture detections.

The first twelve fields used dense scoring; the final eight used numerically verified sparse scoring, with the transition recorded in the output. Sparse inference on these 1536×1282 inputs took approximately 20–23 seconds on three CPU threads. Peak working set was 4.55 GB and peak private allocation 6.09 GB; memory was released when the batch process exited. The optional MoSe₂ checkpoint comparison is paused after seven fields, all with zero proposals at the same threshold, to prioritize microscope responsiveness.

## Attribution and provenance

- Uslu et al., [MaskTerial: a foundation model for automated 2D material flake detection](https://doi.org/10.1039/D5DD00156K).
- [Author code](https://github.com/Jaluus/MaskTerial), MIT license, retained in `external/MaskTerial`.
- [Author checkpoints](https://doi.org/10.5281/zenodo.15765516), **CC BY 4.0**. `weight_source.json` and `mose2_weight_source.json` preserve download metadata and verified MD5 checksums. Synthetic ZIP MD5 `cf5ebef5d26bbc4d416f6edce72de769`.
- [Hugging Face upstream converter](https://github.com/huggingface/transformers/blob/main/src/transformers/models/mask2former/convert_mask2former_original_pytorch_checkpoint_to_pytorch.py), Apache 2.0; its license header is preserved in `upstream_converter.py` and each run records its SHA256.
- Public MoSe₂ images/masks: [MaskTerial real datasets](https://zenodo.org/records/15765514), stored in `external/benchmarks`.

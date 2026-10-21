#!/bin/bash
#SBATCH --job-name=flake-sieve
#SBATCH --partition=normal
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:20:00
#SBATCH --output=flake-sieve-%j.log
set -euo pipefail
: "${FLAKEFINDER_CODE:?Set to the released source directory}"
: "${FLAKEFINDER_BUNDLE:?Set to the transferred, checksum-verified bundle}"
: "${FLAKEFINDER_PYTHON:?Set to the lab Python environment executable}"
: "${SCRATCH:?Sherlock scratch is required for job I/O}"
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-2}"
export OPENBLAS_NUM_THREADS="$OMP_NUM_THREADS"
export MKL_NUM_THREADS="$OMP_NUM_THREADS"
task_dir="$SCRATCH/flakefinder-${SLURM_JOB_ID:?}"
mkdir -p "$task_dir"
cp "$FLAKEFINDER_BUNDLE/manifest.json" "$FLAKEFINDER_BUNDLE/examples.jsonl" "$task_dir/"
export PYTHONPATH="$FLAKEFINDER_CODE${PYTHONPATH:+:$PYTHONPATH}"
cd "$task_dir"
"$FLAKEFINDER_PYTHON" -m flakefinder.training_bundle train --bundle "$task_dir" --output "$task_dir/appearance-sieve.json"
sha256sum appearance-sieve.json > appearance-sieve.sha256
printf 'Results: %s\nCopy verified results to permanent project storage; scratch is temporary.\n' "$task_dir"

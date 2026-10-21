"""Build a conditional hBN/90 nm oxide model for saved broadband RGB images."""
import argparse
import json
from pathlib import Path
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from flakefinder.material_profiles import load_material_profile, manufacturer_response_scenarios, predict_material_ensemble
from watch_graphene_scan import digest, save


def build():
    profile_path = WORKSPACE / 'configs/materials/hbn-sio2-90nm.json'
    response = WORKSPACE / 'configs/materials/data/Axiocam305_vendor_response.json'
    # Dense thin-film sampling, broader thick-film coverage. These are optical
    # thickness hypotheses, not independently calibrated layer assignments.
    thickness = [0.] + [round(.333 * n, 3) for n in range(1, 31)] + list(range(10, 122, 2)) + list(range(125, 501, 5))
    model = predict_material_ensemble(load_material_profile(profile_path), thickness,
        manufacturer_response_scenarios(response, temperatures_k=(3000., 6500.)),
        oxide_nm=(85., 90., 95.), illumination_na=(0., .4), gamma=(1.,),
        wavelengths_nm=json.loads(response.read_text())['wavelengths_nm'][::2])
    model.update(oxide_nm=90., nominal_oxide_nm=90.)
    model['offline_assumptions'] = {
        'profile_sha256': digest(profile_path), 'vendor_response_sha256': digest(response),
        'oxide_scenarios_nm': [85., 90., 95.], 'gamma': 1.,
        'gamma_measured_for_this_run': False, 'no_layer_assignment': True,
        'unmeasured_dark_offset_rgb': [0., 0., 0.],
        'model_covariance_rgb_diagonal': .01**2,
        'model_covariance_kind': '1% engineering mismatch allowance; not measured uncertainty.',
        'signed_contrast': 'Both signs retained; no graphene empirical bands.',
        'scope': 'Conditional optical compatibility; unknown proposals retained.',
        'thickness_grid': '0.333nm steps to10nm, 2nm to120nm, 5nm to500nm; no layer assignments.',
    }
    return model


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new output path; existing models are immutable.')
    save(args.output, build())
    print(args.output)

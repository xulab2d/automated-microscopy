"""Generate immutable illustrative transparent-film competitors; no hardware.

Usage: python scripts/prepare_transparent_film_alternatives.py --output FILE
The indices are sensitivity scenarios, not measured water/polymer/tape constants.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from flakefinder.material_profiles import (MaterialProfile, load_material_profile,
    manufacturer_response_scenarios, predict_material_ensemble, canonical_hash)


def build_tables():
    profile_path = ROOT/'configs/materials/hbn-sio2-90nm.json'
    response_path = ROOT/'configs/materials/data/Axiocam305_vendor_response.json'
    base = load_material_profile(profile_path)
    responses = manufacturer_response_scenarios(response_path, temperatures_k=(3000., 6500.))
    thickness = np.arange(0., 100.0001, .25)
    models = []
    qualification = ('Illustrative generic homogeneous nonabsorbing film with constant refractive index. '
        'Not a calibrated water, polymer, adhesive or tape-residue model; not evidence of chemical identity. '
        'Real contamination can be dispersive, absorbing, nonuniform or beneath hBN. '
        'These alternatives test optical nonuniqueness only; absence of a match does not exclude contamination.')
    for index in (1.33, 1.5, 1.6):
        config = copy.deepcopy(base.config)
        config.update(material='generic_transparent_film', profile_id=f'illustrative-transparent-n{index:g}-v1',
                      calibration_status='illustrative_unmeasured_alternative', caveats=[qualification])
        config.pop('layer_spacing_nm', None)
        config['optical_constants'] = {'n':index, 'k':0.,
            'convention':'positive imaginary index for exp(-i omega t)',
            'source':'Explicit analyst-selected generic transparent-index sensitivity scenario; not measured chemistry',
            'regime':'constant isotropic refractive index across visible wavelengths'}
        profile = MaterialProfile(config, base.silicon_nk, str(profile_path))
        model = predict_material_ensemble(profile, thickness, responses,
            oxide_nm=(85., 90., 95.), illumination_na=(0., .4), gamma=(1.,),
            wavelengths_nm=responses[0]['wavelengths_nm'][::2])
        model.update(calibration_status='illustrative_transparent_film_sensitivity_ensemble',
            source_qualification=qualification, chemical_identity_confirmed=False,
            layer_assignment_allowed=False, nominal_oxide_nm=90.,
            response_scenarios=[{k:v for k,v in r.items() if k not in ('wavelengths_nm','effective_weight_rgb')}
                                for r in responses])
        models.append(model)
    dependencies = [Path(__file__), ROOT/'flakefinder/material_profiles.py',
                    ROOT/'flakefinder/mote2_optics.py', profile_path, response_path]
    return {'schema':1, 'kind':'illustrative_transparent_film_alternatives',
        'models':models, 'model_sha256':[canonical_hash(m) for m in models],
        'qualification':qualification, 'source_sha256':{
            str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in dependencies},
        'shared_scenarios':{'oxide_nm':[85.,90.,95.], 'illumination_na':[0.,.4],
            'lamp':['equal_energy','blackbody_3000K','blackbody_6500K'],
            'response_interpretation':['energy_response','photon_efficiency'], 'gamma':[1.],
            'count':36, 'thickness_nm':{'start':0.,'stop':100.,'step':.25}}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Refusing to overwrite an existing model artifact: '+str(args.output))
    tables=build_tables()
    payload=json.dumps(tables,sort_keys=True,allow_nan=False,separators=(',',':')).encode()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('xb') as stream:
        stream.write(payload);stream.flush();os.fsync(stream.fileno())
    digest=hashlib.sha256(payload).hexdigest()
    if hashlib.sha256(args.output.read_bytes()).hexdigest()!=digest:
        raise IOError('Model output readback hash differs.')
    print(json.dumps({'path':str(args.output),'sha256':digest,'models':3,'scenarios_each':36,
                      'thickness_points_each':401}))


if __name__=='__main__':
    main()

"""Versioned offline thickness attempt from committed region means, no raw edits."""
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):os.environ[key]='1'
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
from collections import Counter
import numpy as np
WORKSPACE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORKSPACE))
from flakefinder.graphene_thickness import estimate_thickness,_read_hash,_inside
from flakefinder.material_profiles import load_material_profile,manufacturer_response_scenarios,predict_material_ensemble
from flakefinder.storage import resolve_output_path


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path,value):
    raw=json.dumps(value,sort_keys=True,allow_nan=False).encode()
    with path.open('xb') as stream:stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def run(project,run_id,version):
    project=project.resolve();run_folder=_inside(project,'data/'+run_id)
    selection=json.loads((run_folder/'graphene-analysis-selection.json').read_bytes())
    source=_inside(project,selection['analysis_relative_path'])
    plan=_read_hash(source/'plan.json',selection['plan_sha256'])
    chip=plan['pin']['chip'];model=json.loads((source/'model.json').read_bytes())
    from flakefinder.material_profiles import canonical_hash
    if canonical_hash(model)!=plan['model_sha256']:raise ValueError('Analysis model changed.')
    ledger=json.loads((source/'ledger.json').read_bytes());ledger_hash=digest(source/'ledger.json')
    output=resolve_output_path(source.parent/version);output.mkdir(exist_ok=False)
    profile_path=WORKSPACE/'configs/materials/graphene-sio2-90nm.json'
    response_path=WORKSPACE/'configs/materials/data/Axiocam305_vendor_response.json'
    profile=load_material_profile(profile_path)
    responses=manufacturer_response_scenarios(response_path,temperatures_k=(3000.,6500.))
    wave=json.loads(response_path.read_bytes())['wavelengths_nm'][::2]
    ensemble=predict_material_ensemble(profile,np.arange(241)*.085,responses,
        oxide_nm=(85.,90.,95.),illumination_na=(0.,.4),gamma=(.8,1.,1.2),wavelengths_nm=wave)
    ensemble.update(layer_spacing_nm=.34,engineering_rgb_sigma=.01,absolute_match_distance2=11.344866730144373)
    # Shared finite arrays for the offline loop avoid repeated JSON-to-array work.
    numeric={**ensemble,'contrast_rgb':np.asarray(ensemble['contrast_rgb'])}
    assumptions={'nominal_oxide_nm':90,'oxide_scenarios_nm':[85,90,95],
        'refractive_index':{'n':2.6,'k':1.3,'source':'https://arxiv.org/abs/0705.0259'},
        'layer_spacing_nm':.34,'grid_layers':[0,60],'grid_step_layers':.25,
        'illumination_na':[0,.4],'lamp_scenarios':'equal energy, 3000K and 6500K blackbodies; vendor camera sensitivity; two response interpretations',
        'gamma_exponents':[.8,1.,1.2],'gamma_qualification':'Unmeasured sensitivity scenarios around assumed linear raw response, not measured bounds.',
        'rgb_model_sigma':.01,'allowance_qualification':'1% engineering residual allowance plus saved region covariance; not calibrated uncertainty.',
        'absolute_match_distance2':ensemble['absolute_match_distance2'],
        'range_qualification':'Finite-scenario envelope; no probability, no guarantee actual optics lies inside. Constant-index, isotropic finite-NA approximation.',
        'empirical_band_layer_labels_used':False,'chemical_identity_confirmed':False}
    source_fields={};candidates=[];uncertain_bins={};counts=Counter();started=time.monotonic()
    for tile,commit in sorted(ledger.items()):
        if commit.get('state')!='assessed':continue
        relative=commit['field_json'];expected=commit['hashes'][relative]
        payload=_read_hash(_inside(source,relative),expected);field=payload['field']
        source_fields[field]={'path':relative,'sha256':expected}
        for row in payload['regions']:
            if row['area_um2']<25:continue
            item={'id':field+'/'+str(row['instance_label']),'contrast_rgb':row['contrast_rgb'],
                'contrast_covariance':row['contrast_covariance'],'area_um2':row['area_um2'],
                'interior_measurement_available':row.get('measurement',{}).get('interior_measurement_available'),
                'class_name':row.get('class_name'),'selected_for_review':row['selected_for_review']}
            counts['useful_observations']+=1
            if row['selected_for_review']:candidates.append(item)
            else:
                # A deterministic contrast-stratified uncertainty audit; all
                # others can be evaluated lazily from their pinned field means.
                bucket=int(np.clip(np.floor(row['contrast_rgb'][0]*10),-1,10))
                values=uncertain_bins.setdefault(bucket,[])
                values.append((hashlib.sha256(item['id'].encode()).hexdigest(),item))
                values.sort(key=lambda pair:pair[0]);del values[40:]
        if len(source_fields)%100==0:print(json.dumps({'fields':len(source_fields),'candidates':len(candidates)}),flush=True)
    chosen=candidates+[item for values in uncertain_bins.values() for _,item in values]
    regions={}
    for item in chosen:
        result=estimate_thickness(item['contrast_rgb'],item['contrast_covariance'],numeric)
        result.update({k:v for k,v in item.items() if k!='contrast_covariance'})
        if not item['interior_measurement_available']:
            result['status']='no_resolved_interior';result['qualification']+=' Whole-region colour only; unresolved interior.'
        regions[item['id']]=result;counts[result['status']]+=1
    examples={}
    for population in model.get('observed_populations',[]):
        examples[population['name']]={'measured_rgb':population['mean_rgb'],
            **estimate_thickness(population['mean_rgb'],np.zeros((3,3)),numeric)}
    if digest(source/'ledger.json')!=ledger_hash:raise ValueError('Analysis ledger changed during thickness attempt.')
    code_paths=[Path(__file__),WORKSPACE/'flakefinder/graphene_thickness.py',WORKSPACE/'flakefinder/material_profiles.py',WORKSPACE/'flakefinder/mote2_optics.py',profile_path,response_path]
    bundle={'schema':1,'run_id':run_id,'chip':chip,'source_plan_sha256':selection['plan_sha256'],
        'source_model_sha256':plan['model_sha256'],'source_ledger_sha256':ledger_hash,
        'source_fields':source_fields,'regions':regions,'ensemble':ensemble,'assumptions':assumptions,
        'code_and_reference_sha256':{str(p):digest(p) for p in code_paths},
        'operator_band_diagnostics':examples,'counts':dict(counts),'candidate_observations':len(candidates),
        'uncertain_observations_sampled':len(chosen)-len(candidates),'selection':'All useful selected observations plus up to40 hash-deterministic unknowns per0.1 red-contrast bin; other observations available lazily.',
        'layer_assignment_confirmed':False,'seconds_for_source_read_and_estimation':time.monotonic()-started}
    sha=save(output/'thickness.json',bundle)
    selector={'schema':1,'run_id':run_id,'source_plan_sha256':selection['plan_sha256'],
        'path':(output/'thickness.json').relative_to(project).as_posix(),'sha256':sha}
    save(output/'selection.json',selector)
    summary={k:v for k,v in bundle.items() if k not in ('source_fields','regions','ensemble')}
    summary.update(path=str(output/'thickness.json'),sha256=sha,selector=selector)
    save(output/'summary.json',summary)
    print(json.dumps(summary,indent=2),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True);parser.add_argument('--run',required=True)
    parser.add_argument('--version',default='operator-empirical-full-v1-thickness-v1')
    args=parser.parse_args();run(args.project,args.run,args.version)

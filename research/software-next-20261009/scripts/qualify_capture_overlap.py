"""Stationary acceptance plan by default; --execute is a future explicit trial.

No motion, objective or lamp changes. Normal operator-hold and hardware lease
checks apply before capture. Never run while another person owns the microscope.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--output',required=True)
    p.add_argument('--offline-verification',required=True);p.add_argument('--count',type=int,default=3)
    p.add_argument('--execute',action='store_true')
    args=p.parse_args()
    if not 3<=args.count<=6:p.error('Stationary trial supports3..6 paired captures.')
    if not args.execute:
        print(json.dumps({'mode':'dry_run_no_client','pairs':args.count,'motion':False,
            'requirements':['Explicit stationary microscope handoff','Operator hold cleared by owner',
                'Matching-source offline late-error/STOP fixture verification','Same20x fixed profile and stable field'],
            'outputs':['Paired full-frame TIFF/JSON','Timing and profile comparison','Source/profile-bound acceptance only on success']},indent=2));return
    from flakefinder.storage import prepare_acquisition_storage,resolve_output_path
    from flakefinder.capture_overlap import BACKEND,source_identity,profile
    from flakefinder.autofocus import focus_metrics
    from flakefinder.imaging import save_frame
    from flakefinder.vendor_lifecycle import acknowledge_raw
    from flakefinder.model import SafetyError,atomic_json
    import numpy as np
    verification=json.loads(Path(args.offline_verification).read_text())
    if verification.get('source_sha256')!=source_identity() or verification.get('late_error_tests_passed') is not True:
        raise SafetyError('Offline protocol verification does not match the staged capture source.')
    config=json.loads(Path(args.config).read_text());settings=dict(config['zen'])
    if not settings.get('job_template'):raise SafetyError('Stationary overlap trial requires the registered camera-only job.')
    settings.update(processing_overlap=False,processing_overlap_acceptance=None)
    # Check hold before importing/constructing any client, including dry-run errors.
    prepare_acquisition_storage(settings.get('job_output_dir','data/zen-captures'))
    from flakefinder.zen import ZenMicroscope
    output=resolve_output_path(args.output)
    if output.exists() and any(output.iterdir()):raise SafetyError('Use a new empty stationary-trial output.')
    output.mkdir(parents=True,exist_ok=True)
    scope=ZenMicroscope(**settings);rows=[]
    try:
        scope.lease.acquire();initial=scope.position()
        def stationary(frame):
            scope.check_cancel();actual=scope.position()
            for value in (actual,frame.position):
                if max(abs(getattr(value,k)-getattr(initial,k)) for k in ('x','y','z'))>.3:
                    raise SafetyError('Stationary trial moved by more than0.3um.')
            if not frame.metadata['zen_vendor_result']['job_unloaded']:raise SafetyError('Camera job did not finish unloading.')
        for i in range(args.count):
            first=time.perf_counter();baseline=scope.capture();baseline_seconds=time.perf_counter()-first;stationary(baseline)
            save_frame(output,f'baseline_{i:02d}',baseline);acknowledge_raw(scope,baseline,output/f'baseline_{i:02d}.tiff')
            started=time.perf_counter()
            # Explicit stationary trial uses the same private implementation
            # tested offline; production remains gated by the acceptance file.
            frame,metric=scope._capture_job(lambda f:focus_metrics(f.rgb))
            overlapped_seconds=time.perf_counter()-started;stationary(frame)
            if metric!=focus_metrics(frame.rgb):raise SafetyError('Overlapped metric differs from processing the returned raw frame.')
            keys=('camera_resources','camera_job_template','objective_position','illumination_percent','white_level')
            if (frame.rgb.shape!=baseline.rgb.shape or frame.rgb.dtype!=baseline.rgb.dtype or
                    any(frame.metadata.get(k)!=baseline.metadata.get(k) for k in keys)):
                raise SafetyError('Stationary paired raw/profile context differs.')
            relative=(np.median(frame.rgb.reshape(-1,3),axis=0)-np.median(baseline.rgb.reshape(-1,3),axis=0))/np.maximum(np.median(baseline.rgb.reshape(-1,3),axis=0),1)
            if np.max(abs(relative))>.03:raise SafetyError('Stationary RGB changed over3%; qualification withheld, not a material threshold.')
            save_frame(output,f'overlap_{i:02d}',frame);acknowledge_raw(scope,frame,output/f'overlap_{i:02d}.tiff')
            rows.append({'pair':i,'baseline_s':baseline_seconds,'overlap_s':overlapped_seconds,'relative_rgb_change':relative.tolist()})
            atomic_json(output/'trial.json',{'state':'running','pairs':rows})
        accepted={'schema':1,'backend':BACKEND,'stationary_verified':True,'source_sha256':source_identity(),
            'profile':profile(scope.job_template,scope.camera_resources,scope.expected_objective_position,scope.camera_white_level),
            'raw_profile_equivalent':True,'late_error_tests_passed':True,'completed_pairs':len(rows),'pairs':rows,
            'stationary_position_um':asdict(initial),'offline_verification_sha256':hashlib.sha256(Path(args.offline_verification).read_bytes()).hexdigest(),
            'qualification':'Fixed-position acceptance only; no motion/throughput claim.3% RGB repeat gate is an engineering stability check.'}
        atomic_json(output/'acceptance.json',accepted);atomic_json(output/'trial.json',{'state':'complete','pairs':rows})
    except BaseException as exc:
        scope.stop();atomic_json(output/'trial.json',{'state':'failed','error':str(exc),'pairs':rows});raise
    finally:scope.close()

if __name__=='__main__':main()

"""Read idle microscope state under the acquisition lease; never arm or move."""
from pathlib import Path
from dataclasses import asdict
import argparse
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from flakefinder.config import load_config,connect
from flakefinder.lease import HardwareLease
from flakefinder.model import atomic_json,Limits,SafetyError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--expected-frame',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();config=load_config(args.config)
    if config['backend']!='zen':raise ValueError('This readback check expects ZEN.')
    expected=json.loads(args.expected_frame.with_suffix('.json').read_text())['position']
    lease=HardwareLease(config['zen'].get('endpoint','localhost:5002'),ROOT/'data/locks')
    lease.acquire();scope=None
    try:
        scope=connect(config)
        pb,workflow=scope._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
        def idle():
            running=scope._rpc(workflow,'IsJobRunning',pb.WorkflowServiceIsJobRunningRequest()).is_job_running
            loaded=scope._rpc(workflow,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest()).is_job_template_loaded
            if running or loaded:raise SafetyError('ZEN has an active or loaded job; no recovery started.')
        idle();first=scope.position();time.sleep(.5);second=scope.position();idle()
        Limits(**config['limits']).check(second)
        delta=max(abs(getattr(second,k)-expected[k]) for k in ('x','y','z'))
        drift=max(abs(getattr(second,k)-getattr(first,k)) for k in ('x','y','z'))
        if delta>.5 or drift>.05:raise SafetyError('Position differs from the saved stopped capture or is not stationary.')
        optics=scope.hardware_controls().verify_optics(config['zen']['expected_objective_position'])
        result={'checked_at':time.time(),'stationary':True,'job_running':False,'job_loaded':False,
                'position_um':asdict(second),'expected_position_um':expected,'maximum_difference_um':delta,
                'drift_um':drift,'objective_position':optics['position'],
                'lamp_percent':scope.hardware_controls().lamp_percent(),
                'scope':'Read-only API inspection. No arm, motion, camera acquisition, stop or settings commands.'}
        atomic_json(args.output,result);print(json.dumps(result,indent=2))
    finally:
        try:
            if scope is not None:scope.close()
        finally:lease.close()


if __name__=='__main__':main()

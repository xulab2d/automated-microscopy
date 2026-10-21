"""Verify the observed no-op and real one-micrometre motion through the adapter."""
from pathlib import Path
from dataclasses import asdict
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect, load_config, arm
from flakefinder.model import atomic_json

def main():
    cfg=load_config('configs/zen-20x-chip-batch.json')
    scope=connect(cfg)
    evidence={}
    try:
        arm(scope,cfg)
        start=scope.position()
        if abs(start.z-15628.125)>.5:
            raise RuntimeError('Position differs from this failure reproduction.')
        evidence.update(start=asdict(start),spec=scope.focus_motion_spec,
                        target_tolerance_um=scope.focus_target_tolerance_um,observations=[])
        for target in (15627.96850003893,start.z-1,15627.96850003893):
            actual=scope.move_z(target)
            evidence['observations'].append({'target_z_um':target,'actual':asdict(actual),
                                             'error_um':abs(actual.z-target)})
        evidence['state']='passed'
    except Exception as exc:
        evidence.update(state='failed',error=str(exc))
        raise
    finally:
        scope.close()
        atomic_json('data/chip-batch-01/focus-precision-validation.json',evidence)
        print(json.dumps(evidence),flush=True)

if __name__=='__main__':main()

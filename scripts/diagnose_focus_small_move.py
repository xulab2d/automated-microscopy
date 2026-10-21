"""Record one guarded submicron Z command and its vendor response."""
from pathlib import Path
from dataclasses import asdict
import json, sys, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config, connect, arm
from flakefinder.model import atomic_json

def main():
    cfg = load_config('configs/zen-20x-chip-batch.json')
    scope = connect(cfg)
    record = {'started_at': time.time()}
    try:
        arm(scope, cfg)
        scope.timeout_s = 5
        initial = scope.position()
        record['initial'] = asdict(initial)
        if abs(initial.z - 15628.125) > .5:
            raise RuntimeError('Stage has moved from the recorded failure; diagnostic not applicable.')
        rpc = scope._motion_rpc
        def instrument(stub, request):
            response = rpc(stub, request)
            record.setdefault('commands', []).append({
                'axis': 'z' if stub is scope.focus else 'xy',
                'target_z_um': request.value * 1e6 if stub is scope.focus else None,
                'position_changed': getattr(response, 'position_changed', None),
                'readback': asdict(scope.position()),
            })
            return response
        scope._motion_rpc = instrument
        scope.move_z(15627.96850003893)
    except Exception as exc:
        record['error'] = str(exc)
    finally:
        try: record['final'] = asdict(scope.position())
        finally: scope.close()
        atomic_json('data/chip-batch-01/focus-small-move-diagnostic.json', record)
        print(json.dumps(record), flush=True)

if __name__ == '__main__': main()

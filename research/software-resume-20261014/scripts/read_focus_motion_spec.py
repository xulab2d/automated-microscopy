"""Read focus positioning specifications from the active MTB hardware."""
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect, load_config
from flakefinder.model import atomic_json

def main():
    scope = connect(load_config('configs/zen-20x-chip-batch.json'))
    try:
        mtb = scope.hardware_controls()
        focus = mtb.Continual(mtb.root.GetComponent('MTBFocus'))
        result = {'configuration': str(mtb.config_path), 'units': 'µm',
                  'position_um': float(focus.GetPosition('µm')),
                  'step_width_um': float(focus.StepWidth('µm')),
                  'max_deviation_um': float(focus.MaxDeviation('µm'))}
        atomic_json('data/chip-batch-01/focus-motion-spec.json', result)
        print(json.dumps(result), flush=True)
    finally: scope.close()

if __name__ == '__main__': main()

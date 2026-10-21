"""Load this separate offline patch over its documented baseline for tests only."""
from pathlib import Path
import importlib.util
import json
import sys

STAGE = Path(__file__).resolve().parents[1]
baseline = json.loads((STAGE / 'BASELINE.json').read_text())
sys.path.insert(0, str(Path(baseline['source']).parents[1]))
import flakefinder

name = 'flakefinder.material_terraces'
spec = importlib.util.spec_from_file_location(name, STAGE / 'flakefinder/material_terraces.py')
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
flakefinder.material_terraces = module

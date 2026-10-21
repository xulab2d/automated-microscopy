"""Run affected existing tests against isolated implementation in THIS process."""
from pathlib import Path
import json,sys,time
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import flakefinder
flakefinder.__path__.insert(0,str(HERE/'flakefinder'))
import pytest

if __name__=='__main__':
    # No hardware clients are instantiated by these offline fixture-based tests.
    start=time.perf_counter()
    code=pytest.main(sys.argv[1:])
    print(json.dumps({'isolated_path':str(HERE/'flakefinder'),'exit_code':code,'runtime_s':time.perf_counter()-start}))
    raise SystemExit(code)

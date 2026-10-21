from pathlib import Path
from types import SimpleNamespace
import sys,time,json
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'research/software-next-20261009'))
from flakefinder.vendor_lifecycle import check_pending
from flakefinder.locked_hashes import LockedHashes
root=Path(r"G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy\data\zen-captures")
cache=LockedHashes();scope=SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=root,_vendor_jobs={})
rows=[]
try:
 for use_cache in [True,False,True,False,True,False,True]:
  scope._vendor_review_hashes=cache if use_cache else None;t=time.perf_counter();result=check_pending(scope)
  row={'cache':use_cache,'seconds':time.perf_counter()-t,'result':result,'locked_files':len(cache.entries),'text_bytes':cache.text_bytes};rows.append(row);print(json.dumps(row),flush=True)
finally:cache.close()
(Path(__file__).parent/'cache-comparison.json').write_text(json.dumps(rows,indent=2))

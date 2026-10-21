"""Coarse physical survey at the already focused Z, through the verified API adapter."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm,transform_from_config
from flakefinder.scan import ScanPlan,run_scan
from flakefinder.report import write_report
cfg=load_config('configs/zen-survey.json');s=connect(cfg)
try:
    arm(s,cfg)
    run_scan(s,ScanPlan(**cfg['survey']),transform_from_config(cfg),'data/grid-discovery-01',context=cfg['context'],
             callback=lambda event,frame,info:print(event,info,flush=True) if event=='tile' else None)
    write_report('data/grid-discovery-01')
except BaseException:s.stop();raise
finally:s.close()

from pathlib import Path
import sys,runpy,json,hashlib
src=Path(__file__).parent/'software-next-20261009'
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
prior='scan-20261011-140151-bdf8664e'
assert json.loads((project/'scan-control/current.json').read_text())['id']==prior
assert not (project/'data'/prior/'supervision/adaptive-focus-continuation.json').exists()
report=json.loads((project/'research/session-20261011-geometry-continuity/repair-report.json').read_text())
for name,h in report['source_after_sha256'].items():assert hashlib.sha256((src/'flakefinder'/name).read_bytes()).hexdigest()==h
sys.argv=['resume_adaptive_focus.py','--project',str(project),'--prior',prior]
runpy.run_path(str(src/'scripts/resume_adaptive_focus.py'),run_name='__main__')

from pathlib import Path
import sys,runpy,json
src=Path(__file__).parent/'software-next-20261009'
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
prior='scan-20261011-133357-4a1271ad'
assert json.loads((project/'scan-control/current.json').read_text())['id']==prior
assert not (project/'data'/prior/'supervision/adaptive-focus-continuation.json').exists()
sys.argv=['resume_adaptive_focus.py','--project',str(project),'--prior',prior]
runpy.run_path(str(src/'scripts/resume_adaptive_focus.py'),run_name='__main__')

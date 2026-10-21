from pathlib import Path
import json,hashlib,shutil
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
root=project/'research/session-20261011-native-memory';src=Path(__file__).parent/'software-next-20261009'
report=json.loads((root/'repair-report.json').read_text())
for n in ('flakefinder/acquisition_resources.py','flakefinder/scan_worker.py','flakefinder/scan_service.py','flakefinder/zen.py'):
 d=root/'source-before'/n;d.parent.mkdir(parents=True,exist_ok=True)
 if not d.exists():shutil.copy2(src/n,d)
 report['source_before_sha256'][n]=hashlib.sha256(d.read_bytes()).hexdigest()
report['authorization_update']='Human explicitly requested removal of arbitrary free-RAM allocations/checks; use operation estimates and actual Windows allocation capacity. Supersedes prior fixed 1.5GiB capture floor.'
(root/'repair-report.json').write_text(json.dumps(report,indent=2))
print('Preserved source and explicit capacity-policy authorization.')

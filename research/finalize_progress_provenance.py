from pathlib import Path
import hashlib,json
root=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy\research\session-20261011-progress-continuation')
path=root/'source-before/scripts/watch_scan_memory.ps1'
raw=path.read_text();target='486f3af3acc1310a51cf1375574f6e09a1428d62bb36382fcbfab54a2ed22a0c'
matched=None
for blanks in range(1,5):
    import re
    candidate=re.sub(r'\n+function Test-CodexRenderer','\n'*blanks+'function Test-CodexRenderer',raw)
    for newline in ('\n','\r\n'):
        data=candidate.replace('\n',newline).encode()
        if hashlib.sha256(data).hexdigest()==target:matched=data
assert matched is not None,'Reconstructed bytes must match the prior source-pinned hash exactly'
path.write_bytes(matched)
report=json.loads((root/'repair-report.json').read_text());report['source_before_sha256']['scripts/watch_scan_memory.ps1']=target
report['source_before_provenance']={'scripts/watch_scan_memory.ps1':'Reconstructed exact prior bytes, independently matched to prior pinned repair-report SHA256.'}
(root/'repair-report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report['source_before_provenance']))

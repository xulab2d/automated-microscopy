"""Publish a new, content-verified code release; never replace live acquisition."""
from pathlib import Path
import argparse
import hashlib
import json
import time


def publish(source, project, version):
    source, project = Path(source).resolve(strict=True), Path(project).resolve(strict=True)
    marker = json.loads((project/'.flakefinder-project.json').read_text(encoding='utf-8-sig'))
    if not marker.get('project_id'):
        raise ValueError('Destination is not a marked project')
    if not version or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in version):
        raise ValueError('Invalid release name')
    target = (project/'software'/'releases'/version).resolve()
    if not target.is_relative_to(project) or target.exists():
        raise ValueError('Use a new confined release directory')
    source_files=[]
    for folder in ('flakefinder','configs','scripts','tests','docs'):
        source_files.extend(p for p in (source/folder).rglob('*') if p.is_file()
            and '__pycache__' not in p.parts and not any(part.startswith('.test-') for part in p.parts)
            and p.suffix not in ('.pyc','.pyo'))
    source_files.extend(p for p in (source/'research'/'acquisition-speed').glob('*') if p.is_file())
    source_files.extend(p for p in (source/'research'/'residue-sieves-resume-v2').glob('*.py') if p.is_file())
    for name in ('pyproject.toml','README.md','requirements-lock.txt','requirements-analysis-lock.txt','requirements-appearance-windows-lock.txt','OPERATOR-HOLD.json','OFFLINE-WORKBENCH.txt',
                 'BASELINE.json','tiff-codec-benchmark.json','RELEASE-NOTES.txt','VALIDATION.json'):
        if (source/name).is_file():source_files.append(source/name)
    target.mkdir(parents=True,exist_ok=False)
    records=[]
    for original in sorted(source_files):
        relative=original.relative_to(source)
        if original.is_symlink() or not original.resolve().is_relative_to(source):
            raise ValueError('Release source links are not allowed')
        raw=original.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as stream:stream.write(raw)
        if hashlib.sha256(destination.read_bytes()).hexdigest()!=digest:
            raise ValueError('Release readback failed: '+str(relative))
        records.append(dict(path=relative.as_posix(),bytes=len(raw),sha256=digest))
    manifest=dict(schema_version=1,version=version,project_id=marker['project_id'],created_at=time.time(),
        files=records,bytes=sum(r['bytes'] for r in records),hardware_accessed=False,
        publication='Mounted Drive write and content readback verified; remote synchronization not asserted')
    (target/'RELEASE.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return dict(path=str(target),files=len(records),bytes=manifest['bytes'])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True)
    p.add_argument('--project',required=True);p.add_argument('--version',required=True)
    a=p.parse_args();print(json.dumps(publish(a.source,a.project,a.version)))

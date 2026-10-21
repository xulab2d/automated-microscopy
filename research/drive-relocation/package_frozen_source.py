"""Freeze-triggered laptop source snapshot; never replaces canonical project files."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import zipfile

import bounded_transfer as transfer
from complete_transfer import freeze_ready
import run_transfer

DIRECTORIES=('flakefinder','scripts','configs','tests','docs')


def selected_files(root):
    paths=[]
    def allowed(path):
        relative=path.relative_to(root).as_posix()
        return not any(fnmatch.fnmatchcase(relative.lower(),pattern.lower()) for pattern in run_transfer.EXCLUDE_FILES)
    for path in root.iterdir():
        if path.is_file() and path.suffix.lower() in ('.py','.toml','.txt','.md','.cmd','.json') and allowed(path):
            paths.append(path)
    for directory in DIRECTORIES:
        for parent,dirs,names in os.walk(root/directory,followlinks=False):
            dirs[:]=[name for name in dirs if name not in ('__pycache__','.pytest_cache') and not name.startswith('.test-')]
            for name in names:
                path=Path(parent)/name
                if allowed(path):
                    paths.append(path)
    return sorted(set(paths),key=lambda path:path.relative_to(root).as_posix())


def build(root,destination,freeze_file,state_folder):
    root=Path(root).resolve()
    destination=transfer.ensure_destination(destination)
    state_folder=Path(state_folder).resolve();state_folder.mkdir(parents=True,exist_ok=True)
    if not freeze_ready(freeze_file,root):
        raise ValueError('Explicit source freeze is required.')
    if destination.exists() or destination.with_suffix(destination.suffix+'.partial').exists():
        raise FileExistsError('Source snapshot path already exists; preserve it and choose a new version.')
    paths=selected_files(root)
    size=sum(path.stat().st_size for path in paths)
    if shutil.disk_usage(transfer.probe.DB.anchor).free < 8*transfer.GIB+2*size:
        raise OSError('Source snapshot cannot fit while preserving disk reserve.')
    freeze=transfer.read_json(freeze_file)
    manifest={'schema':1,'purpose':'Current frozen source code for laptop use; scientific images remain in the complete mirror.',
        'source_root':str(root),'created_at_utc':transfer.probe.now(),'source_freeze':freeze,'files':[],
        'included_directories':list(DIRECTORIES),'excluded':'Images, analysis outputs, environments, caches and credentials; these are not replaced by this code convenience archive.',
        'canonical_project_files_modified':False,'source_originals_retained':True}
    temporary=destination.with_suffix(destination.suffix+'.partial')
    with zipfile.ZipFile(temporary,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in paths:
            before=path.stat()
            if path.is_symlink() or getattr(before,'st_file_attributes',0)&1024:
                raise ValueError('Source snapshot contains a reparse path: '+str(path))
            content=path.read_bytes()
            after=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                raise ValueError('Source changed during snapshot: '+str(path))
            relative=path.relative_to(root).as_posix()
            archive.writestr(relative,content)
            manifest['files'].append({'path':relative,'bytes':len(content),'source_mtime_ns':before.st_mtime_ns,
                'sha256':hashlib.sha256(content).hexdigest()})
        archive.writestr('SOURCE-SNAPSHOT-MANIFEST.json',json.dumps(manifest,indent=2))
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise ValueError('Source ZIP CRC verification failed; partial retained.')
    for row in manifest['files']:
        stat=(root/row['path']).stat()
        if (stat.st_size,stat.st_mtime_ns)!=(row['bytes'],row['source_mtime_ns']):
            raise ValueError('Source changed before package finalization; partial retained.')
    temporary.resolve().relative_to(destination.parent.resolve())
    temporary.rename(destination)
    hashes=transfer.probe.hashes(destination)
    manifest.update(archive=str(destination),archive_hashes=hashes,file_count=len(paths),uncompressed_bytes=size)
    transfer.write_json(state_folder/'source-manifest.json',manifest)
    transfer.write_json(destination.with_name(destination.name+'.manifest.json'),manifest)
    return {'source':str(destination),'destination':str(destination),'relative_path':destination.name,
        **hashes,'source_mtime_ns':hashes['mtime_ns'],'mounted_sha256':hashes['sha256'],
        'state':'mounted_copy_verified','generated_directly_in_drive':True,'source_originals_retained':True}


def run(root,destination,freeze_file,state_folder,stop_file):
    state_folder=Path(state_folder).resolve();state_folder.mkdir(parents=True,exist_ok=True)
    def state(value,**detail):
        data={'state':value,'pid':os.getpid(),'updated_at_utc':transfer.probe.now(),**detail}
        transfer.write_json(state_folder/'package-state.json',data)
        print(json.dumps(data),flush=True)
    def cancel():
        if Path(stop_file).exists():
            raise transfer.RelocationStopped('Relocation STOP requested.')
    try:
        state('waiting_for_source_freeze')
        while not freeze_ready(freeze_file,root):
            cancel();time.sleep(15)
        cancel();state('packaging')
        row=build(root,destination,freeze_file,state_folder)
        state('mounted_copy_verified',archive=row)
        started=time.monotonic()
        while True:
            cancel()
            first=transfer.probe.metadata_snapshots([row])[0]
            time.sleep(10)
            second=transfer.probe.metadata_snapshots([row])[0]
            gate=transfer.client_gate(first,second,row)
            matched=all(value for key,value in gate['snapshot1_checks'].items() if key!='no_pending_operations')
            matched &= all(value for key,value in gate['snapshot2_checks'].items() if key!='no_pending_operations')
            matched &= gate['cloud_ancestry_stable'] and gate['elapsed_seconds']>=10
            if matched:
                proof={'archive':row,'client_confirmation':gate,'metadata_snapshots':[first,second],
                    'source_originals_retained':True,'source_removal_authorized':False,
                    'url':'https://drive.google.com/file/d/'+gate['cloud_id']+'/view'}
                transfer.write_json(state_folder/'upload-evidence.json',proof)
                state('client_confirmed' if gate['client_confirmed'] else 'content_matched',**proof)
                return 0
            if time.monotonic()-started>=7200:
                state('blocked_upload_confirmation',archive=row)
                return 3
            time.sleep(20)
    except Exception as exc:
        state('stopped' if isinstance(exc,transfer.RelocationStopped) else 'blocked',error=repr(exc))
        return 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--freeze-file',required=True)
    parser.add_argument('--state-folder',required=True)
    parser.add_argument('--stop-file',required=True)
    args=parser.parse_args()
    raise SystemExit(run(args.source,args.destination,args.freeze_file,args.state_folder,args.stop_file))

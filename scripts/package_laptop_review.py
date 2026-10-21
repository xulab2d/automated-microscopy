"""Make a preview-only portable review ZIP; never duplicate scientific TIFFs."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RESERVE = 8 * 1024**3


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(4*1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def package(project, destination):
    project, destination = Path(project).resolve(), Path(destination).resolve()
    viewer = project/'laptop-viewer'
    manifest = json.loads((viewer/'snapshot.json').read_text(encoding='utf-8'))
    dataset = (viewer/manifest['data_relative']).resolve()
    if not dataset.is_relative_to(project):
        raise ValueError('The archive requires a dataset within the project folder')
    if destination.exists() or destination.with_suffix(destination.suffix+'.partial').exists():
        raise FileExistsError('Archive or unfinished archive already exists; refusing overwrite')
    inputs = {}
    names = set(manifest['artifacts']) | {'Launcher.py','README.txt','Start Windows.cmd','snapshot.json','preview-assets.json'}
    for relative in names:
        source = (viewer/relative).resolve()
        if not source.is_relative_to(viewer) or not source.is_file():
            raise ValueError('Missing or escaped package source: '+relative)
        expected = manifest['artifacts'].get(relative)
        if expected and sha256(source) != expected:
            raise ValueError('Snapshot changed: '+relative)
        inputs[source.relative_to(project).as_posix()] = source
    for relative, metadata in manifest['data_assets'].items():
        source = (dataset/relative).resolve()
        if not source.is_relative_to(dataset) or not source.is_file() or source.suffix.lower() not in ('.jpg','.jpeg','.png'):
            raise ValueError('Missing or escaped preview/mask: '+relative)
        if source.stat().st_size != metadata['bytes']:
            raise ValueError('Preview or mask changed: '+relative)
        inputs[source.relative_to(project).as_posix()] = source
    total = sum(path.stat().st_size for path in inputs.values())
    free = shutil.disk_usage(destination.parent).free
    # Use uncompressed bytes as a conservative archive bound, plus headers.
    if free-total-len(inputs)*1024 < RESERVE:
        raise RuntimeError('Archive would violate the 8 GiB free-space reserve')
    temporary = destination.with_suffix(destination.suffix+'.partial')
    rows = []
    with zipfile.ZipFile(temporary,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
        for n,(name,source) in enumerate(sorted(inputs.items())):
            digest = hashlib.sha256();size = 0
            with source.open('rb') as incoming, archive.open(name,'w',force_zip64=True) as outgoing:
                for chunk in iter(lambda:incoming.read(4*1024*1024),b''):
                    digest.update(chunk);size += len(chunk);outgoing.write(chunk)
            rows.append({'path':name,'bytes':size,'sha256':digest.hexdigest()})
            if (n+1)%500 == 0:
                print('Archived '+str(n+1)+' / '+str(len(inputs))+' files',flush=True)
        readme = ('LAPTOP REVIEW — PREVIEW-ONLY DOWNLOAD\n\n'
            'Extract this entire ZIP, preserving its folders.\n'
            'Open laptop-viewer/README.txt, then run laptop-viewer/Launcher.py\n'
            'with Python 3.9+. Windows: py -3 Launcher.py; macOS: python3 Launcher.py.\n'
            'No pip installation, ZEN, microscope, Windows hardware drivers or internet is needed.\n\n'
            'Includes all saved raster previews, exact instance-mask PNGs, full per-feature\n'
            'review snapshots, recovery identities/reasons and interactive contrast plots.\n'
            'It deliberately omits raw TIFF/CZI files and the development environment.\n'
            'The full project/data/code transfer is separate on Drive; retain that for\n'
            'future quantitative analysis. JPEGs are display previews, not raw radiometry.\n'
            'The original analysis files are never replaced by these viewer snapshots.\n'
            'There are no confirmed material identities or monolayer assignments.\n')
        archive.writestr('START HERE.txt',readme.encode('utf-8'))
        archive.writestr('laptop-viewer/ARCHIVE-CONTENTS.json',json.dumps({'schema':1,
            'qualification':'Preview-only review subset; raw TIFF/CZI and development project transferred separately.',
            'image_count':manifest['image_count'],'files':rows},indent=2).encode('utf-8'))
    print('Verifying archive CRCs and SHA256...',flush=True)
    with zipfile.ZipFile(temporary) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError('Archive verification failed: '+bad)
        entry_count = len(archive.infolist())
    archive_hash = sha256(temporary)
    temporary.replace(destination)
    report = {'schema':1,'archive':destination.name,'sha256':archive_hash,'bytes':destination.stat().st_size,
        'zip_entries':entry_count,'source_file_count':len(rows),'source_bytes':total,'image_count':manifest['image_count'],
        'snapshot_created_at':manifest['created_at'],'verified_at':time.time(),'crc_verified':True,
        'free_bytes_before':free,'free_bytes_after':shutil.disk_usage(destination.parent).free,
        'minimum_free_reserve_bytes':RESERVE,'files':rows}
    destination.with_suffix(destination.suffix+'.manifest.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return {k:v for k,v in report.items() if k!='files'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,default=ROOT/'Laptop Review.zip')
    args = parser.parse_args()
    print(json.dumps(package(args.project,args.output),indent=2))

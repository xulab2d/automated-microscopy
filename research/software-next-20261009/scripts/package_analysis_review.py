"""Export completed, static analysis reviews directly to the managed Drive.

The ZIP needs no microscope connection, Python installation, or network scripts.
Raw scientific data and the complete project are transferred separately.
"""
from pathlib import Path
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flakefinder.storage import project_root, verify_project


def main():
    verify_project(write_probe=True)
    shared = project_root()
    campaign = shared / 'data/whole-chip-standardized-01'
    appearance = campaign / 'analysis-dinov2-appearance'
    terraces = campaign / 'chip001/analysis-terraces-v3-accepted-parents-qc-01'
    status = json.loads((appearance / 'progress.json').read_text())
    if status.get('state') != 'complete':
        raise RuntimeError('Appearance clustering has not completed successfully.')
    if not (terraces / 'review.html').is_file():
        raise RuntimeError('Accepted-parent terrace review is missing.')
    destination = shared / 'Analysis Review 2026-10-09.zip'
    if destination.exists():
        raise FileExistsError('Versioned analysis package already exists; preserve it.')
    sources = []
    for path in appearance.rglob('*'):
        if path.is_file() and path.relative_to(appearance).parts[0] != 'fields':
            sources.append((path, 'appearance/' + path.relative_to(appearance).as_posix()))
    for path in terraces.rglob('*'):
        if path.is_file():
            sources.append((path, 'terraces/' + path.relative_to(terraces).as_posix()))
    for name in ('ACCEPTED-PARENTS-RESULT.json', 'chip-substrate-cli-integration-01.json'):
        sources.append((shared / 'research/segmentation-v3-audit' / name, 'controls/' + name))
    for name in ('docs/OFFLINE-ANALYSIS.txt', 'START-HERE-LAPTOP.txt',
                 'research/mote2-optical-model-03/HANDOFF.txt'):
        sources.append((ROOT / name, name))
    html = f'''<!doctype html><meta charset="utf-8"><title>Microscopy analysis review</title>
<style>body{{max-width:900px;margin:50px auto;padding:0 24px;background:#111820;color:#dce7ee;font:18px/1.6 system-ui}}a{{color:#75d6e8}}h1{{line-height:1.2}}li{{margin:12px 0}}</style>
<h1>Microscopy analysis review</h1><p>9 October 2026 · Saved data only</p>
<p>Extract the entire ZIP, then open this file. No installation or microscope connection is needed.</p>
<ul><li><a href="appearance/review.html">Review appearance clusters</a> — {status['objects']:,} saved regions from {status['fields_done']:,} successfully analyzed fields, grouped by DINOv2 on the local CPU. Cluster labels are provisional; groups may be mixed.</li>
<li><a href="terraces/review.html">Review preserved boundaries and internal terraces</a> — a feature field and blank-substrate control with chip-level substrate QC. Broad parents and conditional optical matches are separate views. The earlier off-chip check is recorded in the <a href="controls/ACCEPTED-PARENTS-RESULT.json">control audit</a>.</li>
<li><a href="terraces/chip-domain.jpg">View the chip overview and boundary</a></li>
<li><a href="docs/OFFLINE-ANALYSIS.txt">Methods, limitations, and reproducible commands</a></li>
<li><a href="research/mote2-optical-model-03/HANDOFF.txt">Physical contrast model and primary sources</a></li></ul>
<p>No validated monolayers or crystal-quality scores are assigned. The current physical curve is conditional on incompletely calibrated illumination and published thick-material optical constants. The pilot's single optical match has only 2.02 µm² of uniform inscribed area and is not a useful thin-flake result.</p>
<p>The separate <b>Laptop Review.zip</b> contains the original full image gallery and RGB contrast plot. The complete raw-data transfer has its own <b>TRANSFER-STATUS.txt</b>; this smaller review package is not proof that all raw files have uploaded.</p>'''
    manifest = {'schema': 1, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'scope': 'Static analysis review; raw data and full embedding checkpoints remain in the shared project.',
                'appearance_status': status, 'files': []}
    temporary = destination.with_suffix('.zip.partial')
    with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
        for source, relative in sources:
            before = source.stat()
            data = source.read_bytes()
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError('Analysis source changed during export: ' + str(source))
            archive.writestr(relative, data)
            manifest['files'].append({'path': relative, 'bytes': len(data),
                                      'sha256': hashlib.sha256(data).hexdigest()})
        archive.writestr('index.html', html)
        manifest['files'].append({'path': 'index.html', 'bytes': len(html.encode()),
                                 'sha256': hashlib.sha256(html.encode()).hexdigest()})
        archive.writestr('MANIFEST.json', json.dumps(manifest, indent=2))
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP CRC verification failed.')
        for row in manifest['files']:
            if hashlib.sha256(archive.read(row['path'])).hexdigest() != row['sha256']:
                raise RuntimeError('ZIP member verification failed: ' + row['path'])
    temporary.rename(destination)
    value = hashlib.sha256()
    with destination.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    receipt = {'path': str(destination), 'bytes': destination.stat().st_size,
               'sha256': value.hexdigest(), 'files': len(manifest['files']) + 1,
               'created_at_utc': manifest['created_at_utc'], 'cloud_upload_verified': False}
    destination.with_suffix('.zip.receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()

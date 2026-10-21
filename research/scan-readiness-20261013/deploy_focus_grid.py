"""Review/deploy the tested isolated upgrade after the acquisition owner closes.

Default is a read-only delta. --deploy requires the exact closed donor and its
unchanged production source fingerprint. This does not stop or start hardware.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / 'research/software-next-20261009'
STAGED = ROOT / 'research/software-focus-grid-20261013'
OUTPUT = ROOT / 'research/scan-readiness-20261013/focus-grid-upgrade'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--donor', required=True)
    parser.add_argument('--deploy', action='store_true')
    parser.add_argument('--staged', type=Path, default=STAGED)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    staged, output = args.staged.resolve(), args.output.resolve()
    if (not staged.is_relative_to(ROOT / 'research') or staged == PRODUCTION
            or not output.is_relative_to(ROOT / 'research')):
        raise ValueError('Staging and evidence directories must be inside workspace research')
    project = Path(read(ROOT / 'configs/storage.json')['project_root'])
    donor = project / 'data' / args.donor
    if donor.resolve().parent != (project / 'data').resolve():
        raise ValueError('Invalid donor path')
    changes = []
    for directory in ('flakefinder', 'tests'):
        for source in sorted((staged / directory).rglob('*')):
            if not source.is_file() or source.suffix not in ('.py', '.html'):
                continue
            relative = source.relative_to(staged)
            target = PRODUCTION / relative
            before = digest(target) if target.exists() else None
            after = digest(source)
            if before != after:
                changes.append({'path': relative.as_posix(), 'before_sha256': before,
                                'after_sha256': after})
    report = {'donor_run': args.donor, 'files': changes, 'deployed': False}
    if args.deploy:
        sys.path.insert(0, str(PRODUCTION))
        from flakefinder.scan_continuation import process_alive_strict
        from flakefinder.scan_service import source_files
        status = read(donor / 'scan-status.json')
        worker = read(donor / 'worker-start.json')
        request = read(donor / 'scan-request.json')
        if status.get('hardware_closed') is not True or process_alive_strict(worker['pid']):
            raise RuntimeError('Acquisition owner has not closed')
        if status.get('pid') != worker['pid'] or status.get('id') != args.donor:
            raise RuntimeError('Acquisition owner identity differs')
        request_hash = digest(donor / 'scan-request.json')
        if status.get('request_sha256') != request_hash or worker.get('request_sha256') != request_hash:
            raise RuntimeError('Acquisition request identity differs')
        if source_files(PRODUCTION / 'flakefinder') != request['source_release']['files']:
            raise RuntimeError('Production source changed since this acquisition began')
        if not changes:
            raise RuntimeError('No upgrade changes')
        backup = output / 'production-before'
        backup.mkdir(parents=True, exist_ok=False)
        for change in changes:
            relative = Path(change['path'])
            source, target = staged / relative, PRODUCTION / relative
            if digest(source) != change['after_sha256']:
                raise RuntimeError('Staged source changed during deployment')
            if target.exists():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if digest(target) != change['after_sha256']:
                raise RuntimeError('Deployed source hash differs')
        report.update(deployed=True, backup=str(backup))
        with (output / 'deployment.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

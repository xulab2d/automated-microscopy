"""Publish verified isolated recovery artifacts; never edit acquisition/base analysis."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.model import atomic_json


def digest(data):
    return hashlib.sha256(data).hexdigest()


def publish(result_path, chip_root, output):
    result_path, chip_root, output = map(lambda p: Path(p).resolve(), (result_path, chip_root, output))
    if output.parent != chip_root or not output.name.startswith('analysis-') or output.exists():
        raise ValueError('Use a new analysis-* folder immediately inside the chip directory')
    master = json.loads(result_path.read_text(encoding='utf-8'))
    if Path(master['source_folder']).resolve() != chip_root:
        raise ValueError('Recovery source folder differs from target chip')
    acquisition = json.loads((chip_root/'manifest.json').read_text(encoding='utf-8'))
    base = chip_root/'analysis-staged-features'
    primary = json.loads((base/'analysis.json').read_text(encoding='utf-8'))
    failed = {r['tile_key']: r['error'] for r in primary['failed_tiles']}
    output.mkdir()
    artifacts = {}

    def copy(source, relative, expected=None):
        source = Path(source).resolve()
        raw = source.read_bytes()
        sha = digest(raw)
        if expected is not None and sha != expected:
            raise ValueError('Original artifact checksum mismatch: '+str(source))
        target = output/relative
        if not target.resolve().is_relative_to(output):
            raise ValueError('Copy destination escapes publication')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(target.read_bytes()) != sha:
                raise ValueError('Refusing to overwrite a different archived artifact')
        else:
            with target.open('xb') as stream:
                stream.write(raw)
        if digest(target.read_bytes()) != sha:
            raise ValueError('Published artifact readback mismatch')
        artifacts[Path(relative).as_posix()] = sha
        return Path(relative).as_posix()

    copy(result_path, 'archive/recovery-result.json')
    copy(result_path.with_name('RECOVERY.txt'), 'archive/RECOVERY.txt')
    copy(result_path.with_name('recovery-catalog.json'), 'archive/recovery-catalog.json')
    copy(base/'analysis.json', 'archive/primary-analysis.json')
    copy(chip_root/'manifest.json', 'archive/acquisition-manifest.json')
    versions = {}
    for version in master['versions']:
        folder = Path(version['folder']).resolve()
        if folder.parent != result_path.parent:
            raise ValueError('Version is outside the recovery report directory')
        name = folder.name
        pipeline = json.loads((folder/'pipeline.json').read_text(encoding='utf-8'))
        if pipeline['fingerprint'] != version['fingerprint']:
            raise ValueError('Version fingerprint mismatch')
        copy(folder/'analysis.json', 'versions/'+name+'/analysis.json', version['analysis_sha256'])
        copy(folder/'pipeline.json', 'versions/'+name+'/pipeline.json')
        copy(folder/'original-failures.json', 'versions/'+name+'/original-failures.json', pipeline['original_failures_sha256'])
        for relative, sha in pipeline['source_code_sha256'].items():
            relative = Path(relative)
            copy(folder/'source-code'/relative, Path('versions')/name/'source-code'/relative, sha)
        for record in (folder/'fields').glob('*.json'):
            copy(record, Path('versions')/name/'fields'/record.name)
        versions[name] = {'fingerprint': version['fingerprint'], 'folder': folder}
    fields = []
    for outcome in master['outcomes']:
        key = outcome['tile_key'];tile_id = key.rsplit('/',1)[-1]
        if key not in failed or key != chip_root.name+'/'+tile_id:
            raise ValueError('Recovery outcome was not failed in the primary analysis')
        version = outcome['version'];version_root = versions[version]['folder']
        source = Path(outcome['record_file']).resolve()
        if not source.is_relative_to(version_root):
            raise ValueError('Outcome record escapes its version')
        record_file = copy(source, Path('versions')/version/source.relative_to(version_root), outcome['record_sha256'])
        record = json.loads(source.read_text(encoding='utf-8'))
        raw_hash = record['provenance']['source_files_sha256']['.tiff']
        if raw_hash != acquisition['completed'][tile_id]['hashes']['tiles/'+tile_id+'.tiff']:
            raise ValueError('Recovered source frame differs from acquisition manifest')
        status = outcome['state']
        if status == 'excluded' and outcome.get('reason','').startswith('Insufficient spatial support'):
            status = 'unresolved'
        entry = {'tile_key': key, 'status': status, 'original_outcome': outcome['state'],
            'version': version, 'fingerprint': record['fingerprint'], 'source_identity': record['source_identity'],
            'source_tiff_sha256': raw_hash, 'original_error': failed[key], 'record_file': record_file,
            'reason': outcome.get('reason'), 'exclusion_kind': outcome.get('exclusion_kind'),
            'proposal_count': len(record.get('rows',[])),
            'selected_count': sum(bool(r.get('selected_for_review')) for r in record.get('rows',[]))}
        entry['rejected_count'] = entry['proposal_count']-entry['selected_count']
        if status == 'recovered':
            for source_key, digest_key in [('instance_mask','mask_sha256'),('support_masks','support_sha256')]:
                asset = Path(outcome[source_key]).resolve()
                if not asset.is_relative_to(version_root):
                    raise ValueError('Recovery mask escapes its version')
                expected = record[digest_key] if digest_key in record else record['report'][digest_key]
                entry[source_key] = copy(asset, Path('versions')/version/asset.name, expected)
        fields.append(entry)
    manifest = {'schema':1,'kind':'analysis_recovery_supplement',
        'supplement_id':'observed-recovery-'+digest(result_path.read_bytes())[:16],
        'chip_id':chip_root.name,'base_analysis':base.name,'acquisition_fingerprint':acquisition['fingerprint'],
        'versions':{k:{'fingerprint':v['fingerprint']} for k,v in versions.items()},
        'fields':fields,'artifacts':artifacts,'publication_policy':'Append-only copies verified byte-for-byte; raw frames and primary analysis remain in place.',
        'qualification':'Broad optical proposals; no material or layer identification. Unresolved support is not a negative flake finding.'}
    atomic_json(output/'supplement.json',manifest)
    return {'folder':str(output),'artifacts':len(artifacts),'fields':len(fields),
        'counts':{status:sum(x['status']==status for x in fields) for status in ('recovered','excluded','unresolved')},
        'bytes':sum((output/name).stat().st_size for name in artifacts)}


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('result');parser.add_argument('chip');parser.add_argument('output')
    args=parser.parse_args();print(json.dumps(publish(args.result,args.chip,args.output),indent=2))

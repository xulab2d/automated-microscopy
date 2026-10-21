"""Hash-verified, read-only recovery supplements for saved-image galleries."""
from pathlib import Path
from threading import RLock
import hashlib
import json

_cache = {}
_lock = RLock()


def safe_asset(folder, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('Missing supplement asset path')
    path = Path(relative)
    if path.is_absolute() or path.drive or '..' in path.parts:
        raise ValueError('Supplement asset path escapes its folder')
    target = (folder / path).resolve()
    if not target.is_relative_to(folder.resolve()) or not target.is_file():
        raise ValueError('Missing or escaped supplement asset')
    return target


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _validated(path):
    """A malformed entry invalidates the whole manifest, rather than hiding it."""
    folder = path.parent.resolve()
    raw = path.read_bytes()
    value = json.loads(raw)
    if value.get('schema') != 1 or value.get('kind') != 'analysis_recovery_supplement':
        raise ValueError('Unsupported recovery supplement schema')
    artifacts = value.get('artifacts')
    fields = value.get('fields')
    if not isinstance(artifacts, dict) or not artifacts or not isinstance(fields, list):
        raise ValueError('Malformed recovery supplement manifest')
    paths = {}
    stamps = []
    for name, digest in artifacts.items():
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Malformed supplement checksum')
        asset = safe_asset(folder, name)
        stat = asset.stat()
        paths[name] = asset
        stamps.append((name, stat.st_size, stat.st_mtime_ns))
    signature = (_digest(raw), tuple(stamps))
    with _lock:
        previous = _cache.get(path)
        if previous and previous[0] == signature:
            return previous[1]
    for name, asset in paths.items():
        if _digest(asset.read_bytes()) != artifacts[name]:
            raise ValueError('Supplement checksum mismatch: ' + name)
    seen = set()
    for entry in fields:
        key = entry.get('tile_key')
        if not isinstance(key, str) or key in seen or entry.get('status') not in ('recovered', 'excluded', 'unresolved'):
            raise ValueError('Invalid or duplicate supplement field')
        seen.add(key)
        version = value.get('versions', {}).get(entry.get('version'), {})
        if not version.get('fingerprint') or version['fingerprint'] != entry.get('fingerprint'):
            raise ValueError('Unknown supplement version identity')
        record_path = entry.get('record_file')
        if record_path not in paths:
            raise ValueError('Unverified supplement record')
        record = json.loads(paths[record_path].read_text(encoding='utf-8'))
        if record.get('tile_key') != key or record.get('fingerprint') != entry.get('fingerprint') or record.get('source_identity') != entry.get('source_identity'):
            raise ValueError('Supplement record identity mismatch')
        source_hash = record.get('provenance', {}).get('source_files_sha256', {}).get('.tiff')
        if not source_hash or source_hash != entry.get('source_tiff_sha256'):
            raise ValueError('Supplement source identity mismatch')
        if entry['status'] == 'recovered':
            if record.get('state') != 'recovered' or not isinstance(record.get('rows'), list) or not isinstance(record.get('tile'), dict):
                raise ValueError('Malformed recovered field')
            mask = entry.get('instance_mask')
            if mask not in paths or artifacts[mask] != record.get('mask_sha256'):
                raise ValueError('Unverified recovered instance mask')
            if any(row.get('tile_key') != key for row in record['rows']):
                raise ValueError('Recovered observations belong to another field')
            if entry.get('proposal_count') != len(record['rows']) or entry.get('selected_count') != sum(bool(r.get('selected_for_review')) for r in record['rows']):
                raise ValueError('Recovered counts disagree with source observations')
        elif record.get('state') != 'excluded' or not entry.get('reason'):
            raise ValueError('Malformed exclusion outcome')
        elif entry['reason'] != record.get('reason'):
            raise ValueError('Exclusion reason differs from the archived record')
    value = {**value, '_folder': folder}
    with _lock:
        _cache[path] = (signature, value)
        if len(_cache) > 16:
            _cache.pop(next(iter(_cache)))
    return value


def load_supplements(chip_root, base_name, failed_fields, acquisition):
    """Return eligible outcomes and errors; never replace a primary success."""
    chip_root = Path(chip_root).resolve()
    result, errors = {}, []
    for path in sorted(chip_root.glob('analysis-*/supplement.json')):
        try:
            if not path.resolve().is_relative_to(chip_root):
                raise ValueError('Supplement folder escapes chip directory')
            value = _validated(path)
            if value.get('base_analysis') != base_name or value.get('chip_id') != chip_root.name:
                raise ValueError('Supplement targets a different primary analysis or chip')
            if value.get('acquisition_fingerprint') != acquisition.get('fingerprint'):
                raise ValueError('Supplement acquisition fingerprint mismatch')
            eligible = {}
            for entry in value['fields']:
                key = entry['tile_key']
                tile = key.rsplit('/', 1)[-1]
                if key != chip_root.name + '/' + tile:
                    raise ValueError('Supplement field belongs to another chip')
                if tile not in failed_fields:
                    continue
                source_hash = acquisition.get('completed', {}).get(tile, {}).get('hashes', {}).get('tiles/' + tile + '.tiff')
                if not source_hash or source_hash != entry['source_tiff_sha256']:
                    raise ValueError('Supplement does not match the acquired source frame')
                if tile in result:
                    raise ValueError('Multiple supplements target the same failed field')
                eligible[tile] = {**entry, 'supplement_id': value['supplement_id'],
                    'supplement_folder': value['_folder'], 'record_path': safe_asset(value['_folder'], entry['record_file'])}
            result.update(eligible)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            errors.append(path.parent.name + ': ' + str(error))
    return result, errors

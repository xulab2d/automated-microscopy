"""Incremental, read-only index of every committed region-level sieve result.

No images are decoded and no masks or classifications are recomputed. The ledger
binds each shard to its content hash; field identity binds it to the raw source.
"""
from pathlib import Path
import hashlib
import json
import re
import threading


class SavedCandidateIndex:
    def __init__(self, analysis_folder):
        self.folder = Path(analysis_folder).resolve()
        self._fields = {}
        self._lock = threading.RLock()

    @property
    def has_cached_fields(self):
        return bool(self._fields)

    def rows(self, ledger, fields):
        with self._lock:
            combined, warnings = [], []
            for key, entry in ledger.items():
                if key not in fields or entry.get('excluded'):
                    continue
                try:
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', key):
                        raise ValueError('Invalid field identity')
                    name = str(entry.get('folder', '')).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
                    if not re.fullmatch(re.escape(key.replace('/', '__')) + r'-[a-f0-9]+', name):
                        raise ValueError('Unexpected committed attempt path')
                    filename = key.replace('/', '__') + '.json'
                    expected = next((h for p, h in entry.get('hashes', {}).items()
                                     if str(p).replace('\\', '/').endswith('/fields/' + filename)), None)
                    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
                        raise ValueError('Missing committed shard hash')
                    raw_hash = fields[key].get('source_sha256') or fields[key].get('source_identity', {}).get('source_sha256')
                    if not raw_hash:
                        raise ValueError('Missing raw source identity')
                    relative = Path('field-attempts') / name / 'fields' / filename
                    token = (name, expected, raw_hash)
                    cached = self._fields.get(key)
                    if cached and cached[0] == token:
                        combined.extend(cached[1])
                        continue
                    path = (self.folder / relative).resolve()
                    if not path.is_relative_to(self.folder):
                        raise ValueError('Shard escapes analysis folder')
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise ValueError('Committed shard content changed')
                    saved = json.loads(data)
                    rows = []
                    for original in saved['rows']:
                        if original.get('field') != key or original.get('source_tiff_sha256') != raw_hash:
                            raise ValueError('Region source differs from its acquired field')
                        # Exclude large intermediate matrices; preserve the exact
                        # crop, outline, classification and independent reasons.
                        row = {k: original.get(k) for k in (
                            'id', 'field', 'preview', 'source_shape_hw', 'bbox_px',
                            'polygons_px', 'area_um2', 'contrast_rgb', 'sieves',
                            'rank', 'disposition', 'failed_sieves', 'uncertain_sieves',
                            'source_tiff_sha256', 'source_mask_sha256', 'mask_sha256')}
                        row['source_field_reference'] = relative.as_posix()
                        rows.append(row)
                    self._fields[key] = (token, rows)
                    combined.extend(rows)
                except (OSError, ValueError, TypeError, KeyError) as error:
                    warnings.append({'field': key, 'error': str(error), 'index_unavailable': True})
            # A revised ledger cannot silently retain removed/replaced attempts.
            self._fields = {k: v for k, v in self._fields.items() if k in ledger and not ledger[k].get('excluded')}
            return combined, warnings
